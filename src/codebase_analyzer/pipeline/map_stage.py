"""Map stage: one structured LLM call per planned unit (class, pack or split part).

Each response must cover exactly the requested type and member keys. Classes
missing from a pack's answer are retried on their own, and split parts are
merged back into one class.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from codebase_analyzer.analysis.facts import ProjectFacts
from codebase_analyzer.config import Settings
from codebase_analyzer.llm.client import LLMRequest, StructuredRunner
from codebase_analyzer.planning.chunking import Plan, Unit, plan_units, single_file_unit
from codebase_analyzer.prompts import CLASS_SYSTEM
from codebase_analyzer.schema.output import Evidence, FindingOut
from codebase_analyzer.schema.responses import ClassUnitResponse

log = logging.getLogger(__name__)


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


def _unit_validator(unit: Unit, require_all_types: bool) -> Callable[[ClassUnitResponse], None]:
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
    return LLMRequest(
        unit.id,
        CLASS_SYSTEM,
        unit.user_text,
        ClassUnitResponse,
        _unit_validator(unit, require_all_types=unit.kind != "pack"),
    )


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


def _unkey(text: str, unit: Unit) -> str:
    """Replace prompt member keys (e.g. 'm8') the model echoed into prose with the method name."""

    def name(m: re.Match[str]) -> str:
        member_id = unit.member_keys.get(m.group(0))
        return f"{member_id.split('#', 1)[1].split('(', 1)[0]}()" if member_id else m.group(0)

    return re.sub(r"\bm\d+\b", name, text)


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
            member_text[unit.member_keys[m.member_key]].append(_unkey(m.description, unit))
        for f in cls.findings:
            if not (1 <= f.line_start <= f.line_end <= tf.file.line_count):
                out.dropped_findings.append(
                    f"{fqn}: '{f.title}' cited lines {f.line_start}-{f.line_end} outside file (1-{tf.file.line_count})"
                )
                continue
            out.findings.append(
                FindingOut(
                    category=f.category,
                    severity=f.severity,
                    title=_unkey(f.title, unit),
                    detail=_unkey(f.detail, unit),
                    scope=fqn,
                    evidence=[Evidence(file=tf.file.path, line_start=f.line_start, line_end=f.line_end)],
                    source="llm",
                )
            )
    for member_id, texts in member_text.items():
        if member_id in unit.segmented_members:
            prev = out.descriptions.get(member_id)
            joined = " ".join(f"[{unit.id.rsplit(':', 1)[-1]}] {t}" for t in texts)
            out.descriptions[member_id] = f"{prev} {joined}" if prev else joined
        else:
            out.descriptions[member_id] = texts[0]
