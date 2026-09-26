import base64
import hashlib
import hmac
import io
import json
import uuid
import wave
from xml.etree import ElementTree

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import accounts, main, voices
from app.bot import load_clip_frames
from app.db import Call, Tenant
from app.ecommerce import build_agent
from app.integrations import normalize_phone


@pytest.fixture
def client(monkeypatch):
    dialed = {"twilio": [], "plivo": [], "exotel": []}

    async def twilio_call(to, from_, twiml, status_callback):
        dialed["twilio"].append({"to": to})
        return f"CA{uuid.uuid4().hex}"

    async def plivo_call(to, from_, answer_url, hangup_url):
        dialed["plivo"].append({"to": to, "answer_url": answer_url, "hangup_url": hangup_url})
        return f"req-{uuid.uuid4().hex}"

    async def exotel_call(to, caller_id, status_callback, call_id):
        dialed["exotel"].append({"to": to, "status_callback": status_callback, "call_id": call_id})
        return f"ex{uuid.uuid4().hex}"

    monkeypatch.setattr(main.deps.twilio, "place_call", twilio_call)
    monkeypatch.setattr(main.deps.telephony["plivo"], "place_call", plivo_call)
    monkeypatch.setattr(main.deps.telephony["exotel"], "place_call", exotel_call)
    with TestClient(main.app) as c:
        c.dialed = dialed
        c.tick = lambda: c.portal.call(main.service.tick)
        yield c


def signup(client, **overrides):
    body = {
        "name": "Piyush",
        "email": f"owner-{uuid.uuid4().hex[:8]}@example.com",
        "password": "correct horse battery",
        "brand_name": "Kurta Kart",
        **overrides,
    }
    res = client.post("/auth/signup", json=body)
    assert res.status_code == 201, res.text
    data = res.json()
    data["session"] = {"Authorization": f"Bearer {data['session_token']}"}
    data["key"] = {"Authorization": f"Bearer {data['api_key']}"}
    data["email"] = body["email"]
    return data


def always_open(client, auth, **fields):
    res = client.patch(
        "/v1/account",
        headers=auth,
        json={"call_window_start": "00:00", "call_window_end": "00:00", **fields},
    )
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------- accounts


def test_signup_login_logout(client):
    acct = signup(client)
    assert acct["api_key"].startswith("sk_live_") and acct["user"]["role"] == "owner"
    assert client.get("/auth/me", headers=acct["session"]).json()["user"]["email"] == acct["email"]
    assert client.get("/v1/account", headers=acct["key"]).json()["brand_name"] == "Kurta Kart"

    dup = client.post(
        "/auth/signup",
        json={"name": "x", "email": acct["email"], "password": "12345678", "brand_name": "y"},
    )
    assert dup.status_code == 409
    weak = client.post(
        "/auth/signup",
        json={"name": "x", "email": "w@example.com", "password": "short", "brand_name": "y"},
    )
    assert weak.status_code == 422

    assert (
        client.post("/auth/login", json={"email": acct["email"], "password": "nope"}).status_code
        == 401
    )
    login = client.post(
        "/auth/login", json={"email": acct["email"].upper(), "password": "correct horse battery"}
    )
    assert login.status_code == 200
    session = {"Authorization": f"Bearer {login.json()['session_token']}"}
    assert client.post("/auth/logout", headers=session).status_code == 200
    assert client.get("/auth/me", headers=session).status_code == 401


def test_login_lockout(client):
    acct = signup(client)
    for _ in range(accounts.MAX_LOGIN_FAILURES):
        client.post("/auth/login", json={"email": acct["email"], "password": "wrong-password"})
    res = client.post(
        "/auth/login", json={"email": acct["email"], "password": "correct horse battery"}
    )
    assert res.status_code == 429


def test_signup_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(main.settings, "allow_signup", False)
    res = client.post(
        "/auth/signup",
        json={
            "name": "x",
            "email": "closed@example.com",
            "password": "12345678",
            "brand_name": "y",
        },
    )
    assert res.status_code == 403


