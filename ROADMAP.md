# AI Calling Assistant — Product & Engineering Roadmap

Goal: a production-grade, self-hostable **real-time voice AI calling platform** (in the class of
Dograh / Vapi / Retell) that can answer and place phone calls, hold natural low-latency
conversations, take actions via tools, and report on every call.

---

## 1. What "industry-grade" means here (targets)

| Area | Target |
|---|---|
| Voice-to-voice latency (user stops → bot audio starts) | p50 < 800 ms, p95 < 1.3 s |
| Barge-in (user interrupts bot) | bot audio stops < 300 ms |
| Concurrency | 50 calls/node, horizontally scalable workers |
| Availability | 99.9 % for the call-handling plane |
| Call success (no dropped/silent calls) | > 99.5 % |
| Compliance | Recording consent, DND/TCPA/TRAI calling windows, PII redaction, data residency, HIPAA/GDPR-ready self-host |
| Observability | Every call has transcript, recording, per-turn latency (STT/LLM/TTS), cost, outcome |

---

## 2. Reference architecture

```
                ┌──────────────── Control plane ────────────────┐
 Dashboard ───► │ REST API (agents, numbers, campaigns, calls)   │──► Postgres
 (Next.js)      │ Auth / RBAC / API keys / webhooks out          │──► Redis (queues, rate limits)
                └───────────────┬───────────────────────────────┘──► S3 (recordings)
                                │ dispatch
 PSTN ─► Twilio / Plivo /       ▼
 Exotel / Telnyx / SIP ──WS──► Voice workers (Pipecat pipelines, 1 per call)
 Browser ──────── WebRTC ────►   VAD → STT → turn detection → LLM(+tools) → TTS
                                 │            │                │
                                 ▼            ▼                ▼
                         Deepgram/Sarvam  OpenAI/Claude/   Cartesia/ElevenLabs/
                         /Whisper (self)  Groq/Gemini      Sarvam/Kokoro (self)
                                 └────── OpenTelemetry / Langfuse / metrics ──────┘
```

Key design decisions
- **Pipecat** as the real-time media/pipeline engine (same foundation as Dograh; BSD-2).
- **Cascade (STT→LLM→TTS) by default**, speech-to-speech (OpenAI Realtime / Gemini Live) as an
  option per agent. Cascade gives control, tool reliability and cheaper cost; S2S gives the
  lowest latency.
- **Provider-agnostic**: every box is swappable per agent via config (see `agent/agents/*.yaml`).
- **Stateless voice workers**: all call state persisted in the control plane so workers scale
  horizontally and can be drained for deploys.

---

## 3. Phased roadmap

### Phase 1 — Real-time core (Weeks 1–3) ✅ *started in this repo*
- [x] FastAPI voice server with Pipecat 1.11 cascade pipeline (Silero VAD, Deepgram STT,
      OpenAI LLM, Cartesia TTS; Sarvam / ElevenLabs / Claude / Groq switchable)
- [x] Twilio **inbound** (TwiML → bidirectional Media Stream over WebSocket)
- [x] Twilio **outbound** API (`POST /api/calls`)
- [x] Browser test calls over WebRTC (no phone number needed)
- [x] Config-driven agents (YAML): prompt, greeting, voice, language, tools, templating vars
- [x] Built-in tools: `end_call`, `transfer_call` (warm handoff to human)
- [x] Call records + transcripts in DB (SQLite dev / Postgres prod)
- [x] Twilio webhook signature validation, signed stream tokens, API key auth
- [ ] Measure baseline latency on a real phone call; tune VAD/endpointing
- **Exit criteria:** a real phone call to your Twilio number is answered by the agent with
  p50 latency < 1 s, and the transcript shows up in `GET /api/calls/{id}`.

### Phase 2 — Conversation quality (Weeks 3–5)
- [ ] Smart turn detection (Pipecat smart-turn model / Deepgram Flux) instead of silence-only
- [ ] Backchannel & filler handling ("hmm", "one sec…") while tools run
- [ ] Noise suppression (Krisp / RNNoise), answering-machine detection for outbound
- [ ] Idle handling ("are you still there?"), max-duration wrap-up, DTMF input
- [ ] Hinglish / multilingual: Sarvam STT+TTS, per-call language detection & switch
- [ ] Pronunciation dictionaries (brand names, numbers, currency, dates)
- [ ] Hybrid voice: pre-recorded human clips for fixed lines (greeting/disclosure) + TTS
- [ ] Prompt caching + response streaming tuned for first-token latency

