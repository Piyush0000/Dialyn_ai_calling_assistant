import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DB_DIR = Path(tempfile.mkdtemp(prefix="dialyn-tests-"))

# Deterministic settings for tests; must be set before app.config is imported.
os.environ.update(
    {
        "API_KEY": "test-key",
        "DEFAULT_AGENT_ID": "default",
        "SCHEDULER_ENABLED": "false",
        "STREAM_SIGNING_SECRET": "test-secret",
        "PUBLIC_HOST": "voice.example.com",
        # A real file (not :memory:) so concurrent sessions see the same database.
        "DATABASE_URL": f"sqlite+aiosqlite:///{(_DB_DIR / 'test.db').as_posix()}",
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