def test_api_keys_create_list_revoke(client):
    acct = signup(client)
    assert (
        client.get("/v1/api-keys", headers=acct["key"]).status_code == 403
    )  # keys can't manage keys
    new = client.post(
        "/v1/api-keys", headers=acct["session"], json={"name": "Shopify server"}
    ).json()
    new_auth = {"Authorization": f"Bearer {new['api_key']}"}
    assert client.get("/v1/calls", headers=new_auth).status_code == 200
    names = [k["name"] for k in client.get("/v1/api-keys", headers=acct["session"]).json()["data"]]
    assert names == ["Default", "Shopify server"]
    assert client.delete(f"/v1/api-keys/{new['id']}", headers=acct["session"]).status_code == 200
    assert client.get("/v1/calls", headers=new_auth).status_code == 401


def test_team_invite_accept_and_roles(client):
    owner = signup(client)
    invite = client.post(
        "/v1/team/invites",
        headers=owner["session"],
        json={"email": "sam@example.com", "role": "member"},
    )
    assert invite.status_code == 201
    token = invite.json()["invite_path"].rsplit("/", 1)[1]

    joined = client.post(
        "/auth/accept-invite", json={"token": token, "name": "Sam", "password": "12345678"}
    )
    assert joined.status_code == 201 and joined.json()["tenant"]["id"] == owner["tenant"]["id"]
    member = {"Authorization": f"Bearer {joined.json()['session_token']}"}
    # Invite links work once.
    assert (
        client.post(
            "/auth/accept-invite", json={"token": token, "name": "X", "password": "12345678"}
        ).status_code
        == 404
    )
    # Members see the team but cannot invite or manage keys.
    team = client.get("/v1/team", headers=member).json()
    assert {m["email"] for m in team["members"]} == {owner["email"], "sam@example.com"}
    assert (
        client.post("/v1/team/invites", headers=member, json={"email": "z@example.com"}).status_code
        == 403
    )
    assert client.get("/v1/api-keys", headers=member).status_code == 403

    sam = next(m for m in team["members"] if m["email"] == "sam@example.com")
    assert (
        client.delete(
            f"/v1/team/members/{owner['user']['id']}", headers=owner["session"]
        ).status_code
        == 409
    )
    assert (
        client.delete(f"/v1/team/members/{sam['id']}", headers=owner["session"]).status_code == 200
    )
    assert client.get("/v1/team", headers=member).status_code == 401


def test_webhook_secret_rotation(client):
    acct = signup(client)
    assert client.post("/v1/account/webhook-secret", headers=acct["key"]).status_code == 403
    res = client.post("/v1/account/webhook-secret", headers=acct["session"]).json()
    assert (
        res["webhook_secret"].startswith("whsec_")
        and res["webhook_secret"] != acct["webhook_secret"]
    )


# ------------------------------------------------------------ integrations


def _signed(secret: str, payload) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    return body, base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


SHOPIFY_ORDER = {
    "id": 450789469,
    "name": "#1001",
    "total_price": "1499.00",
    "currency": "INR",
    "payment_gateway_names": ["Cash on Delivery (COD)"],
    "line_items": [{"title": "Cotton Kurta", "quantity": 2}],
    "shipping_address": {
        "name": "Riya Sharma",
        "phone": "098765 43210",
        "address1": "12 MG Road",
        "city": "Pune",
        "country_code": "IN",
    },
}


