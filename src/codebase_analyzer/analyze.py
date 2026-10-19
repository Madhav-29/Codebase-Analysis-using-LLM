"""LLM stages: map (class units) -> reduce (domains) -> reduce (project).

The map step sends every planned unit through the structured runner, validates
that each response covers exactly the requested type/member keys, retries
classes missing from pack responses individually, and merges split parts.
Reduce steps are budgeted too: when a domain's or the project's input is larger
than `reduce_input_tokens`, it is summarized hierarchically in groups first.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from pydantic import BaseModel

from .chunking import Plan, Unit, plan_units, single_file_unit
from .config import Settings
from .facts import ProjectFacts
from .llm import LLMRequest, StructuredRunner
from .prompts import (
    CLASS_SYSTEM,
    DOMAIN_SYSTEM,
    PROJECT_SYSTEM,
    render_domain_prompt,
    number_lines,
    render_project_prompt,
)
from .schema import ClassUnitResponse, DomainResponse, Evidence, FindingOut, LLMReducedFinding, ProjectResponse
from .tokens import estimate_tokens

log = logging.getLogger(__name__)

SECRET_KEY = re.compile(r"(password|passwd|secret|token|key|credential)", re.IGNORECASE)  # over-masking is harmless


@dataclass
class ClassResult:
    summary: str
    responsibilities: list[str]
    unit_id: str


@dataclass
class MapOutput:
    plan: Plan
    classes: dict[str, ClassResult] = field(default_factory=dict)  # by type FQN
    descriptions: dict[str, str] = field(default_factory=dict)  # by member id
    findings: list[FindingOut] = field(default_factory=list)
    failed_units: list[tuple[str, str]] = field(default_factory=list)
    dropped_findings: list[str] = field(default_factory=list)
    unit_of_file: dict[str, str] = field(default_factory=dict)  # file -> unit id that analyzed it


@dataclass
class ReduceResult:
    response: BaseModel | None
    error: str | None


# ------------------------------------------------------------------ selection

def select_paths(facts: ProjectFacts, max_classes: int | None) -> list[str]:
    """All main source files, or the first N picked round-robin across domains."""
    by_domain: dict[str, list[str]] = defaultdict(list)
    for jf in sorted(facts.files, key=lambda f: f.path):
        by_domain[facts.types_in_file(jf.path)[0].domain].append(jf.path)
    if max_classes is None:
        return sorted(p for paths in by_domain.values() for p in paths)
    picked: list[str] = []
    queues = [list(v) for _, v in sorted(by_domain.items())]
    while len(picked) < max_classes and any(queues):
        for q in queues:
            if q and len(picked) < max_classes:
                picked.append(q.pop(0))
    return sorted(picked)


# ------------------------------------------------------------------ map

def _unit_validator(unit: Unit, require_all_types: bool):
    def validate(resp: ClassUnitResponse) -> None:
        seen_types = [c.type_key for c in resp.classes]
        unknown = set(seen_types) - set(unit.type_keys)
        dupes = {k for k in seen_types if seen_types.count(k) > 1}
        problems = []
        if unknown:
            problems.append(f"unknown type keys {sorted(unknown)}")
        if dupes:
            problems.append(f"duplicate type keys {sorted(dupes)}")
        missing_types = set(unit.type_keys) - set(seen_types)
        if require_all_types and missing_types:
            problems.append(f"missing type keys {sorted(missing_types)}")
        # members belonging to the types that were returned must all be described
        returned_fqns = {unit.type_keys[k] for k in seen_types if k in unit.type_keys}
        expected_members = {k for k, mid in unit.member_keys.items() if mid.split("#", 1)[0] in returned_fqns}
        got_members = [m.member_key for c in resp.classes for m in c.methods]
        if set(got_members) - set(unit.member_keys):
            problems.append(f"unknown member keys {sorted(set(got_members) - set(unit.member_keys))}")
        if expected_members - set(got_members):
            problems.append(f"missing member keys {sorted(expected_members - set(got_members))}")
        if problems:
            raise ValueError("; ".join(problems))

    return validate


def _class_request(unit: Unit) -> LLMRequest:
    return LLMRequest(unit.id, CLASS_SYSTEM, unit.user_text, ClassUnitResponse,
                      _unit_validator(unit, require_all_types=unit.kind != "pack"))


def run_map(facts: ProjectFacts, settings: Settings, runner: StructuredRunner, paths: list[str]) -> MapOutput:
    plan = plan_units(facts, settings, paths)
    out = MapOutput(plan=plan)
    out.failed_units += [(f"class:{path}", f"planning: {err}") for path, err in plan.failed]
    for path, err in plan.failed:
        log.error("could not plan %s: %s", path, err)

    results = runner.run([_class_request(u) for u in plan.units], stage="class")
    retry_units: list[Unit] = []
    accepted: list[tuple[Unit, ClassUnitResponse]] = []
    for unit in plan.units:
        res = results[unit.id]
        if res.parsed is None:
            out.failed_units.append((unit.id, res.error or "unknown error"))
            continue
        accepted.append((unit, res.parsed))
        if unit.kind == "pack":
            returned = {unit.type_keys[c.type_key] for c in res.parsed.classes}
            for path in unit.files:
                if any(t.decl.fqn not in returned for t in facts.types_in_file(path)):
                    log.warning("%s: %s missing from pack response; retrying individually", unit.id, path)
                    retry_units.append(single_file_unit(path, facts, settings))

    retry_files = {u.files[0] for u in retry_units}
    if retry_units:
        retried = runner.run([_class_request(u) for u in retry_units], stage="class-retry")
        for unit in retry_units:
            res = retried[unit.id]
            if res.parsed is None:
                out.failed_units.append((unit.id, f"missing from pack, individual retry failed: {res.error}"))
            else:
                accepted.append((unit, res.parsed))

    for unit, resp in accepted:
        # a pack's partial answer for a file that was retried individually is discarded
        skip_files = retry_files if unit.kind == "pack" else set()
        _merge_unit(unit, resp, facts, out, skip_files)
    return out


def _merge_unit(unit: Unit, resp: ClassUnitResponse, facts: ProjectFacts, out: MapOutput, skip_files: set[str]) -> None:
    member_text: dict[str, list[str]] = defaultdict(list)
    for cls in resp.classes:
        fqn = unit.type_keys[cls.type_key]
        tf = facts.types[fqn]
        if tf.file.path in skip_files:
            continue
        out.unit_of_file[tf.file.path] = unit.split_parent or unit.id
        existing = out.classes.get(fqn)
        if existing is None:  # split parts: the first part's summary wins, responsibilities are unioned
            out.classes[fqn] = ClassResult(cls.summary, list(cls.responsibilities), unit.split_parent or unit.id)
        else:
            existing.responsibilities += [r for r in cls.responsibilities if r not in existing.responsibilities]
        for m in cls.methods:
            member_text[unit.member_keys[m.member_key]].append(m.description)
        for f in cls.findings:
            if not (1 <= f.line_start <= f.line_end <= tf.file.line_count):
                out.dropped_findings.append(
                    f"{fqn}: '{f.title}' cited lines {f.line_start}-{f.line_end} outside file (1-{tf.file.line_count})")
                continue
            out.findings.append(FindingOut(
                category=f.category, severity=f.severity, title=f.title, detail=f.detail, scope=fqn,
                evidence=[Evidence(file=tf.file.path, line_start=f.line_start, line_end=f.line_end)], source="llm"))
    for member_id, texts in member_text.items():
        if member_id in unit.segmented_members:
            prev = out.descriptions.get(member_id)
            joined = " ".join(f"[{unit.id.rsplit(':', 1)[-1]}] {t}" for t in texts)
            out.descriptions[member_id] = f"{prev} {joined}" if prev else joined
        else:
            out.descriptions[member_id] = texts[0]


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


def _reduce_findings(findings: list[LLMReducedFinding], scope: str, known_files: dict[str, int],
                     dropped: list[str]) -> list[FindingOut]:
    """Keep evidence that cites a file from the input within its real line range (path -> line count)."""
    result = []
    for f in findings:
        evidence = [Evidence(file=e.file, line_start=e.line_start, line_end=e.line_end)
                    for e in f.evidence
                    if e.file in known_files and 1 <= e.line_start <= e.line_end <= known_files[e.file]]
        if f.evidence and not evidence:
            dropped.append(f"{scope}: '{f.title}' cited no file/line range from the input")
            continue
        result.append(FindingOut(category=f.category, severity=f.severity, title=f.title, detail=f.detail,
                                 scope=scope, evidence=evidence, source="llm"))
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


def run_domain_reduce(facts: ProjectFacts, settings: Settings, runner: StructuredRunner,
                      mo: MapOutput) -> tuple[dict[str, ReduceResult], list[FindingOut]]:
    budget = settings.budget.reduce_input_tokens - _tokens(DOMAIN_SYSTEM, settings) - _schema_tokens(DomainResponse, settings)
    domain_items: dict[str, list[str]] = {}
    domain_eps: dict[str, str] = {}
    for d in facts.domains():
        fqns = [f for f, t in facts.types.items() if t.domain == d and f in mo.classes]
        if not fqns:
            continue
        domain_items[d] = [_class_line(f, facts, mo) for f in fqns]
        domain_eps[d] = "\n".join(
            f"- {e.http_method} {e.path} -> {e.controller.rsplit('.', 1)[-1]}.{e.handler_method} ({e.file}:{e.line})"
            for e in facts.endpoints if facts.types[e.controller].domain == d)

    # Hierarchical rounds: while a domain does not fit, summarize groups of its items.
    round_no = 0
    while True:
        oversized = {d: items for d, items in domain_items.items()
                     if _tokens("\n".join(items) + domain_eps[d], settings) > budget}
        if not oversized:
            break
        round_no += 1
        requests, index = [], {}
        for d, items in oversized.items():
            groups = _groups(items, budget, settings)  # partial prompts carry no endpoint list
            for i, g in enumerate(groups, start=1):
                rid = f"domain-partial:{d}:r{round_no}:{i}of{len(groups)}"
                note = f"NOTE: partial input - group {i} of {len(groups)} of this domain's classes (hierarchical summary)."
                requests.append(LLMRequest(rid, DOMAIN_SYSTEM, render_domain_prompt(d, "\n".join(g), "", note), DomainResponse))
                index[rid] = d
        log.info("domain reduce round %d: %d partial requests", round_no, len(requests))
        partial = runner.run(requests, stage="domain-partial")
        new_items: dict[str, list[str]] = defaultdict(list)
        for rid, d in index.items():
            r = partial[rid]
            if r.parsed is None:
                mo.failed_units.append((rid, r.error or "unknown error"))
                continue
            new_items[d].append(f"- Partial summary ({rid}): {r.parsed.summary} "
                                f"Responsibilities: {'; '.join(r.parsed.key_responsibilities)}.")
        for d in oversized:
            domain_items[d] = new_items.get(d, [])
        if round_no >= 3:
            raise RuntimeError("domain reduce did not converge in 3 rounds")

    requests = [LLMRequest(f"domain:{d}", DOMAIN_SYSTEM,
                           render_domain_prompt(d, "\n".join(items), domain_eps[d], None), DomainResponse)
                for d, items in domain_items.items() if items]
    results = runner.run(requests, stage="domain")
    out: dict[str, ReduceResult] = {}
    findings: list[FindingOut] = []
    for d in domain_items:
        r = results.get(f"domain:{d}")
        if r is None or r.parsed is None:
            err = r.error if r else "no input after partial summaries failed"
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
    kinds = defaultdict(int)
    for t in facts.types.values():
        kinds[t.decl.kind] += 1
    notes = defaultdict(int)
    for t in facts.types.values():
        for n in t.framework_notes:
            notes[n.split(":")[0].split(" generates")[0]] += 1
    lines = [f"Context path: {facts.context_path}",
             f"Types: {dict(kinds)}; domains: {', '.join(facts.domains())}",
             f"Endpoints ({len(facts.endpoints)}):"]
    lines += [f"- {c}: " + ", ".join(eps) for c, eps in sorted(by_ctrl.items())]
    lines.append("Framework usage (types per note): " + "; ".join(f"{k} x{v}" for k, v in sorted(notes.items())))
    if deterministic_findings:
        lines.append("Deterministic checks:")
        lines += [f"- {f.title}: {f.detail}" for f in deterministic_findings]
    return "\n".join(lines)


def run_project_reduce(facts: ProjectFacts, settings: Settings, runner: StructuredRunner, mo: MapOutput,
                       domains: dict[str, ReduceResult], deterministic_findings: list[FindingOut]
                       ) -> tuple[ReduceResult, list[FindingOut]]:
    readme_path = next((f.path for f in facts.inventory.by_category("readme")), None)
    readme = facts.inventory.read(readme_path) if readme_path else "(no README)"
    readme = number_lines(list(enumerate(readme.splitlines(), start=1)))
    context_files = project_context_files(facts)
    build_context = "\n\n".join(f"### {p}\n```\n{t}\n```" for p, t in context_files)
    digest = facts_digest(facts, deterministic_findings)

    fixed = (_tokens(PROJECT_SYSTEM, settings) + _schema_tokens(ProjectResponse, settings)
             + _tokens(digest + build_context + readme, settings))
    budget = settings.budget.reduce_input_tokens - fixed
    if budget < 1_000:
        raise RuntimeError(f"project context alone needs {fixed} tokens; raise reduce_input_tokens")

    items = [f"### Domain {d}\n{r.response.summary}\nKey responsibilities: {'; '.join(r.response.key_responsibilities)}"
             for d, r in domains.items() if r.response is not None]
    round_no = 0
    while _tokens("\n".join(items), settings) > budget:  # hierarchical condensation of domain summaries
        round_no += 1
        groups = _groups(items, budget, settings)
        requests = [LLMRequest(f"project-partial:r{round_no}:{i}", DOMAIN_SYSTEM,
                               render_domain_prompt(f"group {i} of {len(groups)}", "\n".join(g), "",
                                                    "NOTE: condensing several domain summaries into one."),
                               DomainResponse) for i, g in enumerate(groups, start=1)]
        res = runner.run(requests, stage="project-partial")
        items = [f"### Domains group {rid}\n{r.parsed.summary}" for rid, r in res.items() if r.parsed is not None]
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
