# Dialyn — AI Calling Assistant

![Python](https://img.shields.io/badge/python-3.12-blue)
![Pipecat](https://img.shields.io/badge/pipecat-1.11-purple)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![Status](https://img.shields.io/badge/status-phase%201-orange)

A self-hostable, real-time voice AI calling agent that answers and places phone calls.
It is inspired by [Dograh](https://www.dograh.com/) (an open-source alternative to Vapi and Retell)
and built on **Pipecat 1.11** + **FastAPI**.

See **[ROADMAP.md](ROADMAP.md)** for the full plan. This repo currently covers Phase 1: the real-time core.

## What works now

- Real-time cascade pipeline: Silero VAD → STT → LLM (with tools) → TTS, with barge-in (the caller can interrupt the agent)
- Providers can be switched per agent: **STT** Deepgram / Sarvam · **LLM** OpenAI / Claude / Groq · **TTS** Cartesia / ElevenLabs / Sarvam
- Twilio **inbound** calls and **outbound** calls (`POST /api/calls`)
- **Browser test calls** over WebRTC at `http://localhost:7860/client/`, so you can test without a phone number
- Tools: `end_call` and `transfer_call` (hands the caller to a human number)
- Every call is stored with its transcript, end reason, duration and a stereo recording (caller on the left channel, agent on the right)
- Security: Twilio signature validation, signed short-lived media-stream tokens, API-key-protected REST API

## Layout

```
agent/
  agents/*.yaml       agent definitions (prompt, greeting, voice, providers, tools)
  app/main.py         HTTP + WebSocket routes (Twilio webhooks, media stream, REST, WebRTC)
  app/bot.py          per-call Pipecat pipeline, tools, transcript, recording
  app/providers.py    STT / LLM / TTS factory
  app/telephony.py    TwiML, signature checks, stream tokens, dial / transfer / hang up
  app/db.py           call records (SQLite dev / Postgres prod)
  tests/              pytest suite
```

## Quick start (browser test, no phone number needed)

```bash
cd agent
uv sync
cp .env.example .env      # add at least DEEPGRAM_API_KEY, OPENAI_API_KEY, CARTESIA_API_KEY, API_KEY
uv run uvicorn app.main:app --port 7860
```

Open http://localhost:7860/client/ and click **Connect**. Then view the transcript:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:7860/api/calls
```

## Real phone calls (Twilio)

1. Expose the server publicly: `ngrok http 7860`. Set `PUBLIC_HOST=<your-ngrok-host>` in `.env` and restart the server.
2. In the Twilio console, go to your number → *A call comes in* → Webhook `POST https://<PUBLIC_HOST>/telephony/twilio/incoming`
   (add `?agent=hindi_sales` to route this number to a different agent).
3. Call your number.

Outbound call:

```bash
curl -X POST https://<PUBLIC_HOST>/api/calls \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"to": "+919876543210", "agent_id": "hindi_sales", "variables": {"customer_name": "Riya"}}'
```

## Adding an agent

Copy `agent/agents/default.yaml` to `agent/agents/<id>.yaml` and edit it. Placeholders such as `{customer_name}`
are filled from the outbound call's `variables`, falling back to the file's `defaults`.

## Tests

```bash
cd agent
uv run --group dev pytest
```

## Docker (with Postgres)

```bash
docker compose up --build
```
