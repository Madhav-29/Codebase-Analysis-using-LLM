"""Anthropic Claude through langchain-anthropic (optional extra: uv sync --extra anthropic)."""

from __future__ import annotations

import os

from langchain_anthropic import ChatAnthropic

from codebase_analyzer.config import Settings
from codebase_analyzer.errors import ConfigError
from codebase_analyzer.llm.providers import Provider

DEFAULT_CONCURRENCY = 1


def make(settings: Settings) -> Provider:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ConfigError("ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example).")
    chat_model = ChatAnthropic(
        model=settings.llm_model,
        max_tokens=settings.budget.output_reserve,
        timeout=settings.llm_timeout_s,
        temperature=settings.llm_temperature,
        max_retries=0,  # the client owns backoff
    )
    return Provider(
        name="anthropic",
        model=settings.llm_model,
        chat_model=chat_model,
        temperature=settings.llm_temperature,
        temperature_note=None,
        concurrency=settings.llm_concurrency or DEFAULT_CONCURRENCY,
    )
