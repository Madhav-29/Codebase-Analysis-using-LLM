"""Reduce stages: class summaries -> domain summaries -> project overview.

Reduce prompts are built from summaries, never code, and are budgeted too: when
a domain's or the project's input exceeds `reduce_input_tokens`, it is
condensed hierarchically in groups first.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel

from codebase_analyzer.analysis.facts import ProjectFacts
from codebase_analyzer.config import Settings
from codebase_analyzer.llm.client import LLMRequest, StructuredRunner
from codebase_analyzer.pipeline.map_stage import MapOutput
from codebase_analyzer.planning.tokens import estimate_tokens
from codebase_analyzer.prompts import (
    DOMAIN_SYSTEM,
    PROJECT_SYSTEM,
    number_lines,
    render_domain_prompt,
    render_project_prompt,
)
from codebase_analyzer.schema.output import Evidence, FindingOut
from codebase_analyzer.schema.responses import DomainResponse, LLMReducedFinding, ProjectResponse

log = logging.getLogger(__name__)

SECRET_KEY = re.compile(r"(password|passwd|secret|token|key|credential)", re.IGNORECASE)  # over-masking is harmless


T = TypeVar("T", bound=BaseModel)


@dataclass
class ReduceResult(Generic[T]):
    response: T | None
    error: str | None


# ------------------------------------------------------------------ reduce helpers


def _tokens(text: str, settings: Settings) -> int:
    return estimate_tokens(text, settings.budget.chars_per_token)


def _schema_tokens(schema: type[BaseModel], settings: Settings) -> int:
    return _tokens(json.dumps(schema.model_json_schema()), settings)


def _groups(items: list[str], budget: int, settings: Settings) -> list[list[str]]:
    groups: list[list[str]] = [[]]
    used = 0
    for item in items:
        cost = _tokens(item, settings) + 1
        if groups[-1] and used + cost > budget:
            groups.append([])
            used = 0
        groups[-1].append(item)
        used += cost
    return groups


def _reduce_findings(
    findings: list[LLMReducedFinding], scope: str, known_files: dict[str, int], dropped: list[str]
) -> list[FindingOut]:
    """Keep evidence that cites a file from the input within its real line range (path -> line count)."""
    result = []
    for f in findings:
        evidence = [
            Evidence(file=e.file, line_start=e.line_start, line_end=e.line_end)
            for e in f.evidence
            if e.file in known_files and 1 <= e.line_start <= e.line_end <= known_files[e.file]
        ]
        if f.evidence and not evidence:
            dropped.append(f"{scope}: '{f.title}' cited no file/line range from the input")
            continue
        result.append(
            FindingOut(
                category=f.category,
                severity=f.severity,
                title=f.title,
                detail=f.detail,
                scope=scope,
                evidence=evidence,
                source="llm",
            )
        )
    return result


# ------------------------------------------------------------------ domain reduce


def _class_line(fqn: str, facts: ProjectFacts, mo: MapOutput) -> str:
    tf = facts.types[fqn]
    res = mo.classes[fqn]
    titles = [f.title for f in mo.findings if f.scope == fqn]
    line = f"- {fqn} ({tf.decl.kind}, {tf.file.path} lines {tf.decl.line_start}-{tf.decl.line_end}): {res.summary}"
    if res.responsibilities:
        line += " Responsibilities: " + "; ".join(res.responsibilities) + "."
    if titles:
        line += " Findings: " + "; ".join(titles) + "."
    return line


def run_domain_reduce(
    facts: ProjectFacts, settings: Settings, runner: StructuredRunner, mo: MapOutput
) -> tuple[dict[str, ReduceResult[DomainResponse]], list[FindingOut]]:
    budget = (
        settings.budget.reduce_input_tokens
        - _tokens(DOMAIN_SYSTEM, settings)
        - _schema_tokens(DomainResponse, settings)
    )
    domain_items: dict[str, list[str]] = {}
    domain_eps: dict[str, str] = {}
    for d in facts.domains():
        fqns = [f for f, t in facts.types.items() if t.domain == d and f in mo.classes]
        if not fqns:
            continue
        domain_items[d] = [_class_line(f, facts, mo) for f in fqns]
        domain_eps[d] = "\n".join(
            f"- {e.http_method} {e.path} -> {e.controller.rsplit('.', 1)[-1]}.{e.handler_method} ({e.file}:{e.line})"
            for e in facts.endpoints
            if facts.types[e.controller].domain == d
        )

    # Hierarchical rounds: while a domain does not fit, summarize groups of its items.
    round_no = 0
    while True:
        oversized = {
            d: items
            for d, items in domain_items.items()
            if _tokens("\n".join(items) + domain_eps[d], settings) > budget
        }
        if not oversized:
            break
        round_no += 1
        requests: list[LLMRequest] = []
        index: dict[str, str] = {}
        for d, items in oversized.items():
            groups = _groups(items, budget, settings)  # partial prompts carry no endpoint list
            for i, g in enumerate(groups, start=1):
                rid = f"domain-partial:{d}:r{round_no}:{i}of{len(groups)}"
                note = (
                    f"NOTE: partial input - group {i} of {len(groups)} of this domain's classes (hierarchical summary)."
                )
                requests.append(
                    LLMRequest(rid, DOMAIN_SYSTEM, render_domain_prompt(d, "\n".join(g), "", note), DomainResponse)
                )
                index[rid] = d
        log.info("domain reduce round %d: %d partial requests", round_no, len(requests))
        partial = runner.run(requests, stage="domain-partial")
        new_items: dict[str, list[str]] = defaultdict(list)
        for rid, d in index.items():
            part = partial[rid]
            if part.parsed is None:
                mo.failed_units.append((rid, part.error or "unknown error"))
                continue
            new_items[d].append(
                f"- Partial summary ({rid}): {part.parsed.summary} "
                f"Responsibilities: {'; '.join(part.parsed.key_responsibilities)}."
            )
        for d in oversized:
            domain_items[d] = new_items.get(d, [])
        if round_no >= 3:
            raise RuntimeError("domain reduce did not converge in 3 rounds")

    requests = [
        LLMRequest(
            f"domain:{d}", DOMAIN_SYSTEM, render_domain_prompt(d, "\n".join(items), domain_eps[d], None), DomainResponse
        )
        for d, items in domain_items.items()
        if items
    ]
    results = runner.run(requests, stage="domain")
    out: dict[str, ReduceResult[DomainResponse]] = {}
    findings: list[FindingOut] = []
    for d in domain_items:
        r = results.get(f"domain:{d}")
        if r is None or r.parsed is None:
            err = (r.error or "unknown error") if r else "no input after partial summaries failed"
            mo.failed_units.append((f"domain:{d}", err))
            out[d] = ReduceResult(None, err)
            continue
        out[d] = ReduceResult(r.parsed, None)
        known = {t.file.path: t.file.line_count for t in facts.types.values() if t.domain == d}
        findings += _reduce_findings(r.parsed.findings, f"domain:{d}", known, mo.dropped_findings)
    return out, findings


# ------------------------------------------------------------------ project reduce


def mask_secrets(yaml_text: str) -> str:
    lines = []
    for line in yaml_text.splitlines():
        key, sep, value = line.partition(":")
        if sep and value.strip() and SECRET_KEY.search(key):
            line = f"{key}: <masked by analyzer: literal value present in file>"
        lines.append(line)
    return "\n".join(lines)


def project_context_files(facts: ProjectFacts) -> list[tuple[str, str]]:
    files = []
    for f in facts.inventory.files:
        if f.category in ("build_config", "app_config") and not f.path.startswith("settings."):
            text = facts.inventory.read(f.path)
            text = mask_secrets(text) if f.category == "app_config" else text
            files.append((f.path, number_lines(list(enumerate(text.splitlines(), start=1)))))
    tests = sorted(f.path for f in facts.inventory.by_category("test_java"))
    if tests:
        files.append(("(test layout)", "Test sources present (supporting context only):\n" + "\n".join(tests)))
    return files


def facts_digest(facts: ProjectFacts, deterministic_findings: list[FindingOut]) -> str:
    by_ctrl: dict[str, list[str]] = defaultdict(list)
    for e in facts.endpoints:
        by_ctrl[e.controller.rsplit(".", 1)[-1]].append(f"{e.http_method} {e.path}")
    kinds: defaultdict[str, int] = defaultdict(int)
    for t in facts.types.values():
        kinds[t.decl.kind] += 1
    notes: defaultdict[str, int] = defaultdict(int)
    for t in facts.types.values():
        for n in t.framework_notes:
            notes[n.split(":")[0].split(" generates")[0]] += 1
    lines = [
        f"Context path: {facts.context_path}",
        f"Types: {dict(kinds)}; domains: {', '.join(facts.domains())}",
        f"Endpoints ({len(facts.endpoints)}):",
    ]
    lines += [f"- {c}: " + ", ".join(eps) for c, eps in sorted(by_ctrl.items())]
    lines.append("Framework usage (types per note): " + "; ".join(f"{k} x{v}" for k, v in sorted(notes.items())))
    if deterministic_findings:
        lines.append("Deterministic checks:")
        lines += [f"- {f.title}: {f.detail}" for f in deterministic_findings]
    return "\n".join(lines)


def run_project_reduce(
    facts: ProjectFacts,
    settings: Settings,
    runner: StructuredRunner,
    mo: MapOutput,
    domains: dict[str, ReduceResult[DomainResponse]],
    deterministic_findings: list[FindingOut],
) -> tuple[ReduceResult[ProjectResponse], list[FindingOut]]:
    readme_path = next((f.path for f in facts.inventory.by_category("readme")), None)
    readme = facts.inventory.read(readme_path) if readme_path else "(no README)"
    readme = number_lines(list(enumerate(readme.splitlines(), start=1)))
    context_files = project_context_files(facts)
    build_context = "\n\n".join(f"### {p}\n```\n{t}\n```" for p, t in context_files)
    digest = facts_digest(facts, deterministic_findings)

    fixed = (
        _tokens(PROJECT_SYSTEM, settings)
        + _schema_tokens(ProjectResponse, settings)
        + _tokens(digest + build_context + readme, settings)
    )
    budget = settings.budget.reduce_input_tokens - fixed
    if budget < 1_000:
        raise RuntimeError(f"project context alone needs {fixed} tokens; raise reduce_input_tokens")

    items = [
        f"### Domain {d}\n{r.response.summary}\nKey responsibilities: {'; '.join(r.response.key_responsibilities)}"
        for d, r in domains.items()
        if r.response is not None
    ]
    round_no = 0
    while _tokens("\n".join(items), settings) > budget:  # hierarchical condensation of domain summaries
        round_no += 1
        groups = _groups(items, budget, settings)
        requests = [
            LLMRequest(
                f"project-partial:r{round_no}:{i}",
                DOMAIN_SYSTEM,
                render_domain_prompt(
                    f"group {i} of {len(groups)}",
                    "\n".join(g),
                    "",
                    "NOTE: condensing several domain summaries into one.",
                ),
                DomainResponse,
            )
            for i, g in enumerate(groups, start=1)
        ]
        partials = runner.run(requests, stage="project-partial")
        items = [f"### Domains group {rid}\n{r.parsed.summary}" for rid, r in partials.items() if r.parsed is not None]
        if round_no >= 3:
            raise RuntimeError("project reduce did not converge in 3 rounds")

    user = render_project_prompt("\n\n".join(items), digest, build_context, readme)
    res = runner.run([LLMRequest("project", PROJECT_SYSTEM, user, ProjectResponse)], stage="project")["project"]
    if res.parsed is None:
        mo.failed_units.append(("project", res.error or "unknown error"))
        return ReduceResult(None, res.error), []
    known = {f.path: len(facts.inventory.read(f.path).splitlines()) for f in facts.inventory.files}
    findings = _reduce_findings(res.parsed.findings, "project", known, mo.dropped_findings)
    return ReduceResult(res.parsed, None), drop_duplicates_of(deterministic_findings, findings, mo.dropped_findings)


def drop_duplicates_of(deterministic: list[FindingOut], llm: list[FindingOut], dropped: list[str]) -> list[FindingOut]:
    """Deterministic findings win: an LLM finding with the same category and evidence file is a duplicate."""
    taken = {(f.category, e.file) for f in deterministic for e in f.evidence}
    kept = []
    for f in llm:
        if any((f.category, e.file) in taken for e in f.evidence):
            dropped.append(f"{f.scope}: '{f.title}' duplicates a deterministic {f.category} finding")
        else:
            kept.append(f)
    return kept
