"""HTTP / WebSocket entry points for the voice agent.

Routes
  POST /telephony/twilio/incoming   Twilio "A call comes in" webhook -> TwiML media stream
  POST /telephony/twilio/status     Twilio status callbacks
  WS   /telephony/twilio/stream     Twilio bidirectional media stream (runs the pipeline)
  POST /api/calls                   Place an outbound call            (X-API-Key)
  GET  /api/calls[/{id}]            Call records + transcripts        (X-API-Key)
  GET  /api/agents                  Configured agents                 (X-API-Key)
  POST /api/offer, PATCH /api/offer Browser WebRTC test calls
  GET  /client/                     Prebuilt browser test UI
"""

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse, Response
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

from app.agent_config import AgentNotFound, list_agents, load_agent
from app.bot import CallSession, Deps, run_call
from app.config import get_settings
from app.db import CallStore
from app.telephony import (
    Twilio,
    reject_twiml,
    sign_stream_token,
    stream_twiml,
    verify_stream_token,
)

settings = get_settings()
deps = Deps(settings=settings, store=CallStore(settings.database_url), twilio=Twilio(settings))
webrtc_handler = SmallWebRTCRequestHandler()
# Strong references so in-flight call tasks are not garbage-collected.
_call_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.database_url.startswith("sqlite"):
        from pathlib import Path

        Path(settings.database_url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    await deps.store.init()
    yield
    await webrtc_handler.close()
    await deps.store.close()


app = FastAPI(title="AI Calling Agent", lifespan=lifespan)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not settings.api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _agent_or_404(agent_id: str):
    try:
        return load_agent(settings.agents_dir, agent_id)
    except AgentNotFound:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{agent_id}'") from None


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


def _stream_twiml_for(call_id: str, agent_id: str, from_number: str, to_number: str) -> str:
    return stream_twiml(
        settings.twilio_stream_url,
        {
            "call_id": call_id,
            "agent_id": agent_id,
            "token": sign_stream_token(settings.stream_signing_secret, call_id),
            "from_number": from_number,
            "to_number": to_number,
        },
    )


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
    twiml = _stream_twiml_for(call.id, agent_id, form.get("From", ""), form.get("To", ""))
    return Response(twiml, media_type="application/xml")


_TWILIO_STATUS = {
    "queued": "queued",
    "initiated": "queued",
    "ringing": "ringing",
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
    # "answered"/"in-progress"/"completed" are owned by the pipeline itself.
    status = _TWILIO_STATUS.get(form.get("CallStatus", ""))
    if status and call.status in ("queued", "ringing"):
        await deps.store.update(call.id, status=status)
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

    agent = load_agent(settings.agents_dir, call.agent_id).render(call.variables)
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


# ------------------------------------------------------------------ REST API


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
            twiml=_stream_twiml_for(call.id, agent_id, from_number, body.to),
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
    return [c.to_dict() for c in await deps.store.list(min(limit, 200), offset)]


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


@app.post("/api/offer")
async def webrtc_offer(request: SmallWebRTCRequest):
    request_data = request.request_data or {}
    agent_id = request_data.get("agent_id") or settings.default_agent_id
    agent = _agent_or_404(agent_id)
    variables = {k: str(v) for k, v in (request_data.get("variables") or {}).items()}

    async def on_connection(connection: SmallWebRTCConnection):
        call = await deps.store.create(
            agent_id=agent_id, direction="web", provider="webrtc", variables=variables
        )
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        session = CallSession(call_id=call.id, agent=agent.render(variables))
        task = asyncio.create_task(run_call(transport, session, deps))
        _call_tasks.add(task)
        task.add_done_callback(_call_tasks.discard)

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
