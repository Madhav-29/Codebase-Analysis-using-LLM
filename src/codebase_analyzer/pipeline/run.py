"""The one end-to-end path: facts -> map -> reduce -> assembled, verified output.

The CLI and the integration tests both call `run_analysis`, so what is tested is
what runs. The caller owns the runner, so usage is available even when a run
stops early (quota, auth).
"""

from __future__ import annotations

from datetime import UTC, datetime

from codebase_analyzer.analysis.checks import deterministic_findings
from codebase_analyzer.analysis.facts import ProjectFacts
from codebase_analyzer.config import Settings
from codebase_analyzer.llm.client import StructuredRunner
from codebase_analyzer.pipeline.assemble import build_output
from codebase_analyzer.pipeline.map_stage import run_map
from codebase_analyzer.pipeline.reduce_stage import run_domain_reduce, run_project_reduce
from codebase_analyzer.schema.output import AnalysisOutput, Metadata, Usage


def run_analysis(
    facts: ProjectFacts,
    settings: Settings,
    runner: StructuredRunner,
    *,
    selected: list[str],
    repo_url: str | None = None,
    commit_sha: str | None = None,
    max_classes: int | None = None,
) -> AnalysisOutput:
    started = datetime.now(UTC)
    mo = run_map(facts, settings, runner, selected)
    domains, domain_findings = run_domain_reduce(facts, settings, runner, mo)
    det = deterministic_findings(facts, settings)
    project, project_findings = run_project_reduce(facts, settings, runner, mo, domains, det)

    u, p = runner.usage, runner.provider
    metadata = Metadata(
        repo_url=repo_url,
        commit_sha=commit_sha,
        analyzed_at=started.isoformat(timespec="seconds"),
        prompt_version=runner.prompt_version,
        provider=p.name,
        model=p.model,
        temperature=p.temperature,
        temperature_note=p.temperature_note,
        token_budget={**settings.budget.model_dump(), "input_limit": settings.budget.input_limit},
        packing=settings.packing.model_dump(),
        context_path=facts.context_path,
        usage=Usage(
            llm_calls=u.llm_calls,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_hits=u.cache_hits,
            cached_input_tokens=u.cached_input_tokens,
            cached_output_tokens=u.cached_output_tokens,
        ),
        max_classes=max_classes,
    )
    return build_output(
        facts=facts,
        settings=settings,
        selected=selected,
        mo=mo,
        domains=domains,
        project=project,
        findings=mo.findings + domain_findings + project_findings + det,
        metadata=metadata,
    )
