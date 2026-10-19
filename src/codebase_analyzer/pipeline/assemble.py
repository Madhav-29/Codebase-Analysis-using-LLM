"""Local JSON assembly and the coverage check.

The output is assembled from facts first; LLM content is attached by id. The
final `verify_coverage` asserts every parsed type and member is present exactly
once, so a bug that silently drops a method fails the run loudly.
"""

from __future__ import annotations

from collections import Counter

from codebase_analyzer.analysis.facts import ProjectFacts
from codebase_analyzer.analysis.key_methods import select_key_methods
from codebase_analyzer.analysis.metrics import complexity_band
from codebase_analyzer.config import Settings
from codebase_analyzer.pipeline.map_stage import MapOutput
from codebase_analyzer.pipeline.reduce_stage import ReduceResult
from codebase_analyzer.schema.output import (
    AnalysisOutput,
    ClassOut,
    ComplexityOut,
    ComplexitySummary,
    ComplexMethodRef,
    Coverage,
    DomainOut,
    EndpointOut,
    FailedUnit,
    FindingOut,
    KeyMethod,
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


def _key_methods(facts: ProjectFacts, classes: list[ClassOut], settings: Settings) -> list[KeyMethod]:
    methods = {m.id: m for c in classes for m in c.methods}
    result = []
    for k in select_key_methods(facts, settings):
        m = methods[k.member_id]
        result.append(KeyMethod(id=m.id, class_fqn=k.class_fqn, signature=m.signature, description=m.description,
                                description_source=m.description_source, reasons=list(k.reasons),
                                endpoints=list(k.endpoints),
                                cyclomatic=m.complexity.cyclomatic if m.complexity else None))
    return result


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
        metadata=metadata, project_overview=overview, key_methods=_key_methods(facts, classes, settings),
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
