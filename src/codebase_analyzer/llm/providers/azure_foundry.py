"""GPT models on Azure AI Foundry through langchain-azure-ai (optional extra: uv sync --extra azure).

The deployment is reached through its OpenAI-compatible v1 endpoint. Auth is
AZURE_AI_API_KEY when set, otherwise Microsoft Entra ID via DefaultAzureCredential
(az login, managed identity, ...), so no key is needed where Entra ID is available.
"""

from __future__ import annotations

import os

from azure.identity import DefaultAzureCredential
from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel

from codebase_analyzer.config import Settings
from codebase_analyzer.errors import ConfigError
from codebase_analyzer.llm.providers import Provider

# 4 parallel calls of up to ~20k tokens stay inside a 100 RPM / 100k TPM deployment; 429s still back off.
DEFAULT_CONCURRENCY = 4
# tiktoken does not know deployment names; gpt-4o maps to o200k_base, the GPT-4o/5.x tokenizer.
TIKTOKEN_MODEL = "gpt-4o"


def make(settings: Settings) -> Provider:
    endpoint = (settings.azure_ai_endpoint or "").rstrip("/")
    if not endpoint:
        raise ConfigError(
            "AZURE_AI_ENDPOINT is not set. Use the deployment's v1 endpoint, "
            "https://<resource>.services.ai.azure.com/openai/v1 (see .env.example)."
        )
    if not endpoint.endswith("/openai/v1"):
        raise ConfigError(
            f"AZURE_AI_ENDPOINT must be the OpenAI-compatible v1 endpoint ending in /openai/v1, got {endpoint!r}"
        )
    key = os.environ.get("AZURE_AI_API_KEY")
    chat_model = AzureAIOpenAIApiChatModel(
        endpoint=endpoint,
        credential=key or DefaultAzureCredential(),
        model=settings.llm_model,
        max_completion_tokens=settings.budget.output_reserve,  # includes reasoning tokens
        reasoning_effort=settings.llm_reasoning_effort,
        timeout=settings.llm_timeout_s,
        max_retries=0,  # the client owns backoff
        store=False,  # the analyzed code is not retained server-side
        tiktoken_model_name=TIKTOKEN_MODEL,
    )
    return Provider(
        name="azure_foundry",
        model=settings.llm_model,
        chat_model=chat_model,
        temperature=None,
        temperature_note=(
            "Not sent: reasoning models accept no sampling parameters; output is steered by reasoning_effort. "
            "Reproducibility comes from the response cache."
        ),
        concurrency=settings.llm_concurrency or DEFAULT_CONCURRENCY,
        structured_output={"method": "json_schema", "strict": True},
        reasoning_effort=settings.llm_reasoning_effort,
    )
