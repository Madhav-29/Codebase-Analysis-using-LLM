"""Local JSON assembly, deterministic findings, and the coverage check.

The output is assembled from facts first; LLM content is attached by id. The
final `verify_coverage` asserts every parsed type and member is present exactly
once, so a bug that silently drops a method fails the run loudly.
"""

from __future__ import annotations

import re
from collections import Counter

from .analyze import MapOutput, ReduceResult
from .config import Settings
from .facts import ProjectFacts
from .metrics import complexity_band
from .schema import (
    AnalysisOutput,
    ClassOut,
    ComplexityOut,
    ComplexitySummary,
    ComplexMethodRef,
    Coverage,
    DomainOut,
    EndpointOut,
    Evidence,
    FailedUnit,
    FindingOut,
    Metadata,
    MethodOut,
    PackRecord,
    ParameterOut,
    ProjectOverview,
    SkippedFile,
    SplitRecord,
)


class CoverageError(AssertionError):
    pass


# ------------------------------------------------------------------ deterministic findings

def deterministic_findings(facts: ProjectFacts, settings: Settings) -> list[FindingOut]:
    findings: list[FindingOut] = []

    # 1. Methods at or above the "high" cyclomatic threshold; boilerplate (e.g. hand-written equals) reported apart.
    high = settings.complexity_thresholds["high"]
    complex_members = [(t, mf) for t in facts.types.values() for mf in t.members
                       if mf.complexity and mf.complexity.cyclomatic >= high]
    for trivial in (False, True):
        group = [(t, mf) for t, mf in complex_members if bool(mf.deterministic and mf.deterministic.trivial) == trivial]
        if not group:
            continue
        names = ", ".join(f"{t.decl.name}.{mf.member.name} (CC {mf.complexity.cyclomatic})" for t, mf in group)
        findings.append(FindingOut(
            category="maintainability", severity="info" if trivial else "low",
            title=(f"{len(group)} boilerplate method(s) with cyclomatic complexity >= {high}" if trivial
                   else f"{len(group)} method(s) with cyclomatic complexity >= {high}"),
            detail=f"Measured by lizard: {names}." + (
                " These are hand-written equals/hashCode-style members classified as boilerplate, not business logic."
                if trivial else ""),
            scope="project",
            evidence=[Evidence(file=t.file.path, line_start=mf.member.line_start, line_end=mf.member.line_end)
                      for t, mf in group],
            source="deterministic"))

    # 2. README endpoint claims vs. resolved controller mappings.
    readme = next((f for f in facts.inventory.by_category("readme")), None)
    if readme and facts.endpoints:
        text = facts.inventory.read(readme.path)
        base = facts.context_path.rstrip("/")
        # evidence: the README lines mentioning the API base path (its endpoint list), else the whole file
        cited = [n for n, line in enumerate(text.splitlines(), start=1) if base and base in line]
        readme_evidence = [Evidence(file=readme.path, line_start=min(cited, default=1),
                                    line_end=max(cited, default=text.count("\n") + 1))]
        claimed = {m.group(1) for m in re.finditer(re.escape(base) + r"/([A-Za-z0-9_-]+)", text)} if base else set()
        resources: dict[str, set[str]] = {}
        for e in facts.endpoints:
            seg = e.path[len(base):].strip("/").split("/", 1)[0]
            resources.setdefault(seg, set()).add(e.http_method)
        provided_by_framework = {"actuator"}  # Spring Boot Actuator, not a controller
        not_in_code = sorted(claimed - set(resources) - provided_by_framework)
        not_in_readme = sorted(set(resources) - claimed)
        if not_in_code or not_in_readme:
            detail = []
            if not_in_code:
                detail.append(f"README lists {', '.join(base + '/' + r for r in not_in_code)} but no controller maps them.")
            if not_in_readme:
                detail.append(f"Controllers map {', '.join(base + '/' + r for r in not_in_readme)} which the README does not list.")
            findings.append(FindingOut(
                category="documentation_mismatch", severity="low", title="README endpoint list differs from controller mappings",
                detail=" ".join(detail), scope="project",
                evidence=readme_evidence, source="deterministic"))
        if "CRUD" in text:
            crud = {"GET", "POST", "PUT", "DELETE"}
            partial = {r: sorted(crud - verbs) for r, verbs in resources.items() if crud - verbs and r in claimed}
            if partial:
                findings.append(FindingOut(
                    category="documentation_mismatch", severity="info",
                    title="README claims CRUD support; some listed resources lack HTTP verbs",
                    detail="Missing verbs per resource (from mappings): "
                           + "; ".join(f"{base}/{r}: {', '.join(v)}" for r, v in sorted(partial.items())) + ".",
                    scope="project",
                    evidence=readme_evidence,
                    source="deterministic"))
    return findings


