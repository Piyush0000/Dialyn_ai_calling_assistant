"""Twilio helpers: TwiML, webhook signature checks, stream tokens and call control."""

import asyncio
import hashlib
import hmac
import time
from xml.sax.saxutils import escape, quoteattr

import httpx
from loguru import logger
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.config import Settings

STREAM_TOKEN_TTL_SECS = 120


# ---------------------------------------------------------------- stream tokens
# Twilio cannot authenticate its media WebSocket, so we hand it a short-lived HMAC
# token inside the TwiML <Parameter> list and verify it on the first "start" message.


def sign_stream_token(
    secret: str, call_id: str, now: float | None = None, ttl_secs: int = STREAM_TOKEN_TTL_SECS
) -> str:
    expires = int((now or time.time()) + ttl_secs)
    mac = hmac.new(secret.encode(), f"{call_id}.{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def verify_stream_token(secret: str, call_id: str, token: str, now: float | None = None) -> bool:
    try:
        expires_str, mac = token.split(".", 1)
        expires = int(expires_str)
    except (ValueError, AttributeError):
        return False
    if expires < (now or time.time()):
        return False
    expected = hmac.new(secret.encode(), f"{call_id}.{expires}".encode(), hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), mac)


# ---------------------------------------------------------------------- TwiML


def stream_twiml(stream_url: str, parameters: dict[str, str]) -> str:
    params = "".join(
        f"<Parameter name={quoteattr(k)} value={quoteattr(v or '')} />"
        for k, v in parameters.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<Stream url={quoteattr(stream_url)}>{params}</Stream>"
        "</Connect></Response>"
    )


def transfer_twiml(number: str, message: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{escape(message)}</Say><Dial>{escape(number)}</Dial></Response>"
    )


def reject_twiml(message: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{escape(message)}</Say><Hangup/></Response>"
    )


# ------------------------------------------------------------------ Twilio API


class Twilio:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: Client | None = None
        self._validator = RequestValidator(settings.twilio_auth_token)

    @property
    def client(self) -> Client:
        if self._client is None:
            if not (self._settings.twilio_account_sid and self._settings.twilio_auth_token):
                raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set")
            self._client = Client(
                self._settings.twilio_account_sid, self._settings.twilio_auth_token
            )
        return self._client

    def is_valid_request(self, url: str, params: dict[str, str], signature: str) -> bool:
        if not self._settings.validate_twilio_signature:
            return True
        return self._validator.validate(url, params, signature)

    async def place_call(self, to: str, from_: str, twiml: str, status_callback: str) -> str:
        call = await asyncio.to_thread(
            self.client.calls.create,
            to=to,
            from_=from_,
            twiml=twiml,
            status_callback=status_callback,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
        return call.sid

    async def redirect(self, call_sid: str, twiml: str) -> None:
        await asyncio.to_thread(self.client.calls(call_sid).update, twiml=twiml)

    async def hang_up(self, call_sid: str) -> None:
        try:
            await asyncio.to_thread(self.client.calls(call_sid).update, status="completed")
        except Exception as e:  # already completed, network error, etc.
            logger.warning(f"Hang-up for {call_sid} failed: {e}")

    async def transfer(self, provider_call_id: str, number: str, message: str) -> None:
        # Replacing the call's TwiML ends our media stream and bridges the caller.
        await self.redirect(provider_call_id, transfer_twiml(number, message))


# ------------------------------------------------------------------ Plivo (India)


def plivo_stream_xml(stream_url: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Stream bidirectional="true" keepCallAlive="true" '
        f'contentType="audio/x-mulaw;rate=8000">{escape(stream_url)}</Stream></Response>'
    )


def plivo_transfer_xml(number: str, message: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"<Speak>{escape(message)}</Speak>"
        f"<Dial><Number>{escape(number.lstrip('+'))}</Number></Dial></Response>"
    )


class Plivo:
    """Plivo Voice API. Numbers go over the wire without the leading '+'."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=15)

    @property
    def _base(self) -> str:
        if not (self._settings.plivo_auth_id and self._settings.plivo_auth_token):
            raise RuntimeError("PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN are not set")
        return f"https://api.plivo.com/v1/Account/{self._settings.plivo_auth_id}"

    @property
    def _auth(self) -> tuple[str, str]:
        return (self._settings.plivo_auth_id, self._settings.plivo_auth_token)

    async def place_call(self, to: str, from_: str, answer_url: str, hangup_url: str) -> str:
        response = await self._http.post(
            f"{self._base}/Call/",
            auth=self._auth,
            json={
                "from": from_.lstrip("+"),
                "to": to.lstrip("+"),
                "answer_url": answer_url,
                "answer_method": "POST",
                "hangup_url": hangup_url,
                "hangup_method": "POST",
                "ring_timeout": 30,
            },
        )
        response.raise_for_status()
        return response.json()["request_uuid"]

    async def hang_up(self, call_uuid: str) -> None:
        try:
            response = await self._http.delete(f"{self._base}/Call/{call_uuid}/", auth=self._auth)
            if response.status_code not in (204, 404):
                logger.warning(f"Plivo hang-up for {call_uuid}: http {response.status_code}")
        except httpx.HTTPError as e:
            logger.warning(f"Plivo hang-up for {call_uuid} failed: {e}")

    async def transfer_to_url(self, call_uuid: str, xml_url: str) -> None:
        response = await self._http.post(
            f"{self._base}/Call/{call_uuid}/",
            auth=self._auth,
            json={"legs": "aleg", "aleg_url": xml_url, "aleg_method": "POST"},
        )
        response.raise_for_status()


# ----------------------------------------------------------------- Exotel (India)


class Exotel:
    """Exotel 'connect to flow' calls. The flow's Voicebot applet asks our
    /telephony/exotel/stream-url endpoint which WebSocket to stream to."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=15)

    async def place_call(self, to: str, caller_id: str, status_callback: str, call_id: str) -> str:
        s = self._settings
        if not (s.exotel_sid and s.exotel_api_key and s.exotel_api_token and s.exotel_app_id):
            raise RuntimeError(
                "EXOTEL_SID / EXOTEL_API_KEY / EXOTEL_API_TOKEN / EXOTEL_APP_ID are not set"
            )
        response = await self._http.post(
            f"https://{s.exotel_subdomain}/v1/Accounts/{s.exotel_sid}/Calls/connect.json",
            auth=(s.exotel_api_key, s.exotel_api_token),
            data={
                "From": to,
                "CallerId": caller_id or s.exotel_caller_id,
                "Url": f"http://my.exotel.com/{s.exotel_sid}/exoml/start_voice/{s.exotel_app_id}",
                "StatusCallback": status_callback,
                "StatusCallbackEvents[0]": "terminal",
                "CustomField": call_id,
            },
        )
        response.raise_for_status()
        return response.json()["Call"]["Sid"]

    async def hang_up(self, call_sid: str) -> None:
        # Closing the voicebot stream ends the applet and the flow hangs up.
        return None