### Phase 3 — Actions & knowledge (Weeks 5–7)
- [ ] Custom HTTP tools per agent (webhook tools with JSON schema, auth, timeouts)
- [ ] MCP tool servers
- [ ] Knowledge base / RAG (pgvector): upload PDFs/FAQs, retrieve per turn
- [ ] Calendar booking (Google/Cal.com), CRM sync (HubSpot/Salesforce/Zoho), WhatsApp/SMS follow-up
- [ ] Post-call analysis: summary, structured extraction (JSON schema), sentiment, outcome tag
- [ ] Outbound webhooks: `call.started`, `call.ended`, `call.analyzed`

### Phase 4 — Workflow builder & dashboard (Weeks 7–10)
- [ ] Next.js dashboard: agents, phone numbers, calls list, transcript + recording player
- [ ] Visual workflow builder (node graph → Pipecat Flows): greet → qualify → branch → book → end
- [ ] In-browser "test call" and prompt playground
- [ ] Multi-tenant orgs, RBAC, API keys, usage & cost per call

### Phase 5 — Campaigns & scale (Weeks 10–13)
- [ ] Outbound campaigns: CSV upload, per-contact variables, retries, calling windows, concurrency caps
- [ ] Job queue (Redis/Arq or Celery) dispatching calls to a worker pool
- [ ] DND / consent registry checks, caller-ID rotation, recording disclosure
- [ ] Kubernetes deploy: HPA on active-call count, graceful drain, multi-region
- [ ] Load test: 200 concurrent synthetic calls (Pipecat evals + SIP load generator)

### Phase 6 — Enterprise & self-hosting (Weeks 13+)
- [ ] Direct SIP trunking (Asterisk/FreeSWITCH/LiveKit SIP) to cut telephony cost
- [ ] Fully on-prem models: faster-whisper STT, vLLM (Llama/Qwen), Kokoro/Piper TTS on GPU
- [ ] SSO (SAML/OIDC), audit logs, data retention policies, PII redaction in transcripts
- [ ] Evals & regression suite per agent (scripted conversations with pass/fail assertions)
- [ ] SOC 2 / ISO 27001 readiness

---

## 4. Suggested stack

| Layer | Choice | Alternatives |
|---|---|---|
| Media pipeline | Pipecat 1.11 (Python) | LiveKit Agents |
| API | FastAPI + SQLAlchemy async | — |
| DB / cache | Postgres (+pgvector), Redis | — |
| Telephony | Twilio (start), Plivo/Exotel (India cost), Telnyx | SIP trunk + Asterisk |
| STT | Deepgram Nova-3 / Flux | Sarvam (Indic), faster-whisper (self-host) |
| LLM | GPT-4.1-mini / Claude Haiku / Groq Llama | Gemini Flash, vLLM self-host |
| TTS | Cartesia Sonic | ElevenLabs Flash, Sarvam Bulbul (Indic), Kokoro (self-host) |
| Frontend | Next.js + Tailwind + shadcn/ui | — |
| Observability | OpenTelemetry + Langfuse, Prometheus/Grafana | — |
| Deploy | Docker → Kubernetes | Fly.io / Render for early stage |

---

## 5. Cost model (per connected minute, rough, cascade)

| Item | ~USD/min |
|---|---|
| Telephony (Twilio US / Plivo IN) | 0.008 – 0.014 |
| STT (Deepgram streaming) | ~0.0077 |
| LLM (mini-class model) | ~0.002 – 0.01 |
| TTS (Cartesia / ElevenLabs) | 0.02 – 0.06 |
| **Total** | **≈ 0.04 – 0.09** |

Self-hosting STT/TTS/LLM on GPUs (Phase 6) brings this toward ~0.01–0.02 at volume.

---

## 6. Risks & mitigations
- **Latency spikes from providers** → region-pinned providers, streaming everywhere, fallback providers.
- **Hallucinated actions** → tools with strict schemas, confirmation turns before irreversible actions.
- **Regulatory (TCPA / TRAI DND)** → consent capture, calling windows, DND scrubbing before dial.
- **Vendor lock-in** → provider-agnostic agent config from day 1 (already in Phase 1).
