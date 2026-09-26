"""Voices: catalog, spoken previews and recorded human greetings.

  GET    /v1/voices                         Voices per provider (+ which providers have keys)
  POST   /v1/voices/preview                 Speak a sample line -> audio (wav or mp3)
  PUT    /v1/clips/{language}/{event}       Upload a recorded greeting (WAV, 16-bit, <= 30 s)
  GET    /v1/clips/{language}/{event}/audio Download it
  DELETE /v1/clips/{language}/{event}       Remove it

A recorded clip replaces the synthetic greeting for that event and language: the
customer first hears a real person, then the AI continues the conversation. Keep the
clip generic (no customer name) since it is the same for every call.
"""

import base64
import io
import time
import wave
from pathlib import Path
from typing import Any, Literal

import httpx
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.accounts import Principal, current_tenant, require_role
from app.context import deps, settings
from app.db import Tenant
from app.ecommerce import TEMPLATES

router = APIRouter()
Language = Literal["en", "hi"]
MAX_CLIP_BYTES = 5 * 1024 * 1024
MAX_CLIP_SECS = 30

SARVAM_SPEAKERS = [
    "priya", "ritu", "neha", "pooja", "simran", "kavya", "ishita", "shreya", "roopa",
    "shubh", "aditya", "rahul", "rohan", "amit", "dev", "varun", "manan", "sumit", "kabir",
]  # fmt: skip
FALLBACK_DEEPGRAM = [
    ("aura-2-thalia-en", "feminine"), ("aura-2-helena-en", "feminine"),
    ("aura-2-andromeda-en", "feminine"), ("aura-2-apollo-en", "masculine"),
]  # fmt: skip
SAMPLE_TEXT = {
    "en": "Hi, this is Priya calling about your recent order. Do you have a minute?",
    "hi": "नमस्ते, मैं प्रिया बोल रही हूँ, आपके order के बारे में call किया है। क्या अभी एक मिनट बात कर सकते हैं?",
}
_deepgram_cache: tuple[float, list[dict]] = (0.0, [])
_http = httpx.AsyncClient(timeout=20)


# ----------------------------------------------------------------- catalog


async def _deepgram_voices() -> list[dict[str, Any]]:
    global _deepgram_cache
    cached_at, voices = _deepgram_cache
    if voices and time.time() - cached_at < 3600:
        return voices
    try:
        response = await _http.get(
            "https://api.deepgram.com/v1/models",
            headers={"Authorization": f"Token {settings.deepgram_api_key}"},
        )
        response.raise_for_status()
        voices = sorted(
            (
                {
                    "id": m["canonical_name"],
                    "label": m["canonical_name"].split("-")[2].title(),
                    "gender": ((m.get("metadata") or {}).get("tags") or [""])[0],
                    "accent": (m.get("metadata") or {}).get("accent", ""),
                    "languages": ["en"],
                }
                for m in response.json().get("tts", [])
                if m.get("canonical_name", "").startswith("aura-2")
                and any(lang.startswith("en") for lang in m.get("languages", []))
            ),
            key=lambda v: v["id"],
        )
        _deepgram_cache = (time.time(), voices)
    except (httpx.HTTPError, KeyError, ValueError):
        voices = [
            {
                "id": v,
                "label": v.split("-")[2].title(),
                "gender": g,
                "accent": "",
                "languages": ["en"],
            }
            for v, g in FALLBACK_DEEPGRAM
        ]
    return voices


@router.get("/v1/voices")
async def list_voices(tenant: Tenant = Depends(current_tenant)):
    providers = {
        "deepgram": {
            "configured": bool(settings.deepgram_api_key),
            "note": "Free signup credit. English only.",
            "voices": await _deepgram_voices() if settings.deepgram_api_key else [],
        },
        "sarvam": {
            "configured": bool(settings.sarvam_api_key),
            "note": "Indian voices for Hindi / Hinglish and Indian English.",
            "voices": [
                {"id": s, "label": s.title(), "languages": ["hi", "en"]} for s in SARVAM_SPEAKERS
            ],
        },
        "elevenlabs": {
            "configured": bool(settings.elevenlabs_api_key),
            "note": "Most human-sounding; paste any voice ID, including a cloned voice.",
            "voices": [],
        },
        "cartesia": {
            "configured": bool(settings.cartesia_api_key),
            "note": "Very low latency; paste any Cartesia voice ID.",
            "voices": [],
        },
    }
    return {"providers": providers, "current": tenant.voice or {}}


# ----------------------------------------------------------------- preview


class PreviewBody(BaseModel):
    provider: Literal["deepgram", "sarvam", "elevenlabs", "cartesia"]
    voice: str = Field(min_length=1, max_length=120)
    language: Language = "en"
    model: str | None = None
    text: str | None = Field(default=None, max_length=300)


