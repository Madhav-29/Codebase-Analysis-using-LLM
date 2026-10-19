"""Token counting.

Planning (chunking, packing, budgets) uses a conservative local estimate so it
is deterministic and needs no network: ceil(chars / chars_per_token). The
default of 3.0 chars/token over-counts typical Java (~3.5-4 chars/token), which
leaves headroom; the safety margin in the budget adds more.

`count_native` asks the provider for an exact count through LangChain
(`get_num_tokens`, which for Gemini calls the `count_tokens` API). The
`estimate` command uses it to check that the local estimate really is
conservative for the planned units.
"""

from __future__ import annotations

import math

from langchain_core.language_models import BaseChatModel


def estimate_tokens(text: str, chars_per_token: float) -> int:
    return math.ceil(len(text) / chars_per_token)


def count_native(llm: BaseChatModel, text: str) -> int | None:
    try:
        return llm.get_num_tokens(text)
    except Exception:  # provider without native counting, or offline
        return None
