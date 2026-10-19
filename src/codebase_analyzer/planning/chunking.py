"""Build LLM analysis units that fit the token budget.

Unit kinds:
* class - one source file (its top-level type plus nested types) per call, with
  bounded supporting context (signatures of referenced project types).
* pack  - up to N small, simple files from the same domain and package in one
  call. Large or complex files are never packed.
* split - a file too large for one call, split at member boundaries. Every part
  repeats the file skeleton (package, imports, type headers, fields and a
  placeholder line per member) so line numbers and structure stay visible.
  A single member larger than the budget is cut into labeled line segments.

Every unit's estimated size (system prompt + schema + user prompt) is checked
against `TokenBudget.input_limit`; nothing is truncated silently.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from codebase_analyzer.analysis.facts import MemberFact, ProjectFacts, TypeFact
from codebase_analyzer.config import Settings
from codebase_analyzer.planning.tokens import estimate_tokens
from codebase_analyzer.prompts import (
    CLASS_SYSTEM,
    ClassPrompt,
    CodeBlock,
    MemberRequest,
    TypeRequest,
    number_lines,
    render_class_prompt,
)
from codebase_analyzer.schema.responses import ClassUnitResponse

MIN_PART_TOKENS = 400  # below this, a split part cannot hold meaningful code


class PlanningError(Exception):
    """A file cannot be fitted into the budget even after splitting."""


@dataclass
class Unit:
    id: str
    kind: Literal["class", "pack", "split"]
    files: list[str]
    type_keys: dict[str, str]  # key shown to the LLM -> type FQN
    member_keys: dict[str, str]  # key shown to the LLM -> member id
    prompt: ClassPrompt
    user_text: str
    est_tokens: int
    split_parent: str | None = None  # logical file unit id, for split parts
    segmented_members: list[str] = field(default_factory=list)  # member ids cut into segments


@dataclass
class Plan:
    units: list[Unit]
    failed: list[tuple[str, str]]  # (file, error) for files that could not be planned
    context_dropped: list[str]  # files whose supporting context did not fit


def fixed_overhead(settings: Settings) -> int:
    schema = json.dumps(ClassUnitResponse.model_json_schema())
    return estimate_tokens(CLASS_SYSTEM, settings.budget.chars_per_token) + estimate_tokens(
        schema, settings.budget.chars_per_token
    )


def _type_key(fact: TypeFact, facts: ProjectFacts) -> str:
    pkg = fact.file.package
    return fact.decl.fqn[len(pkg) + 1 :] if pkg else fact.decl.fqn


# ------------------------------------------------------------ supporting context


def _skeleton(fact: TypeFact, facts: ProjectFacts) -> str:
    d = fact.decl
    anns = " ".join(a.text for a in d.annotations if a.name not in ("Override",))
    lines = [f"// {d.fqn} ({d.kind}) {anns}".rstrip()]
    if d.extends or d.implements:
        lines.append(f"//   extends {', '.join(d.extends) or '-'}; implements {', '.join(d.implements) or '-'}")
    if d.enum_constants:
        lines.append(f"//   constants: {', '.join(d.enum_constants)}")
    if d.fields:
        lines.append("//   fields: " + ", ".join(f"{f.type} {f.name}" for f in d.fields))
    for m in d.members:
        if "private" not in m.modifiers:
            lines.append(f"//   {m.signature}")
    for n in d.nested:
        lines.append(_skeleton(facts.types[n.fqn], facts))
    return "\n".join(lines)


def build_context(path: str, facts: ProjectFacts, settings: Settings) -> tuple[str | None, list[str]]:
    """Signatures of project types referenced from `path`, bounded by the context budget."""
    own = {t.decl.name for t in facts.types_in_file(path)}
    top_level = {t.decl.name: t for t in facts.types.values() if t.top_level == t.decl.fqn}
    source = next(f.source for f in facts.files if f.path == path)
    counts = Counter(
        tok for tok in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", source) if tok in top_level and tok not in own
    )

    cap = settings.budget.supporting_context_tokens
    used = 0
    shown: list[str] = []
    omitted: list[str] = []
    for name, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        text = _skeleton(top_level[name], facts)
        cost = estimate_tokens(text, settings.budget.chars_per_token)
        if used + cost <= cap:
            shown.append(text)
            used += cost
        else:
            omitted.append(name)
    return ("\n".join(shown) if shown else None), omitted


# ------------------------------------------------------------ blocks


def _requests(
    types: list[TypeFact], facts: ProjectFacts, members: list[tuple[TypeFact, MemberFact]], counter: list[int]
) -> tuple[list[TypeRequest], list[MemberRequest], dict[str, str], dict[str, str]]:
    type_reqs = [TypeRequest(_type_key(t, facts), t.decl.kind, t.decl.line_start, t.decl.line_end) for t in types]
    type_keys = {_type_key(t, facts): t.decl.fqn for t in types}
    member_reqs: list[MemberRequest] = []
    member_keys: dict[str, str] = {}
    for t, mf in members:
        counter[0] += 1
        key = f"m{counter[0]}"
        member_keys[key] = mf.id
        m = mf.member
        member_reqs.append(MemberRequest(key, _type_key(t, facts), m.signature, m.line_start, m.line_end))
    return type_reqs, member_reqs, type_keys, member_keys


_DETERMINISTIC_NOTE = (
    "Members not listed above are boilerplate or described elsewhere; "
    "they are shown for context and may be cited in findings."
)


def _file_block(path: str, facts: ProjectFacts, counter: list[int]) -> tuple[CodeBlock, dict[str, str], dict[str, str]]:
    types = facts.types_in_file(path)
    jf = types[0].file
    lines = list(enumerate(jf.source.splitlines(), start=1))
    llm_members = [(t, mf) for t in types for mf in t.members if mf.needs_llm]
    treq, mreq, tkeys, mkeys = _requests(types, facts, llm_members, counter)
    block = CodeBlock(path, types[0].domain, number_lines(lines), treq, mreq, deterministic_note=_DETERMINISTIC_NOTE)
    return block, tkeys, mkeys


def _estimate(prompt: ClassPrompt, overhead: int, settings: Settings) -> tuple[str, int]:
    text = render_class_prompt(prompt)
    return text, overhead + estimate_tokens(text, settings.budget.chars_per_token)


def _file_tokens(path: str, facts: ProjectFacts, settings: Settings) -> int:
    source = next(f.source for f in facts.files if f.path == path)
    return estimate_tokens(source, settings.budget.chars_per_token)


def _max_cyclomatic(path: str, facts: ProjectFacts) -> int:
    values = [mf.complexity.cyclomatic for t in facts.types_in_file(path) for mf in t.members if mf.complexity]
    return max(values, default=0)


# ------------------------------------------------------------ splitting


def _split_file(path: str, facts: ProjectFacts, settings: Settings, overhead: int) -> list[Unit]:
    types = facts.types_in_file(path)
    jf = types[0].file
    src_lines = jf.source.splitlines()
    all_members = sorted(((t, mf) for t in types for mf in t.members), key=lambda x: x[1].member.line_start)
    member_lines = {n for _, mf in all_members for n in range(mf.member.line_start, mf.member.line_end + 1)}

    # Skeleton: every line outside member ranges, plus one placeholder per member.
    skeleton: list[tuple[int, str]] = []
    for n, text in enumerate(src_lines, start=1):
        starts = [mf for _, mf in all_members if mf.member.line_start == n]
        if starts:
            m = starts[0].member
            skeleton.append((n, f"    // [member, lines {m.line_start}-{m.line_end}] {m.signature}"))
        elif n not in member_lines:
            skeleton.append((n, text))
    skeleton_code = number_lines(skeleton)

    context, omitted = build_context(path, facts, settings)
    limit = settings.budget.input_limit

    def part_prompt(
        code: str, members: list[tuple[TypeFact, MemberFact]], note: str, counter: list[int], ctx: str | None
    ) -> tuple[ClassPrompt, dict[str, str], dict[str, str], list[MemberRequest]]:
        treq, mreq, tkeys, mkeys = _requests(types, facts, members, counter)
        block = CodeBlock(
            path, types[0].domain, code, treq, mreq, part_note=note, deterministic_note=_DETERMINISTIC_NOTE
        )
        return ClassPrompt([block], ctx, omitted if ctx else []), tkeys, mkeys, mreq

    def note_for(i: int, n: int) -> str:
        return (
            f"NOTE: this file was split into {n} parts to fit the token budget; this is part {i}. "
            "The skeleton above the '...' shows every member as a placeholder line; full bodies follow it."
        )

    probe_code = skeleton_code + "\n    ...\n"
    probe, _, _, _ = part_prompt(probe_code, [], note_for(99, 99), [0], context)
    base_cost = _estimate(probe, overhead, settings)[1]
    if limit - base_cost < MIN_PART_TOKENS and context:
        context, omitted = None, []
        probe, _, _, _ = part_prompt(probe_code, [], note_for(99, 99), [0], None)
        base_cost = _estimate(probe, overhead, settings)[1]
    avail = limit - base_cost
    if avail < MIN_PART_TOKENS:
        raise PlanningError(f"file skeleton alone needs {base_cost} tokens; budget input limit is {limit}")

    # Greedy grouping of whole members; oversized members become line segments.
    def cost(lines: list[tuple[int, str]], mf: MemberFact | None = None) -> int:
        # code lines + the member's request line in the prompt (if it is requested)
        request = ""
        if mf is not None and mf.needs_llm:
            request = f"- m999 in X: `{mf.member.signature}` (lines 9999-9999) [segment 99/99, lines 9999-9999: ...]"
        return estimate_tokens(number_lines(lines) + "\n" + request, settings.budget.chars_per_token) + 4

    parts: list[list[tuple[TypeFact, MemberFact, list[tuple[int, str]], str | None]]] = []
    current: list[tuple[TypeFact, MemberFact, list[tuple[int, str]], str | None]] = []
    current_cost = 0
    for t, mf in all_members:
        m = mf.member
        body_lines = [(n, src_lines[n - 1]) for n in range(m.line_start, m.line_end + 1)]
        c = cost(body_lines, mf)
        if c > avail:
            if current:
                parts.append(current)
                current, current_cost = [], 0
            segments: list[list[tuple[int, str]]] = [[]]
            for line in body_lines:
                if segments[-1] and cost(segments[-1] + [line], mf) > avail:
                    segments.append([])
                segments[-1].append(line)
            for i, seg in enumerate(segments, start=1):
                label = f"segment {i}/{len(segments)}, lines {seg[0][0]}-{seg[-1][0]}"
                parts.append([(t, mf, seg, label)])
            continue
        if current_cost + c > avail:
            parts.append(current)
            current, current_cost = [], 0
        current.append((t, mf, body_lines, None))
        current_cost += c
    if current:
        parts.append(current)

    units: list[Unit] = []
    parent = f"class:{path}"
    segmented = sorted({mf.id for p in parts for _, mf, _, label in p if label})
    for i, part in enumerate(parts, start=1):
        code_lines = [ln for _, _, lines, _ in part for ln in lines]
        code = skeleton_code + "\n    ...\n" + number_lines(code_lines)
        note = note_for(i, len(parts))
        llm_members = [(t, mf) for t, mf, _, _ in part if mf.needs_llm]
        prompt, tkeys, mkeys, mreq = part_prompt(code, llm_members, note, [0], context)
        for req, (_, _, _, segment_label) in zip(mreq, [p for p in part if p[1].needs_llm], strict=True):
            req.segment = segment_label
        text, est = _estimate(prompt, overhead, settings)
        if est > limit:
            raise PlanningError(f"split part {i} estimated at {est} tokens exceeds limit {limit}")
        units.append(
            Unit(
                f"split:{path}:part{i}of{len(parts)}",
                "split",
                [path],
                tkeys,
                mkeys,
                prompt,
                text,
                est,
                split_parent=parent,
                segmented_members=segmented,
            )
        )
    return units


# ------------------------------------------------------------ planning


def plan_units(facts: ProjectFacts, settings: Settings, paths: list[str]) -> Plan:
    overhead = fixed_overhead(settings)
    limit = settings.budget.input_limit
    pk = settings.packing
    units: list[Unit] = []
    failed: list[tuple[str, str]] = []
    context_dropped: list[str] = []
    packable: dict[tuple[str, str], list[str]] = {}

    for path in paths:
        types = facts.types_in_file(path)
        context, omitted = build_context(path, facts, settings)
        block, tkeys, mkeys = _file_block(path, facts, [0])
        prompt = ClassPrompt([block], context, omitted)
        text, est = _estimate(prompt, overhead, settings)
        if est > limit and context:
            prompt = ClassPrompt([block])
            text, est = _estimate(prompt, overhead, settings)
            if est <= limit:
                context_dropped.append(path)
        if est > limit:
            try:
                units.extend(_split_file(path, facts, settings, overhead))
            except PlanningError as exc:
                failed.append((path, str(exc)))
            continue
        small = _file_tokens(path, facts, settings) <= pk.max_class_tokens
        simple = _max_cyclomatic(path, facts) <= pk.max_class_cyclomatic
        if pk.enabled and small and simple:
            packable.setdefault((types[0].domain, types[0].file.package), []).append(path)
        else:
            units.append(Unit(f"class:{path}", "class", [path], tkeys, mkeys, prompt, text, est))

    for (domain, package), group in packable.items():
        units.extend(_pack_group(domain, package, group, facts, settings, overhead))
    units.sort(key=lambda u: u.files[0])
    return Plan(units, failed, context_dropped)


def _pack_group(
    domain: str, package: str, paths: list[str], facts: ProjectFacts, settings: Settings, overhead: int
) -> list[Unit]:
    limit = settings.budget.input_limit
    max_classes = settings.packing.max_classes
    packs: list[list[str]] = []
    current: list[str] = []
    for path in paths:
        candidate = [*current, path]
        counter = [0]
        blocks = [_file_block(p, facts, counter)[0] for p in candidate]
        _, est = _estimate(ClassPrompt(blocks), overhead, settings)
        if current and (len(candidate) > max_classes or est > limit):
            packs.append(current)
            current = [path]
        else:
            current = candidate
    if current:
        packs.append(current)

    units: list[Unit] = []
    for i, group in enumerate(packs, start=1):
        if len(group) == 1:
            path = group[0]
            context, omitted = build_context(path, facts, settings)
            block, tkeys, mkeys = _file_block(path, facts, [0])
            prompt = ClassPrompt([block], context, omitted)
            text, est = _estimate(prompt, overhead, settings)
            if est > limit:
                prompt = ClassPrompt([block])
                text, est = _estimate(prompt, overhead, settings)
            units.append(Unit(f"class:{path}", "class", [path], tkeys, mkeys, prompt, text, est))
            continue
        counter = [0]
        blocks, tkeys, mkeys = [], {}, {}
        for path in group:
            block, tk, mk = _file_block(path, facts, counter)
            blocks.append(block)
            tkeys.update(tk)
            mkeys.update(mk)
        prompt = ClassPrompt(blocks)
        text, est = _estimate(prompt, overhead, settings)
        units.append(Unit(f"pack:{domain}:{package}:{i}", "pack", group, tkeys, mkeys, prompt, text, est))
    return units


def single_file_unit(path: str, facts: ProjectFacts, settings: Settings) -> Unit:
    """A standalone class unit for one file, used to retry a class missing from a pack response."""
    block, tkeys, mkeys = _file_block(path, facts, [0])
    prompt = ClassPrompt([block])
    text, est = _estimate(prompt, fixed_overhead(settings), settings)
    return Unit(f"class:{path}", "class", [path], tkeys, mkeys, prompt, text, est)
