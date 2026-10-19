"""Token counting.

Planning (chunking, packing, budgets) uses a conservative local estimate so it
is deterministic and needs no network: ceil(chars / chars_per_token). The
default of 3.0 chars/token over-counts typical Java (~3.5-4 chars/token), which
leaves headroom; the safety margin in the budget adds more.

`count_native` asks the provider for an exact count (LangChain's
`get_num_tokens_from_messages`, which uses Anthropic's count_tokens endpoint)
and is used only to calibrate the pre-run cost estimate.
"""

from __future__ import annotations

import math

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage


def estimate_tokens(text: str, chars_per_token: float) -> int:
    return math.ceil(len(text) / chars_per_token)


def count_native(llm: BaseChatModel, messages: list[BaseMessage]) -> int | None:
    try:
        return llm.get_num_tokens_from_messages(messages)
    except Exception:  # provider without native counting, or offline
        return None