async def synthesize(body: PreviewBody) -> tuple[bytes, str]:
    """Return (audio bytes, media type) for a short sample."""
    text = body.text or SAMPLE_TEXT[body.language]
    if body.provider == "deepgram":
        response = await _http.post(
            "https://api.deepgram.com/v1/speak",
            params={
                "model": body.voice,
                "encoding": "linear16",
                "container": "wav",
                "sample_rate": 24000,
            },
            headers={"Authorization": f"Token {settings.deepgram_api_key}"},
            json={"text": text},
        )
        response.raise_for_status()
        return response.content, "audio/wav"
    if body.provider == "sarvam":
        response = await _http.post(
            "https://api.sarvam.ai/text-to-speech",
            headers={"api-subscription-key": settings.sarvam_api_key},
            json={
                "text": text,
                "target_language_code": "hi-IN" if body.language == "hi" else "en-IN",
                "speaker": body.voice,
                "model": body.model or "bulbul:v3",
            },
        )
        response.raise_for_status()
        return base64.b64decode(response.json()["audios"][0]), "audio/wav"
    if body.provider == "elevenlabs":
        response = await _http.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{body.voice}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": settings.elevenlabs_api_key},
            json={"text": text, "model_id": body.model or "eleven_flash_v2_5"},
        )
        response.raise_for_status()
        return response.content, "audio/mpeg"
    response = await _http.post(
        "https://api.cartesia.ai/tts/bytes",
        headers={"X-API-Key": settings.cartesia_api_key, "Cartesia-Version": "2025-04-16"},
        json={
            "model_id": body.model or "sonic-3.6",
            "transcript": text,
            "voice": {"mode": "id", "id": body.voice},
            "language": body.language,
            "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 24000},
        },
    )
    response.raise_for_status()
    return response.content, "audio/wav"


@router.post("/v1/voices/preview")
async def preview_voice(body: PreviewBody, tenant: Tenant = Depends(current_tenant)):
    key = {
        "deepgram": settings.deepgram_api_key,
        "sarvam": settings.sarvam_api_key,
        "elevenlabs": settings.elevenlabs_api_key,
        "cartesia": settings.cartesia_api_key,
    }[body.provider]
    if not key:
        raise HTTPException(
            status_code=400, detail=f"{body.provider.upper()}_API_KEY is not set on the server"
        )
    try:
        audio, media_type = await synthesize(body)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                f"{body.provider} rejected the request ({e.response.status_code}); "
                "check the voice ID"
            ),
        ) from e
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"{body.provider} is unreachable") from e
    return Response(audio, media_type=media_type)


# ------------------------------------------------------------ recorded clips


def _clip_path(tenant_id: str, language: str, event: str) -> Path:
    return settings.recordings_dir.parent / "clips" / tenant_id / f"{language}_{event}.wav"


def normalize_clip(data: bytes) -> tuple[bytes, float]:
    """Validate a WAV upload and return it as mono 16-bit WAV plus its duration."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError) as e:
        raise HTTPException(status_code=422, detail="Upload a WAV file (16-bit PCM)") from e
    if width != 2:
        raise HTTPException(status_code=422, detail="The WAV must be 16-bit PCM")
    if not 8000 <= rate <= 48000:
        raise HTTPException(status_code=422, detail="Sample rate must be 8-48 kHz")
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    secs = len(samples) / rate
    if secs < 0.5 or secs > MAX_CLIP_SECS:
        raise HTTPException(status_code=422, detail=f"The clip must be 0.5-{MAX_CLIP_SECS} seconds")
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return out.getvalue(), round(secs, 1)


def _check_event(event: str) -> None:
    if event not in TEMPLATES:
        raise HTTPException(status_code=404, detail=f"Unknown event '{event}'")


@router.put("/v1/clips/{language}/{event}")
async def upload_clip(
    language: Language,
    event: str,
    file: UploadFile = File(...),
    text: str = Form(..., min_length=1, max_length=500),
    principal: Principal = Depends(require_role("owner", "admin")),
):
    _check_event(event)
    data = await file.read(MAX_CLIP_BYTES + 1)
    if len(data) > MAX_CLIP_BYTES:
        raise HTTPException(status_code=413, detail="The clip must be under 5 MB")
    wav, secs = normalize_clip(data)
    tenant = principal.tenant
    path = _clip_path(tenant.id, language, event)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav)
    clips = {
        **(tenant.clips or {}),
        f"{language}:{event}": {"path": str(path), "text": text, "secs": secs},
    }
    await deps.store.update_tenant(tenant.id, clips=clips)
    return {"language": language, "event": event, "text": text, "secs": secs}


@router.get("/v1/clips/{language}/{event}/audio")
async def download_clip(language: Language, event: str, tenant: Tenant = Depends(current_tenant)):
    clip = (tenant.clips or {}).get(f"{language}:{event}")
    if not clip or not Path(clip["path"]).is_file():
        raise HTTPException(status_code=404, detail="No clip")
    return FileResponse(clip["path"], media_type="audio/wav")


@router.delete("/v1/clips/{language}/{event}")
async def delete_clip(
    language: Language, event: str, principal: Principal = Depends(require_role("owner", "admin"))
):
    tenant = principal.tenant
    clips = dict(tenant.clips or {})
    clip = clips.pop(f"{language}:{event}", None)
    if clip is None:
        raise HTTPException(status_code=404, detail="No clip")
    Path(clip["path"]).unlink(missing_ok=True)
    await deps.store.update_tenant(tenant.id, clips=clips)
    return {"ok": True}
