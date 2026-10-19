"""Pydantic schemas.

Two groups:
* LLM response models: what each structured-output call must return. They hold
  only interpretation (descriptions, summaries, findings) keyed by ids we supply.
* Output models: the final JSON document. Facts and LLM content live side by
  side, and every LLM-written field carries a `source`/`description_source`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

FindingCategory = Literal[
    "potential_bug", "error_handling", "validation", "transactions", "security", "performance",
    "maintainability", "design_pattern", "data_access", "api_design", "configuration",
    "documentation_mismatch",
]
Severity = Literal["info", "low", "medium", "high"]


# ---------------------------------------------------------------- LLM responses

class LLMFinding(BaseModel):
    category: FindingCategory
    severity: Severity
    title: str = Field(description="Short title, at most ~12 words.")
    detail: str = Field(description="What the code does and why it matters, grounded in the shown lines.")
    line_start: int = Field(description="First line (as numbered in the prompt) of the supporting code.")
    line_end: int = Field(description="Last line of the supporting code.")


class LLMMethod(BaseModel):
    member_key: str = Field(description="The member key exactly as given, e.g. 'm3'.")
    description: str = Field(description="1-3 sentences: what the member does, based only on the shown code. 'unknown' if not determinable.")


class LLMClass(BaseModel):
    type_key: str = Field(description="The type key exactly as given.")
    summary: str = Field(description="1-3 sentence purpose of the type.")
    responsibilities: list[str] = Field(description="Up to 5 short bullet phrases.")
    methods: list[LLMMethod] = Field(description="One entry per requested member key of this type.")
    findings: list[LLMFinding] = Field(description="Noteworthy aspects; empty if none.")


class ClassUnitResponse(BaseModel):
    classes: list[LLMClass] = Field(description="Exactly one entry per requested type key.")


class LLMEvidence(BaseModel):
    file: str = Field(description="Repository-relative file path exactly as given in the input.")
    line_start: int
    line_end: int


class LLMReducedFinding(BaseModel):
    category: FindingCategory
    severity: Severity
    title: str
    detail: str
    evidence: list[LLMEvidence] = Field(description="Files/lines from the input that support the finding.")


class DomainResponse(BaseModel):
    summary: str = Field(description="3-6 sentence summary of the functional area.")
    key_responsibilities: list[str] = Field(description="Up to 8 short bullet phrases.")
    findings: list[LLMReducedFinding] = Field(description="Cross-class observations only; empty if none.")


class CrossCuttingConcern(BaseModel):
    name: str
    description: str


class ProjectResponse(BaseModel):
    purpose: str
    functionality: list[str] = Field(description="Main capabilities, up to 12 items.")
    architecture: str = Field(description="Layering and how requests flow through the code.")
    technologies: list[str] = Field(description="Frameworks/libraries actually used in the code or build.")
    cross_cutting_concerns: list[CrossCuttingConcern]
    findings: list[LLMReducedFinding] = Field(description="Project-level observations; empty if none.")


# ---------------------------------------------------------------- final output

class Usage(BaseModel):
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    est_cost_usd: float | None = None


class Metadata(BaseModel):
    repo_url: str | None
    commit_sha: str | None
    analyzed_at: str
    schema_version: str = SCHEMA_VERSION
    prompt_version: str
    llm_provider: str
    llm_model: str
    temperature: float | None
    temperature_note: str | None
    effort: str | None
    token_budget_config: dict
    packing_config: dict
    context_path: str
    usage: Usage
    max_classes: int | None


class ProjectOverview(BaseModel):
    purpose: str | None
    functionality: list[str]
    architecture: str | None
    technologies: list[str]
    cross_cutting_concerns: list[CrossCuttingConcern]
    source: Literal["llm"] = "llm"
    status: Literal["ok", "failed", "not_run"]


class EndpointOut(BaseModel):
    http_method: str
    path: str
    controller: str
    handler_method: str
    file: str
    line: int
    security: list[str]
    unresolved: bool
    source: Literal["deterministic"] = "deterministic"


class DomainOut(BaseModel):
    name: str
    summary: str | None
    key_responsibilities: list[str]
    classes: list[str]
    endpoints: list[str]
    summary_source: Literal["llm"] = "llm"
    status: Literal["ok", "failed", "not_run"]


class ComplexityOut(BaseModel):
    cyclomatic: int
    nloc: int
    parameter_count: int
    token_count: int
    band: str


class ParameterOut(BaseModel):
    name: str
    type: str
    annotations: list[str]


class MethodOut(BaseModel):
    id: str
    name: str
    kind: str
    signature: str
    modifiers: list[str]
    annotations: list[str]
    parameters: list[ParameterOut]
    return_type: str | None
    line_start: int
    line_end: int
    has_body: bool
    trivial: bool
    description: str | None
    description_source: Literal["deterministic", "llm", "not_analyzed", "failed"]
    complexity: ComplexityOut | None


class ClassOut(BaseModel):
    fqn: str
    kind: str
    domain: str
    file: str
    line_start: int
    line_end: int
    modifiers: list[str]
    annotations: list[str]
    extends: list[str]
    implements: list[str]
    fields: list[str]
    enum_constants: list[str]
    framework_notes: list[str]
    summary: str | None
    responsibilities: list[str]
    summary_source: Literal["llm", "not_analyzed", "failed"]
    analysis_unit: str | None
    methods: list[MethodOut]


class ComplexMethodRef(BaseModel):
    id: str
    cyclomatic: int
    nloc: int
    file: str
    line_start: int


class ComplexitySummary(BaseModel):
    methods_total: int
    methods_with_metrics: int
    avg_cyclomatic: float
    max_cyclomatic: int
    band_counts: dict[str, int]
    top_complex_methods: list[ComplexMethodRef]
    top_complex_trivial_methods: list[ComplexMethodRef]
    thresholds_used: dict[str, int]
    notes: list[str]


class Evidence(BaseModel):
    file: str
    line_start: int
    line_end: int


class FindingOut(BaseModel):
    category: str
    severity: str
    title: str
    detail: str
    scope: str  # class FQN, "domain:<name>" or "project"
    evidence: list[Evidence]
    source: Literal["llm", "deterministic"]


class SkippedFile(BaseModel):
    file: str
    reason: str


class FailedUnit(BaseModel):
    unit: str
    error: str


class SplitRecord(BaseModel):
    unit: str
    file: str
    reason: str
    parts: int
    segmented_members: list[str]


class PackRecord(BaseModel):
    unit: str
    classes: list[str]
    est_tokens: int


class Coverage(BaseModel):
    files_discovered: int
    files_analyzed: int
    files_skipped: list[SkippedFile]
    classes_total: int
    classes_summarized: int
    methods_total: int
    methods_described: int
    methods_described_by_source: dict[str, int]
    methods_undescribed: list[str]
    units_total: int
    units_packed: list[PackRecord]
    units_split: list[SplitRecord]
    units_failed: list[FailedUnit]
    findings_dropped: list[str]


class AnalysisOutput(BaseModel):
    metadata: Metadata
    project_overview: ProjectOverview
    endpoints: list[EndpointOut]
    domains: list[DomainOut]
    classes: list[ClassOut]
    complexity_summary: ComplexitySummary
    findings: list[FindingOut]
    coverage: Coverage
