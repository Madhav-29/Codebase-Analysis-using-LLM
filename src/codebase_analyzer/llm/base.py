"""The slice of LangChain's chat-model interface the analyzer depends on.

Every LangChain chat model satisfies it structurally, and so does the test fake.
LangChain already adapts the providers, so there are no wrapper classes: provider
modules return LangChain models as they are.
"""

from __future__ import annotations

from typing import Any, Protocol

from langchain_core.language_models import LanguageModelInput
from langchain_core.runnables import Runnable


class ChatModel(Protocol):
    def with_structured_output(
        self, schema: Any, *, include_raw: bool = ..., **kwargs: Any
    ) -> Runnable[LanguageModelInput, Any]: ...

    def get_num_tokens(self, text: str) -> int: ...
