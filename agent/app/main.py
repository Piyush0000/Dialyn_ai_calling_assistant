"""HTTP / WebSocket entry points.

Merchant API: see app/api_v1.py (/v1/*, /admin/*).

Telephony
  POST /telephony/twilio/incoming   Twilio "A call comes in" webhook -> TwiML media stream
  POST /telephony/twilio/status     Twilio status callbacks (drives retries)
  WS   /telephony/twilio/stream     Twilio bidirectional media stream (runs the pipeline)

Internal / testing (X-API-Key = platform admin key)
  POST /api/calls, GET /api/calls[/{id}], GET /api/agents   YAML-agent calls

Browser
  GET  /test/{call_id}?token=       Browser test page for a merchant web call
  GET  /test/{call_id}/live?token=  Live status + transcript for that page
  POST /start, /sessions/{id}/api/offer   Prebuilt UI (/client/) signalling
  POST|PATCH /api/offer             WebRTC signalling
"""

import asyncio
import secrets
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pydantic import BaseModel, Field

from app.agent_config import AgentConfig, AgentNotFound, list_agents, load_agent
from app.api_v1 import router as v1_router
from app.bot import CallSession, run_call
from app.context import deps, resolve_agent, service, settings
from app.providers import missing_keys
from app.telephony import reject_twiml, sign_stream_token, stream_twiml, verify_stream_token

STATIC_DIR = Path(__file__).parent / "static"
webrtc_handler = SmallWebRTCRequestHandler()
# Strong references so in-flight call tasks are not garbage-collected.
_call_tasks: set[asyncio.Task] = set()
# Browser sessions created by POST /start -> request body (e.g. {"agent_id": ...}).
_web_sessions: OrderedDict[str, dict] = OrderedDict()
_MAX_WEB_SESSIONS = 1000


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.database_url.startswith("sqlite"):
        Path(settings.database_url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    await deps.store.init()
    scheduler = asyncio.create_task(service.run_forever()) if settings.scheduler_enabled else None
    yield
    if scheduler:
        scheduler.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler
    await service.close()
    await webrtc_handler.close()
    await deps.store.close()


app = FastAPI(title="Dialyn — AI Calling API", lifespan=lifespan)
app.include_router(v1_router)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not settings.api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _agent_or_404(agent_id: str) -> AgentConfig:
    try:
        return load_agent(settings.agents_dir, agent_id)
    except AgentNotFound:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{agent_id}'") from None


def _require_keys(agent: AgentConfig) -> None:
    if missing := missing_keys(agent, settings):
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent.id}' needs these keys in agent/.env: {', '.join(missing)}",
        )