def _enable(client, acct, provider, secret="shpss_test_secret", **extra):
    res = client.put(
        f"/v1/integrations/{provider}",
        headers=acct["session"],
        json={"enabled": True, "secret": secret, **extra},
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_shopify_flow(client):
    acct = signup(client)
    tid = acct["tenant"]["id"]
    url = f"/integrations/shopify/{tid}"
    assert client.post(url, content=b"{}").status_code == 404  # not enabled yet
    cfg = _enable(client, acct, "shopify")
    assert cfg["webhook_url"].endswith(url) and cfg["events"]["cod_verification"]

    body, sig = _signed("shpss_test_secret", SHOPIFY_ORDER)
    headers = {"X-Shopify-Topic": "orders/create", "X-Shopify-Hmac-Sha256": sig}
    assert (
        client.post(
            url, content=body, headers={**headers, "X-Shopify-Hmac-Sha256": "bad"}
        ).status_code
        == 401
    )

    first = client.post(url, content=body, headers=headers).json()["results"][0]
    again = client.post(url, content=body, headers=headers).json()["results"][0]
    assert first["event"] == "cod_verification" and first["call_id"] == again["call_id"]
    call = client.get(f"/v1/calls/{first['call_id']}", headers=acct["key"]).json()
    assert call["to_number"] == "+919876543210" and call["customer_name"] == "Riya Sharma"
    assert call["payload"]["order"]["items"] == [{"name": "Cotton Kurta", "quantity": 2}]
    assert call["metadata"]["source"] == "shopify"

    # COD orders are "paid" at delivery: no payment-success call.
    body, sig = _signed("shpss_test_secret", SHOPIFY_ORDER)
    paid = client.post(
        url, content=body, headers={"X-Shopify-Topic": "orders/paid", "X-Shopify-Hmac-Sha256": sig}
    ).json()
    assert paid["results"] == []

    # Out-for-delivery events reuse the customer from the earlier order webhook.
    event = {"id": 1, "order_id": 450789469, "fulfillment_id": 7, "status": "out_for_delivery"}
    body, sig = _signed("shpss_test_secret", event)
    out = client.post(
        url,
        content=body,
        headers={"X-Shopify-Topic": "fulfillment_events/create", "X-Shopify-Hmac-Sha256": sig},
    ).json()
    ofd = client.get(f"/v1/calls/{out['results'][0]['call_id']}", headers=acct["key"]).json()
    assert ofd["event_type"] == "out_for_delivery" and ofd["to_number"] == "+919876543210"


def test_shopify_disabled_event_is_skipped(client):
    acct = signup(client)
    _enable(client, acct, "shopify")
    prepaid = {**SHOPIFY_ORDER, "id": 99, "payment_gateway_names": ["razorpay"]}
    body, sig = _signed("shpss_test_secret", prepaid)
    res = client.post(
        f"/integrations/shopify/{acct['tenant']['id']}",
        content=body,
        headers={"X-Shopify-Topic": "orders/create", "X-Shopify-Hmac-Sha256": sig},
    ).json()
    assert res["results"] == [{"event": "order_confirmation", "skipped": "event disabled"}]


def test_woocommerce_flow(client):
    acct = signup(client)
    _enable(client, acct, "woocommerce", secret="woo-secret-123")
    url = f"/integrations/woocommerce/{acct['tenant']['id']}"
    order = {
        "id": 727,
        "number": "727",
        "status": "processing",
        "payment_method": "cod",
        "payment_method_title": "Cash on delivery",
        "total": "899.00",
        "currency": "INR",
        "billing": {
            "first_name": "Aman",
            "last_name": "Verma",
            "phone": "+91 98123 45678",
            "country": "IN",
            "address_1": "5 FC Road",
            "city": "Pune",
        },
        "shipping": {},
        "line_items": [{"name": "Dupatta", "quantity": 1}],
    }
    body, sig = _signed("woo-secret-123", order)
    created = client.post(
        url,
        content=body,
        headers={"X-WC-Webhook-Topic": "order.created", "X-WC-Webhook-Signature": sig},
    ).json()
    call = client.get(f"/v1/calls/{created['results'][0]['call_id']}", headers=acct["key"]).json()
    assert call["event_type"] == "cod_verification" and call["to_number"] == "+919812345678"
    assert call["payload"]["order"]["address"] == "5 FC Road, Pune"

    failed = {**order, "id": 728, "status": "failed", "payment_method": "razorpay"}
    body, sig = _signed("woo-secret-123", failed)
    res = client.post(
        url,
        content=body,
        headers={"X-WC-Webhook-Topic": "order.updated", "X-WC-Webhook-Signature": sig},
    ).json()
    assert res["results"][0]["event"] == "payment_failed" and "call_id" in res["results"][0]

    body = b"webhook_id=15"
    sig = base64.b64encode(hmac.new(b"woo-secret-123", body, hashlib.sha256).digest()).decode()
    assert (
        client.post(url, content=body, headers={"X-WC-Webhook-Signature": sig}).json()["ping"]
        is True
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("098765 43210", "+919876543210"),
        ("9876543210", "+919876543210"),
        ("+91-98765-43210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("0044 20 7946 0958", "+442079460958"),
        ("", None),
        ("12", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw, "91") == expected


# --------------------------------------------------------------- telephony


def _create_phone_call(client, acct, **customer):
    res = client.post(
        "/v1/calls",
        headers=acct["key"],
        json={
            "event": "out_for_delivery",
            "customer": {"name": "Riya", "phone": "+919876543210", **customer},
            "order": {"id": f"O-{uuid.uuid4().hex[:6]}"},
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_plivo_dial_answer_and_busy_retry(client):
    acct = signup(client)
    always_open(client, acct["key"], telephony_provider="plivo", from_number="+918000000000")
    call_id = _create_phone_call(client, acct)
    client.tick()
    placed = next(p for p in client.dialed["plivo"] if call_id in p["answer_url"])
    answer_path = placed["answer_url"].split("voice.example.com", 1)[1]

    xml = client.post(answer_path)
    stream = ElementTree.fromstring(xml.text).find("Stream")
    assert stream.get("bidirectional") == "true"
    assert stream.text.startswith(f"wss://voice.example.com/telephony/plivo/stream/{call_id}/")
    assert client.post(f"/telephony/plivo/answer/{call_id}?token=bad").status_code == 403

    hangup_path = placed["hangup_url"].split("voice.example.com", 1)[1]
    client.post(hangup_path, data={"CallStatus": "busy", "HangupCause": "USER_BUSY"})
    call = client.get(f"/v1/calls/{call_id}", headers=acct["key"]).json()
    assert call["status"] == "scheduled" and call["end_reason"] == "busy"
    assert call["timeline"][1]["event"] == "dialing attempt 1 via plivo"


def test_exotel_dial_stream_url_and_status(client):
    acct = signup(client)
    always_open(client, acct["key"], telephony_provider="exotel")
    call_id = _create_phone_call(client, acct)
    client.tick()
    placed = next(p for p in client.dialed["exotel"] if p["call_id"] == call_id)

    url = client.get("/telephony/exotel/stream-url", params={"CustomField": call_id}).json()["url"]
    assert url.startswith(f"wss://voice.example.com/telephony/exotel/stream/{call_id}/")
    assert (
        client.get("/telephony/exotel/stream-url", params={"CustomField": "nope"}).status_code
        == 404
    )

    status_path = placed["status_callback"].split("voice.example.com", 1)[1]
    client.post(status_path, data={"Status": "no-answer", "CallSid": "x"})
    assert (
        client.get(f"/v1/calls/{call_id}", headers=acct["key"]).json()["end_reason"] == "no_answer"
    )


def test_plivo_transfer_xml(client):
    acct = signup(client)
    always_open(client, acct["key"], telephony_provider="plivo", support_number="+911140000000")
    call_id = _create_phone_call(client, acct)
    from app.telephony import sign_stream_token

    token = sign_stream_token("test-secret", call_id)
    xml = ElementTree.fromstring(
        client.post(f"/telephony/plivo/transfer/{call_id}?token={token}").text
    )
    assert xml.find("./Dial/Number").text == "911140000000"


# ------------------------------------------------------------------ voices


def _wav(secs=1.0, rate=16000, channels=2) -> bytes:
    samples = (np.sin(np.linspace(0, 440 * 2 * np.pi * secs, int(rate * secs))) * 8000).astype(
        np.int16
    )
    if channels == 2:
        samples = np.repeat(samples, 2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


def test_voice_catalog_and_preview(client, monkeypatch):
    acct = signup(client)
    catalog = client.get("/v1/voices", headers=acct["key"]).json()["providers"]
    assert "priya" in [v["id"] for v in catalog["sarvam"]["voices"]]

    monkeypatch.setattr(main.settings, "elevenlabs_api_key", "")
    res = client.post(
        "/v1/voices/preview", headers=acct["key"], json={"provider": "elevenlabs", "voice": "abc"}
    )
    assert res.status_code == 400

    async def fake_synthesize(body):
        return b"RIFFfake", "audio/wav"

    monkeypatch.setattr(voices, "synthesize", fake_synthesize)
    res = client.post(
        "/v1/voices/preview",
        headers=acct["key"],
        json={"provider": "sarvam", "voice": "priya", "language": "hi"},
    )
    assert (
        res.status_code == 200
        and res.headers["content-type"] == "audio/wav"
        and res.content == b"RIFFfake"
    )


def test_recorded_greeting_clip(client):
    acct = signup(client)
    files = {"file": ("greeting.wav", _wav(), "audio/wav")}
    data = {"text": "Namaste, main Kurta Kart se Priya bol rahi hoon."}
    assert (
        client.put(
            "/v1/clips/hi/cod_verification", headers=acct["key"], files=files, data=data
        ).status_code
        == 403
    )
    res = client.put(
        "/v1/clips/hi/cod_verification", headers=acct["session"], files=files, data=data
    )
    assert res.status_code == 200 and res.json()["secs"] == 1.0

    bad = client.put(
        "/v1/clips/hi/cod_verification",
        headers=acct["session"],
        files={"file": ("x.wav", b"not audio", "audio/wav")},
        data=data,
    )
    assert bad.status_code == 422
    long = client.put(
        "/v1/clips/hi/cod_verification",
        headers=acct["session"],
        files={"file": ("x.wav", _wav(secs=31, rate=8000, channels=1), "audio/wav")},
        data=data,
    )
    assert long.status_code == 422

    assert (
        client.get("/v1/account", headers=acct["key"]).json()["clips"]["hi:cod_verification"][
            "secs"
        ]
        == 1.0
    )
    audio = client.get("/v1/clips/hi/cod_verification/audio", headers=acct["key"])
    with wave.open(io.BytesIO(audio.content)) as wf:
        assert wf.getnchannels() == 1  # stored as mono

    tenant = client.portal.call(main.deps.store.get_tenant, acct["tenant"]["id"])
    call = Call(
        id="c",
        tenant_id=tenant.id,
        event_type="cod_verification",
        language="hi",
        customer_name="Riya",
        payload={"order": {"id": "1"}},
    )
    agent = build_agent(tenant, call)
    assert agent.greeting == data["text"] and agent.greeting_audio.endswith(
        "hi_cod_verification.wav"
    )
    frames = load_clip_frames(agent.greeting_audio)
    assert sum(len(f.audio) for f in frames) == 16000 * 2 and frames[0].sample_rate == 16000

    # Other languages/events still use the synthetic greeting.
    english = Call(
        id="d",
        tenant_id=tenant.id,
        event_type="cod_verification",
        language="en",
        customer_name="Riya",
        payload={"order": {"id": "1"}},
    )
    assert build_agent(tenant, english).greeting_audio is None
    assert (
        client.delete("/v1/clips/hi/cod_verification", headers=acct["session"]).status_code == 200
    )
    assert client.get("/v1/clips/hi/cod_verification/audio", headers=acct["key"]).status_code == 404


def test_tenant_to_dict_hides_integration_secrets():
    t = Tenant(
        id="t",
        name="n",
        brand_name="b",
        api_key_hash="h",
        api_key_prefix="p",
        webhook_secret="w",
        voice={},
        integrations={"shopify": {"secret": "s"}},
        clips={"hi:x": {"path": "/p", "text": "t", "secs": 1}},
    )
    data = t.to_dict()
    assert "integrations" not in data and "webhook_secret" not in data
    assert data["clips"] == {"hi:x": {"text": "t", "secs": 1}}
