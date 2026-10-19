"""Deterministic domain grouping from package conventions.

The domain is the first package segment below the project's base package. When
that segment is a container such as `services` or `modules`, the next segment is
used instead: `com.example.app.services.catalog.controller` -> `catalog`,
`com.example.app.common.security` -> `common`. Types directly in the base
package are grouped as `application`.
"""

from __future__ import annotations

import os

CONTAINER_SEGMENTS = {"services", "service", "modules", "module", "features", "domains", "apps"}


def base_package(packages: list[str]) -> str:
    """Longest common dotted prefix of all packages."""
    split = [p.split(".") for p in packages if p]
    if not split:
        return ""
    prefix = os.path.commonprefix(split)
    return ".".join(prefix)


def domain_for(package: str, base: str) -> str:
    rest = package[len(base):].lstrip(".") if base and package.startswith(base) else package
    segments = [s for s in rest.split(".") if s]
    if not segments:
        return "application"
    if segments[0] in CONTAINER_SEGMENTS and len(segments) > 1:
        return segments[1]
    return segments[0]
