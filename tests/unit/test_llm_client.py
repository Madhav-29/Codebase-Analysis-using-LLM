import hashlib
import json

import httpx
import openai
import pytest
from langchain_core.exceptions import ModelAuthenticationError, ModelRateLimitError
from langchain_openai.chat_models.base import (
    OpenAIAuthenticationError,
    OpenAIModelNotFoundError,
    OpenAIRateLimitError,
)
from pydantic import ValidationError

from codebase_analyzer.analysis.facts import build_facts
from codebase_analyzer.config import Settings
from codebase_analyzer.errors import ProviderAuthError, ProviderQuotaExhausted
from codebase_analyzer.llm.cache import ResponseCache, cache_key
from codebase_analyzer.llm.client import LLMRequest, StructuredRunner, classify, retry_hint_seconds
from codebase_analyzer.pipeline.map_stage import _unit_validator
from codebase_analyzer.planning.chunking import plan_units
from codebase_analyzer.schema.responses import ClassUnitResponse, DomainResponse

from ..fakes import FakeStructuredModel, class_payload, fake_provider

VALID = {
    "classes": [
        {
            "type_key": "A",
            "summary": "s",
            "responsibilities": [],
            "findings": [],
            "methods": [{"member_key": "m1", "description": "d"}],
        }
    ]
}


