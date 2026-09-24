import itertools
from xml.etree import ElementTree

import pytest

from app.agent_config import AgentConfig, AgentNotFound, list_agents, load_agent
from app.config import get_settings
from app.providers import build_llm, build_stt, build_tts
from app.telephony import sign_stream_token, stream_twiml, transfer_twiml, verify_stream_token

settings = get_settings()


def test_all_agent_files_are_valid():
    agents = list_agents(settings.agents_dir)
    assert {a.id for a in agents} >= {"default", "hindi_sales"}


def test_render_fills_variables_and_blanks_unknown():
    agent = AgentConfig(
        id="x",
        name="x",
        system_prompt="Hi {customer_name} from {company_name} {missing}",
        greeting="Hello {customer_name}",
        defaults={"company_name": "Acme"},
    )
    rendered = agent.render({"customer_name": "Riya"})
    assert rendered.system_prompt == "Hi Riya from Acme "
    assert rendered.greeting == "Hello Riya"
    assert agent.system_prompt.startswith("Hi {customer_name}")  # original untouched


@pytest.mark.parametrize("bad_id", ["../etc/passwd", "a/b", "", "default.yaml"])
def test_load_agent_rejects_path_tricks(bad_id):
    with pytest.raises(AgentNotFound):
        load_agent(settings.agents_dir, bad_id)


def test_stream_token_roundtrip_and_expiry():
    token = sign_stream_token("s", "call-1", now=1000)
    assert verify_stream_token("s", "call-1", token, now=1000)
    assert not verify_stream_token("s", "call-2", token, now=1000)
    assert not verify_stream_token("other", "call-1", token, now=1000)
    assert not verify_stream_token("s", "call-1", token, now=5000)
    assert not verify_stream_token("s", "call-1", "garbage", now=1000)


def test_twiml_is_well_formed_and_escaped():
    xml = stream_twiml("wss://h/ws", {"from_number": "+1<&>", "call_id": 'a"b'})
    root = ElementTree.fromstring(xml)
    params = {p.get("name"): p.get("value") for p in root.iter("Parameter")}
    assert root.find("./Connect/Stream").get("url") == "wss://h/ws"
    assert params == {"from_number": "+1<&>", "call_id": 'a"b'}
    assert ElementTree.fromstring(transfer_twiml("+1555", "Hold <on>")).find("Dial").text == "+1555"


@pytest.mark.parametrize(
    "stt,llm,tts",
    list(
        itertools.product(
            ["deepgram", "sarvam"],
            ["openai", "anthropic", "groq"],
            ["cartesia", "elevenlabs", "sarvam"],
        )
    ),
)
def test_every_provider_combination_builds(stt, llm, tts):
    agent = load_agent(settings.agents_dir, "default").model_copy(deep=True)
    agent.stt.provider, agent.llm.provider, agent.tts.provider = stt, llm, tts
    # Model/voice names are provider-specific; fall back to each provider's default.
    if stt != "deepgram":
        agent.stt.model = agent.stt.language = None
    if tts != "cartesia":
        agent.tts.voice = agent.tts.model = None
    if llm != "openai":
        agent.llm.model = {"anthropic": "claude-haiku-4-5", "groq": "llama-3.3-70b-versatile"}[llm]
    assert build_stt(agent, settings)
    assert build_llm(agent, settings)
    assert build_tts(agent, settings)
