"""Resolve REST endpoints from source.

path = server.servlet.context-path + class-level @RequestMapping + method-level
mapping. HTTP methods come from the mapping annotation actually present in the
code (README claims are not used here). Values that are not string literals
(e.g. constants) are kept verbatim in braces and flagged as unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from codebase_analyzer.analysis.java_parser import Annotation, JavaFile, TypeDecl

MAPPING_METHODS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "PatchMapping": "PATCH",
    "DeleteMapping": "DELETE",
}
SECURITY_ANNOTATIONS = {"Secured", "PreAuthorize", "RolesAllowed", "PostAuthorize"}
CONTROLLER_ANNOTATIONS = {"RestController", "Controller"}


@dataclass(frozen=True)
class Endpoint:
    http_method: str
    path: str
    controller: str  # FQN
    handler_method: str
    signature: str
    file: str
    line: int
    security: tuple[str, ...]
    unresolved: bool


def read_context_path(resources_dir: Path) -> str:
    """Read server.servlet.context-path from application.{yaml,yml,properties}."""
    for name in ("application.yaml", "application.yml"):
        path = resources_dir / name
        if path.exists():
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                value = (((doc or {}).get("server") or {}).get("servlet") or {}).get("context-path")
                if value:
                    return str(value)
    props = resources_dir / "application.properties"
    if props.exists():
        for line in props.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "server.servlet.context-path":
                return value.strip()
    return ""


def _literal(value: str) -> tuple[str, bool]:
    """Decode a Java string literal; non-literals are returned as '{expr}', unresolved."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1], False
    return "{" + value + "}", True


def _paths(ann: Annotation | None) -> list[tuple[str, bool]]:
    if ann is None:
        return [("", False)]
    raw = ann.arguments.get("path", ann.arguments.get("value"))
    if raw is None:
        return [("", False)]
    values = raw if isinstance(raw, list) else [raw]
    return [_literal(v) for v in values] or [("", False)]


def _http_methods(ann: Annotation) -> list[str]:
    if ann.name in MAPPING_METHODS:
        return [MAPPING_METHODS[ann.name]]
    raw = ann.arguments.get("method")
    if raw is None:
        return ["ANY"]  # @RequestMapping without method= matches every HTTP method
    values = raw if isinstance(raw, list) else [raw]
    return [v.rsplit(".", 1)[-1] for v in values]


def join_path(*parts: str) -> str:
    segments = [s.strip("/") for s in parts if s and s.strip("/")]
    return "/" + "/".join(segments)


def _security(annotations: list[Annotation]) -> list[str]:
    return [a.text for a in annotations if a.name in SECURITY_ANNOTATIONS]


def controller_endpoints(jf: JavaFile, decl: TypeDecl, context_path: str) -> list[Endpoint]:
    if not any(a.name in CONTROLLER_ANNOTATIONS for a in decl.annotations):
        return []
    class_mapping = next((a for a in decl.annotations if a.name == "RequestMapping"), None)
    class_security = _security(decl.annotations)
    endpoints: list[Endpoint] = []
    for member in decl.members:
        for ann in member.annotations:
            if ann.name not in MAPPING_METHODS and ann.name != "RequestMapping":
                continue
            for class_path, class_unresolved in _paths(class_mapping):
                for method_path, method_unresolved in _paths(ann):
                    for http_method in _http_methods(ann):
                        endpoints.append(Endpoint(
                            http_method=http_method,
                            path=join_path(context_path, class_path, method_path),
                            controller=decl.fqn,
                            handler_method=member.name,
                            signature=member.signature,
                            file=jf.path,
                            line=member.line_start,
                            security=tuple(class_security + _security(member.annotations)),
                            unresolved=class_unresolved or method_unresolved,
                        ))
    return endpoints


def resolve_endpoints(files: list[JavaFile], context_path: str) -> list[Endpoint]:
    result = [
        ep for jf in files for decl in jf.all_types() for ep in controller_endpoints(jf, decl, context_path)
    ]
    return sorted(result, key=lambda e: (e.path, e.http_method))