async def _twilio_form(request: Request) -> dict[str, str]:
    """Parse a Twilio webhook and verify its X-Twilio-Signature."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    # Twilio signs the public URL it called, not our internal one behind the proxy.
    url = f"{settings.public_base_url}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    if not deps.twilio.is_valid_request(url, form, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return form


def _stream_twiml_for(call_id: str, from_number: str, to_number: str) -> str:
    return stream_twiml(
        settings.twilio_stream_url,
        {
            "call_id": call_id,
            "token": sign_stream_token(settings.stream_signing_secret, call_id),
            "from_number": from_number,
            "to_number": to_number,
        },
    )


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _call_tasks.add(task)
    task.add_done_callback(_call_tasks.discard)


# ------------------------------------------------------------------ Twilio


@app.post("/telephony/twilio/incoming")
async def twilio_incoming(request: Request, agent: str | None = None):
    form = await _twilio_form(request)
    agent_id = agent or settings.default_agent_id
    try:
        load_agent(settings.agents_dir, agent_id)
    except AgentNotFound:
        logger.error(f"Inbound call for unknown agent {agent_id}")
        return Response(
            reject_twiml("Sorry, this line is not available."), media_type="application/xml"
        )

    call = await deps.store.create(
        agent_id=agent_id,
        direction="inbound",
        status="ringing",
        provider_call_id=form.get("CallSid"),
        from_number=form.get("From"),
        to_number=form.get("To"),
    )
    twiml = _stream_twiml_for(call.id, form.get("From", ""), form.get("To", ""))
    return Response(twiml, media_type="application/xml")


# Twilio statuses that mean this dial attempt never reached a conversation.
_FAILED_ATTEMPT = {
    "busy": "busy",
    "no-answer": "no_answer",
    "failed": "failed",
    "canceled": "canceled",
}


@app.post("/telephony/twilio/status")
async def twilio_status(request: Request):
    form = await _twilio_form(request)
    call = await deps.store.get_by_provider_id(form.get("CallSid", ""))
    if call is None:
        return {"ok": True}
    twilio_status = form.get("CallStatus", "")
    # "in-progress"/"completed" after answer are owned by the pipeline itself.
    if twilio_status == "ringing" and call.status in ("queued", "dialing"):
        await deps.store.update(call.id, status="ringing", log="ringing")
    elif reason := _FAILED_ATTEMPT.get(twilio_status):
        if call.tenant_id:
            await service.attempt_failed(call.id, reason)
        elif call.status in ("queued", "ringing"):
            await deps.store.update(call.id, status=reason, log=reason)
    elif twilio_status == "completed" and call.status in ("dialing", "ringing"):
        # Ended before our media stream connected (e.g. picked up and hung up at once).
        await service.attempt_failed(call.id, "ended_before_connect")
    return {"ok": True}


@app.websocket("/telephony/twilio/stream")
async def twilio_stream(websocket: WebSocket):
    await websocket.accept()
    transport_type, call_data = await parse_telephony_websocket(websocket)
    params = call_data.body
    call_id = params.get("call_id", "")

    if transport_type != "twilio" or not verify_stream_token(
        settings.stream_signing_secret, call_id, params.get("token", "")
    ):
        logger.warning("Rejected media stream with invalid token")
        await websocket.close(code=4003)
        return

    call = await deps.store.get(call_id)
    if call is None:
        await websocket.close(code=4004)
        return

    agent = await resolve_agent(call)
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=TwilioFrameSerializer(
                stream_sid=call_data.stream_id,
                call_sid=call_data.call_id,
                # We hang up ourselves so a transferred call is not dropped.
                params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
            ),
        ),
    )
    session = CallSession(call_id=call.id, agent=agent, provider_call_id=call_data.call_id)
    await run_call(transport, session, deps, sample_rate=8000)


# ------------------------------------------------ internal YAML-agent API


class OutboundCallRequest(BaseModel):
    to: str = Field(pattern=r"^\+[1-9]\d{6,14}$", description="E.164 number")
    agent_id: str | None = None
    from_number: str | None = Field(default=None, pattern=r"^\+[1-9]\d{6,14}$")
    variables: dict[str, str] = Field(default_factory=dict)


@app.post("/api/calls", dependencies=[Depends(require_api_key)], status_code=201)
async def create_call(body: OutboundCallRequest):
    agent_id = body.agent_id or settings.default_agent_id
    _agent_or_404(agent_id)
    from_number = body.from_number or settings.twilio_phone_number
    call = await deps.store.create(
        agent_id=agent_id,
        direction="outbound",
        status="queued",
        from_number=from_number,
        to_number=body.to,
        variables=body.variables,
    )
    try:
        sid = await deps.twilio.place_call(
            to=body.to,
            from_=from_number,
            twiml=_stream_twiml_for(call.id, from_number, body.to),
            status_callback=f"{settings.public_base_url}/telephony/twilio/status",
        )
    except Exception as e:
        logger.exception("Failed to place call")
        await deps.store.update(call.id, status="failed", end_reason=f"dial_error: {e}"[:64])
        raise HTTPException(status_code=502, detail="Telephony provider rejected the call") from e
    await deps.store.update(call.id, provider_call_id=sid)
    return {"id": call.id, "provider_call_id": sid, "status": "queued"}


@app.get("/api/calls", dependencies=[Depends(require_api_key)])
async def get_calls(limit: int = 50, offset: int = 0):
    return [c.to_dict() for c in await deps.store.list_calls(min(limit, 200), offset)]


@app.get("/api/calls/{call_id}", dependencies=[Depends(require_api_key)])
async def get_call(call_id: str):
    call = await deps.store.get(call_id)
    if call is None:
        raise HTTPException(status_code=404)
    return call.to_dict()


@app.get("/api/agents", dependencies=[Depends(require_api_key)])
async def get_agents():
    return [a.model_dump() for a in list_agents(settings.agents_dir)]


@app.get("/health")
async def health():
    return {"ok": True}


# ------------------------------------------------------------ Browser (WebRTC)


async def _web_test_call(call_id: str, token: str):
    call = await deps.store.get(call_id)
    if call is None or not verify_stream_token(settings.stream_signing_secret, call_id, token):
        raise HTTPException(status_code=403, detail="Invalid or expired test link")
    return call


@app.get("/test/{call_id}", include_in_schema=False)
async def test_page(call_id: str, token: str):
    await _web_test_call(call_id, token)
    return HTMLResponse((STATIC_DIR / "test_call.html").read_text(encoding="utf-8"))


@app.get("/test/{call_id}/live", include_in_schema=False)
async def test_live(call_id: str, token: str):
    call = await _web_test_call(call_id, token)
    return {
        "event": call.event_type,
        "customer_name": call.customer_name,
        "language": call.language,
        "order": call.payload.get("order", {}),
        "status": call.status,
        "outcome": call.outcome,
        "outcome_data": call.outcome_data,
        "transcript": call.transcript,
        "timeline": call.timeline,
        "duration_secs": call.duration_secs,
    }


@app.post("/start")
async def start_web_session(request: Request):
    """Called by the prebuilt test UI before it sends its WebRTC offer."""
    try:
        body = (await request.json()).get("body") or {}
    except Exception:
        body = {}
    session_id = str(uuid.uuid4())
    _web_sessions[session_id] = body if isinstance(body, dict) else {}
    while len(_web_sessions) > _MAX_WEB_SESSIONS:
        _web_sessions.popitem(last=False)
    return {"sessionId": session_id}


@app.post("/sessions/{session_id}/api/offer")
async def session_offer(session_id: str, request: Request):
    if session_id not in _web_sessions:
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    data = await request.json()
    offer = SmallWebRTCRequest(
        sdp=data["sdp"],
        type=data["type"],
        pc_id=data.get("pc_id"),
        restart_pc=data.get("restart_pc"),
        request_data=data.get("request_data")
        or data.get("requestData")
        or _web_sessions[session_id],
    )
    return await webrtc_offer(offer)


@app.patch("/sessions/{session_id}/api/offer")
async def session_ice(session_id: str, request: SmallWebRTCPatchRequest):
    return await webrtc_ice(request)


@app.api_route("/sessions/{session_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def session_other(session_id: str, path: str):
    # The UI may ping other session paths; nothing to do for local sessions.
    return Response(status_code=200 if session_id in _web_sessions else 404)


@app.post("/api/offer")
async def webrtc_offer(request: SmallWebRTCRequest):
    request_data = request.request_data or {}

    if request_data.get("call_id"):
        # Merchant web test call created via POST /v1/calls with channel="web".
        call = await _web_test_call(request_data["call_id"], request_data.get("token", ""))
        if call.status != "waiting_for_browser":
            raise HTTPException(status_code=409, detail=f"This test call is already {call.status}")
        agent = await resolve_agent(call)
        _require_keys(agent)
        call_id = call.id
    else:
        agent_id = request_data.get("agent_id") or settings.default_agent_id
        variables = {k: str(v) for k, v in (request_data.get("variables") or {}).items()}
        agent = _agent_or_404(agent_id).render(variables)
        _require_keys(agent)
        call_id = None

    async def on_connection(connection: SmallWebRTCConnection):
        nonlocal call_id
        if call_id is None:
            call = await deps.store.create(
                agent_id=agent.id, direction="web", channel="web", provider="webrtc"
            )
            call_id = call.id
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        _spawn(run_call(transport, CallSession(call_id=call_id, agent=agent), deps))

    return await webrtc_handler.handle_web_request(
        request=request, webrtc_connection_callback=on_connection
    )


@app.patch("/api/offer")
async def webrtc_ice(request: SmallWebRTCPatchRequest):
    await webrtc_handler.handle_patch_request(request)
    return {"status": "success"}


try:
    from pipecat_ai_prebuilt.frontend import PipecatPrebuiltUI

    app.mount("/client", PipecatPrebuiltUI)

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(url="/client/")
except ImportError:  # prebuilt UI is optional
    pass


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    run()
