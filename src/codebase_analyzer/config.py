"""Runtime settings, validated once at startup.

Values come from (highest first) CLI flags, environment variables, `.env`, then
the defaults below. API keys are deliberately not settings: provider modules
read them from the environment, so they never reach logs, metadata or output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TokenBudget(BaseModel):
    """Per-request token budget.

    A request is: system prompt + output schema + code + supporting context
    + reserved output tokens + safety margin. Chunking guarantees the sum of the
    input parts stays within `request_tokens - output_reserve - safety margin`.
    """

    request_tokens: int = 32_000
    output_reserve: int = 12_000  # also sent as max output tokens (includes model thinking)
    safety_margin_ratio: float = 0.10
    supporting_context_tokens: int = 2_000  # cap for referenced-type signatures
    reduce_input_tokens: int = 16_000  # cap for domain/project reduce prompts
    chars_per_token: float = 3.0  # conservative local estimate for code

    @property
    def input_limit(self) -> int:
        margin = int(self.request_tokens * self.safety_margin_ratio)
        return self.request_tokens - self.output_reserve - margin

    @model_validator(mode="after")
    def _check_room_for_input(self) -> TokenBudget:
        if self.input_limit < 1_000:
            raise ValueError(
                f"request_tokens={self.request_tokens} leaves only {self.input_limit} input tokens after "
                f"output_reserve={self.output_reserve} and the safety margin; raise request_tokens or lower output_reserve"
            )
        return self


class PackingConfig(BaseModel):
    """Guardrails for packing several small classes into one LLM call."""

    enabled: bool = True
    max_classes: int = 8
    max_class_tokens: int = 1_500  # classes larger than this are never packed
    max_class_cyclomatic: int = 5  # classes with a method above this are never packed


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Literal["google_genai", "azure_foundry", "anthropic"] = "google_genai"
    llm_model: str = "gemini-3.5-flash-lite"  # model id, or the deployment name on Azure
    llm_temperature: float = 0.0  # sent only to models that accept sampling parameters
    # sent only to reasoning models (azure_foundry)
    llm_reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] = "low"
    azure_ai_endpoint: str | None = None  # https://<resource>.services.ai.azure.com/openai/v1
    llm_concurrency: int | None = None  # None: the provider's default (see llm/providers)
    llm_max_attempts: int = 3  # per request, for unparseable or locally invalid output
    llm_backoff_initial_s: float = 10.0  # rate-limit/server-error backoff, doubled each retry
    llm_backoff_max_s: float = 120.0
    llm_backoff_retries: int = 8
    llm_timeout_s: float = 300.0

    budget: TokenBudget = Field(default_factory=TokenBudget)
    packing: PackingConfig = Field(default_factory=PackingConfig)

    cache_dir: Path = Path(".cache/llm")
    use_cache: bool = True

    complexity_thresholds: dict[str, int] = Field(default_factory=lambda: {"moderate": 6, "high": 11, "very_high": 21})
    top_complex_methods: int = 10
