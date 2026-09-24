"""Twilio helpers: TwiML, webhook signature checks, stream tokens and call control."""

import asyncio
import hashlib
import hmac
import time
from xml.sax.saxutils import escape, quoteattr

from loguru import logger
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.config import Settings

STREAM_TOKEN_TTL_SECS = 120


# ---------------------------------------------------------------- stream tokens
# Twilio cannot authenticate its media WebSocket, so we hand it a short-lived HMAC
# token inside the TwiML <Parameter> list and verify it on the first "start" message.


def sign_stream_token(secret: str, call_id: str, now: float | None = None) -> str:
    expires = int((now or time.time()) + STREAM_TOKEN_TTL_SECS)
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
