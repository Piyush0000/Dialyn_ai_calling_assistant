from xml.etree import ElementTree

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app import main
from app.telephony import verify_stream_token

AUTH = {"X-API-Key": "test-key"}


@pytest.fixture
def client(monkeypatch):
    placed = []

    async def fake_place_call(to, from_, twiml, status_callback):
        placed.append({"to": to, "from": from_, "twiml": twiml, "cb": status_callback})
        return "CA123"

    monkeypatch.setattr(main.deps.twilio, "place_call", fake_place_call)
    with TestClient(main.app) as c:
        c.placed = placed
        yield c


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_api_requires_key(client):
    assert client.get("/api/calls").status_code == 401
    assert client.get("/api/calls", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/api/agents", headers=AUTH).status_code == 200


def test_inbound_webhook_returns_signed_stream(client):
    res = client.post(
        "/telephony/twilio/incoming",
        data={"CallSid": "CAin", "From": "+15551112222", "To": "+15550001111"},
    )
    assert res.status_code == 200
    stream = ElementTree.fromstring(res.text).find("./Connect/Stream")
    assert stream.get("url") == "wss://voice.example.com/telephony/twilio/stream"
    params = {p.get("name"): p.get("value") for p in stream.iter("Parameter")}
    assert params["agent_id"] == "default"
    assert verify_stream_token("test-secret", params["call_id"], params["token"])

    call = client.get(f"/api/calls/{params['call_id']}", headers=AUTH).json()
    assert call["direction"] == "inbound"
    assert call["provider_call_id"] == "CAin"


def test_inbound_unknown_agent_is_rejected_politely(client):
    res = client.post("/telephony/twilio/incoming?agent=nope", data={"CallSid": "CAx"})
    assert "<Hangup/>" in res.text


def test_outbound_call(client):
    res = client.post(
        "/api/calls",
        headers=AUTH,
        json={
            "to": "+919876543210",
            "agent_id": "hindi_sales",
            "variables": {"customer_name": "Riya"},
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["provider_call_id"] == "CA123"
    assert client.placed[0]["to"] == "+919876543210"
    assert client.placed[0]["cb"] == "https://voice.example.com/telephony/twilio/status"

    call = client.get(f"/api/calls/{body['id']}", headers=AUTH).json()
    assert call["variables"] == {"customer_name": "Riya"}

    # A "busy" status callback marks the call as such.
    client.post("/telephony/twilio/status", data={"CallSid": "CA123", "CallStatus": "busy"})
    assert client.get(f"/api/calls/{body['id']}", headers=AUTH).json()["status"] == "busy"


def test_outbound_validates_number(client):
    res = client.post("/api/calls", headers=AUTH, json={"to": "12345"})
    assert res.status_code == 422


def test_stream_with_bad_token_is_closed(client):
    import json

    with client.websocket_connect("/telephony/twilio/stream") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        ws.send_text(
            json.dumps(
                {
                    "event": "start",
                    "start": {
                        "streamSid": "MZ1",
                        "callSid": "CA1",
                        "customParameters": {"call_id": "x", "token": "bad"},
                    },
                }
            )
        )
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_browser_start_flow(client, monkeypatch):
    # The prebuilt UI calls /start, then posts its offer under /sessions/{id}/.
    session_id = client.post("/start", json={"transport": "webrtc", "body": {}}).json()["sessionId"]
    assert (
        client.post("/sessions/nope/api/offer", json={"sdp": "x", "type": "offer"}).status_code
        == 404
    )

    # Missing provider keys are reported clearly instead of a silent failed call.
    monkeypatch.setattr(main.settings, "deepgram_api_key", "")
    res = client.post(f"/sessions/{session_id}/api/offer", json={"sdp": "x", "type": "offer"})
    assert res.status_code == 400
    assert "DEEPGRAM_API_KEY" in res.json()["detail"]
