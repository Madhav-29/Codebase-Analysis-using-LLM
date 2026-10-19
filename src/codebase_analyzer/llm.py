"""LangChain model factory and a validated structured-output runner.

LangChain's role here is deliberately narrow: a provider-neutral chat model
(`ChatGoogleGenerativeAI`, optionally `ChatAnthropic`), `with_structured_output`
bound to our Pydantic schemas, and LangChain's provider-neutral error classes
(`ModelRateLimitError`, ...). Orchestration (map/reduce, budgets, caching,
retries) is plain Python so every step is visible and testable.

Per request:
1. A valid cached response is replayed without calling the model.
2. Rate limits (429 / RESOURCE_EXHAUSTED), server errors and timeouts are
   retried with exponential backoff, honoring the server's retry-delay hint.
   SDK-level retries are disabled so there is exactly one backoff policy.
3. A daily-quota 429 stops the whole run (`QuotaExhausted`); auth/model errors
   stop it too (`FatalLLMError`). Everything finished so far is already cached,
   so re-running the same command resumes where it stopped.
4. Unparseable output and responses that fail local validation are re-sent up
   to `llm_max_attempts` times with the validation error appended.
5. After that the request is reported as failed. Nothing is fabricated.

Each successful response is written to the cache as soon as it arrives.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from langchain_core.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from .cache import ResponseCache, cache_key
from .config import Settings

log = logging.getLogger(__name__)

Validator = Callable[[BaseModel], None]  # raises ValueError with a readable message

TRANSIENT_ERRORS = (ModelRateLimitError, ModelAPIError, ModelConnectionError, ModelTimeoutError,
                    TimeoutError, ConnectionError)
FATAL_ERRORS = (ModelAuthenticationError, ModelPermissionDeniedError, ModelNotFoundError)
# Gemini puts the suggested wait in the 429 body: "'retryDelay': '23s'" or "Please retry in 23.5s".
_RETRY_HINT = re.compile(r"retry(?:Delay['\"]?:\s*['\"]|\s+in\s+)(\d+(?:\.\d+)?)s", re.IGNORECASE)


class QuotaExhausted(RuntimeError):
    """The provider's daily quota is used up; re-run later to resume from the cache."""


class FatalLLMError(RuntimeError):
    """An error no retry can fix (bad API key, no access, unknown model)."""


def is_daily_quota(exc: BaseException) -> bool:
    return isinstance(exc, ModelRateLimitError) and "PerDay" in str(exc)


def retry_hint_seconds(exc: BaseException) -> float | None:
    m = _RETRY_HINT.search(str(exc))
    return float(m.group(1)) if m else None


def effective_temperature(settings: Settings) -> float | None:
    """The temperature actually applied; None when the model ignores custom sampling (e.g. Gemini 3.5 Flash-Lite)."""
    if settings.llm_provider == "google_genai":
        try:  # private helper: the integration's own list of fixed-sampling models
            from langchain_google_genai.chat_models import _uses_fixed_sampling_and_disallows_prefill
        except ImportError:
            return settings.llm_temperature
        if _uses_fixed_sampling_and_disallows_prefill(settings.llm_model):
            return None
    return settings.llm_temperature


def make_chat_model(settings: Settings) -> BaseChatModel:
    common: dict = {"model": settings.llm_model, "max_tokens": settings.budget.output_reserve,
                    "timeout": settings.llm_timeout_s}
    if (temperature := effective_temperature(settings)) is not None:
        common["temperature"] = temperature
    if settings.llm_provider == "google_genai":
        from langchain_google_genai import ChatGoogleGenerativeAI

        # max_retries=1 means "no SDK retries" in google-genai; our runner owns backoff.
        return ChatGoogleGenerativeAI(**common, max_retries=1)
    if settings.llm_provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:  # optional extra
            raise RuntimeError("Install the anthropic extra: uv sync --extra anthropic") from exc
        return ChatAnthropic(**common, max_retries=0)
    raise ValueError(f"unknown provider {settings.llm_provider!r}")


@dataclass
class LLMRequest:
    id: str
    system: str
    user: str
    schema: type[BaseModel]
    validator: Validator | None = None


