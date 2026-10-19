"""Token estimate used for all planning.

Planning (chunking, packing, budgets) uses a conservative local estimate so it
is deterministic, offline and provider-independent: ceil(chars / chars_per_token).
The default of 3.0 chars/token over-counts typical Java (~3.5-4 chars/token),
which leaves headroom; the budget's safety margin adds more. Because it does not
depend on the provider, every model receives identical prompts and packs.
The provider's own count (`llm.client.count_native`) is used only to verify it.
"""

from __future__ import annotations

import math


def estimate_tokens(text: str, chars_per_token: float) -> int:
    return math.ceil(len(text) / chars_per_token)

