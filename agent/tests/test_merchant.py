import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import calls as calls_module
from app import main
from app.bot import CallSession, _build_tools
from app.calls import next_allowed_time
from app.db import Call, Tenant
from app.ecommerce import TEMPLATES, build_agent

ADMIN = {"X-Admin-Key": "test-key"}
ORDER = {
    "id": "ORD-1001",
    "amount": 1499,
    "items": [{"name": "Cotton Kurta", "quantity": 2}],
    "payment_method": "cod",
    "address": "12 MG Road, Pune",
}


@pytest.fixture
def client(monkeypatch):
    placed, webhooks = [], []

    async def fake_place_call(to, from_, twiml, status_callback):
        placed.append({"to": to, "from": from_, "twiml": twiml})
        return f"CA{uuid.uuid4().hex}"  # unique, like real Twilio call SIDs

    async def fake_post(url, content, headers):
        webhooks.append({"url": url, "body": content, "headers": headers})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(main.deps.twilio, "place_call", fake_place_call)
    monkeypatch.setattr(main.service._http, "post", fake_post)
    with TestClient(main.app) as c:
        c.placed, c.webhooks = placed, webhooks
        c.tick = lambda: c.portal.call(main.service.tick)
        yield c


def make_tenant(client, **overrides):
    body = {
        "name": "Shop",
        "brand_name": "Kurta Kart",
        "call_window_start": "00:00",
        "call_window_end": "00:00",  # any time
        "webhook_url": "https://merchant.example.com/hooks",
        **overrides,
    }
    res = client.post("/admin/tenants", headers=ADMIN, json=body)
    assert res.status_code == 201, res.text
    data = res.json()
    return {"Authorization": f"Bearer {data['api_key']}"}, data


def create_call(client, auth, **overrides):
    body = {
        "event": "cod_verification",
        "customer": {"name": "Riya", "phone": "+919876543210"},
        "order": ORDER,
        **overrides,
    }
    res = client.post("/v1/calls", headers=auth, json=body)
    assert res.status_code == 201, res.text
    return res.json()


