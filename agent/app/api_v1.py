"""Public merchant API (v1). Authenticate with ``Authorization: Bearer sk_live_...``.

  POST /v1/calls                 Schedule a call for an order event
  GET  /v1/calls                 List calls (filters: status, event, order_id)
  GET  /v1/calls/{id}            Call details: status, outcome, transcript, timeline
  GET  /v1/calls/{id}/recording  Stereo WAV (customer left, agent right)
  POST /v1/calls/{id}/cancel     Cancel a call that has not been dialed yet
  GET  /v1/events                Supported order events and their outcomes
  GET  /v1/stats                 Counts by status / outcome / event
  GET  /v1/account, PATCH /v1/account   Merchant settings

Platform admin (``X-Admin-Key: <API_KEY from .env>``):
  POST /admin/tenants            Create a merchant; returns its API key once
"""

import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl

from app.accounts import Principal, current_tenant, provision_tenant, require_role
from app.calls import public_call, web_test_token
from app.context import deps, service, settings
from app.db import Tenant, now_utc
from app.ecommerce import DEFAULT_STACKS, TEMPLATES, EventType

router = APIRouter()
E164 = r"^\+[1-9]\d{6,14}$"
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


# ------------------------------------------------------------------- auth


def require_admin(x_admin_key: str = Header(default="")) -> None:
    if not settings.api_key or not secrets.compare_digest(x_admin_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid admin key")


# ----------------------------------------------------------------- models


class Item(BaseModel):
    name: str
    quantity: int = 1
    price: float | None = None


class Order(BaseModel):
    id: str
    amount: float | None = None
    currency: str = "INR"
    items: list[Item] = Field(default_factory=list)
    payment_method: str | None = None
    address: str | None = None
    expected_delivery: str | None = None
    courier: str | None = None
    tracking_number: str | None = None
    payment_link: str | None = None


class Customer(BaseModel):
    name: str
    phone: str = Field(pattern=E164, description="E.164, e.g. +919876543210")
    language: Literal["en", "hi"] | None = None


class CreateCall(BaseModel):
    event: EventType
    customer: Customer
    order: Order
    schedule_at: datetime | None = Field(
        default=None, description="Earliest time to call; default now (inside calling hours)"
    )
    channel: Literal["phone", "web"] = Field(
        default="phone", description="'web' = test the call in your browser, no phone needed"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class AccountUpdate(BaseModel):
    brand_name: str | None = None
    agent_name: str | None = None
    default_language: Literal["en", "hi"] | None = None
    timezone: str | None = None
    call_window_start: str | None = Field(default=None, pattern=HHMM)
    call_window_end: str | None = Field(default=None, pattern=HHMM)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    retry_delay_minutes: int | None = Field(default=None, ge=1, le=1440)
    max_concurrent_calls: int | None = Field(default=None, ge=1, le=500)
    from_number: str | None = Field(default=None, pattern=E164)
    support_number: str | None = Field(default=None, pattern=E164)
    webhook_url: HttpUrl | None = None
    voice: dict[str, Any] | None = None
    telephony_provider: Literal["twilio", "plivo", "exotel"] | None = None


class CreateTenant(AccountUpdate):
    name: str
    brand_name: str


# ------------------------------------------------------------------ calls


@router.post("/v1/calls", status_code=201)
async def create_call(
    body: CreateCall,
    tenant: Tenant = Depends(current_tenant),
    idempotency_key: str | None = Header(default=None),
):
    return _created(await schedule_call(tenant, body, idempotency_key))


async def schedule_call(tenant: Tenant, body: CreateCall, idempotency_key: str | None = None):
    """Create (or, with the same idempotency key, return) a call. Shared with integrations."""
    if idempotency_key:
        existing = await deps.store.get_by_idempotency_key(tenant.id, idempotency_key)
        if existing:
            return existing

    language = body.customer.language or tenant.default_language
    # Browser test calls skip the dialer: they wait for the tester to open test_url.
    initial = "scheduled" if body.channel == "phone" else "waiting_for_browser"
    call = await deps.store.create(
        tenant_id=tenant.id,
        agent_id=f"ecom:{body.event}",
        event_type=body.event,
        channel=body.channel,
        direction="outbound" if body.channel == "phone" else "web",
        provider=(tenant.telephony_provider or settings.default_telephony_provider)
        if body.channel == "phone"
        else "webrtc",
        status=initial,
        to_number=body.customer.phone,
        customer_name=body.customer.name,
        language=language,
        order_id=body.order.id,
        idempotency_key=idempotency_key,
        payload={"order": body.order.model_dump(exclude_none=True)},
        metadata_=body.metadata,
        max_attempts=tenant.max_attempts if body.channel == "phone" else 1,
        next_attempt_at=(body.schedule_at or now_utc()) if body.channel == "phone" else None,
        timeline=[{"at": now_utc().isoformat(), "event": "created", "status": initial}],
    )
    return call


def _created(call) -> dict[str, Any]:
    data = {"id": call.id, "status": call.status, "next_attempt_at": call.next_attempt_at}
    if call.channel == "web":
        token = web_test_token(settings.stream_signing_secret, call.id)
        data["test_url"] = f"/test/{call.id}?token={token}"
    return data


@router.get("/v1/calls")
async def list_calls(
    tenant: Tenant = Depends(current_tenant),
    status: str | None = None,
    event: str | None = None,
    order_id: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    calls = await deps.store.list_calls(
        limit, offset, tenant_id=tenant.id, status=status, event_type=event, order_id=order_id
    )
    return {"data": [public_call(c) for c in calls]}


async def _tenant_call(call_id: str, tenant: Tenant):
    call = await deps.store.get(call_id, tenant_id=tenant.id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@router.get("/v1/calls/{call_id}")
async def get_call(call_id: str, tenant: Tenant = Depends(current_tenant)):
    call = await _tenant_call(call_id, tenant)
    data = public_call(call, include_transcript=True)
    if call.channel == "web" and call.status == "waiting_for_browser":
        data["test_url"] = _created(call)["test_url"]
    return data


@router.get("/v1/calls/{call_id}/recording")
async def get_recording(call_id: str, tenant: Tenant = Depends(current_tenant)):
    call = await _tenant_call(call_id, tenant)
    if not call.recording_path or not Path(call.recording_path).is_file():
        raise HTTPException(status_code=404, detail="No recording for this call")
    return FileResponse(call.recording_path, media_type="audio/wav", filename=f"{call.id}.wav")


@router.post("/v1/calls/{call_id}/cancel")
async def cancel_call(call_id: str, tenant: Tenant = Depends(current_tenant)):
    call = await _tenant_call(call_id, tenant)
    if not await service.cancel(call):
        raise HTTPException(status_code=409, detail=f"Cannot cancel a call that is {call.status}")
    return {"id": call.id, "status": "canceled"}


# ---------------------------------------------------------------- account


@router.get("/v1/events")
async def list_events():
    return {
        "data": [
            {"event": name, "title": t.title, "goal": t.goal, "outcomes": list(t.outcomes)}
            for name, t in TEMPLATES.items()
        ],
        "languages": list(DEFAULT_STACKS),
    }


ANSWERED = ("completed", "transferred")
FINISHED = (*ANSWERED, "unreachable", "failed")


@router.get("/v1/stats")
async def stats(tenant: Tenant = Depends(current_tenant), days: int = Query(default=14, le=90)):
    """Breakdowns plus a daily trend (in the merchant's timezone) for the dashboard."""
    counts = await deps.store.stats(tenant.id)
    tz = ZoneInfo(tenant.timezone)
    today = now_utc().astimezone(tz).date()
    first_day = today - timedelta(days=days - 1)
    since = datetime.combine(first_day, datetime.min.time(), tzinfo=tz)
    rows = await deps.store.activity_since(tenant.id, since)

    daily = {first_day + timedelta(days=i): {"calls": 0, "answered": 0} for i in range(days)}
    durations = []
    for created_at, status, duration in rows:
        bucket = daily.get(created_at.astimezone(tz).date())
        if bucket is not None:
            bucket["calls"] += 1
            bucket["answered"] += status in ANSWERED
        if status in ANSWERED and duration:
            durations.append(duration)

    by_status = counts["status"]
    finished = sum(by_status.get(s, 0) for s in FINISHED)
    answered = sum(by_status.get(s, 0) for s in ANSWERED)
    return {
        **counts,
        "totals": {
            "calls": sum(by_status.values()),
            "finished": finished,
            "answered": answered,
            "answer_rate": round(answered / finished, 3) if finished else None,
            "avg_duration_secs": round(sum(durations) / len(durations), 1) if durations else None,
            "pending": sum(
                by_status.get(s, 0)
                for s in ("scheduled", "dialing", "ringing", "in_progress", "waiting_for_browser")
            ),
        },
        "daily": [{"date": d.isoformat(), **v} for d, v in daily.items()],
    }


@router.get("/v1/account")
async def get_account(tenant: Tenant = Depends(current_tenant)):
    return tenant.to_dict()


@router.patch("/v1/account")
async def update_account(body: AccountUpdate, tenant: Tenant = Depends(current_tenant)):
    fields = body.model_dump(exclude_unset=True)
    if "webhook_url" in fields and fields["webhook_url"] is not None:
        fields["webhook_url"] = str(fields["webhook_url"])
    _check_timezone(fields.get("timezone"))
    updated = await deps.store.update_tenant(tenant.id, **fields)
    return updated.to_dict()


@router.post("/v1/account/webhook-secret")
async def rotate_webhook_secret(principal: Principal = Depends(require_role("owner", "admin"))):
    """Issue a new webhook signing secret (the old one stops working immediately)."""
    secret = "whsec_" + secrets.token_urlsafe(24)
    await deps.store.update_tenant(principal.tenant.id, webhook_secret=secret)
    return {"webhook_secret": secret, "note": "Copy it now; it is not shown again."}


@router.post("/admin/tenants", status_code=201, dependencies=[Depends(require_admin)])
async def create_tenant(body: CreateTenant):
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "webhook_url" in fields:
        fields["webhook_url"] = str(fields["webhook_url"])
    _check_timezone(fields.get("timezone"))
    tenant, api_key, webhook_secret = await provision_tenant(**fields)
    return {
        "tenant": tenant.to_dict(),
        "api_key": api_key,
        "webhook_secret": webhook_secret,
        "note": "Store the API key and webhook secret now; they are not shown again.",
    }


def _check_timezone(name: str | None) -> None:
    if name is None:
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail=f"Unknown timezone '{name}'") from None
