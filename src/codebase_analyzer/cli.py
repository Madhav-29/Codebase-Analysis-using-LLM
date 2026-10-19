"""Command-line interface.

    codebase-analyzer estimate --repo ./target_repo
    codebase-analyzer analyze  --repo ./target_repo --output output/analysis.json [--max-classes 5]
    codebase-analyzer validate output/analysis.json --repo ./target_repo
    codebase-analyzer schema   --output output/analysis.schema.json
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
from dotenv import load_dotenv

from codebase_analyzer.analysis.checks import deterministic_findings
from codebase_analyzer.pipeline.assemble import build_output, verify_coverage
from codebase_analyzer.pipeline.map_stage import run_map, select_paths
from codebase_analyzer.pipeline.reduce_stage import run_domain_reduce, run_project_reduce
from codebase_analyzer.llm.cache import ResponseCache
from codebase_analyzer.planning.chunking import Plan, plan_units
from codebase_analyzer.config import Settings, TokenBudget
from codebase_analyzer.analysis.facts import ProjectFacts, build_facts
from codebase_analyzer.llm.client import FatalLLMError, QuotaExhausted, StructuredRunner, effective_temperature, make_chat_model
from codebase_analyzer.prompts import CLASS_SYSTEM, PROMPT_VERSION
from codebase_analyzer.schema.output import AnalysisOutput, Metadata, Usage

app = typer.Typer(add_completion=False, help="LLM-powered structured knowledge extraction for Java/Spring repositories.")
log = logging.getLogger("codebase_analyzer")


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def resolve_repo(repo: Path) -> tuple[Path, str | None, str | None]:
    """Return (local root, origin URL, HEAD commit SHA). The repository is only read, never modified."""
    root = repo.resolve()
    if not (root / "src/main/java").is_dir():
        raise typer.BadParameter(f"{root} has no src/main/java directory")
    return root, _git(root, "remote", "get-url", "origin"), _git(root, "rev-parse", "HEAD")


def _settings(provider, model, no_cache, request_tokens, output_reserve, concurrency) -> Settings:
    s = Settings()
    if provider:
        s.llm_provider = provider
    if model:
        s.llm_model = model
    if no_cache:
        s.use_cache = False
    if request_tokens or output_reserve:
        overrides = {k: v for k, v in (("request_tokens", request_tokens), ("output_reserve", output_reserve)) if v}
        try:
            s.budget = TokenBudget(**{**s.budget.model_dump(), **overrides})  # re-validated
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if concurrency:
        s.llm_concurrency = concurrency
    return s


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "google_genai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print_plan(facts: ProjectFacts, s: Settings, selected: list[str], plan: Plan) -> None:
    kinds = {k: sum(1 for u in plan.units if u.kind == k) for k in ("class", "pack", "split")}
    domains = {facts.types_in_file(p)[0].domain for p in selected}
    est_in = sum(u.est_tokens for u in plan.units)
    n_types = sum(len(u.type_keys) for u in plan.units)
    n_members = sum(len(u.member_keys) for u in plan.units)
    # Visible-output heuristic: ~150 tokens per type summary, ~60 per method, ~150 per unit for findings.
    visible_out = 150 * n_types + 60 * n_members + 150 * len(plan.units)
    reduce_calls = len(domains) + 1  # one per domain + project; more only if a reduce input must be condensed
    reduce_in, reduce_out = reduce_calls * 6_000, reduce_calls * 1_200
    typer.echo(f"Files selected: {len(selected)} / {len(facts.files)}; domains: {len(domains)}")
    typer.echo(f"Budget: request {s.budget.request_tokens}, output reserve {s.budget.output_reserve}, "
               f"input limit {s.budget.input_limit} (chars/token {s.budget.chars_per_token})")
    typer.echo(f"Class units: {len(plan.units)} {kinds}; planning failures: {len(plan.failed)}")
    for path, err in plan.failed:
        typer.echo(f"  cannot plan {path}: {err}")
    typer.echo(f"Largest unit: {max((u.est_tokens for u in plan.units), default=0):,} est. input tokens")
    typer.echo(f"Types summarized: {n_types}; members described by LLM: {n_members}")
    typer.echo(f"Estimated LLM calls (without cache, retries): {len(plan.units) + reduce_calls} "
               f"({len(plan.units)} class + {reduce_calls} reduce)")
    typer.echo(f"Estimated tokens: input ~{est_in + reduce_in:,} (class {est_in:,}, conservative), "
               f"visible output ~{visible_out + reduce_out:,} (+ model thinking)")


RepoOpt = Annotated[Path, typer.Option("--repo", help="Local path of the target repository (a git checkout).")]
ProviderOpt = Annotated[Optional[str], typer.Option("--provider", help="google_genai | anthropic (default: LLM_PROVIDER).")]
ModelOpt = Annotated[Optional[str], typer.Option("--model", help="Model id (default: LLM_MODEL).")]
MaxOpt = Annotated[Optional[int], typer.Option("--max-classes", help="Analyze only N source files (round-robin across domains).")]
BudgetOpt = Annotated[Optional[int], typer.Option("--request-tokens", help="Per-request token budget (default 32000).")]
ReserveOpt = Annotated[Optional[int], typer.Option("--output-reserve", help="Tokens reserved for output, also max output tokens (default 12000).")]


@app.command()
def estimate(repo: RepoOpt, provider: ProviderOpt = None, model: ModelOpt = None, max_classes: MaxOpt = None,
             request_tokens: BudgetOpt = None, output_reserve: ReserveOpt = None,
             native_count: Annotated[bool, typer.Option(help="Also count class-unit input tokens with the provider's count_tokens API.")] = False,
             verbose: bool = False) -> None:
    """Plan the analysis and estimate LLM calls and tokens without calling the LLM."""
    load_dotenv()
    _setup_logging(verbose)
    s = _settings(provider, model, False, request_tokens, output_reserve, None)
    root, _, sha = resolve_repo(repo)
    facts = build_facts(root, s)
    selected = select_paths(facts, max_classes)
    plan = plan_units(facts, s, selected)
    typer.echo(f"Repository: {root} @ {sha}")
    _print_plan(facts, s, selected, plan)
    if native_count:
        from codebase_analyzer.planning.tokens import count_native

        llm = make_chat_model(s)
        pairs = [(u.est_tokens, count_native(llm, CLASS_SYSTEM + "\n" + u.user_text)) for u in plan.units]
        if all(n is not None for _, n in pairs):
            ratio = max(n / est for est, n in pairs)
            typer.echo(f"Provider count of class-unit prompts (excl. schema): {sum(n for _, n in pairs):,} tokens; "
                       f"max provider/estimate ratio {ratio:.2f} (must be < 1 for the estimate to be conservative)")
        else:
            typer.echo("Provider token counting unavailable; the local estimate is used.")


@app.command()
def analyze(repo: RepoOpt, output: Annotated[Path, typer.Option("--output")] = Path("output/analysis.json"),
            provider: ProviderOpt = None, model: ModelOpt = None, max_classes: MaxOpt = None,
            no_cache: Annotated[bool, typer.Option("--no-cache")] = False, request_tokens: BudgetOpt = None,
            output_reserve: ReserveOpt = None, concurrency: Annotated[Optional[int], typer.Option("--concurrency")] = None,
            verbose: bool = True) -> None:
    """Run the full pipeline and write the JSON analysis."""
    load_dotenv()
    _setup_logging(verbose)
    s = _settings(provider, model, no_cache, request_tokens, output_reserve, concurrency)
    root, url, sha = resolve_repo(repo)
    started = datetime.now(timezone.utc)

    facts: ProjectFacts = build_facts(root, s)
    selected = select_paths(facts, max_classes)
    typer.echo(f"Repository: {root} @ {sha}; facts: {len(facts.files)} files, {len(facts.types)} types, "
               f"{sum(len(t.members) for t in facts.types.values())} members, {len(facts.endpoints)} endpoints")
    _print_plan(facts, s, selected, plan_units(facts, s, selected))

    runner = StructuredRunner(make_chat_model(s), s, ResponseCache(s.cache_dir) if s.use_cache else None)
    try:
        mo = run_map(facts, s, runner, selected)
        domains, domain_findings = run_domain_reduce(facts, s, runner, mo)
        det = deterministic_findings(facts, s)
        project, project_findings = run_project_reduce(facts, s, runner, mo, domains, det)
    except (QuotaExhausted, FatalLLMError) as exc:
        u = runner.usage
        typer.echo(f"\nSTOPPED: {type(exc).__name__}: {str(exc)[:500]}", err=True)
        typer.echo(f"Completed before stopping: {u.llm_calls} calls, {u.cache_hits} cache hits. "
                   f"Every finished response is cached in {s.cache_dir}.", err=True)
        if isinstance(exc, QuotaExhausted):
            typer.echo("Resume after the daily quota resets (midnight Pacific time) by re-running the same command:\n  "
                       + " ".join(["codebase-analyzer", *sys.argv[1:]]), err=True)
        raise typer.Exit(2) from exc

    u = runner.usage
    temperature = effective_temperature(s)
    metadata = Metadata(
        repo_url=url, commit_sha=sha, analyzed_at=started.isoformat(timespec="seconds"),
        prompt_version=PROMPT_VERSION, provider=s.llm_provider, model=s.llm_model, temperature=temperature,
        temperature_note=None if temperature is not None else
        f"Not sent: {s.llm_model} uses fixed sampling defaults and ignores temperature. "
        "Reproducibility comes from the response cache.",
        token_budget={**s.budget.model_dump(), "input_limit": s.budget.input_limit},
        packing=s.packing.model_dump(), context_path=facts.context_path,
        usage=Usage(llm_calls=u.llm_calls, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                    cache_hits=u.cache_hits, cached_input_tokens=u.cached_input_tokens,
                    cached_output_tokens=u.cached_output_tokens),
        max_classes=max_classes,
    )
    findings = mo.findings + domain_findings + project_findings + det
    result = build_output(facts=facts, settings=s, selected=selected, mo=mo, domains=domains, project=project,
                          findings=findings, metadata=metadata)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    c = result.coverage
    typer.echo("\n=== Run summary ===")
    typer.echo(f"Commit: {sha}  Model: {s.llm_provider}/{s.llm_model}  Prompt: {PROMPT_VERSION}")
    typer.echo(f"Files analyzed: {c.files_analyzed}/{len(facts.files)}  Classes summarized: {c.classes_summarized}/{c.classes_total}")
    typer.echo(f"Methods: {c.methods_total} total, {c.methods_described} described {c.methods_described_by_source}")
    typer.echo(f"Units: {c.units_total} (packed {len(c.packs)}, split {len(c.units_split)}, failed {len(c.units_failed)})")
    typer.echo(f"Key methods: {len(result.key_methods)}")
    typer.echo(f"Endpoints: {len(result.endpoints)}  Findings: {len(result.findings)} (dropped {len(c.findings_dropped)})")
    typer.echo(f"LLM calls: {u.llm_calls}  cache hits: {u.cache_hits}  tokens in/out: {u.input_tokens:,}/{u.output_tokens:,}")
    for stage, st in u.per_stage.items():
        typer.echo(f"  {stage}: {st}")
    for f in c.units_failed:
        typer.echo(f"  FAILED {f.unit}: {f.error[:200]}")
    typer.echo(f"Wrote {output}")


@app.command()
def validate(path: Path, repo: Annotated[Optional[Path], typer.Option("--repo", help="Also re-parse this repository and check every type/member is present.")] = None) -> None:
    """Validate an analysis JSON file against the output schema (and, with --repo, against the source)."""
    result = AnalysisOutput.model_validate_json(path.read_text(encoding="utf-8"))
    typer.echo(f"{path}: schema OK ({len(result.classes)} classes, "
               f"{sum(len(c.methods) for c in result.classes)} methods, {len(result.findings)} findings)")
    if repo is not None:
        root, _, sha = resolve_repo(repo)
        if sha != result.metadata.commit_sha:
            typer.echo(f"warning: repository is at {sha}, analysis was made at {result.metadata.commit_sha}", err=True)
        verify_coverage(result, build_facts(root, Settings()))
        typer.echo("coverage OK: every parsed type and member is present exactly once")


@app.command()
def schema(output: Annotated[Path, typer.Option("--output")] = Path("output/analysis.schema.json")) -> None:
    """Write the JSON Schema of the analysis output."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(AnalysisOutput.model_json_schema(), indent=2) + "\n", encoding="utf-8")
    typer.echo(f"Wrote {output}")


if __name__ == "__main__":
    app()
