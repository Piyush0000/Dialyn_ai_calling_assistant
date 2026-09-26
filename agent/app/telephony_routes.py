"""Plivo and Exotel (India) call-control callbacks and media streams.

Every URL carries a short-lived HMAC token for its call, so only the telephony
provider that we handed the URL to can drive the call.

Plivo
  POST /telephony/plivo/answer/{call_id}?token=     answer_url  -> <Stream> XML
  POST /telephony/plivo/hangup/{call_id}?token=     hangup_url  -> retries for busy/no-answer
  POST /telephony/plivo/transfer/{call_id}?token=   aleg_url    -> <Dial> to the human team
  WS   /telephony/plivo/stream/{call_id}/{token}    bidirectional audio

Exotel
  GET|POST /telephony/exotel/stream-url             Voicebot applet asks where to stream
  POST /telephony/exotel/status/{call_id}?token=    StatusCallback -> retries
  WS   /telephony/exotel/stream/{call_id}/{token}   bidirectional audio
"""

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response
from loguru import logger
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.serializers.plivo import PlivoFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from app.bot import CallSession, run_call
from app.context import deps, resolve_agent, service, settings
from app.telephony import (
    plivo_stream_xml,
    plivo_transfer_xml,
    sign_stream_token,
    verify_stream_token,
)

router = APIRouter()
NOT_CONNECTED = ("dialing", "ringing")


async def _call_for(call_id: str, token: str):
    call = await deps.store.get(call_id)
    if call is None or not verify_stream_token(settings.stream_signing_secret, call_id, token):
        raise HTTPException(status_code=403, detail="Invalid call token")
    return call


def _wss(provider: str, call_id: str) -> str:
    token = sign_stream_token(settings.stream_signing_secret, call_id)
    return f"wss://{settings.public_host}/telephony/{provider}/stream/{call_id}/{token}"


async def _form_or_json(request: Request) -> dict[str, str]:
    if "json" in request.headers.get("content-type", ""):
        data = await request.json()
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    return {k: str(v) for k, v in (await request.form()).items()}


def _failure_reason(status: str) -> str:
    status = status.lower().replace("_", "-")
    if "busy" in status:
        return "busy"
    if "no-answer" in status or "timeout" in status or "not-answered" in status:
        return "no_answer"
    if "cancel" in status:
        return "canceled"
    return "failed"


async def _run_stream(websocket: WebSocket, provider: str, call_id: str, token: str) -> None:
    await websocket.accept()
    call = await deps.store.get(call_id)
    if call is None or not verify_stream_token(settings.stream_signing_secret, call_id, token):
        await websocket.close(code=4003)
        return
    transport_type, call_data = await parse_telephony_websocket(websocket)
    if transport_type != provider:
        logger.warning(f"[{call_id}] expected {provider} stream, got {transport_type}")
        await websocket.close(code=4003)
        return

    if provider == "plivo":
        serializer = PlivoFrameSerializer(
            stream_id=call_data.stream_id,
            call_id=call_data.call_id,
            params=PlivoFrameSerializer.InputParams(auto_hang_up=False),
        )
    else:
        serializer = ExotelFrameSerializer(
            stream_sid=call_data.stream_id, call_sid=call_data.call_id
        )

    # The provider's live call id (Plivo's call_uuid replaces the request_uuid from dialing).
    await deps.store.update(call.id, provider_call_id=call_data.call_id, provider=provider)
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )
    session = CallSession(
        call_id=call.id,
        agent=await resolve_agent(call),
        provider_call_id=call_data.call_id,
        provider=provider,
    )
    await run_call(transport, session, deps, sample_rate=8000)


# ------------------------------------------------------------------ Plivo


@router.post("/telephony/plivo/answer/{call_id}")
async def plivo_answer(call_id: str, token: str):
    call = await _call_for(call_id, token)
    await deps.store.update(call.id, log="answered_by_customer")
    return Response(plivo_stream_xml(_wss("plivo", call.id)), media_type="application/xml")


@router.post("/telephony/plivo/hangup/{call_id}")
async def plivo_hangup(call_id: str, token: str, request: Request):
    call = await _call_for(call_id, token)
    form = await _form_or_json(request)
    if call.status in NOT_CONNECTED:
        cause = form.get("HangupCause") or form.get("CallStatus") or "failed"
        await service.attempt_failed(call.id, _failure_reason(cause))
    return {"ok": True}


@router.post("/telephony/plivo/transfer/{call_id}")
async def plivo_transfer(call_id: str, token: str):
    call = await _call_for(call_id, token)
    number = (await resolve_agent(call)).transfer_number
    if not number:
        raise HTTPException(status_code=409, detail="No transfer number configured")
    return Response(
        plivo_transfer_xml(number, "Please hold while I connect you."), media_type="application/xml"
    )


@router.websocket("/telephony/plivo/stream/{call_id}/{token}")
async def plivo_stream(websocket: WebSocket, call_id: str, token: str):
    await _run_stream(websocket, "plivo", call_id, token)


# ----------------------------------------------------------------- Exotel


@router.api_route("/telephony/exotel/stream-url", methods=["GET", "POST"])
async def exotel_stream_url(request: Request):
    params = {**dict(request.query_params)}
    if request.method == "POST":
        params.update(await _form_or_json(request))
    call_id = params.get("CustomField") or params.get("custom_field") or ""
    call = await deps.store.get(call_id) if call_id else None
    if call is None and params.get("CallSid"):
        call = await deps.store.get_by_provider_id(params["CallSid"])
    if call is None or call.status not in NOT_CONNECTED:
        raise HTTPException(status_code=404, detail="Unknown call")
    # The call id is an unguessable UUID; also require the provider's CallSid when given.
    if (
        params.get("CallSid")
        and call.provider_call_id
        and params["CallSid"] != call.provider_call_id
    ):
        raise HTTPException(status_code=403, detail="Call mismatch")
    return {"url": _wss("exotel", call.id)}


@router.post("/telephony/exotel/status/{call_id}")
async def exotel_status(call_id: str, token: str, request: Request):
    call = await _call_for(call_id, token)
    form = await _form_or_json(request)
    status = form.get("Status") or form.get("status") or ""
    if call.status in NOT_CONNECTED and status and status.lower() != "completed":
        await service.attempt_failed(call.id, _failure_reason(status))
    elif call.status in NOT_CONNECTED and status.lower() == "completed":
        await service.attempt_failed(call.id, "ended_before_connect")
    return {"ok": True}


@router.websocket("/telephony/exotel/stream/{call_id}/{token}")
async def exotel_stream(websocket: WebSocket, call_id: str, token: str):
    await _run_stream(websocket, "exotel", call_id, token)
