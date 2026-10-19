"""Models of analysis.json, the single source of truth for its JSON Schema.

Facts and LLM content live side by side; every LLM-written field carries a
`source`, `summary_source` or `description_source`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from codebase_analyzer.schema.responses import CrossCuttingConcern

SCHEMA_VERSION = "1.0"


class Usage(BaseModel):
    """llm_calls/input_tokens/output_tokens are live calls in this run; cache_hits were replayed from the
    response cache, and cached_*_tokens is what those responses cost when they were originally produced."""

    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    cached_input_tokens: int = 0
    cached_output_tokens: int = 0


class Metadata(BaseModel):
    repo_url: str | None
    commit_sha: str | None
    analyzed_at: str
    schema_version: str = SCHEMA_VERSION
    prompt_version: str
    provider: str
    model: str
    temperature: float | None  # None: the model uses fixed sampling and ignores it
    temperature_note: str | None
    token_budget: dict[str, Any]
    packing: dict[str, Any]
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


class KeyMethod(BaseModel):
    """Index entry for a method that matters to a reader; full facts are under classes[].methods[] by id."""

    id: str
    class_fqn: str
    signature: str
    description: str | None
    description_source: str
    reasons: list[str]  # endpoint_handler | service_api | data_access | complex
    endpoints: list[str]  # "GET /api/v1/actors/{id}" for handlers
    cyclomatic: int | None


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
    avg_nloc: float
    max_cyclomatic: int
    max_nloc: int
    band_counts: dict[str, int]
    top_complex_methods: list[ComplexMethodRef]
    top_complex_trivial_methods: list[ComplexMethodRef]
    thresholds: dict[str, int]
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
    packs: list[PackRecord]
    units_split: list[SplitRecord]
    units_failed: list[FailedUnit]
    findings_dropped: list[str]


class AnalysisOutput(BaseModel):
    metadata: Metadata
    project_overview: ProjectOverview
    key_methods: list[KeyMethod]
    endpoints: list[EndpointOut]
    domains: list[DomainOut]
    classes: list[ClassOut]
    complexity_summary: ComplexitySummary
    findings: list[FindingOut]
    coverage: Coverage
