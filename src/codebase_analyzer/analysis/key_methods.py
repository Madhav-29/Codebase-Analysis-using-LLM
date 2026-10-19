"""Deterministic selection of the methods a reader should look at first.

Selection uses facts only (routes, Spring stereotypes, complexity); descriptions
are attached later at assembly. Trivial members and abstract declarations never
qualify.
"""

from __future__ import annotations

from dataclasses import dataclass

from codebase_analyzer.analysis.facts import ProjectFacts
from codebase_analyzer.config import Settings


@dataclass(frozen=True)
class KeySelection:
    member_id: str
    class_fqn: str
    reasons: tuple[str, ...]  # endpoint_handler | service_api | data_access | complex
    endpoints: tuple[str, ...]  # "GET /api/v1/actors/{id}" for handlers


def select_key_methods(facts: ProjectFacts, settings: Settings) -> list[KeySelection]:
    routes: dict[tuple[str, int], list[str]] = {}
    for e in facts.endpoints:
        routes.setdefault((e.controller, e.line), []).append(f"{e.http_method} {e.path}")
    moderate = settings.complexity_thresholds["moderate"]
    result = []
    for fqn, tf in facts.types.items():
        stereotypes = {a.name for a in tf.decl.annotations}
        for mf in tf.members:
            m = mf.member
            if (mf.deterministic and mf.deterministic.trivial) or m.body is None:
                continue
            eps = routes.get((fqn, m.line_start), [])
            public = "public" in m.modifiers
            reasons = tuple(r for r, hit in (
                ("endpoint_handler", bool(eps)),
                ("service_api", public and "Service" in stereotypes),
                ("data_access", public and "Repository" in stereotypes),
                ("complex", bool(mf.complexity and mf.complexity.cyclomatic >= moderate)),
            ) if hit)
            if reasons:
                result.append(KeySelection(mf.id, fqn, reasons, tuple(eps)))
    return result
