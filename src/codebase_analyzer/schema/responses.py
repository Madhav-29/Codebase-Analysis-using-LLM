"""What each structured-output LLM call must return.

These models hold only interpretation (descriptions, summaries, findings) keyed
by ids the analyzer supplies; facts never come from here.

FROZEN CONTRACT: each model's JSON Schema is sent to the model and is part of
every response-cache key. Any change, including adding a class docstring (Pydantic
turns it into a schema description), changes the prompt: bump PROMPT_VERSION.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FindingCategory = Literal[
    "potential_bug",
    "error_handling",
    "validation",
    "transactions",
    "security",
    "performance",
    "maintainability",
    "design_pattern",
    "data_access",
    "api_design",
    "configuration",
    "documentation_mismatch",
]
Severity = Literal["info", "low", "medium", "high"]


class LLMFinding(BaseModel):
    category: FindingCategory
    severity: Severity
    title: str = Field(description="Short title, at most ~12 words.")
    detail: str = Field(description="What the code does and why it matters, grounded in the shown lines.")
    line_start: int = Field(description="First line (as numbered in the prompt) of the supporting code.")
    line_end: int = Field(description="Last line of the supporting code.")


class LLMMethod(BaseModel):
    member_key: str = Field(description="The member key exactly as given, e.g. 'm3'.")
    description: str = Field(
        description="1-3 sentences: what the member does, based only on the shown code. 'unknown' if not determinable."
    )


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
