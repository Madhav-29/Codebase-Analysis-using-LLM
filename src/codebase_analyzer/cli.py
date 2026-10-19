"""Command-line interface.

    codebase-analyzer estimate --repo ../spring-rest-sakila
    codebase-analyzer analyze  --repo https://github.com/codejsha/spring-rest-sakila \
        --ref 62873f7 --output output/analysis.json --max-classes 5
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
from dotenv import load_dotenv

from .analyze import run_domain_reduce, run_map, run_project_reduce, select_paths
from .assemble import build_output, deterministic_findings
from .cache import ResponseCache
from .chunking import plan_units
from .config import PRICING_USD_PER_MTOK, Settings, TokenBudget
from .facts import ProjectFacts, build_facts
from .llm import StructuredRunner, make_chat_model
from .prompts import CLASS_SYSTEM, PROMPT_VERSION
from .schema import Metadata, Usage

app = typer.Typer(add_completion=False, help="LLM-powered structured knowledge extraction for Java/Spring repositories.")
log = logging.getLogger("codebase_analyzer")


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def resolve_repo(repo: str, ref: str | None, clone_dir: Path) -> tuple[Path, str | None, str | None]:
    """Return (local root, repo URL, commit SHA). URLs are cloned into clone_dir."""
    if repo.startswith(("http://", "https://", "git@")):
        if not (clone_dir / ".git").exists():
            log.info("cloning %s into %s", repo, clone_dir)
            subprocess.run(["git", "clone", "--quiet", repo, str(clone_dir)], check=True)
        elif _git(clone_dir, "remote", "get-url", "origin") != repo:
            raise typer.BadParameter(f"{clone_dir} already holds a different repository")
        root, url = clone_dir, repo
    else:
        root = Path(repo).resolve()
        if not (root / "src/main/java").is_dir():
            raise typer.BadParameter(f"{root} has no src/main/java directory")
        url = _git(root, "remote", "get-url", "origin")
    if ref:
        subprocess.run(["git", "-C", str(root), "checkout", "--quiet", ref], check=True)
    return root, url, _git(root, "rev-parse", "HEAD")


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
    if verbose:
        for noisy in ("httpx", "anthropic", "openai"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


RepoOpt = Annotated[str, typer.Option("--repo", help="Local path or git URL of the target repository.")]
RefOpt = Annotated[Optional[str], typer.Option("--ref", help="Commit/branch to check out before analysis.")]
ProviderOpt = Annotated[Optional[str], typer.Option("--provider", help="anthropic | openai (default: LLM_PROVIDER).")]
ModelOpt = Annotated[Optional[str], typer.Option("--model", help="Model id (default: LLM_MODEL).")]
MaxOpt = Annotated[Optional[int], typer.Option("--max-classes", help="Analyze only N source files (round-robin across domains).")]
BudgetOpt = Annotated[Optional[int], typer.Option("--request-tokens", help="Per-request token budget (default 32000).")]
ReserveOpt = Annotated[Optional[int], typer.Option("--output-reserve", help="Tokens reserved for output, also max_tokens (default 12000).")]


@app.command()
def estimate(repo: RepoOpt, ref: RefOpt = None, provider: ProviderOpt = None, model: ModelOpt = None,
             max_classes: MaxOpt = None, request_tokens: BudgetOpt = None, output_reserve: ReserveOpt = None,
             native_count: Annotated[bool, typer.Option(help="Also count input tokens with the provider API.")] = False,
             verbose: bool = False) -> None:
    """Plan the analysis and estimate tokens and cost without calling the LLM."""
    load_dotenv()
    _setup_logging(verbose)
    s = _settings(provider, model, False, request_tokens, output_reserve, None)
    root, _, sha = resolve_repo(repo, ref, s.clone_dir)
    facts = build_facts(root, s)
    selected = select_paths(facts, max_classes)
    plan = plan_units(facts, s, selected)

    kinds = {k: sum(1 for u in plan.units if u.kind == k) for k in ("class", "pack", "split")}
    est_in = sum(u.est_tokens for u in plan.units)
    n_types = sum(len(u.type_keys) for u in plan.units)
    n_members = sum(len(u.member_keys) for u in plan.units)
    # Visible output heuristic: ~150 tokens per type summary, ~60 per method, ~150 per unit for findings.
    visible_out = 150 * n_types + 60 * n_members + 150 * len(plan.units)
    domains = {facts.types_in_file(p)[0].domain for p in selected}
    reduce_calls = len(domains) + 1
    reduce_in = reduce_calls * 6_000
    reduce_out = reduce_calls * 1_200

    typer.echo(f"Repository: {root} @ {sha}")
    typer.echo(f"Files selected: {len(selected)} / {len(facts.files)}; domains: {len(domains)}")
    typer.echo(f"Budget: request {s.budget.request_tokens}, output reserve {s.budget.output_reserve}, "
               f"input limit {s.budget.input_limit} (chars/token {s.budget.chars_per_token})")
    typer.echo(f"Class units: {len(plan.units)} {kinds}; planning failures: {len(plan.failed)}")
    for path, err in plan.failed:
        typer.echo(f"  cannot plan {path}: {err}")
    typer.echo(f"Largest unit: {max((u.est_tokens for u in plan.units), default=0)} est. tokens")
    typer.echo(f"Types summarized: {n_types}; members described by LLM: {n_members}")
    typer.echo(f"Class stage input (local conservative estimate): {est_in:,} tokens")
    if native_count:
        from langchain_core.messages import HumanMessage, SystemMessage

        from .tokens import count_native

        llm = make_chat_model(s)
        native = [count_native(llm, [SystemMessage(CLASS_SYSTEM), HumanMessage(u.user_text)]) for u in plan.units]
        if all(n is not None for n in native):
            typer.echo(f"Class stage input (provider count, excl. schema): {sum(native):,} tokens")
    typer.echo(f"Class stage visible output (heuristic): ~{visible_out:,} tokens (+ model thinking)")
    typer.echo(f"Reduce stage: {reduce_calls} calls, ~{reduce_in:,} input / ~{reduce_out:,} output tokens")
    price = PRICING_USD_PER_MTOK.get(s.llm_model)
    if price:
        total_in, total_out = est_in + reduce_in, visible_out + reduce_out
        low = total_in / 1e6 * price[0] + total_out / 1e6 * price[1]
        high = total_in / 1e6 * price[0] + 3 * total_out / 1e6 * price[1]
        typer.echo(f"Estimated cost for {s.llm_model}: ${low:.2f} - ${high:.2f} "
                   "(upper bound assumes model thinking adds 2x the visible output)")


@app.command()
def analyze(repo: RepoOpt, output: Annotated[Path, typer.Option("--output")] = Path("output/analysis.json"),
            ref: RefOpt = None, provider: ProviderOpt = None, model: ModelOpt = None, max_classes: MaxOpt = None,
            no_cache: Annotated[bool, typer.Option("--no-cache")] = False, request_tokens: BudgetOpt = None,
            output_reserve: ReserveOpt = None, concurrency: Annotated[Optional[int], typer.Option("--concurrency")] = None,
            verbose: bool = True) -> None:
    """Run the full pipeline and write the JSON analysis."""
    load_dotenv()
    _setup_logging(verbose)
    s = _settings(provider, model, no_cache, request_tokens, output_reserve, concurrency)
    root, url, sha = resolve_repo(repo, ref, s.clone_dir)
    started = datetime.now(timezone.utc)

    facts: ProjectFacts = build_facts(root, s)
    selected = select_paths(facts, max_classes)
    log.info("facts: %d files, %d types, %d members, %d endpoints; analyzing %d files",
             len(facts.files), len(facts.types), sum(len(t.members) for t in facts.types.values()),
             len(facts.endpoints), len(selected))

    runner = StructuredRunner(make_chat_model(s), s, ResponseCache(s.cache_dir) if s.use_cache else None)
    mo = run_map(facts, s, runner, selected)
    domains, domain_findings = run_domain_reduce(facts, s, runner, mo)
    det = deterministic_findings(facts, s)
    project, project_findings = run_project_reduce(facts, s, runner, mo, domains, det)

    u = runner.usage
    temp = s.effective_temperature()
    metadata = Metadata(
        repo_url=url, commit_sha=sha, analyzed_at=started.isoformat(timespec="seconds"),
        prompt_version=PROMPT_VERSION, llm_provider=s.llm_provider, llm_model=s.llm_model, temperature=temp,
        temperature_note=None if temp is not None else
        "Not sent: this model rejects non-default sampling parameters. Reproducibility comes from the response cache.",
        effort=s.llm_effort if s.llm_provider == "anthropic" else None,
        token_budget_config={**s.budget.model_dump(), "input_limit": s.budget.input_limit},
        packing_config=s.packing.model_dump(), context_path=facts.context_path,
        usage=Usage(llm_calls=u.llm_calls, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                    cache_hits=u.cache_hits, est_cost_usd=u.cost_usd(s.llm_model)),
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
    typer.echo(f"Units: {c.units_total} (packed {len(c.units_packed)}, split {len(c.units_split)}, failed {len(c.units_failed)})")
    typer.echo(f"Endpoints: {len(result.endpoints)}  Findings: {len(result.findings)} (dropped {len(c.findings_dropped)})")
    typer.echo(f"LLM calls: {u.llm_calls}  cache hits: {u.cache_hits}  tokens in/out: {u.input_tokens:,}/{u.output_tokens:,}"
               f"  est. cost: {'$%.2f' % u.cost_usd(s.llm_model) if u.cost_usd(s.llm_model) is not None else 'n/a'}")
    for stage, st in u.per_stage.items():
        typer.echo(f"  {stage}: {st}")
    for f in c.units_failed:
        typer.echo(f"  FAILED {f.unit}: {f.error[:200]}")
    typer.echo(f"Wrote {output}")


if __name__ == "__main__":
    app()
