"""Agent definitions: YAML files describing prompt, voice and provider choices."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class STTConfig(BaseModel):
    provider: Literal["deepgram", "sarvam"] = "deepgram"
    model: str | None = None
    language: str | None = None


class LLMConfig(BaseModel):
    provider: Literal["openai", "anthropic", "groq"] = "openai"
    model: str = "gpt-4.1-mini"
    temperature: float | None = None


class TTSConfig(BaseModel):
    provider: Literal["cartesia", "elevenlabs", "sarvam"] = "cartesia"
    voice: str | None = None
    model: str | None = None
    language: str | None = None


class AgentConfig(BaseModel):
    id: str
    name: str
    language: str = "en"
    system_prompt: str
    greeting: str | None = None
    defaults: dict[str, str] = Field(default_factory=dict)
    stt: STTConfig = Field(default_factory=STTConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    tools: list[Literal["end_call", "transfer_call"]] = Field(default_factory=list)
    transfer_number: str = ""
    max_call_duration_secs: int = 600
    record_audio: bool = True

    def render(self, variables: dict[str, str] | None = None) -> "AgentConfig":
        """Return a copy with {placeholders} in prompt and greeting filled in."""
        values = _Blank({**self.defaults, **(variables or {})})
        return self.model_copy(
            update={
                "system_prompt": self.system_prompt.format_map(values),
                "greeting": self.greeting.format_map(values) if self.greeting else None,
            }
        )


class _Blank(dict):
    """format_map helper: unknown placeholders render as empty strings."""

    def __missing__(self, key: str) -> str:
        return ""


class AgentNotFound(LookupError):
    pass


def load_agent(agents_dir: Path, agent_id: str) -> AgentConfig:
    # Agent ids come from URLs/requests; never let them escape the agents dir.
    if not agent_id.replace("_", "").replace("-", "").isalnum():
        raise AgentNotFound(agent_id)
    path = agents_dir / f"{agent_id}.yaml"
    if not path.is_file():
        raise AgentNotFound(agent_id)
    with path.open(encoding="utf-8") as f:
        return AgentConfig.model_validate(yaml.safe_load(f))


def list_agents(agents_dir: Path) -> list[AgentConfig]:
    return [load_agent(agents_dir, p.stem) for p in sorted(agents_dir.glob("*.yaml"))]
