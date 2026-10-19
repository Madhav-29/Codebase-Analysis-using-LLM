"""Rule-based findings: complexity hot spots and README claims vs controller mappings.

These need no LLM; the project-reduce prompt receives them so the model does not
repeat them, and LLM findings that duplicate them are dropped.
"""

from __future__ import annotations

import re

from codebase_analyzer.analysis.facts import ProjectFacts
from codebase_analyzer.config import Settings
from codebase_analyzer.schema.output import Evidence, FindingOut


def deterministic_findings(facts: ProjectFacts, settings: Settings) -> list[FindingOut]:
    findings: list[FindingOut] = []

    # 1. Methods at or above the "high" cyclomatic threshold; boilerplate (e.g. hand-written equals) reported apart.
    high = settings.complexity_thresholds["high"]
    complex_members = [
        (t, mf, mf.complexity.cyclomatic)
        for t in facts.types.values()
        for mf in t.members
        if mf.complexity and mf.complexity.cyclomatic >= high
    ]
    for trivial in (False, True):
        group = [m for m in complex_members if bool(m[1].deterministic and m[1].deterministic.trivial) == trivial]
        if not group:
            continue
        names = ", ".join(f"{t.decl.name}.{mf.member.name} (CC {cc})" for t, mf, cc in group)
        findings.append(
            FindingOut(
                category="maintainability",
                severity="info" if trivial else "low",
                title=(
                    f"{len(group)} boilerplate method(s) with cyclomatic complexity >= {high}"
                    if trivial
                    else f"{len(group)} method(s) with cyclomatic complexity >= {high}"
                ),
                detail=f"Measured by lizard: {names}."
                + (
                    " These are hand-written equals/hashCode-style members classified as boilerplate, not business logic."
                    if trivial
                    else ""
                ),
                scope="project",
                evidence=[
                    Evidence(file=t.file.path, line_start=mf.member.line_start, line_end=mf.member.line_end)
                    for t, mf, _ in group
                ],
                source="deterministic",
            )
        )

    # 2. README endpoint claims vs. resolved controller mappings.
    readme = next((f for f in facts.inventory.by_category("readme")), None)
    if readme and facts.endpoints:
        text = facts.inventory.read(readme.path)
        base = facts.context_path.rstrip("/")
        # evidence: the README lines mentioning the API base path (its endpoint list), else the whole file
        cited = [n for n, line in enumerate(text.splitlines(), start=1) if base and base in line]
        readme_evidence = [
            Evidence(
                file=readme.path, line_start=min(cited, default=1), line_end=max(cited, default=text.count("\n") + 1)
            )
        ]
        claimed = {m.group(1) for m in re.finditer(re.escape(base) + r"/([A-Za-z0-9_-]+)", text)} if base else set()
        resources: dict[str, set[str]] = {}
        for e in facts.endpoints:
            seg = e.path[len(base) :].strip("/").split("/", 1)[0]
            resources.setdefault(seg, set()).add(e.http_method)
        provided_by_framework = {"actuator"}  # Spring Boot Actuator, not a controller
        not_in_code = sorted(claimed - set(resources) - provided_by_framework)
        not_in_readme = sorted(set(resources) - claimed)
        if not_in_code or not_in_readme:
            detail = []
            if not_in_code:
                detail.append(
                    f"README lists {', '.join(base + '/' + r for r in not_in_code)} but no controller maps them."
                )
            if not_in_readme:
                detail.append(
                    f"Controllers map {', '.join(base + '/' + r for r in not_in_readme)} which the README does not list."
                )
            findings.append(
                FindingOut(
                    category="documentation_mismatch",
                    severity="low",
                    title="README endpoint list differs from controller mappings",
                    detail=" ".join(detail),
                    scope="project",
                    evidence=readme_evidence,
                    source="deterministic",
                )
            )
        if "CRUD" in text:
            crud = {"GET", "POST", "PUT", "DELETE"}
            partial = {r: sorted(crud - verbs) for r, verbs in resources.items() if crud - verbs and r in claimed}
            if partial:
                findings.append(
                    FindingOut(
                        category="documentation_mismatch",
                        severity="info",
                        title="README claims CRUD support; some listed resources lack HTTP verbs",
                        detail="Missing verbs per resource (from mappings): "
                        + "; ".join(f"{base}/{r}: {', '.join(v)}" for r, v in sorted(partial.items()))
                        + ".",
                        scope="project",
                        evidence=readme_evidence,
                        source="deterministic",
                    )
                )
    return findings