# ------------------------------------------------------------------ assembly

def _complexity_summary(facts: ProjectFacts, settings: Settings) -> ComplexitySummary:
    th = settings.complexity_thresholds
    measured = [(t, mf) for t in facts.types.values() for mf in t.members if mf.complexity]
    ccs = [mf.complexity.cyclomatic for _, mf in measured]
    nlocs = [mf.complexity.nloc for _, mf in measured]

    def ref(t, mf) -> ComplexMethodRef:
        return ComplexMethodRef(id=mf.id, cyclomatic=mf.complexity.cyclomatic, nloc=mf.complexity.nloc,
                                file=t.file.path, line_start=mf.member.line_start)

    def ranked(trivial: bool):
        sel = [(t, mf) for t, mf in measured if bool(mf.deterministic and mf.deterministic.trivial) == trivial]
        sel.sort(key=lambda x: (-x[1].complexity.cyclomatic, -x[1].complexity.nloc, x[1].id))
        return [ref(t, mf) for t, mf in sel[: settings.top_complex_methods]]

    return ComplexitySummary(
        methods_total=sum(len(t.members) for t in facts.types.values()),
        methods_with_metrics=len(measured),
        avg_cyclomatic=round(sum(ccs) / len(ccs), 2) if ccs else 0.0,
        avg_nloc=round(sum(nlocs) / len(nlocs), 2) if nlocs else 0.0,
        max_cyclomatic=max(ccs, default=0),
        max_nloc=max(nlocs, default=0),
        band_counts=dict(Counter(complexity_band(c, th) for c in ccs)),
        top_complex_methods=ranked(trivial=False),
        top_complex_trivial_methods=ranked(trivial=True),
        thresholds=th,
        notes=[
            "Cyclomatic complexity, NLOC and token count are computed by lizard per member; parameter count comes from the parser.",
            "Abstract declarations have no body and no metrics.",
            "Bands by cyclomatic complexity (CC): low if CC < moderate, moderate if CC < high, high if CC < very_high, else very_high (see thresholds).",
            "top_complex_trivial_methods lists boilerplate (mostly hand-written equals/hashCode) separately so it does not crowd out business logic.",
            "Methods of anonymous classes inside a member are not folded into that member's complexity.",
        ],
    )


