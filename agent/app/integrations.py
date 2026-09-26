"""Shopify and WooCommerce: turn store webhooks into calls automatically.

Setup (no app install needed):
  Shopify      Settings -> Notifications -> Webhooks: add JSON webhooks for
               "Order creation", "Order payment", "Fulfillment creation" and
               "Fulfillment event creation" pointing at the URL from GET /v1/integrations.
               Paste the signing secret shown there into Dialyn.
  WooCommerce  WooCommerce -> Settings -> Advanced -> Webhooks: add "Order created" and
               "Order updated" webhooks (API version v3) with a secret of your choice.

Every request is verified with the store's secret (HMAC-SHA256, base64) before use.
One call per order per event (idempotency), so repeated webhooks never double-call.

  GET  /v1/integrations                      Status + webhook URLs
  PUT  /v1/integrations/{provider}           Secret, enabled events, country code, language
  POST /integrations/shopify/{tenant_id}     Shopify webhook receiver
  POST /integrations/woocommerce/{tenant_id} WooCommerce webhook receiver
"""

import base64
import hashlib
import hmac
import json
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from app.accounts import Principal, current_tenant, require_role
from app.api_v1 import CreateCall, schedule_call
from app.context import deps, settings
from app.db import Tenant
from app.ecommerce import TEMPLATES

router = APIRouter()
ProviderName = Literal["shopify", "woocommerce"]

DEFAULT_EVENTS = {
    "order_confirmation": False,
    "cod_verification": True,
    "payment_success": False,
    "payment_failed": True,
    "order_shipped": False,
    "out_for_delivery": True,
}
TOPICS = {
    "shopify": ["orders/create", "orders/paid", "fulfillments/create", "fulfillment_events/create"],
    "woocommerce": ["order.created", "order.updated"],
}
COUNTRY_CODES = {"IN": "91", "US": "1", "CA": "1", "GB": "44", "AE": "971", "SG": "65", "AU": "61"}


class IntegrationSettings(BaseModel):
    enabled: bool = True
    secret: str | None = Field(default=None, min_length=8, max_length=200)
    events: dict[str, bool] | None = None
    default_country_code: str = Field(default="91", pattern=r"^\d{1,3}$")
    language: Literal["en", "hi"] | None = None


def _config(tenant: Tenant, provider: str) -> dict[str, Any]:
    return (tenant.integrations or {}).get(provider) or {}


@router.get("/v1/integrations")
async def list_integrations(tenant: Tenant = Depends(current_tenant)):
    out = {}
    for provider in ("shopify", "woocommerce"):
        cfg = _config(tenant, provider)
        out[provider] = {
            "enabled": cfg.get("enabled", False),
            "configured": bool(cfg.get("secret")),
            "events": {**DEFAULT_EVENTS, **(cfg.get("events") or {})},
            "default_country_code": cfg.get("default_country_code", "91"),
            "language": cfg.get("language"),
            "webhook_url": f"{settings.public_base_url}/integrations/{provider}/{tenant.id}",
            "topics": TOPICS[provider],
        }
    return out


@router.put("/v1/integrations/{provider}")
async def save_integration(
    provider: ProviderName,
    body: IntegrationSettings,
    principal: Principal = Depends(require_role("owner", "admin")),
):
    tenant = principal.tenant
    current = _config(tenant, provider)
    events = {**DEFAULT_EVENTS, **(current.get("events") or {})}
    for name, on in (body.events or {}).items():
        if name not in TEMPLATES:
            raise HTTPException(status_code=422, detail=f"Unknown event '{name}'")
        events[name] = on
    secret = body.secret or current.get("secret")
    if body.enabled and not secret:
        raise HTTPException(status_code=422, detail="Add the webhook signing secret first")
    updated = {
        "enabled": body.enabled,
        "secret": secret,
        "events": events,
        "default_country_code": body.default_country_code,
        "language": body.language,
    }
    await deps.store.update_tenant(
        tenant.id, integrations={**(tenant.integrations or {}), provider: updated}
    )
    return (await list_integrations(await deps.store.get_tenant(tenant.id)))[provider]


# ------------------------------------------------------------- helpers


def normalize_phone(raw: str | None, country_code: str) -> str | None:
    """Best-effort E.164: '+91 98765-43210', '09876543210', '9876543210' -> +919876543210."""
    if not raw:
        return None
    has_plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if has_plus:
        candidate = "+" + digits
    elif digits.startswith("00"):
        candidate = "+" + digits[2:]
    elif country_code == "91" and len(digits) == 11 and digits.startswith("0"):
        candidate = "+91" + digits[1:]
    elif country_code == "91" and len(digits) == 10:
        candidate = "+91" + digits
    elif digits.startswith(country_code) and len(digits) > 10:
        candidate = "+" + digits
    else:
        candidate = "+" + country_code + digits.lstrip("0")
    return candidate if re.fullmatch(r"\+[1-9]\d{6,14}", candidate) else None


