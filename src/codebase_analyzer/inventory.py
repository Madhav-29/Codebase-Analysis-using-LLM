"""Walk the target repository and classify files.

Only `src/main/java` is a primary analysis target. Build files, application
config and the README are read as project context; tests are supporting context.
Everything excluded is recorded with a reason so coverage can report it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

EXCLUDED_DIRS = {".git", ".gradle", ".idea", ".vscode", "build", "out", "target", "node_modules", "bin"}
BINARY_EXTENSIONS = {".jar", ".class", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz", ".pdf"}
CONTEXT_FILES = {
    "build.gradle.kts", "build.gradle", "settings.gradle.kts", "pom.xml",
    "gradle/libs.versions.toml", "README.md",
}


@dataclass(frozen=True)
class RepoFile:
    path: str  # repo-relative, POSIX separators
    category: str  # main_java | test_java | app_config | build_config | readme | other
    size_bytes: int


@dataclass
class Inventory:
    root: Path
    files: list[RepoFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)

    def by_category(self, category: str) -> list[RepoFile]:
        return [f for f in self.files if f.category == category]

    def read(self, rel_path: str) -> str:
        return (self.root / rel_path).read_text(encoding="utf-8")


def _classify(rel: str) -> str:
    if rel.startswith("src/main/java/") and rel.endswith(".java"):
        return "main_java"
    if rel.startswith("src/test/java/") and rel.endswith(".java"):
        return "test_java"
    if rel.startswith("src/main/resources/") and rel.rsplit("/", 1)[-1].startswith("application"):
        return "app_config"
    if rel == "README.md":
        return "readme"
    if rel in CONTEXT_FILES:
        return "build_config"
    return "other"


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    with path.open("rb") as fh:
        return b"\0" in fh.read(4096)


def build_inventory(root: Path) -> Inventory:
    inv = Inventory(root=root)
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts[:-1]):
            continue  # whole excluded directories are not listed file by file
        if not path.is_file():
            continue
        rel = "/".join(rel_parts)
        if _is_binary(path):
            inv.skipped.append((rel, "binary"))
            continue
        category = _classify(rel)
        inv.files.append(RepoFile(rel, category, path.stat().st_size))
        if category == "other":
            inv.skipped.append((rel, "not an analysis target"))
        elif category == "test_java":
            inv.skipped.append((rel, "test source: supporting context only"))
    return inv