def build_output(*, facts: ProjectFacts, settings: Settings, selected: list[str], mo: MapOutput,
                 domains: dict[str, ReduceResult], project: ReduceResult, findings: list[FindingOut],
                 metadata: Metadata) -> AnalysisOutput:
    selected_set = set(selected)
    th = settings.complexity_thresholds
    eps_by_ctrl: dict[str, list[str]] = {}
    for e in facts.endpoints:
        eps_by_ctrl.setdefault(e.controller, []).append(f"{e.http_method} {e.path}")

    classes: list[ClassOut] = []
    source_counts: Counter = Counter()
    undescribed: list[str] = []
    for fqn, tf in facts.types.items():
        analyzed = tf.file.path in selected_set
        res = mo.classes.get(fqn)
        methods = []
        for mf in tf.members:
            m = mf.member
            if mf.deterministic:
                desc, src = mf.deterministic.description, "deterministic"
            elif mf.id in mo.descriptions:
                desc, src = mo.descriptions[mf.id], "llm"
            else:
                desc, src = None, ("failed" if analyzed else "not_analyzed")
                undescribed.append(mf.id)
            source_counts[src] += 1
            c = mf.complexity
            methods.append(MethodOut(
                id=mf.id, name=m.name, kind=m.kind, signature=m.signature, modifiers=m.modifiers,
                annotations=[a.text for a in m.annotations],
                parameters=[ParameterOut(name=p.name, type=p.type + ("..." if p.varargs else ""),
                                         annotations=[a.text for a in p.annotations]) for p in m.parameters],
                return_type=m.return_type, line_start=m.line_start, line_end=m.line_end, has_body=m.body is not None,
                trivial=bool(mf.deterministic and mf.deterministic.trivial), description=desc, description_source=src,
                complexity=ComplexityOut(cyclomatic=c.cyclomatic, nloc=c.nloc, parameter_count=c.parameter_count,
                                         token_count=c.token_count, band=complexity_band(c.cyclomatic, th)) if c else None,
            ))
        d = tf.decl
        classes.append(ClassOut(
            fqn=fqn, kind=d.kind, domain=tf.domain, file=tf.file.path, line_start=d.line_start, line_end=d.line_end,
            modifiers=d.modifiers, annotations=[a.text for a in d.annotations], extends=d.extends,
            implements=d.implements, fields=[f"{f.type} {f.name}" for f in d.fields], enum_constants=d.enum_constants,
            framework_notes=tf.framework_notes,
            summary=res.summary if res else None, responsibilities=res.responsibilities if res else [],
            summary_source="llm" if res else ("failed" if analyzed else "not_analyzed"),
            analysis_unit=mo.unit_of_file.get(tf.file.path), methods=methods,
        ))

    domain_out = []
    for name in facts.domains():
        r = domains.get(name)
        fqns = [c.fqn for c in classes if c.domain == name]
        domain_out.append(DomainOut(
            name=name, summary=r.response.summary if r and r.response else None,
            key_responsibilities=r.response.key_responsibilities if r and r.response else [],
            classes=fqns, endpoints=[ep for f in fqns for ep in eps_by_ctrl.get(f, [])],
            status="ok" if r and r.response else ("failed" if r else "not_run"),
        ))

    p = project.response
    overview = ProjectOverview(
        purpose=p.purpose if p else None, functionality=p.functionality if p else [],
        architecture=p.architecture if p else None, technologies=p.technologies if p else [],
        cross_cutting_concerns=p.cross_cutting_concerns if p else [],
        status="ok" if p else ("failed" if project.error else "not_run"),
    )

    plan = mo.plan
    split_parents: dict[str, list] = {}
    for u in plan.units:
        if u.kind == "split":
            split_parents.setdefault(u.split_parent, []).append(u)
    skipped = [SkippedFile(file=f, reason=r) for f, r in facts.inventory.skipped]
    skipped += [SkippedFile(file=jf.path, reason="not selected (--max-classes)")
                for jf in facts.files if jf.path not in selected_set]
    coverage = Coverage(
        files_discovered=len(facts.inventory.files) + sum(1 for _, r in facts.inventory.skipped if r == "binary"),
        files_analyzed=len({c.file for c in classes if c.summary_source == "llm"}),
        files_skipped=skipped,
        classes_total=len(classes),
        classes_summarized=sum(1 for c in classes if c.summary_source == "llm"),
        methods_total=sum(len(c.methods) for c in classes),
        methods_described=sum(1 for c in classes for m in c.methods if m.description is not None),
        methods_described_by_source=dict(source_counts),
        methods_undescribed=undescribed,
        units_total=len(plan.units),
        packs=[PackRecord(unit=u.id, classes=list(u.type_keys.values()), est_tokens=u.est_tokens)
                      for u in plan.units if u.kind == "pack"],
        units_split=[SplitRecord(unit=parent, file=parts[0].files[0], parts=len(parts),
                                 reason=f"file exceeds input budget of {settings.budget.input_limit} tokens",
                                 segmented_members=parts[0].segmented_members)
                     for parent, parts in split_parents.items()],
        units_failed=[FailedUnit(unit=u, error=e) for u, e in mo.failed_units],
        findings_dropped=mo.dropped_findings,
    )

    output = AnalysisOutput(
        metadata=metadata, project_overview=overview,
        endpoints=[EndpointOut(http_method=e.http_method, path=e.path, controller=e.controller,
                               handler_method=e.handler_method, file=e.file, line=e.line,
                               security=list(e.security), unresolved=e.unresolved) for e in facts.endpoints],
        domains=domain_out, classes=classes, complexity_summary=_complexity_summary(facts, settings),
        findings=findings, coverage=coverage,
    )
    verify_coverage(output, facts)
    return output


def verify_coverage(output: AnalysisOutput, facts: ProjectFacts) -> None:
    """Fail loudly if any parsed type or member is missing or duplicated in the output."""
    expected_types = set(facts.types)
    got_types = [c.fqn for c in output.classes]
    expected_members = Counter(mf.id for mf in facts.members())
    got_members = Counter(m.id for c in output.classes for m in c.methods)
    problems = []
    if set(got_types) != expected_types or len(got_types) != len(set(got_types)):
        problems.append(f"types missing: {sorted(expected_types - set(got_types))[:10]}, "
                        f"duplicated: {[t for t, n in Counter(got_types).items() if n > 1][:10]}")
    if got_members != expected_members:
        missing = sorted((expected_members - got_members).elements())
        extra = sorted((got_members - expected_members).elements())
        problems.append(f"members missing: {missing[:10]} ({len(missing)}), unexpected/duplicated: {extra[:10]} ({len(extra)})")
    undescribed = {m.id for c in output.classes for m in c.methods if m.description is None}
    if undescribed != set(output.coverage.methods_undescribed):
        problems.append("methods without description are not all listed in coverage.methods_undescribed")
    if problems:
        raise CoverageError("; ".join(problems))