@dataclass
class LLMResult:
    id: str
    parsed: BaseModel | None
    error: str | None
    attempts: int
    cached: bool
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class UsageTotals:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    failures: int = 0
    per_stage: dict[str, dict[str, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _stage(self, stage: str) -> dict[str, int]:
        return self.per_stage.setdefault(stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_hits": 0})

    def add_call(self, stage: str, inp: int, out: int) -> None:
        with self._lock:
            self.llm_calls += 1
            self.input_tokens += inp
            self.output_tokens += out
            s = self._stage(stage)
            s["calls"] += 1
            s["input_tokens"] += inp
            s["output_tokens"] += out

    def add_cache_hit(self, stage: str) -> None:
        with self._lock:
            self.cache_hits += 1
            self._stage(stage)["cache_hits"] += 1

    def add_failure(self) -> None:
        with self._lock:
            self.failures += 1


class StructuredRunner:
    def __init__(self, model: BaseChatModel, settings: Settings, cache: ResponseCache | None):
        self.model = model
        self.settings = settings
        self.cache = cache
        self.usage = UsageTotals()
        self.sleep = time.sleep  # replaced in tests
        self._stop = threading.Event()

    def _key(self, req: LLMRequest) -> str:
        s = self.settings
        return cache_key(provider=s.llm_provider, model=s.llm_model, temperature=effective_temperature(s),
                         schema=req.schema, system=req.system, user=req.user)

    def _validate(self, req: LLMRequest, parsed: BaseModel) -> None:
        # Re-validate locally even though the provider enforced the schema.
        req.schema.model_validate(parsed.model_dump())
        if req.validator:
            req.validator(parsed)

    def run(self, requests: list[LLMRequest], stage: str) -> dict[str, LLMResult]:
        with ThreadPoolExecutor(max_workers=max(1, self.settings.llm_concurrency)) as pool:
            results = list(pool.map(lambda r: self._run_one(r, stage), requests))
        return {r.id: r for r in results}

    def _run_one(self, req: LLMRequest, stage: str) -> LLMResult:
        key = self._key(req)
        hit = self.cache.get(key) if self.cache else None
        if hit is not None:
            try:
                parsed = req.schema.model_validate(hit["parsed"])
                self._validate(req, parsed)
                self.usage.add_cache_hit(stage)
                return LLMResult(req.id, parsed, None, 0, True)
            except (ValidationError, ValueError) as exc:
                log.warning("ignoring invalid cache entry for %s: %s", req.id, exc)

        runnable = self.model.with_structured_output(req.schema, method="json_schema", include_raw=True)
        feedback: str | None = None
        total_in = total_out = 0
        for attempt in range(1, self.settings.llm_max_attempts + 1):
            log.info("%s: %s attempt %d", stage, req.id, attempt)
            out = self._invoke_with_backoff(runnable, self._messages(req, feedback), req.id)
            inp, outp = self._usage_of(out)
            self.usage.add_call(stage, inp, outp)
            total_in, total_out = total_in + inp, total_out + outp
            error = self._check(req, out)
            if error is None:
                if self.cache:
                    self.cache.put(key, out["parsed"], {"input_tokens": inp, "output_tokens": outp})
                return LLMResult(req.id, out["parsed"], None, attempt, False, total_in, total_out)
            log.warning("%s: %s failed attempt %d: %s", stage, req.id, attempt, error[:500])
            feedback = error
        self.usage.add_failure()
        return LLMResult(req.id, None, feedback, self.settings.llm_max_attempts, False, total_in, total_out)

    def _invoke_with_backoff(self, runnable, messages: list[BaseMessage], rid: str):
        """Invoke once, retrying transient errors. Returns the output or a non-transient exception."""
        s = self.settings
        delay = s.llm_backoff_initial_s
        for retry in range(s.llm_backoff_retries + 1):
            if self._stop.is_set():
                raise QuotaExhausted("stopped: the daily quota was exhausted by another request")
            try:
                return runnable.invoke(messages)
            except FATAL_ERRORS as exc:
                self._stop.set()
                raise FatalLLMError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - classified below
                if is_daily_quota(exc):
                    self._stop.set()
                    raise QuotaExhausted(str(exc)) from exc
                if not isinstance(exc, TRANSIENT_ERRORS) or retry == s.llm_backoff_retries:
                    return exc
                wait = min(retry_hint_seconds(exc) or delay, s.llm_backoff_max_s)
                log.warning("%s: %s; retry %d in %.0fs", rid, type(exc).__name__, retry + 1, wait)
                self.sleep(wait)
                delay = min(delay * 2, s.llm_backoff_max_s)
        raise AssertionError("unreachable")

    @staticmethod
    def _messages(req: LLMRequest, feedback: str | None) -> list[BaseMessage]:
        user = req.user
        if feedback:
            user += ("\n\nYour previous response to this request was rejected by validation: "
                     f"{feedback}\nReturn a corrected, complete response.")
        return [SystemMessage(req.system), HumanMessage(user)]

    @staticmethod
    def _usage_of(out) -> tuple[int, int]:
        raw = out.get("raw") if isinstance(out, dict) else None
        meta = getattr(raw, "usage_metadata", None) or {}
        return int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0))

    def _check(self, req: LLMRequest, out) -> str | None:
        if isinstance(out, BaseException):
            return f"{type(out).__name__}: {out}"
        if out.get("parsing_error") is not None or out.get("parsed") is None:
            meta = getattr(out.get("raw"), "response_metadata", {}) or {}
            stop = meta.get("finish_reason") or meta.get("stop_reason")
            return f"unparseable structured output (finish_reason={stop}): {out.get('parsing_error')}"
        try:
            self._validate(req, out["parsed"])
        except (ValidationError, ValueError) as exc:
            return str(exc)
        return None
