"""Human-readable Markdown report rendered from analysis.json.

Deterministic: the same JSON always renders the same report, and nothing here
calls an LLM. The JSON stays the source of truth; the report is a reading aid.
"""

from __future__ import annotations

from collections import Counter

from codebase_analyzer.schema.output import AnalysisOutput, FindingOut

SEVERITY_ORDER = ("high", "medium", "low", "info")


def _cell(text: object) -> str:
    """Make text safe for a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _short(fqn: str) -> str:
    return fqn.rsplit(".", 1)[-1]


def method_label(member_id: str) -> str:
    """'pkg.Class#name(pkg.Type)@L12' -> 'Class#name(pkg.Type)'; parameter types may contain dots."""
    owner, _, rest = member_id.partition("#")
    return f"{_short(owner)}#{rest.rsplit('@L', 1)[0]}"


def _evidence(f: FindingOut) -> str:
    return ", ".join(f"`{e.file.rsplit('/', 1)[-1]}:{e.line_start}-{e.line_end}`" for e in f.evidence[:3]) + (
        f" (+{len(f.evidence) - 3} more)" if len(f.evidence) > 3 else ""
    )


def render_report(a: AnalysisOutput) -> str:
    m, o, c, cs = a.metadata, a.project_overview, a.coverage, a.complexity_summary
    repo = (m.repo_url or "repository").rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    out: list[str] = [
        f"# Analysis report: {repo} @ {(m.commit_sha or 'unknown')[:7]}",
        "",
        f"Generated from `analysis.json` (schema {m.schema_version}, prompt {m.prompt_version}). "
        f"Model: `{m.provider}/{m.model}`"
        + (f" (served: {', '.join(m.served_models)})" if m.served_models else "")
        + ".",
        "",
        "## Overview",
        "",
        o.purpose or "_Not available._",
        "",
    ]
    out += [f"- {item}" for item in o.functionality]
    if o.architecture:
        out += ["", f"**Architecture.** {o.architecture}"]
    if o.technologies:
        out += ["", "**Technologies.** " + ", ".join(o.technologies)]

    u = m.usage
    out += [
        "",
        "## Run",
        "",
        "| | |",
        "|---|---|",
        f"| LLM calls | {u.llm_calls} live, {u.cache_hits} replayed from cache |",
        f"| Tokens (live) | {u.input_tokens:,} in / {u.output_tokens:,} out ({u.reasoning_tokens:,} reasoning) |",
        f"| Tokens (cached responses, original cost) | {u.cached_input_tokens:,} in / {u.cached_output_tokens:,} out |",
        "| Cost | token counts only; prices not included |",
        "",
        "## Coverage",
        "",
        f"- Files analyzed: {c.files_analyzed} of {c.files_discovered} discovered ({len(c.files_skipped)} skipped, with reasons in the JSON)",
        f"- Classes summarized: {c.classes_summarized} / {c.classes_total}",
        f"- Methods described: {c.methods_described} / {c.methods_total} "
        + "("
        + ", ".join(f"{k}: {v}" for k, v in sorted(c.methods_described_by_source.items()))
        + ")",
        f"- LLM units: {c.units_total} ({len(c.packs)} packs, {len(c.units_split)} splits, {len(c.units_failed)} failed)",
        f"- Findings rejected by checks: {len(c.findings_dropped)}",
    ]

    out += ["", "## Domains", "", "| Domain | Classes | Endpoints | Summary |", "|---|---|---|---|"]
    out += [
        f"| {d.name} | {len(d.classes)} | {len(d.endpoints)} | {_cell(d.summary or '_not summarized_')} |"
        for d in a.domains
    ]

    out += ["", f"## Endpoints ({len(a.endpoints)})", "", "| Method | Path | Handler | Location |", "|---|---|---|---|"]
    out += [
        f"| {e.http_method} | `{e.path}` | `{_short(e.controller)}.{e.handler_method}` "
        f"| `{e.file.rsplit('/', 1)[-1]}:{e.line}` |"
        for e in a.endpoints
    ]

    reasons = Counter(r for k in a.key_methods for r in k.reasons)
    out += [
        "",
        "## Key methods",
        "",
        f"{len(a.key_methods)} methods: "
        + ", ".join(f"{n} {r.replace('_', ' ')}" for r, n in reasons.most_common())
        + ". The full list with signatures and descriptions is `key_methods` in the JSON.",
        "",
        "## Complexity",
        "",
        f"Average cyclomatic complexity {cs.avg_cyclomatic} (max {cs.max_cyclomatic}); average NLOC {cs.avg_nloc} "
        f"(max {cs.max_nloc}). Bands: "
        + ", ".join(f"{b} {cs.band_counts.get(b, 0)}" for b in ("low", "moderate", "high", "very_high"))
        + " (CC thresholds: "
        + ", ".join(f"{band.replace('_', ' ')} >= {value}" for band, value in cs.thresholds.items())
        + ").",
        "",
        "Most complex business logic:",
        "",
        "| CC | NLOC | Method | Location |",
        "|---|---|---|---|",
    ]
    out += [
        f"| {r.cyclomatic} | {r.nloc} | `{_cell(method_label(r.id))}` | `{r.file.rsplit('/', 1)[-1]}:{r.line_start}` |"
        for r in cs.top_complex_methods
    ]
    if cs.top_complex_trivial_methods:
        top = cs.top_complex_trivial_methods[0]
        out += [
            "",
            f"Boilerplate ranked separately: {len(cs.top_complex_trivial_methods)} listed, the highest "
            f"`{method_label(top.id)}` at CC {top.cyclomatic}.",
        ]

    by_severity = Counter(f.severity for f in a.findings)
    out += [
        "",
        f"## Findings ({len(a.findings)})",
        "",
        ", ".join(f"{s}: {by_severity.get(s, 0)}" for s in SEVERITY_ORDER)
        + ". LLM findings are review leads, not verified bugs; see the evaluation for measured precision.",
    ]
    for severity in SEVERITY_ORDER:
        group = sorted((f for f in a.findings if f.severity == severity), key=lambda f: (f.scope, f.title))
        if not group:
            continue
        out += ["", f"### {severity.capitalize()} ({len(group)})", ""]
        out += [
            f"- **{_cell(f.title)}** ({f.category}, {f.source}) in `{_short(f.scope)}`, {_evidence(f)}. {_cell(f.detail)}"
            for f in group
        ]
    return "\n".join(out) + "\n"
