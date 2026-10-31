"""Provider switch is configuration only: the factory maps settings to the right LangChain model without network calls."""

import pytest
from pydantic import SecretStr

from app.agent.llm import FakeChatModel, build_chat_model
from app.config import get_settings


def _settings(**kw):
    return get_settings().model_copy(update=kw)


def test_fake_provider():
    assert isinstance(build_chat_model(_settings(llm_provider="fake")), FakeChatModel)


def test_anthropic_needs_a_key_and_maps_parameters():
    with pytest.raises(RuntimeError, match="API key"):
        build_chat_model(_settings(llm_provider="anthropic", anthropic_api_key=None))
    m = build_chat_model(_settings(llm_provider="anthropic", anthropic_api_key=SecretStr("sk-ant-test"), llm_model="claude-haiku-4-5-20251001",
                                   llm_max_tokens=500, llm_temperature=0.0))
    assert type(m).__name__ == "ChatAnthropic" and m.model == "claude-haiku-4-5-20251001" and m.max_tokens == 500


def test_summary_model_can_differ():
    s = _settings(llm_provider="anthropic", anthropic_api_key=SecretStr("sk-ant-test"), llm_model="claude-sonnet-5-5", llm_summary_model="claude-haiku-4-5-20251001")
    assert build_chat_model(s).model == "claude-sonnet-5-5" and build_chat_model(s, summary=True).model == "claude-haiku-4-5-20251001"


def test_openai_needs_its_own_key():
    with pytest.raises(RuntimeError, match="API key"):
        build_chat_model(_settings(llm_provider="openai", openai_api_key=None, llm_model="gpt-4.1-mini"))


def test_bedrock_uses_the_iam_role_not_an_api_key(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")  # boto3 only needs *some* credentials to construct the client
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    m = build_chat_model(_settings(llm_provider="bedrock", anthropic_api_key=None, aws_region="eu-central-1",
                                   llm_model="eu.anthropic.claude-haiku-4-5-20251001-v1:0"))
    assert type(m).__name__ == "ChatBedrockConverse"
    assert m.model_id == "eu.anthropic.claude-haiku-4-5-20251001-v1:0" and m.region_name == "eu-central-1"