def wait_for(client, auth, call_id, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        call = client.get(f"/v1/calls/{call_id}", headers=auth).json()
        if predicate(call):
            return call
        time.sleep(0.05)
    raise AssertionError(f"condition not met: {call}")


# ------------------------------------------------------------------ auth


def test_admin_key_required(client):
    assert client.post("/admin/tenants", json={"name": "x", "brand_name": "x"}).status_code == 401


def test_api_key_required_and_tenants_isolated(client):
    assert client.get("/v1/calls").status_code == 401
    assert client.get("/v1/calls", headers={"Authorization": "Bearer nope"}).status_code == 401

    auth_a, _ = make_tenant(client)
    auth_b, _ = make_tenant(client, name="Other")
    call = create_call(client, auth_a)
    assert client.get(f"/v1/calls/{call['id']}", headers=auth_b).status_code == 404
    assert all(
        c["id"] != call["id"] for c in client.get("/v1/calls", headers=auth_b).json()["data"]
    )


def test_account_hides_secrets_and_updates(client):
    auth, created = make_tenant(client)
    assert created["api_key"].startswith("sk_live_")
    account = client.get("/v1/account", headers=auth).json()
    assert "api_key_hash" not in account and "webhook_secret" not in account
    res = client.patch(
        "/v1/account", headers=auth, json={"agent_name": "Neha", "timezone": "Mars/X"}
    )
    assert res.status_code == 422
    res = client.patch("/v1/account", headers=auth, json={"agent_name": "Neha"})
    assert res.json()["agent_name"] == "Neha"


# ----------------------------------------------------------- scheduling


def test_idempotency_key_returns_same_call(client):
    auth, _ = make_tenant(client)
    headers = {**auth, "Idempotency-Key": "order-1001-cod"}
    body = {
        "event": "cod_verification",
        "customer": {"name": "R", "phone": "+919876543210"},
        "order": ORDER,
    }
    first = client.post("/v1/calls", headers=headers, json=body).json()
    second = client.post("/v1/calls", headers=headers, json=body).json()
    assert first["id"] == second["id"]


def test_due_call_is_dialed_with_signed_stream(client):
    auth, _ = make_tenant(client)
    call = create_call(client, auth)
    client.tick()
    assert client.placed and client.placed[-1]["to"] == "+919876543210"
    assert call["id"] in client.placed[-1]["twiml"]
    detail = client.get(f"/v1/calls/{call['id']}", headers=auth).json()
    assert detail["status"] == "dialing" and detail["attempts"] == 1
    assert [t["event"] for t in detail["timeline"]][:2] == ["created", "dialing attempt 1"]


def test_outside_calling_hours_defers(client, monkeypatch):
    auth, _ = make_tenant(client, call_window_start="09:00", call_window_end="21:00")
    call = create_call(client, auth)
    fake_now = datetime(2030, 1, 1, 21, 30, tzinfo=UTC)  # 03:00 IST
    monkeypatch.setattr(calls_module, "now_utc", lambda: fake_now)
    monkeypatch.setattr("app.db.now_utc", lambda: fake_now)
    before = len(client.placed)
    client.tick()
    detail = client.get(f"/v1/calls/{call['id']}", headers=auth).json()
    assert len(client.placed) == before
    assert detail["status"] == "scheduled"
    assert detail["next_attempt_at"].startswith("2030-01-02T03:30")  # 09:00 IST


def test_busy_retries_then_unreachable_with_signed_webhook(client):
    auth, created = make_tenant(client, max_attempts=2, retry_delay_minutes=1)
    call = create_call(client, auth, metadata={"shop_order": 1001})
    client.tick()

    sid = client.get(f"/v1/calls/{call['id']}", headers=auth).json()["provider_call_id"]
    client.post("/telephony/twilio/status", data={"CallSid": sid, "CallStatus": "busy"})
    detail = client.get(f"/v1/calls/{call['id']}", headers=auth).json()
    assert detail["status"] == "scheduled" and detail["end_reason"] == "busy"

    # Second attempt (force it due now) also fails -> unreachable + webhook.
    async def make_due():
        await main.deps.store.update(call["id"], next_attempt_at=datetime.now(UTC))

    client.portal.call(make_due)
    client.tick()
    sid = client.get(f"/v1/calls/{call['id']}", headers=auth).json()["provider_call_id"]
    client.post("/telephony/twilio/status", data={"CallSid": sid, "CallStatus": "no-answer"})

    final = wait_for(client, auth, call["id"], lambda c: c["webhook_status"] == "delivered")
    assert final["status"] == "unreachable" and final["attempts"] == 2

    hook = client.webhooks[-1]
    expected = hmac.new(created["webhook_secret"].encode(), hook["body"], hashlib.sha256)
    assert hook["headers"]["X-Signature"] == f"sha256={expected.hexdigest()}"
    payload = json.loads(hook["body"])
    assert payload["type"] == "call.completed"
    assert payload["data"]["order_id"] == "ORD-1001"
    assert payload["data"]["metadata"] == {"shop_order": 1001}


def test_cancel_before_dial(client):
    auth, _ = make_tenant(client)
    call = create_call(client, auth, schedule_at="2099-01-01T10:00:00Z")
    assert (
        client.post(f"/v1/calls/{call['id']}/cancel", headers=auth).json()["status"] == "canceled"
    )
    assert client.post(f"/v1/calls/{call['id']}/cancel", headers=auth).status_code == 409


def test_stats(client):
    auth, _ = make_tenant(client)
    create_call(client, auth)
    create_call(client, auth, event="order_shipped")
    stats = client.get("/v1/stats", headers=auth).json()
    assert stats["event_type"] == {"cod_verification": 1, "order_shipped": 1}


# ------------------------------------------------------------ browser test


def test_web_call_gets_test_page(client):
    auth, _ = make_tenant(client)
    call = create_call(
        client,
        auth,
        channel="web",
        customer={"name": "Riya", "phone": "+919876543210", "language": "hi"},
    )
    assert call["status"] == "waiting_for_browser"
    assert client.get(call["test_url"]).status_code == 200
    live = client.get(call["test_url"].replace("?", "/live?")).json()
    assert live["event"] == "cod_verification" and live["language"] == "hi"
    assert client.get(f"/test/{call['id']}?token=bad").status_code == 403
    client.tick()  # web calls are never dialed
    assert not any(call["id"] in p["twiml"] for p in client.placed)


# --------------------------------------------------------------- templates


def _tenant(**kw) -> Tenant:
    base = dict(
        id="t1",
        brand_name="Kurta Kart",
        agent_name="Priya",
        support_number="+911234567890",
        voice={},
    )
    return Tenant(**{**base, **kw})


def _call(event: str, language: str = "en") -> Call:
    return Call(
        id="c1",
        tenant_id="t1",
        event_type=event,
        language=language,
        customer_name="Riya",
        payload={"order": ORDER},
    )


@pytest.mark.parametrize("event", list(TEMPLATES))
def test_every_event_builds_grounded_agent(event):
    agent = build_agent(_tenant(), _call(event))
    assert "ORD-1001" in agent.system_prompt and "Cotton Kurta" in agent.system_prompt
    assert "Never invent" in agent.system_prompt
    assert set(TEMPLATES[event].outcomes) <= set(agent.outcomes)
    assert "Riya" in agent.greeting and "Kurta Kart" in agent.greeting
    assert agent.transfer_number == "+911234567890"


def test_hinglish_agent_uses_indic_stack_and_merchant_override():
    agent = build_agent(_tenant(), _call("out_for_delivery", "hi"))
    assert agent.tts.provider == "sarvam" and "नमस्ते" in agent.greeting
    custom = _tenant(voice={"hi": {"tts": {"provider": "elevenlabs", "voice": "cloned-human"}}})
    agent = build_agent(custom, _call("out_for_delivery", "hi"))
    assert (agent.tts.provider, agent.tts.voice) == ("elevenlabs", "cloned-human")
    assert agent.stt.provider == "sarvam"  # untouched stages keep defaults


async def test_record_outcome_tool_validates_and_saves():
    updates = []

    class Store:
        async def update(self, call_id, **fields):
            updates.append(fields)

    agent = build_agent(_tenant(), _call("cod_verification"))
    session = CallSession(call_id="c1", agent=agent)
    deps = SimpleNamespace(store=Store())
    _, handlers = _build_tools(session, deps, [])
    results = []

    async def result_callback(result, properties=None):
        results.append(result)

    await handlers["record_outcome"](
        SimpleNamespace(arguments={"outcome": "made_up"}, result_callback=result_callback)
    )
    assert results[-1]["status"] == "error" and session.outcome is None

    args = {"outcome": "change_requested", "notes": "New address: 5 FC Road", "preferred_time": ""}
    await handlers["record_outcome"](
        SimpleNamespace(arguments=args, result_callback=result_callback)
    )
    assert session.outcome == "change_requested"
    assert session.end_after_reply  # hang up after the goodbye
    assert session.outcome_data == {"notes": "New address: 5 FC Road"}
    assert updates[-1]["outcome"] == "change_requested"


# ------------------------------------------------------------ calling hours


@pytest.mark.parametrize(
    "utc_time,expected",
    [
        (
            datetime(2030, 1, 1, 5, 0, tzinfo=UTC),
            datetime(2030, 1, 1, 5, 0, tzinfo=UTC),
        ),  # 10:30 IST ok
        (
            datetime(2030, 1, 1, 1, 0, tzinfo=UTC),
            datetime(2030, 1, 1, 3, 30, tzinfo=UTC),
        ),  # 06:30 -> 09:00
        (
            datetime(2030, 1, 1, 16, 0, tzinfo=UTC),
            datetime(2030, 1, 2, 3, 30, tzinfo=UTC),
        ),  # 21:30 -> next day
    ],
)
def test_next_allowed_time(utc_time, expected):
    assert next_allowed_time(utc_time, "Asia/Kolkata", "09:00", "21:00") == expected


def test_always_open_and_overnight_windows():
    t = datetime(2030, 1, 1, 18, 29, tzinfo=UTC)  # 23:59 IST
    assert next_allowed_time(t, "Asia/Kolkata", "00:00", "00:00") == t
    assert next_allowed_time(t, "Asia/Kolkata", "20:00", "02:00") == t  # inside overnight
    noon = datetime(2030, 1, 1, 6, 30, tzinfo=UTC)  # 12:00 IST -> opens 20:00 IST
    assert next_allowed_time(noon, "Asia/Kolkata", "20:00", "02:00") == datetime(
        2030, 1, 1, 14, 30, tzinfo=UTC
    )
