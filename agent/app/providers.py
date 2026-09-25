"""Factory for STT / LLM / TTS services so every agent can pick its own providers."""

from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

from app.agent_config import AgentConfig
from app.config import Settings


class ProviderConfigError(RuntimeError):
    pass


def _require(value: str, name: str) -> str:
    if not value:
        raise ProviderConfigError(f"{name} is not set")
    return value


_KEYS = {
    ("stt", "deepgram"): "deepgram_api_key",
    ("stt", "sarvam"): "sarvam_api_key",
    ("llm", "openai"): "openai_api_key",
    ("llm", "anthropic"): "anthropic_api_key",
    ("llm", "groq"): "groq_api_key",
    ("tts", "cartesia"): "cartesia_api_key",
    ("tts", "elevenlabs"): "elevenlabs_api_key",
    ("tts", "sarvam"): "sarvam_api_key",
    ("tts", "deepgram"): "deepgram_api_key",
}


def missing_keys(agent: AgentConfig, settings: Settings) -> list[str]:
    """Env var names the agent's providers need but that are not configured."""
    missing: list[str] = []
    for stage in ("stt", "llm", "tts"):
        attr = _KEYS.get((stage, getattr(agent, stage).provider))
        if attr and not getattr(settings, attr) and attr.upper() not in missing:
            missing.append(attr.upper())
    return missing


# Pipecat drops a TTS reply if no audio arrives within 3 s. Cloud TTS occasionally
# stalls for longer, so give it room: a late greeting beats a silent one.
CLOUD_TTS_OPTIONS = {"stop_frame_timeout_s": 6.0}


def _given(**values):
    """Drop unset options so the provider's own defaults apply."""
    return {k: v for k, v in values.items() if v is not None}


def build_stt(agent: AgentConfig, settings: Settings) -> STTService:
    cfg = agent.stt
    if cfg.provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        return DeepgramSTTService(
            api_key=_require(settings.deepgram_api_key, "DEEPGRAM_API_KEY"),
            settings=DeepgramSTTService.Settings(
                **_given(model=cfg.model, language=cfg.language or agent.language),
                smart_format=True,
            ),
        )
    if cfg.provider == "sarvam":
        from pipecat.services.sarvam.stt import SarvamSTTService

        return SarvamSTTService(
            api_key=_require(settings.sarvam_api_key, "SARVAM_API_KEY"),
            settings=SarvamSTTService.Settings(**_given(model=cfg.model, language=cfg.language)),
        )
    raise ProviderConfigError(f"Unknown STT provider {cfg.provider}")


def build_llm(agent: AgentConfig, settings: Settings) -> LLMService:
    cfg = agent.llm
    if cfg.provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService as Service

        key = _require(settings.openai_api_key, "OPENAI_API_KEY")
    elif cfg.provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService as Service

        key = _require(settings.anthropic_api_key, "ANTHROPIC_API_KEY")
    elif cfg.provider == "groq":
        from pipecat.services.groq.llm import GroqLLMService as Service

        key = _require(settings.groq_api_key, "GROQ_API_KEY")
    else:
        raise ProviderConfigError(f"Unknown LLM provider {cfg.provider}")

    return Service(
        api_key=key,
        settings=Service.Settings(
            **_given(
                model=cfg.model,
                system_instruction=agent.system_prompt,
                temperature=cfg.temperature,
            )
        ),
    )


def build_tts(agent: AgentConfig, settings: Settings) -> TTSService:
    cfg = agent.tts
    if cfg.provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        return CartesiaTTSService(
            **CLOUD_TTS_OPTIONS,
            api_key=_require(settings.cartesia_api_key, "CARTESIA_API_KEY"),
            settings=CartesiaTTSService.Settings(
                **_given(voice=cfg.voice, model=cfg.model, language=cfg.language)
            ),
        )
    if cfg.provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        return ElevenLabsTTSService(
            **CLOUD_TTS_OPTIONS,
            api_key=_require(settings.elevenlabs_api_key, "ELEVENLABS_API_KEY"),
            settings=ElevenLabsTTSService.Settings(
                **_given(voice=cfg.voice, model=cfg.model, language=cfg.language)
            ),
        )
    if cfg.provider == "sarvam":
        from pipecat.services.sarvam.tts import SarvamTTSService

        return SarvamTTSService(
            **CLOUD_TTS_OPTIONS,
            api_key=_require(settings.sarvam_api_key, "SARVAM_API_KEY"),
            settings=SarvamTTSService.Settings(
                **_given(voice=cfg.voice, model=cfg.model, language=cfg.language)
            ),
        )
    if cfg.provider == "deepgram":
        # Aura voices; shares the Deepgram STT account/credit.
        from pipecat.services.deepgram.tts import DeepgramTTSService

        return DeepgramTTSService(
            **CLOUD_TTS_OPTIONS,
            api_key=_require(settings.deepgram_api_key, "DEEPGRAM_API_KEY"),
            settings=DeepgramTTSService.Settings(**_given(voice=cfg.voice)),
        )
    if cfg.provider == "kokoro":
        # Runs locally on CPU: free, no API key. Downloads ~340 MB of model files on first use.
        from pipecat.services.kokoro.tts import KokoroTTSService

        return KokoroTTSService(settings=KokoroTTSService.Settings(**_given(voice=cfg.voice)))
    raise ProviderConfigError(f"Unknown TTS provider {cfg.provider}")
