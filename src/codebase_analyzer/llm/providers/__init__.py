"""Provider selection: the only package that imports provider SDKs.

Each provider module turns `Settings` into a ready LangChain chat model plus the
provider-specific facts the rest of the pipeline needs (what is actually sent
for sampling and reasoning, extra structured-output arguments, a concurrency
that fits the provider's rate limits). Optional providers are imported lazily,
so a missing extra is a clear ConfigError instead of an ImportError at startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codebase_analyzer.config import Settings
from codebase_analyzer.errors import ConfigError
from codebase_analyzer.llm.base import ChatModel


@dataclass(frozen=True)
class Provider:
    name: str
    model: str
    chat_model: ChatModel
    temperature: float | None  # what is sent; None = not sent (the model ignores or rejects it)
    temperature_note: str | None  # why temperature is not sent, recorded in output metadata
    concurrency: int
    structured_output: dict[str, Any] = field(default_factory=lambda: {"method": "json_schema"})
    reasoning_effort: str | None = None  # sent to reasoning models; part of the cache key when set


def make_provider(settings: Settings) -> Provider:
    if settings.llm_provider == "google_genai":
        from codebase_analyzer.llm.providers import google_genai

        return google_genai.make(settings)
    if settings.llm_provider == "azure_foundry":
        try:
            from codebase_analyzer.llm.providers import azure_foundry
        except ImportError as exc:
            raise ConfigError("The azure_foundry provider needs its extra: uv sync --extra azure") from exc
        return azure_foundry.make(settings)
    if settings.llm_provider == "anthropic":
        try:
            from codebase_analyzer.llm.providers import anthropic
        except ImportError as exc:
            raise ConfigError("The anthropic provider needs its extra: uv sync --extra anthropic") from exc
        return anthropic.make(settings)
    raise ConfigError(f"Unknown LLM_PROVIDER {settings.llm_provider!r}")
