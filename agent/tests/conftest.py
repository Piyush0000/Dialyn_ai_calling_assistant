import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Deterministic settings for tests; must be set before app.config is imported.
os.environ.update(
    {
        "API_KEY": "test-key",
        "DEFAULT_AGENT_ID": "default",
        "STREAM_SIGNING_SECRET": "test-secret",
        "PUBLIC_HOST": "voice.example.com",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "VALIDATE_TWILIO_SIGNATURE": "false",
        "TWILIO_PHONE_NUMBER": "+15550001111",
        "OPENAI_API_KEY": "sk-test",
        "ANTHROPIC_API_KEY": "test",
        "GROQ_API_KEY": "test",
        "DEEPGRAM_API_KEY": "test",
        "SARVAM_API_KEY": "test",
        "CARTESIA_API_KEY": "test",
        "ELEVENLABS_API_KEY": "test",
    }
)
