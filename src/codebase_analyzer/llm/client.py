"""Validated structured-output calls with caching, retries and error handling.

Per request:
1. A valid cached response is replayed without calling the model.
2. Rate limits, server errors and timeouts are retried with exponential backoff,
   honoring the server's retry-delay hint. SDK-level retries are disabled in the
   providers so there is exactly one backoff policy.
3. A daily-quota error stops the whole run (ProviderQuotaExhausted), and so do
   auth/model errors (ProviderAuthError). Everything finished so far is cached,
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
from typing import Any, Literal

from langchain_core.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from codebase_analyzer.config import Settings
from codebase_analyzer.errors import ProviderAuthError, ProviderQuotaExhausted
from codebase_analyzer.llm.base import ChatModel
from codebase_analyzer.llm.cache import ResponseCache, cache_key
from codebase_analyzer.llm.providers import Provider

log = logging.getLogger(__name__)

Validator = Callable[[BaseModel], None]  # raises ValueError with a readable message
Outcome = Literal["fatal", "quota", "transient", "error"]

TRANSIENT_ERRORS = (ModelRateLimitError, ModelAPIError, ModelConnectionError, ModelTimeoutError,
                    TimeoutError, ConnectionError)
FATAL_ERRORS = (ModelAuthenticationError, ModelPermissionDeniedError, ModelNotFoundError)
# Gemini puts the suggested wait in the 429 body: "'retryDelay': '23s'" or "Please retry in 23.5s".
_RETRY_HINT = re.compile(r"retry(?:Delay['\"]?:\s*['\"]|\s+in\s+)(\d+(?:\.\d+)?)s", re.IGNORECASE)


def classify(exc: BaseException) -> Outcome:
    """Map a provider exception to what the client does next; the one place errors are interpreted."""
    if isinstance(exc, FATAL_ERRORS):
        return "fatal"
    if isinstance(exc, ModelRateLimitError) and "PerDay" in str(exc):
        return "quota"
    if isinstance(exc, TRANSIENT_ERRORS):
        return "transient"
    return "error"  # e.g. a rejected request: counts as a failed attempt


def retry_hint_seconds(exc: BaseException) -> float | None:
    m = _RETRY_HINT.search(str(exc))
    return float(m.group(1)) if m else None


def count_native(model: ChatModel, text: str) -> int | None:
    """The provider's own token count (e.g. Gemini's count_tokens), or None if unavailable."""
    try:
        return model.get_num_tokens(text)
    except Exception:  # provider without native counting, or offline
        return None


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
    parsed: Any  # an instance of the request's schema, or None on failure
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
    cached_input_tokens: int = 0  # tokens originally spent producing the responses replayed from cache
    cached_output_tokens: int = 0
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

    def add_cache_hit(self, stage: str, usage: dict[str, int]) -> None:
        with self._lock:
            self.cache_hits += 1
            self.cached_input_tokens += usage.get("input_tokens", 0)
            self.cached_output_tokens += usage.get("output_tokens", 0)
            self._stage(stage)["cache_hits"] += 1

    def add_failure(self) -> None:
        with self._lock:
            self.failures += 1


class StructuredRunner:
    def __init__(self, provider: Provider, settings: Settings, cache: ResponseCache | None, prompt_version: str):
        self.provider = provider
        self.settings = settings
        self.cache = cache
        self.prompt_version = prompt_version
        self.usage = UsageTotals()
        self.sleep: Callable[[float], None] = time.sleep  # replaced in tests
        self._stop = threading.Event()

    def _key(self, req: LLMRequest) -> str:
        p = self.provider
        return cache_key(prompt_version=self.prompt_version, provider=p.name, model=p.model, temperature=p.temperature,
                         schema=req.schema, system=req.system, user=req.user)

    def _validate(self, req: LLMRequest, parsed: BaseModel) -> None:
        # Re-validate locally even though the provider enforced the schema.
        req.schema.model_validate(parsed.model_dump())
        if req.validator:
            req.validator(parsed)

    def run(self, requests: list[LLMRequest], stage: str) -> dict[str, LLMResult]:
        with ThreadPoolExecutor(max_workers=max(1, self.provider.concurrency)) as pool:
            results = list(pool.map(lambda r: self._run_one(r, stage), requests))
        return {r.id: r for r in results}

    def _run_one(self, req: LLMRequest, stage: str) -> LLMResult:
        key = self._key(req)
        hit = self.cache.get(key) if self.cache else None
        if hit is not None:
            try:
                parsed = req.schema.model_validate(hit["parsed"])
                self._validate(req, parsed)
                self.usage.add_cache_hit(stage, hit.get("usage", {}))
                return LLMResult(req.id, parsed, None, 0, True)
            except (ValidationError, ValueError) as exc:
                log.warning("ignoring invalid cache entry for %s: %s", req.id, exc)

        runnable = self.provider.chat_model.with_structured_output(
            req.schema, include_raw=True, **self.provider.structured_output)
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

    def _invoke_with_backoff(self, runnable: Any, messages: list[BaseMessage], rid: str) -> Any:
        """Invoke once, retrying transient errors. Returns the output or a non-transient exception."""
        s = self.settings
        delay = s.llm_backoff_initial_s
        for retry in range(s.llm_backoff_retries + 1):
            if self._stop.is_set():
                raise ProviderQuotaExhausted("stopped: the daily quota was exhausted by another request")
            try:
                return runnable.invoke(messages)
            except Exception as exc:
                outcome = classify(exc)
                if outcome == "fatal":
                    self._stop.set()
                    raise ProviderAuthError(str(exc)) from exc
                if outcome == "quota":
                    self._stop.set()
                    raise ProviderQuotaExhausted(str(exc)) from exc
                if outcome == "error" or retry == s.llm_backoff_retries:
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
    def _usage_of(out: Any) -> tuple[int, int]:
        raw = out.get("raw") if isinstance(out, dict) else None
        meta = getattr(raw, "usage_metadata", None) or {}
        return int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0))

    def _check(self, req: LLMRequest, out: Any) -> str | None:
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
