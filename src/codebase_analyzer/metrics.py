"""Per-method complexity metrics computed with lizard.

lizard's whole-file Java parsing is heuristic and was observed to drop every
function in some files (e.g. a class-level annotation with arguments combined
with a generic class header). To make the join with the tree-sitter output exact,
each member's source is analyzed on its own inside a minimal wrapper class, and
the outermost function lizard reports is taken as that member's metrics.

Consequence: methods of anonymous classes declared inside a member are not
folded into that member's cyclomatic complexity.
"""

from __future__ import annotations

from dataclasses import dataclass

import lizard

from .java_parser import Member


@dataclass(frozen=True)
class Complexity:
    cyclomatic: int
    nloc: int
    parameter_count: int
    token_count: int


def member_complexity(member: Member) -> Complexity | None:
    """Return metrics for a member with a body; None for abstract declarations."""
    if member.body is None:
        return None
    wrapped = "class __Metrics__ {\n" + member.source + "\n}\n"
    result = lizard.analyze_file.analyze_source_code("Member.java", wrapped)
    if not result.function_list:
        return None
    outer = max(result.function_list, key=lambda f: (f.end_line - f.start_line, f.token_count))
    return Complexity(
        cyclomatic=outer.cyclomatic_complexity,
        nloc=outer.nloc,
        parameter_count=len(member.parameters),  # from the parser: exact, incl. varargs
        token_count=outer.token_count,
    )


def complexity_band(cyclomatic: int, thresholds: dict[str, int]) -> str:
    if cyclomatic >= thresholds["very_high"]:
        return "very_high"
    if cyclomatic >= thresholds["high"]:
        return "high"
    if cyclomatic >= thresholds["moderate"]:
        return "moderate"
    return "low"
