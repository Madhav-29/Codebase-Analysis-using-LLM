"""Azure AI Foundry provider and OpenAI-family behavior. Skipped unless the azure extra is installed."""

import httpx
import pytest

pytest.importorskip("langchain_azure_ai", reason="needs the azure extra: uv sync --extra azure")

import openai
from langchain_core.messages import HumanMessage
from langchain_openai.chat_models.base import (
    OpenAIAuthenticationError,
    OpenAIModelNotFoundError,
    OpenAIRateLimitError,
)

from codebase_analyzer.config import Settings
from codebase_analyzer.errors import ConfigError
from codebase_analyzer.llm.client import LLMRequest, classify, retry_hint_seconds
from codebase_analyzer.llm.providers import make_provider
from codebase_analyzer.schema.responses import DomainResponse

from ..fakes import FakeStructuredModel
from .test_llm_client import DOMAIN_OK, _runner

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


def _http_error(cls, status, headers=None, body=None):
    request = httpx.Request("POST", "https://example.services.ai.azure.com/openai/v1/chat/completions")
    return cls("error", response=httpx.Response(status, headers=headers or {}, request=request), body=body)


def test_azure_errors_are_classified_and_retry_after_headers_honored():
    limited = _http_error(OpenAIRateLimitError, 429, {"retry-after-ms": "1500"})
    assert classify(limited) == "transient" and retry_hint_seconds(limited) == 1.5
    assert retry_hint_seconds(_http_error(OpenAIRateLimitError, 429, {"retry-after": "7"})) == 7.0
    assert classify(_http_error(OpenAIAuthenticationError, 401)) == "fatal"
    assert classify(_http_error(OpenAIModelNotFoundError, 404)) == "fatal"  # unknown deployment name
    filtered = _http_error(openai.BadRequestError, 400, body={"code": "content_filter", "message": "filtered"})
    assert classify(filtered) == "filtered"


def test_content_filter_fails_the_unit_at_once_and_the_run_continues():
    filtered = _http_error(openai.BadRequestError, 400, body={"code": "content_filter", "message": "filtered"})
    fake = FakeStructuredModel(lambda s, u, n: filtered if "user 1" in u else DOMAIN_OK)
    reqs = [LLMRequest(f"r{i}", "sys", f"user {i}", DomainResponse) for i in (1, 2)]
    results = _runner(fake).run(reqs, "t")
    assert results["r1"].parsed is None and results["r1"].error.startswith("content filter")
    assert results["r1"].attempts == 1 and sum("user 1" in u for _, u in fake.calls) == 1  # no retries
    assert results["r2"].parsed is not None
