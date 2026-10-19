"""Runtime settings.

All values can be overridden through environment variables (or `.env`) and most
through CLI flags. API keys are read by the provider SDKs from the environment;
they are never stored in settings or written to output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# USD per million tokens (input, output). Used only for the cost estimate in the
# run summary; unknown models report no cost rather than a guessed one.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class TokenBudget(BaseModel):
    """Per-request token budget.

    A request is: system prompt + output schema + code + supporting context
    + reserved output tokens + safety margin. Chunking guarantees the sum of the
    input parts stays within `request_tokens - output_reserve - safety margin`.
    """

    request_tokens: int = 32_000
    output_reserve: int = 12_000  # also sent as max_tokens (includes model thinking)
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

    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    # Claude Sonnet 5.5 rejects non-default sampling parameters, so temperature
    # is only sent when explicitly set or for providers that accept it.
    llm_temperature: float | None = None
    llm_effort: Literal["low", "medium", "high"] | None = "medium"  # Anthropic only
    llm_concurrency: int = 4
    llm_max_attempts: int = 3  # attempts per unit, for transient and validation failures
    llm_timeout_s: float = 300.0

    budget: TokenBudget = Field(default_factory=TokenBudget)
    packing: PackingConfig = Field(default_factory=PackingConfig)

    cache_dir: Path = Path(".cache/llm")
    use_cache: bool = True
    clone_dir: Path = Path("target_repo")

    complexity_thresholds: dict[str, int] = Field(
        default_factory=lambda: {"moderate": 6, "high": 11, "very_high": 21}
    )
    top_complex_methods: int = 10

    def effective_temperature(self) -> float | None:
        if self.llm_temperature is not None:
            return self.llm_temperature
        return 0.0 if self.llm_provider == "openai" else None
