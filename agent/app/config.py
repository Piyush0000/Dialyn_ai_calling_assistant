"""Application settings loaded from environment / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    public_host: str = "localhost:7860"
    port: int = 7860
    api_key: str = ""
    stream_signing_secret: str = "dev-secret"
    database_url: str = f"sqlite+aiosqlite:///{(BASE_DIR / 'data' / 'calls.db').as_posix()}"
    default_agent_id: str = "default"
    agents_dir: Path = BASE_DIR / "agents"
    recordings_dir: Path = BASE_DIR / "data" / "recordings"
    validate_twilio_signature: bool = True
    # Background dialer for merchant calls; tests turn it off and call tick() directly.
    scheduler_enabled: bool = True

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    deepgram_api_key: str = ""
    sarvam_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    groq_api_key: str = ""
    cartesia_api_key: str = ""
    elevenlabs_api_key: str = ""

    @property
    def public_base_url(self) -> str:
        return f"https://{self.public_host}"

    @property
    def twilio_stream_url(self) -> str:
        return f"wss://{self.public_host}/telephony/twilio/stream"


@lru_cache
def get_settings() -> Settings:
    return Settings()