def test_schema_accepts_valid_and_rejects_invalid_responses():
    assert ClassUnitResponse.model_validate(VALID).classes[0].methods[0].member_key == "m1"
    bad_enum = {
        "classes": [
            {
                **VALID["classes"][0],
                "findings": [
                    {
                        "category": "vibes",
                        "severity": "info",
                        "title": "t",
                        "detail": "d",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValidationError):
        ClassUnitResponse.model_validate(bad_enum)
    with pytest.raises(ValidationError):
        ClassUnitResponse.model_validate({"classes": [{"type_key": "A"}]})  # required fields missing


def _unit(make_repo, kind_files):
    s = Settings()
    facts = build_facts(make_repo(kind_files), s)
    return plan_units(facts, s, sorted(f.path for f in facts.files)).units


SRC = "package p;\npublic class A {\n  int f(int x) { if (x > 1) return x; return 0; }\n  class B { int g() { return 1 + 2; } }\n}\n"


def test_unit_validator_requires_exact_type_and_member_keys(make_repo):
    [unit] = _unit(make_repo, {"p/A.java": SRC})
    validate = _unit_validator(unit, require_all_types=True)
    user = unit.user_text
    validate(ClassUnitResponse.model_validate(class_payload(user)))  # complete: ok
    with pytest.raises(ValueError, match="missing member keys"):
        validate(ClassUnitResponse.model_validate(class_payload(user, drop_members={"m1"})))
    with pytest.raises(ValueError, match="missing type keys"):
        validate(ClassUnitResponse.model_validate(class_payload(user, drop_types={"A.B"})))
    extra = class_payload(user)
    extra["classes"][0]["type_key"] = "Nope"
    with pytest.raises(ValueError, match="unknown type keys"):
        validate(ClassUnitResponse.model_validate(extra))
    # packs tolerate a missing class (it is retried individually) but not missing members of returned classes
    _unit_validator(unit, require_all_types=False)(
        ClassUnitResponse.model_validate(class_payload(user, drop_types={"A.B"}))
    )


def _req(validator=None):
    return LLMRequest("r1", "sys", "- A (class, lines 1-2)\n", DomainResponse, validator)


DOMAIN_OK = {"summary": "s", "key_responsibilities": [], "findings": []}


def test_runner_retries_validation_failures_with_feedback_then_succeeds():
    def responder(schema, user, n):
        return {"summary": "s"} if n == 1 else DOMAIN_OK  # first answer is schema-invalid

    fake = FakeStructuredModel(responder)
    runner = StructuredRunner(fake_provider(fake), Settings(), None, "v")
    res = runner.run([_req()], stage="t")["r1"]
    assert res.parsed is not None and res.attempts == 2
    assert "rejected by validation" in fake.calls[1][1]
    assert runner.usage.llm_calls == 2 and runner.usage.input_tokens == 200


def _runner(model, settings=None, cache=None):
    runner = StructuredRunner(fake_provider(model), settings or Settings(), cache, "v")
    runner.sleeps = []
    runner.sleep = runner.sleeps.append  # record backoff waits instead of sleeping
    return runner


def test_runner_retries_transient_errors_and_reports_persistent_failure():
    flaky = FakeStructuredModel(lambda s, u, n: ConnectionError("boom") if n == 1 else DOMAIN_OK)
    runner = _runner(flaky)
    res = runner.run([_req()], "t")["r1"]
    assert res.parsed is not None and res.attempts == 1  # a transient error is not a validation attempt
    assert runner.sleeps == [Settings().llm_backoff_initial_s]

    always_bad = FakeStructuredModel(lambda s, u, n: {"summary": "missing fields"})
    settings = Settings()
    res = StructuredRunner(fake_provider(always_bad), settings, None, "v").run([_req()], "t")["r1"]
    assert res.parsed is None and res.error and res.attempts == settings.llm_max_attempts
    assert len(always_bad.calls) == settings.llm_max_attempts  # no fabricated output, no extra calls


def test_local_validator_failure_triggers_retry():
    def must_mention_x(resp):
        if "x" not in resp.summary:
            raise ValueError("summary must mention x")

    fake = FakeStructuredModel(lambda s, u, n: DOMAIN_OK if n == 1 else {**DOMAIN_OK, "summary": "x"})
    res = StructuredRunner(fake_provider(fake), Settings(), None, "v").run([_req(must_mention_x)], "t")["r1"]
    assert res.parsed.summary == "x" and "summary must mention x" in fake.calls[1][1]


def test_cache_replays_without_calling_the_model(tmp_path):
    cache = ResponseCache(tmp_path)
    first = FakeStructuredModel(lambda s, u, n: DOMAIN_OK)
    StructuredRunner(fake_provider(first), Settings(), cache, "v").run([_req()], "t")
    second = FakeStructuredModel(lambda s, u, n: pytest.fail("model must not be called on a cache hit"))
    runner = StructuredRunner(fake_provider(second), Settings(), cache, "v")
    assert runner.run([_req()], "t")["r1"].cached
    assert runner.usage.cache_hits == 1 and runner.usage.llm_calls == 0

    third = FakeStructuredModel(lambda s, u, n: DOMAIN_OK)  # any key input change is a miss
    StructuredRunner(fake_provider(third, model_name="another-model"), Settings(), cache, "v").run([_req()], "t")
    assert len(third.calls) == 1


def test_rate_limit_backs_off_exponentially_and_honors_server_hint():
    errors = [
        ModelRateLimitError("429 RESOURCE_EXHAUSTED"),
        ModelRateLimitError("429 RESOURCE_EXHAUSTED"),
        ModelRateLimitError("429 RESOURCE_EXHAUSTED. Please retry in 7.5s."),
    ]
    fake = FakeStructuredModel(lambda s, u, n: errors[n - 1] if n <= 3 else DOMAIN_OK)
    runner = _runner(fake, Settings(llm_backoff_initial_s=10, llm_backoff_max_s=15))
    assert runner.run([_req()], "t")["r1"].parsed is not None
    assert runner.sleeps == [10, 15, 7.5]  # doubled, capped at max, then the server's hint


def test_daily_quota_stops_the_run_and_a_rerun_resumes_from_cache(tmp_path):
    cache = ResponseCache(tmp_path)
    daily = ModelRateLimitError("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    reqs = [LLMRequest(f"r{i}", "sys", f"user {i}", DomainResponse) for i in (1, 2, 3)]
    first = FakeStructuredModel(lambda s, u, n: DOMAIN_OK if n == 1 else daily)
    with pytest.raises(ProviderQuotaExhausted):
        _runner(first, cache=cache).run(reqs, "t")
    assert len(first.calls) == 2  # r1 answered, r2 hit the daily quota, r3 never sent

    second = FakeStructuredModel(lambda s, u, n: DOMAIN_OK)
    runner = _runner(second, cache=cache)
    results = runner.run(reqs, "t")
    assert results["r1"].cached and not results["r2"].cached and len(second.calls) == 2
    assert runner.usage.cache_hits == 1


def test_auth_error_is_fatal_without_retries():
    fake = FakeStructuredModel(lambda s, u, n: ModelAuthenticationError("401 API key not valid"))
    with pytest.raises(ProviderAuthError):
        _runner(fake).run([_req()], "t")
    assert len(fake.calls) == 1


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


def test_reasoning_tokens_and_served_model_are_recorded_live_and_from_cache(tmp_path):
    cache = ResponseCache(tmp_path)
    live = _runner(FakeStructuredModel(lambda s, u, n: DOMAIN_OK), cache=cache)
    live.run([_req()], "t")
    assert live.usage.reasoning_tokens == 8 and live.usage.served_models == {"fake-model-2026-01-01"}
    replay = _runner(FakeStructuredModel(lambda s, u, n: pytest.fail("must replay from cache")), cache=cache)
    replay.run([_req()], "t")
    assert replay.usage.cached_reasoning_tokens == 8 and replay.usage.served_models == {"fake-model-2026-01-01"}


def test_cache_keys_isolate_providers_models_and_reasoning_effort():
    base = {
        "prompt_version": "v",
        "provider": "google_genai",
        "model": "m",
        "temperature": None,
        "system": "s",
        "user": "u",
    }
    legacy = hashlib.sha256(
        json.dumps({**base, "schema": DomainResponse.model_json_schema()}, sort_keys=True).encode()
    ).hexdigest()
    key = cache_key(**base, schema=DomainResponse)
    assert key == legacy  # an unset knob leaves keys written before it existed valid
    variants = {
        cache_key(**{**base, "provider": "azure_foundry"}, schema=DomainResponse),
        cache_key(**{**base, "model": "gpt-5.4-mini"}, schema=DomainResponse),
        cache_key(**base, schema=DomainResponse, reasoning_effort="low"),
        cache_key(**base, schema=DomainResponse, reasoning_effort="high"),
    }
    assert len(variants) == 4 and key not in variants
