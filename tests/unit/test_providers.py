import pytest
from langchain_core.messages import HumanMessage

from codebase_analyzer.config import Settings
from codebase_analyzer.errors import ConfigError
from codebase_analyzer.llm.providers import make_provider
from codebase_analyzer.llm.providers.google_genai import applied_temperature


def test_gemini_chat_model_is_configured_from_settings(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-not-a-real-key")  # constructing the client makes no request
    p = make_provider(Settings(llm_provider="google_genai", llm_model="gemini-3.8-flash"))
    llm = p.chat_model
    assert type(llm).__name__ == "ChatGoogleGenerativeAI"
    assert llm.model == "gemini-3.8-flash" and llm.temperature == 0.0 and p.temperature == 0.0
    assert llm.max_output_tokens == Settings().budget.output_reserve and llm.max_retries == 1
    assert p.concurrency == 1 and p.structured_output == {"method": "json_schema"}


def test_temperature_is_not_sent_to_fixed_sampling_models(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-not-a-real-key")
    lite = Settings(llm_provider="google_genai", llm_model="gemini-3.5-flash-lite")
    p = make_provider(lite)
    assert applied_temperature(lite) is None and p.temperature is None and p.chat_model.temperature is None
    assert "fixed sampling" in p.temperature_note
    assert applied_temperature(Settings(llm_provider="google_genai", llm_model="gemini-3.8-flash")) == 0.0


def test_missing_credentials_fail_fast_with_an_actionable_message():
    with pytest.raises(ConfigError, match="GOOGLE_API_KEY is not set"):
        make_provider(Settings(llm_provider="google_genai"))
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is not set"):
        make_provider(Settings(llm_provider="anthropic"))


def test_anthropic_provider_is_configured_from_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
    p = make_provider(Settings(llm_provider="anthropic", llm_model="claude-sonnet-5-5", llm_concurrency=3))
    assert type(p.chat_model).__name__ == "ChatAnthropic" and p.chat_model.max_retries == 0
    assert p.temperature == 0.0 and p.concurrency == 3


AZURE = {
    "llm_provider": "azure_foundry",
    "llm_model": "gpt-5.4-mini",
    "azure_ai_endpoint": "https://example.services.ai.azure.com/openai/v1",
}


def test_azure_request_sends_reasoning_effort_and_output_cap_but_no_sampling(monkeypatch):
    monkeypatch.setenv("AZURE_AI_API_KEY", "dummy-not-a-real-key")  # constructing the client makes no request
    p = make_provider(Settings(**AZURE))
    payload = p.chat_model._get_request_payload([HumanMessage("hi")])
    assert payload["model"] == "gpt-5.4-mini" and payload["reasoning_effort"] == "low"
    assert payload["max_completion_tokens"] == Settings().budget.output_reserve  # includes reasoning tokens
    assert payload["store"] is False and "temperature" not in payload and "top_p" not in payload
    assert p.temperature is None and "reasoning" in p.temperature_note and p.reasoning_effort == "low"
    assert p.structured_output == {"method": "json_schema", "strict": True}
    assert p.concurrency == 4 and p.chat_model.max_retries == 0
    assert p.chat_model.tiktoken_model_name == "gpt-4o"  # o200k_base; deployment names are unknown to tiktoken


def test_azure_without_a_key_uses_entra_id():
    from azure.identity import DefaultAzureCredential

    assert isinstance(make_provider(Settings(**AZURE)).chat_model.credential, DefaultAzureCredential)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [(None, "AZURE_AI_ENDPOINT is not set"), ("https://example.openai.azure.com/", "ending in /openai/v1")],
)
def test_azure_endpoint_is_validated(endpoint, message):
    with pytest.raises(ConfigError, match=message):
        make_provider(Settings(**{**AZURE, "azure_ai_endpoint": endpoint}))


def _schema_nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _schema_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _schema_nodes(value)


@pytest.mark.parametrize("schema_name", ["ClassUnitResponse", "DomainResponse", "ProjectResponse"])
def test_response_schemas_satisfy_openai_strict_mode(schema_name):
    # LangChain hands the Pydantic class to the OpenAI SDK, which builds the strict schema it sends.
    from openai.lib._parsing._completions import type_to_response_format_param

    from codebase_analyzer.schema import responses

    spec = type_to_response_format_param(getattr(responses, schema_name))["json_schema"]
    objects = [n for n in _schema_nodes(spec["schema"]) if "properties" in n]
    assert spec["strict"] is True and objects
    for node in objects:  # strict mode: every field required, no undeclared fields
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])