def _verify(secret: str, body: bytes, signature: str) -> bool:
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return bool(signature) and hmac.compare_digest(expected, signature)


def _join(*parts: Any) -> str | None:
    text = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
    return text or None


async def _schedule(
    tenant: Tenant, provider: str, event: str, customer: dict, order: dict, cfg: dict, extra: dict
) -> dict[str, Any]:
    events = {**DEFAULT_EVENTS, **(cfg.get("events") or {})}
    if not events.get(event):
        return {"event": event, "skipped": "event disabled"}
    if not customer.get("phone"):
        return {"event": event, "skipped": "no usable phone number"}
    if cfg.get("language") and not customer.get("language"):
        customer["language"] = cfg["language"]
    try:
        body = CreateCall(
            event=event,
            customer=customer,
            order={k: v for k, v in order.items() if v not in (None, "", [])},
            metadata={"source": provider, **extra},
        )
    except ValidationError as e:
        logger.warning(f"[{tenant.id}] {provider} {event} not scheduled: {e}")
        return {"event": event, "skipped": "invalid order data"}
    call = await schedule_call(tenant, body, idempotency_key=f"{provider}:{order['id']}:{event}")
    return {"event": event, "call_id": call.id}


async def _receive(request: Request, provider: str, tenant_id: str, signature_header: str):
    tenant = await deps.store.get_tenant(tenant_id)
    cfg = _config(tenant, provider) if tenant else {}
    if not tenant or not cfg.get("enabled") or not cfg.get("secret"):
        raise HTTPException(status_code=404, detail="Integration not enabled")
    body = await request.body()
    if not _verify(cfg["secret"], body, request.headers.get(signature_header, "")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    return tenant, cfg, body


# ------------------------------------------------------------- Shopify


def _is_cod(gateways: list[str]) -> bool:
    return any("cash on delivery" in g.lower() or g.lower() == "cod" for g in gateways or [])


def _shopify_customer(order: dict, cc: str) -> dict:
    ship = order.get("shipping_address") or {}
    bill = order.get("billing_address") or {}
    cust = order.get("customer") or {}
    name = ship.get("name") or _join(cust.get("first_name"), cust.get("last_name")) or "Customer"
    country = COUNTRY_CODES.get((ship.get("country_code") or bill.get("country_code") or ""), cc)
    phone = ship.get("phone") or order.get("phone") or bill.get("phone") or cust.get("phone")
    return {"name": name.replace(",", ""), "phone": normalize_phone(phone, country)}


def _shopify_order(order: dict) -> dict:
    ship = order.get("shipping_address") or {}
    return {
        "id": str(order["id"]),
        "amount": float(order["total_price"]) if order.get("total_price") else None,
        "currency": order.get("currency") or "INR",
        "items": [
            {"name": li.get("title") or li.get("name", "item"), "quantity": li.get("quantity", 1)}
            for li in order.get("line_items") or []
        ],
        "payment_method": ", ".join(order.get("payment_gateway_names") or []) or None,
        "address": _join(
            ship.get("address1"), ship.get("address2"), ship.get("city"), ship.get("zip")
        ),
    }


@router.post("/integrations/shopify/{tenant_id}")
async def shopify_webhook(tenant_id: str, request: Request):
    tenant, cfg, raw = await _receive(request, "shopify", tenant_id, "X-Shopify-Hmac-Sha256")
    topic = request.headers.get("X-Shopify-Topic", "")
    data = json.loads(raw or b"{}")
    cc = cfg.get("default_country_code", "91")
    extra = {"shopify_topic": topic}
    results = []

    if topic in ("orders/create", "orders/paid"):
        order, customer = _shopify_order(data), _shopify_customer(data, cc)
        extra["shopify_order_name"] = data.get("name")
        cod = _is_cod(data.get("payment_gateway_names"))
        if topic == "orders/create":
            event = "cod_verification" if cod else "order_confirmation"
            results.append(await _schedule(tenant, "shopify", event, customer, order, cfg, extra))
        elif not cod:  # COD orders are marked paid at delivery; no "payment received" call
            results.append(
                await _schedule(tenant, "shopify", "payment_success", customer, order, cfg, extra)
            )

    elif topic == "fulfillments/create":
        dest = data.get("destination") or {}
        order_id = str(data.get("order_id"))
        previous = await deps.store.latest_call_for_order(tenant.id, order_id)
        customer = {
            "name": dest.get("name") or (previous.customer_name if previous else "Customer"),
            "phone": normalize_phone(dest.get("phone"), cc)
            or (previous.to_number if previous else None),
        }
        order = {
            **((previous.payload or {}).get("order", {}) if previous else {}),
            "id": order_id,
            "courier": data.get("tracking_company"),
            "tracking_number": data.get("tracking_number"),
        }
        results.append(
            await _schedule(tenant, "shopify", "order_shipped", customer, order, cfg, extra)
        )

    elif topic == "fulfillment_events/create" and data.get("status") == "out_for_delivery":
        order_id = str(data.get("order_id"))
        previous = await deps.store.latest_call_for_order(tenant.id, order_id)
        if previous is None:
            results.append({"event": "out_for_delivery", "skipped": "order not seen before"})
        else:
            customer = {
                "name": previous.customer_name,
                "phone": previous.to_number,
                "language": previous.language,
            }
            order = {**(previous.payload or {}).get("order", {}), "id": order_id}
            results.append(
                await _schedule(tenant, "shopify", "out_for_delivery", customer, order, cfg, extra)
            )

    logger.info(f"[{tenant.id}] shopify {topic}: {results}")
    return {"ok": True, "topic": topic, "results": results}


# --------------------------------------------------------- WooCommerce

_WOO_SHIPPED = {"shipped", "wc-shipped", "partial-shipped", "dispatched"}
_WOO_OUT_FOR_DELIVERY = {"out-for-delivery", "out_for_delivery", "wc-out-for-delivery"}


def _woo_customer(order: dict, cc: str) -> dict:
    bill, ship = order.get("billing") or {}, order.get("shipping") or {}
    name = _join(ship.get("first_name"), ship.get("last_name")) or _join(
        bill.get("first_name"), bill.get("last_name")
    )
    country = COUNTRY_CODES.get(bill.get("country") or ship.get("country") or "", cc)
    return {
        "name": (name or "Customer").replace(",", ""),
        "phone": normalize_phone(ship.get("phone") or bill.get("phone"), country),
    }


def _woo_order(order: dict) -> dict:
    ship = order.get("shipping") or {}
    bill = order.get("billing") or {}
    addr = ship if ship.get("address_1") else bill
    return {
        "id": str(order["id"]),
        "amount": float(order["total"]) if order.get("total") else None,
        "currency": order.get("currency") or "INR",
        "items": [
            {"name": li.get("name", "item"), "quantity": li.get("quantity", 1)}
            for li in order.get("line_items") or []
        ],
        "payment_method": order.get("payment_method_title") or order.get("payment_method"),
        "address": _join(
            addr.get("address_1"), addr.get("address_2"), addr.get("city"), addr.get("postcode")
        ),
    }


@router.post("/integrations/woocommerce/{tenant_id}")
async def woocommerce_webhook(tenant_id: str, request: Request):
    tenant, cfg, raw = await _receive(request, "woocommerce", tenant_id, "X-WC-Webhook-Signature")
    topic = request.headers.get("X-WC-Webhook-Topic", "")
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": True, "ping": True}  # WooCommerce sends "webhook_id=..." when a hook is saved
    if topic not in ("order.created", "order.updated") or "id" not in data:
        return {"ok": True, "topic": topic, "results": []}

    cc = cfg.get("default_country_code", "91")
    customer, order = _woo_customer(data, cc), _woo_order(data)
    status = (data.get("status") or "").lower()
    cod = (data.get("payment_method") or "").lower() == "cod"
    extra = {
        "woocommerce_topic": topic,
        "woocommerce_status": status,
        "order_number": data.get("number"),
    }

    event = None
    if status == "failed":
        event = "payment_failed"
    elif status in _WOO_OUT_FOR_DELIVERY:
        event = "out_for_delivery"
    elif status in _WOO_SHIPPED:
        event = "order_shipped"
    elif topic == "order.created" and cod:
        event = "cod_verification"
    elif topic == "order.created" and status == "processing":
        event = "order_confirmation"
    elif topic == "order.updated" and status == "processing" and not cod and data.get("date_paid"):
        event = "payment_success"

    results = (
        [await _schedule(tenant, "woocommerce", event, customer, order, cfg, extra)]
        if event
        else []
    )
    logger.info(f"[{tenant.id}] woocommerce {topic}/{status}: {results}")
    return {"ok": True, "topic": topic, "results": results}
