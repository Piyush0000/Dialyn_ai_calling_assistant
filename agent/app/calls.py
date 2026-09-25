"""Merchant call lifecycle: schedule -> dial (inside calling hours) -> retry -> webhook.

Status flow
  scheduled -> dialing -> ringing -> in_progress -> completed | transferred
            \\-> (busy / no_answer / failed) -> scheduled again, or unreachable after
               the last attempt
  web test calls: scheduled -> waiting_for_browser -> in_progress -> completed
  canceled at any point before dialing
"""

import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from app.bot import Deps
from app.db import Call, Tenant, now_utc
from app.telephony import sign_stream_token, stream_twiml

FINAL_STATUSES = ("completed", "transferred", "unreachable", "failed", "canceled")
WEBHOOK_ATTEMPTS = 3
WEB_TEST_TOKEN_TTL_SECS = 3600


# ------------------------------------------------------------------ API keys


def new_api_key() -> str:
    return "sk_live_" + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# -------------------------------------------------------------- calling hours


def _parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def next_allowed_time(when: datetime, tz_name: str, start: str, end: str) -> datetime:
    """Earliest moment >= ``when`` inside the daily calling window, in ``when``'s timezone.

    ``start == end`` means calls are allowed at any time; ``start > end`` is an overnight
    window (e.g. 20:00-02:00).
    """
    start_t, end_t = _parse_hhmm(start), _parse_hhmm(end)
    if start_t == end_t:
        return when
    tz = ZoneInfo(tz_name)
    local = when.astimezone(tz)
    now_t = local.time()
    if start_t < end_t:
        inside = start_t <= now_t < end_t
    else:
        inside = now_t >= start_t or now_t < end_t
    if inside:
        return when
    # Outside the window, the next opening is today's start if still ahead, else tomorrow's.
    day = local.date() if now_t < start_t else local.date() + timedelta(days=1)
    return datetime.combine(day, start_t, tzinfo=tz).astimezone(when.tzinfo)


# ------------------------------------------------------------------- service


class CallService:
    def __init__(self, deps: Deps, poll_interval_secs: float = 5.0):
        self.deps = deps
        self.poll_interval_secs = poll_interval_secs
        self._http = httpx.AsyncClient(timeout=10)
        self._background: set[asyncio.Task] = set()

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        await self._http.aclose()

    # ---------------------------------------------------------- scheduling

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(self.poll_interval_secs)

    async def tick(self) -> None:
        store = self.deps.store
        for call in await store.due_calls(now_utc()):
            tenant = await store.get_tenant(call.tenant_id)
            if tenant is None:
                await store.update(call.id, status="failed", log="tenant_missing")
                continue

            now = now_utc()
            allowed = next_allowed_time(
                now, tenant.timezone, tenant.call_window_start, tenant.call_window_end
            )
            if allowed > now:
                await store.update(
                    call.id, next_attempt_at=allowed, log=f"outside_calling_hours: next {allowed}"
                )
                continue

            if await store.count_active(tenant.id) >= tenant.max_concurrent_calls:
                continue  # picked up again on a later tick

            await self.dial(call, tenant)

    async def dial(self, call: Call, tenant: Tenant) -> None:
        settings = self.deps.settings
        from_number = tenant.from_number or settings.twilio_phone_number
        await self.deps.store.update(
            call.id,
            status="dialing",
            attempts=call.attempts + 1,
            from_number=from_number,
            log=f"dialing attempt {call.attempts + 1}",
        )
        twiml = stream_twiml(
            settings.twilio_stream_url,
            {
                "call_id": call.id,
                "token": sign_stream_token(settings.stream_signing_secret, call.id),
                "from_number": from_number,
                "to_number": call.to_number or "",
            },
        )
        try:
            sid = await self.deps.twilio.place_call(
                to=call.to_number,
                from_=from_number,
                twiml=twiml,
                status_callback=f"{settings.public_base_url}/telephony/twilio/status",
            )
        except Exception as e:
            logger.warning(f"[{call.id}] dial failed: {e}")
            await self.attempt_failed(call.id, f"dial_error: {e}"[:120])
            return
        await self.deps.store.update(call.id, provider_call_id=sid, log="provider_accepted")

    async def attempt_failed(self, call_id: str, reason: str) -> None:
        """A dial attempt did not connect: retry later, or give up after the last attempt."""
        store = self.deps.store
        call = await store.get(call_id)
        if call is None or call.status in FINAL_STATUSES:
            return
        tenant = await store.get_tenant(call.tenant_id) if call.tenant_id else None
        if tenant and call.attempts < call.max_attempts:
            retry_at = now_utc() + timedelta(minutes=tenant.retry_delay_minutes)
            await store.update(
                call.id,
                status="scheduled",
                next_attempt_at=retry_at,
                provider_call_id=None,
                end_reason=reason,
                log=f"{reason}; retry at {retry_at.isoformat()}",
            )
            return
        await store.update(
            call.id,
            status="unreachable" if tenant else "failed",
            end_reason=reason,
            ended_at=now_utc(),
            log=f"{reason}; giving up",
        )
        await self.call_finished(call.id)

    async def cancel(self, call: Call) -> bool:
        if call.status not in ("scheduled", "waiting_for_browser"):
            return False
        await self.deps.store.update(
            call.id, status="canceled", ended_at=now_utc(), log="canceled_by_merchant"
        )
        return True

    # ------------------------------------------------------------ webhooks

    async def call_finished(self, call_id: str) -> None:
        call = await self.deps.store.get(call_id)
        if call is None or call.tenant_id is None:
            return
        tenant = await self.deps.store.get_tenant(call.tenant_id)
        if tenant is None or not tenant.webhook_url:
            return
        task = asyncio.create_task(self._deliver_webhook(tenant, call))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _deliver_webhook(self, tenant: Tenant, call: Call) -> None:
        body = json.dumps(
            {"type": "call.completed", "data": public_call(call, include_transcript=True)},
            default=str,
        ).encode()
        signature = hmac.new(tenant.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Signature": f"sha256={signature}"}
        for attempt in range(1, WEBHOOK_ATTEMPTS + 1):
            try:
                response = await self._http.post(tenant.webhook_url, content=body, headers=headers)
                if response.status_code < 300:
                    await self.deps.store.update(
                        call.id, webhook_status="delivered", log="webhook_delivered"
                    )
                    return
                error = f"http {response.status_code}"
            except httpx.HTTPError as e:
                error = type(e).__name__
            logger.warning(f"[{call.id}] webhook attempt {attempt} failed: {error}")
            await asyncio.sleep(2**attempt)
        await self.deps.store.update(call.id, webhook_status="failed", log="webhook_failed")


def web_test_token(secret: str, call_id: str) -> str:
    return sign_stream_token(secret, call_id, ttl_secs=WEB_TEST_TOKEN_TTL_SECS)


def public_call(call: Call, *, include_transcript: bool = False) -> dict[str, Any]:
    data = call.to_dict()
    for private in ("variables", "agent_id", "idempotency_key", "provider"):
        data.pop(private, None)
    data["metadata"] = data.pop("metadata", {}) or {}
    if not include_transcript:
        data.pop("transcript", None)
    return data
