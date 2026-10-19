"""LangChain model factory and a batched, validated structured-output runner.

LangChain's role here is deliberately narrow: a provider-neutral chat model
(`ChatAnthropic` / `ChatOpenAI`), `with_structured_output` bound to our Pydantic
schemas, and `.batch()` with a concurrency limit. Orchestration (map/reduce,
budgets, caching) is plain Python so every step is visible and testable.

Retries:
* transient API errors (429/5xx/connection) are retried with backoff by the
  provider SDK (`max_retries`);
* failures that survive that, unparseable output, and responses that fail our
  local validation are re-sent up to `llm_max_attempts` times, with the
  validation error appended so the model can correct itself;
* after that the request is reported as failed. Nothing is fabricated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from .cache import ResponseCache, cache_key
from .config import PRICING_USD_PER_MTOK, Settings

log = logging.getLogger(__name__)

Validator = Callable[[BaseModel], None]  # raises ValueError with a readable message


def make_chat_model(settings: Settings) -> BaseChatModel:
    temperature = settings.effective_temperature()
    common: dict = {"model": settings.llm_model, "max_tokens": settings.budget.output_reserve,
                    "max_retries": 4, "timeout": settings.llm_timeout_s}
    if temperature is not None:
        common["temperature"] = temperature
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if settings.llm_effort:
            common["effort"] = settings.llm_effort
        return ChatAnthropic(**common)
    if settings.llm_provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # optional extra
            raise RuntimeError("Install the openai extra: uv sync --extra openai") from exc
        return ChatOpenAI(**common)
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

    def add(self, stage: str, inp: int, out: int, calls: int = 1) -> None:
        self.llm_calls += calls
        self.input_tokens += inp
        self.output_tokens += out
        s = self.per_stage.setdefault(stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_hits": 0})
        s["calls"] += calls
        s["input_tokens"] += inp
        s["output_tokens"] += out

    def cost_usd(self, model: str) -> float | None:
        price = PRICING_USD_PER_MTOK.get(model)
        if price is None:
            return None
        return round(self.input_tokens / 1e6 * price[0] + self.output_tokens / 1e6 * price[1], 4)


class StructuredRunner:
    def __init__(self, model: BaseChatModel, settings: Settings, cache: ResponseCache | None):
        self.model = model
        self.settings = settings
        self.cache = cache
        self.usage = UsageTotals()

    def _key(self, req: LLMRequest) -> str:
        s = self.settings
        return cache_key(provider=s.llm_provider, model=s.llm_model, temperature=s.effective_temperature(),
                         effort=s.llm_effort, schema=req.schema, system=req.system, user=req.user)

    def _validate(self, req: LLMRequest, parsed: BaseModel) -> None:
        # Re-validate locally even though the provider enforced the schema.
        req.schema.model_validate(parsed.model_dump())
        if req.validator:
            req.validator(parsed)

    def run(self, requests: list[LLMRequest], stage: str) -> dict[str, LLMResult]:
        results: dict[str, LLMResult] = {}
        pending: list[LLMRequest] = []
        for req in requests:
            hit = self.cache.get(self._key(req)) if self.cache else None
            if hit is not None:
                try:
                    parsed = req.schema.model_validate(hit["parsed"])
                    self._validate(req, parsed)
                    results[req.id] = LLMResult(req.id, parsed, None, 0, True)
                    self.usage.cache_hits += 1
                    self.usage.per_stage.setdefault(
                        stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_hits": 0})["cache_hits"] += 1
                    continue
                except (ValidationError, ValueError) as exc:
                    log.warning("ignoring invalid cache entry for %s: %s", req.id, exc)
            pending.append(req)

        feedback: dict[str, str] = {}
        tokens: dict[str, tuple[int, int]] = {}
        for attempt in range(1, self.settings.llm_max_attempts + 1):
            if not pending:
                break
            retry: list[LLMRequest] = []
            for schema in {r.schema for r in pending}:
                group = [r for r in pending if r.schema is schema]
                runnable = self.model.with_structured_output(schema, method="json_schema", include_raw=True)
                inputs = [self._messages(r, feedback.get(r.id)) for r in group]
                log.info("%s: sending %d request(s), attempt %d", stage, len(group), attempt)
                outputs = runnable.batch(inputs, config={"max_concurrency": self.settings.llm_concurrency},
                                         return_exceptions=True)
                for req, out in zip(group, outputs):
                    inp, outp = self._record_usage(out, stage)
                    prev = tokens.get(req.id, (0, 0))
                    tokens[req.id] = (prev[0] + inp, prev[1] + outp)
                    error = self._check(req, out)
                    if error is None:
                        parsed = out["parsed"]
                        if self.cache:
                            self.cache.put(self._key(req), parsed, {"input_tokens": inp, "output_tokens": outp})
                        results[req.id] = LLMResult(req.id, parsed, None, attempt, False, *tokens[req.id])
                    else:
                        log.warning("%s: %s failed attempt %d: %s", stage, req.id, attempt, error)
                        feedback[req.id] = error
                        retry.append(req)
            pending = retry

        for req in pending:
            self.usage.failures += 1
            results[req.id] = LLMResult(req.id, None, feedback.get(req.id, "unknown error"),
                                        self.settings.llm_max_attempts, False, *tokens.get(req.id, (0, 0)))
        return results

    @staticmethod
    def _messages(req: LLMRequest, feedback: str | None) -> list[BaseMessage]:
        user = req.user
        if feedback:
            user += ("\n\nYour previous response to this request was rejected by validation: "
                     f"{feedback}\nReturn a corrected, complete response.")
        return [SystemMessage(req.system), HumanMessage(user)]

    def _record_usage(self, out, stage: str) -> tuple[int, int]:
        raw = out.get("raw") if isinstance(out, dict) else None
        meta = getattr(raw, "usage_metadata", None) or {}
        inp, outp = int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0))
        self.usage.add(stage, inp, outp)
        return inp, outp

    def _check(self, req: LLMRequest, out) -> str | None:
        if isinstance(out, BaseException):
            return f"{type(out).__name__}: {out}"
        if out.get("parsing_error") is not None or out.get("parsed") is None:
            raw = out.get("raw")
            stop = (getattr(raw, "response_metadata", {}) or {}).get("stop_reason")
            return f"unparseable structured output (stop_reason={stop}): {out.get('parsing_error')}"
        try:
            self._validate(req, out["parsed"])
        except (ValidationError, ValueError) as exc:
            return str(exc)
        return None
