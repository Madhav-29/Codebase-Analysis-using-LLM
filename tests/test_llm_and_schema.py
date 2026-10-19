import pytest
from pydantic import ValidationError

from codebase_analyzer.analyze import _unit_validator
from codebase_analyzer.cache import ResponseCache
from codebase_analyzer.chunking import plan_units
from codebase_analyzer.config import Settings
from codebase_analyzer.facts import build_facts
from langchain_core.exceptions import ModelAuthenticationError, ModelRateLimitError

from codebase_analyzer.llm import (
    FatalLLMError,
    LLMRequest,
    QuotaExhausted,
    StructuredRunner,
    effective_temperature,
    make_chat_model,
)
from codebase_analyzer.schema import ClassUnitResponse, DomainResponse

from .fakes import FakeStructuredModel, class_payload

VALID = {"classes": [{"type_key": "A", "summary": "s", "responsibilities": [], "findings": [],
                      "methods": [{"member_key": "m1", "description": "d"}]}]}


def test_schema_accepts_valid_and_rejects_invalid_responses():
    assert ClassUnitResponse.model_validate(VALID).classes[0].methods[0].member_key == "m1"
    bad_enum = {"classes": [{**VALID["classes"][0], "findings": [
        {"category": "vibes", "severity": "info", "title": "t", "detail": "d", "line_start": 1, "line_end": 1}]}]}
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
    _unit_validator(unit, require_all_types=False)(ClassUnitResponse.model_validate(class_payload(user, drop_types={"A.B"})))


def _req(validator=None):
    return LLMRequest("r1", "sys", "- A (class, lines 1-2)\n", DomainResponse, validator)


DOMAIN_OK = {"summary": "s", "key_responsibilities": [], "findings": []}


def test_runner_retries_validation_failures_with_feedback_then_succeeds():
    def responder(schema, user, n):
        return {"summary": "s"} if n == 1 else DOMAIN_OK  # first answer is schema-invalid

    fake = FakeStructuredModel(responder)
    runner = StructuredRunner(fake, Settings(), cache=None)
    res = runner.run([_req()], stage="t")["r1"]
    assert res.parsed is not None and res.attempts == 2
    assert "rejected by validation" in fake.calls[1][1]
    assert runner.usage.llm_calls == 2 and runner.usage.input_tokens == 200


def _runner(model, settings=None, cache=None):
    runner = StructuredRunner(model, settings or Settings(), cache)
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
    res = StructuredRunner(always_bad, settings, None).run([_req()], "t")["r1"]
    assert res.parsed is None and res.error and res.attempts == settings.llm_max_attempts
    assert len(always_bad.calls) == settings.llm_max_attempts  # no fabricated output, no extra calls


def test_local_validator_failure_triggers_retry():
    def must_mention_x(resp):
        if "x" not in resp.summary:
            raise ValueError("summary must mention x")

    fake = FakeStructuredModel(lambda s, u, n: DOMAIN_OK if n == 1 else {**DOMAIN_OK, "summary": "x"})
    res = StructuredRunner(fake, Settings(), None).run([_req(must_mention_x)], "t")["r1"]
    assert res.parsed.summary == "x" and "summary must mention x" in fake.calls[1][1]


def test_cache_replays_without_calling_the_model(tmp_path):
    cache = ResponseCache(tmp_path)
    first = FakeStructuredModel(lambda s, u, n: DOMAIN_OK)
    StructuredRunner(first, Settings(), cache).run([_req()], "t")
    second = FakeStructuredModel(lambda s, u, n: pytest.fail("model must not be called on a cache hit"))
    runner = StructuredRunner(second, Settings(), cache)
    assert runner.run([_req()], "t")["r1"].cached
    assert runner.usage.cache_hits == 1 and runner.usage.llm_calls == 0

    other_model = Settings(llm_model="another-model")  # any key input change is a miss
    third = FakeStructuredModel(lambda s, u, n: DOMAIN_OK)
    StructuredRunner(third, other_model, cache).run([_req()], "t")
    assert len(third.calls) == 1


def test_rate_limit_backs_off_exponentially_and_honors_server_hint():
    errors = [ModelRateLimitError("429 RESOURCE_EXHAUSTED"), ModelRateLimitError("429 RESOURCE_EXHAUSTED"),
              ModelRateLimitError("429 RESOURCE_EXHAUSTED. Please retry in 7.5s.")]
    fake = FakeStructuredModel(lambda s, u, n: errors[n - 1] if n <= 3 else DOMAIN_OK)
    runner = _runner(fake, Settings(llm_backoff_initial_s=10, llm_backoff_max_s=15))
    assert runner.run([_req()], "t")["r1"].parsed is not None
    assert runner.sleeps == [10, 15, 7.5]  # doubled, capped at max, then the server's hint


def test_daily_quota_stops_the_run_and_a_rerun_resumes_from_cache(tmp_path):
    cache = ResponseCache(tmp_path)
    daily = ModelRateLimitError("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    reqs = [LLMRequest(f"r{i}", "sys", f"user {i}", DomainResponse) for i in (1, 2, 3)]
    first = FakeStructuredModel(lambda s, u, n: DOMAIN_OK if n == 1 else daily)
    with pytest.raises(QuotaExhausted):
        _runner(first, cache=cache).run(reqs, "t")
    assert len(first.calls) == 2  # r1 answered, r2 hit the daily quota, r3 never sent

    second = FakeStructuredModel(lambda s, u, n: DOMAIN_OK)
    runner = _runner(second, cache=cache)
    results = runner.run(reqs, "t")
    assert results["r1"].cached and not results["r2"].cached and len(second.calls) == 2
    assert runner.usage.cache_hits == 1


def test_auth_error_is_fatal_without_retries():
    fake = FakeStructuredModel(lambda s, u, n: ModelAuthenticationError("401 API key not valid"))
    with pytest.raises(FatalLLMError):
        _runner(fake).run([_req()], "t")
    assert len(fake.calls) == 1


def test_gemini_chat_model_is_configured_from_settings(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-not-a-real-key")  # constructing the client makes no request
    llm = make_chat_model(Settings(llm_provider="google_genai", llm_model="gemini-3.8-flash"))
    assert type(llm).__name__ == "ChatGoogleGenerativeAI"
    assert llm.model == "gemini-3.8-flash" and llm.temperature == 0.0
    assert llm.max_output_tokens == Settings().budget.output_reserve and llm.max_retries == 1


def test_temperature_is_not_sent_to_fixed_sampling_models(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-not-a-real-key")
    lite = Settings(llm_provider="google_genai", llm_model="gemini-3.5-flash-lite")
    assert effective_temperature(lite) is None and make_chat_model(lite).temperature is None
    assert effective_temperature(Settings(llm_provider="google_genai", llm_model="gemini-3.8-flash")) == 0.0
