# Dialyn — AI Calling for E-commerce

![Python](https://img.shields.io/badge/python-3.12-blue)
![Pipecat](https://img.shields.io/badge/pipecat-1.11-purple)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![Status](https://img.shields.io/badge/status-beta-orange)

An API that any e-commerce platform can plug into so that a natural-sounding voice agent calls its
customers about their orders. The agent handles order confirmation, cash-on-delivery verification,
payment success and failure, shipping updates and out-for-delivery calls. Every call is logged with
its outcome, transcript, recording and timeline, and the result is sent back to the store's webhook.

Built on **Pipecat 1.11** + **FastAPI**, inspired by [Dograh](https://www.dograh.com/). See
**[ROADMAP.md](ROADMAP.md)** for the plan.

## How it works

```
Store backend ──POST /v1/calls──► Dialyn ──(inside calling hours, with retries)──► customer's phone
      ▲                                     real-time: listen → understand → speak (~0.8 s reply)
      └──────── webhook: call.completed {outcome, transcript, timeline, …} ◄──┘
```

| Event (`event`) | What the agent does | Outcomes it records |
|---|---|---|
| `order_confirmation` | Confirms the order, items and address | `confirmed`, `cancel_requested`, `change_requested` |
| `cod_verification` | Verifies a cash-on-delivery order before shipping | `confirmed`, `cancel_requested`, `change_requested`, `prepaid_requested` |
| `payment_success` | Thanks the customer and confirms the payment | `acknowledged` |
| `payment_failed` | Helps the customer retry or switch to COD | `will_retry_payment`, `switch_to_cod`, `cancel_requested` |
| `order_shipped` | Shares courier and delivery date | `acknowledged`, `change_requested` |
| `out_for_delivery` | Checks the customer is available, or captures a new time | `will_be_available`, `reschedule_requested`, `address_issue`, `cancel_requested` |

Every event can also end with `callback_requested`, `wrong_number` or `needs_human`. The agent only
states the order facts you send and never promises changes; it notes them for your team.

Languages: `en` (English) and `hi` (Hinglish, using Sarvam's Indian voices).

## Quick start

```bash
cd agent
uv sync
cp .env.example .env   # add DEEPGRAM_API_KEY + GROQ_API_KEY (free); SARVAM_API_KEY for Hinglish
uv run uvicorn app.main:app --port 7860
```

**1. Create your store:** open http://localhost:7860/dashboard → **Create store**. You get an owner
login, a first API key and a webhook secret (shown once, on the Developers page).

Or from the command line (platform admin; `API_KEY` from `.env`). On Windows PowerShell use
`curl.exe`, since plain `curl` is a different command there:

```bash
curl.exe -X POST http://localhost:7860/admin/tenants -H "X-Admin-Key: <API_KEY>" -H "Content-Type: application/json" -d "{\"name\":\"Demo Store\",\"brand_name\":\"Kurta Kart\",\"agent_name\":\"Priya\"}"
```

The response contains the merchant's `api_key` (`sk_live_…`) and `webhook_secret`. They are shown only once.

**2. Schedule a call.** Use `"channel": "web"` to test it in your browser without a phone:

```bash
curl.exe -X POST http://localhost:7860/v1/calls -H "Authorization: Bearer <sk_live_...>" -H "Content-Type: application/json" -d "{\"event\":\"cod_verification\",\"channel\":\"web\",\"customer\":{\"name\":\"Piyush\",\"phone\":\"+919876543210\",\"language\":\"en\"},\"order\":{\"id\":\"KK-20931\",\"amount\":1499,\"items\":[{\"name\":\"Cotton Kurta\",\"quantity\":2}],\"payment_method\":\"Cash on delivery\",\"address\":\"12 MG Road, Pune\"}}"
```

**3. Open the returned `test_url`** (e.g. `http://localhost:7860/test/<id>?token=…`) and click **Start call**.
The page shows the live transcript, the outcome and the timeline.

## Merchant dashboard

Open http://localhost:7860/dashboard: **Sign in** with email, **Create store**, or open it with an API key.

- **Overview**: total calls, answer rate, average call length, calls per day, outcomes, event and status breakdowns
- **Calls**: every call with status, outcome and attempts; filter by status, event or order ID
- **Call detail**: outcome and notes, order facts, recording player, transcript, timeline, cancel, open browser test
- **New call**: try any event in the browser (free) or schedule a real phone call
- **Integrations**: connect Shopify / WooCommerce, pick which events call customers
- **Voice & greetings**: choose and preview voices per language; upload a real person's greeting
- **Team**: invite teammates (owner / admin / member roles), remove members
- **Settings**: brand, agent name, language, calling hours, retries, concurrency, phone provider, caller ID, support number, webhook
- **Developers**: create / revoke API keys, rotate the webhook secret, API example, events and outcomes

## Shopify and WooCommerce (automatic calls)

In the dashboard's **Integrations** page copy your webhook URL, then:

- **Shopify**: Settings → Notifications → Webhooks → add JSON webhooks for *Order creation*, *Order payment*,
  *Fulfillment creation* and *Fulfillment event creation*. Paste Shopify's signing secret into Dialyn.
- **WooCommerce**: WooCommerce → Settings → Advanced → Webhooks → add *Order created* and *Order updated*
  (API v3) with a secret; paste the same secret into Dialyn.

| Store event | Call |
|---|---|
| New COD order | `cod_verification` (on by default) |
| New prepaid order | `order_confirmation` |
| Payment received (non-COD) | `payment_success` |
| Payment failed (WooCommerce `failed`) | `payment_failed` (on by default) |
| Fulfillment created / Woo status `shipped` | `order_shipped` |
| Shopify fulfillment event `out_for_delivery` / Woo status `out-for-delivery` | `out_for_delivery` (on by default) |

Every webhook is verified with your secret; each order gets at most one call per event. Phone numbers
like `098765 43210` are normalized to `+919876543210`.

## Human-sounding voices

- **English**: 41 Deepgram voices (free credit); **Hinglish**: Sarvam's Indian voices (`priya`, `ritu`, `neha`, …).
- **ElevenLabs / Cartesia**: paste any voice ID, including a voice cloned from a real person (with their consent).
- **Recorded greeting**: upload a WAV of a real person saying the opening line for any event and language.
  Customers hear the human first; the AI continues the conversation.

## India telephony (Plivo / Exotel)

Pick the provider per store in **Settings → Phone provider** and set the credentials in `.env`:

- **Plivo**: `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_PHONE_NUMBER`. Nothing to configure in Plivo:
  Dialyn passes the answer and hangup URLs with every call.
- **Exotel**: `EXOTEL_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_CALLER_ID` (ExoPhone) and `EXOTEL_APP_ID`:
  a flow whose *Voicebot* applet URL is `https://<PUBLIC_HOST>/telephony/exotel/stream-url`.

Busy / unanswered calls are retried on every provider; transfers to a human work on Twilio and Plivo.

## API reference

Interactive docs: http://localhost:7860/docs

| Method & path | Purpose |
|---|---|
| `POST /v1/calls` | Schedule a call. Optional `schedule_at`, `metadata`, `Idempotency-Key` header |
| `GET /v1/calls?status=&event=&order_id=` | List calls |
| `GET /v1/calls/{id}` | Status, attempts, outcome, transcript, timeline |
| `GET /v1/calls/{id}/recording` | Stereo WAV (customer left, agent right) |
| `POST /v1/calls/{id}/cancel` | Cancel before it is dialed |
| `GET /v1/stats` | Counts by status / outcome / event |
| `GET /v1/events` | Supported events and outcomes |
| `GET/PATCH /v1/account` | Brand, agent name, calling hours, retries, concurrency, webhook, voices |

**Calling hours and retries** (per merchant): calls only go out between `call_window_start` and
`call_window_end` in the merchant's `timezone` (default 09:00–21:00 Asia/Kolkata). Busy or unanswered
calls are retried after `retry_delay_minutes`, up to `max_attempts`, then marked `unreachable`.

**Webhook**: when a call finishes, `POST <webhook_url>` receives
`{"type": "call.completed", "data": {…call…}}` with header `X-Signature: sha256=<HMAC-SHA256(body, webhook_secret)>`.
Verify the signature before trusting it.

**Voices**: each merchant can override the provider per language through `voice` in `PATCH /v1/account`,
e.g. `{"hi": {"tts": {"provider": "elevenlabs", "voice": "<voice id>"}}}` to use a cloned human voice.

## Real phone calls (Twilio)

1. Expose the server: `ngrok http 7860`, set `PUBLIC_HOST=<ngrok host>` and the `TWILIO_*` values in `.env`, then restart.
2. Create calls with `"channel": "phone"` (the default). The scheduler dials them through Twilio.
3. Inbound calls to your Twilio number: set its webhook to `POST https://<PUBLIC_HOST>/telephony/twilio/incoming`.

## Layout

```
agent/
  app/api_v1.py       merchant API (/v1/*) and admin (/admin/tenants)
  app/accounts.py     signup, login sessions, team invites, API keys
  app/integrations.py Shopify + WooCommerce webhooks → calls
  app/voices.py       voice catalog, previews, recorded greetings
  app/telephony_routes.py  Plivo + Exotel callbacks and media streams
  app/calls.py        scheduler: calling hours, dialing, retries, signed webhooks
  app/ecommerce.py    event templates → agent (prompt, greeting, outcomes, voices)
  app/bot.py          real-time pipeline, tools (record_outcome, end_call, transfer_call)
  app/main.py         Twilio webhooks + media stream, browser test page, WebRTC
  app/db.py           merchants and calls (SQLite dev / Postgres prod)
  app/static/         browser test-call page
  agents/*.yaml       stand-alone YAML agents (e.g. the `free` demo agent)
  tests/              pytest suite
```

## Tests

```bash
cd agent
uv run --group dev pytest
```

## Docker (with Postgres)

```bash
docker compose up --build
```
