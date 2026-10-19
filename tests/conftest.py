from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_source():
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture
def make_repo(tmp_path):
    """Create a minimal repo with the given {package_path/File.java: source} under src/main/java."""

    def _make(files: dict[str, str], context_path: str | None = None) -> Path:
        for rel, src in files.items():
            path = tmp_path / "src/main/java" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(src, encoding="utf-8")
        if context_path:
            res = tmp_path / "src/main/resources"
            res.mkdir(parents=True, exist_ok=True)
            (res / "application.yaml").write_text(f"server:\n  servlet:\n    context-path: {context_path}\n")
        return tmp_path

    return _make
