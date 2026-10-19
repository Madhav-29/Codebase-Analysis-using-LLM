"""Google Gemini through langchain-google-genai."""

from __future__ import annotations

import os

from langchain_google_genai import ChatGoogleGenerativeAI

from codebase_analyzer.config import Settings
from codebase_analyzer.errors import ConfigError
from codebase_analyzer.llm.providers import Provider

DEFAULT_CONCURRENCY = 1  # the free tier allows only a few requests per minute


def applied_temperature(settings: Settings) -> float | None:
    """None for models with fixed sampling (e.g. Gemini 3.5 Flash-Lite), which ignore temperature.

    The integration keeps its own list of those models; asking it avoids
    recording a temperature in the metadata that was never applied.
    """
    try:  # private helper, so tolerate its absence in other versions
        from langchain_google_genai.chat_models import _uses_fixed_sampling_and_disallows_prefill
    except ImportError:
        return settings.llm_temperature
    return None if _uses_fixed_sampling_and_disallows_prefill(settings.llm_model) else settings.llm_temperature


def make(settings: Settings) -> Provider:
    if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        raise ConfigError("GOOGLE_API_KEY is not set. Get a key at https://aistudio.google.com/apikey "
                          "and put it in .env (see .env.example).")
    temperature = applied_temperature(settings)
    sampling = {"temperature": temperature} if temperature is not None else {}
    chat_model = ChatGoogleGenerativeAI(
        model=settings.llm_model, max_tokens=settings.budget.output_reserve, timeout=settings.llm_timeout_s,
        max_retries=1,  # 1 means "no SDK retries" in google-genai; the client owns backoff
        **sampling,
    )
    note = None if temperature is not None else (
        f"Not sent: {settings.llm_model} uses fixed sampling defaults and ignores temperature. "
        "Reproducibility comes from the response cache.")
    return Provider(name="google_genai", model=settings.llm_model, chat_model=chat_model, temperature=temperature,
                    temperature_note=note, concurrency=settings.llm_concurrency or DEFAULT_CONCURRENCY)
