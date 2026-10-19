"""Deterministic stage: everything that is extracted, not interpreted.

Builds one `ProjectFacts` object from the repository: parsed files, every type
and member with a stable id, complexity metrics, trivial/abstract handling,
framework notes, domain grouping and REST endpoints. The LLM stages only add
descriptions on top of this; they never change these facts.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from codebase_analyzer.analysis.endpoints import Endpoint, read_context_path, resolve_endpoints
from codebase_analyzer.analysis.grouping import base_package, domain_for
from codebase_analyzer.analysis.inventory import Inventory, build_inventory
from codebase_analyzer.analysis.java_parser import JavaFile, Member, TypeDecl, parse_java
from codebase_analyzer.analysis.metrics import Complexity, member_complexity
from codebase_analyzer.analysis.rules import (
    DeterministicDescription,
    describe_abstract,
    describe_trivial,
    framework_notes,
    implementations_index,
)
from codebase_analyzer.config import Settings


@dataclass
class MemberFact:
    id: str
    member: Member
    complexity: Complexity | None
    deterministic: DeterministicDescription | None  # None -> needs an LLM description

    @property
    def needs_llm(self) -> bool:
        return self.deterministic is None


@dataclass
class TypeFact:
    decl: TypeDecl
    file: JavaFile
    domain: str
    top_level: str  # FQN of the enclosing top-level type (its own FQN if top-level)
    framework_notes: list[str]
    members: list[MemberFact] = field(default_factory=list)


@dataclass
class ProjectFacts:
    root: Path
    inventory: Inventory
    files: list[JavaFile]
    types: dict[str, TypeFact]  # by FQN, in source order
    endpoints: list[Endpoint]
    context_path: str
    base_package: str

    def members(self) -> Iterator[MemberFact]:
        for t in self.types.values():
            yield from t.members

    def types_in_file(self, path: str) -> list[TypeFact]:
        return [t for t in self.types.values() if t.file.path == path]

    def domains(self) -> list[str]:
        return sorted({t.domain for t in self.types.values()})


def member_id(type_fqn: str, m: Member) -> str:
    params = ",".join(p.type for p in m.parameters)
    return f"{type_fqn}#{m.name}({params})@L{m.line_start}"


def build_facts(root: Path, settings: Settings) -> ProjectFacts:
    inventory = build_inventory(root)
    files = [parse_java(inventory.read(f.path), f.path) for f in inventory.by_category("main_java")]
    for jf in files:
        if jf.has_errors:
            inventory.skipped.append((jf.path, "tree-sitter reported a syntax error; facts may be partial"))

    base = base_package([jf.package for jf in files])
    all_decls = [t for jf in files for t in jf.all_types()]
    impls = implementations_index(all_decls)

    types: dict[str, TypeFact] = {}
    for jf in files:
        for top in jf.types:
            for decl in top.walk():
                fact = TypeFact(
                    decl=decl,
                    file=jf,
                    domain=domain_for(jf.package, base),
                    top_level=top.fqn,
                    framework_notes=framework_notes(decl, jf),
                )
                for m in decl.members:
                    det = describe_trivial(m, decl) or describe_abstract(m, decl, impls)
                    fact.members.append(MemberFact(member_id(decl.fqn, m), m, member_complexity(m), det))
                types[decl.fqn] = fact

    context_path = read_context_path(root / "src/main/resources")
    return ProjectFacts(
        root=root,
        inventory=inventory,
        files=files,
        types=types,
        endpoints=resolve_endpoints(files, context_path),
        context_path=context_path,
        base_package=base,
    )
