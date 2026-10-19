"""Layering rules, checked on the source (no imports executed).

Facts and planning never depend on the LLM layer, the LLM layer knows nothing
about Java analysis, and provider SDKs are imported only in llm/providers/.
"""

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "codebase_analyzer"
PROVIDER_SDKS = (
    "langchain_google_genai",
    "langchain_azure_ai",
    "langchain_openai",
    "langchain_anthropic",
    "google.genai",
    "openai",
    "anthropic",
    "azure",
    "tiktoken",
)
FORBIDDEN = {  # layer -> internal layers it must not import
    "analysis": {"planning", "llm", "pipeline", "cli"},
    "planning": {"llm", "pipeline", "cli"},
    "schema": {"analysis", "planning", "llm", "pipeline", "cli", "prompts"},
    "prompts": {"analysis", "planning", "llm", "pipeline", "cli"},
    "llm": {"analysis", "planning", "pipeline", "schema", "prompts", "cli"},
    "report": {"llm", "pipeline", "cli"},
    "evaluation": {"llm", "pipeline", "cli"},
}


def _layer(path: Path) -> str:
    rel = path.relative_to(PKG)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def _imports(path: Path) -> set[str]:
    package = ["codebase_analyzer", *path.relative_to(PKG).parent.parts]
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            found.add(".".join([*base, *([node.module] if node.module else [])]))
    return found


def test_layers_point_inward():
    violations = []
    for path in sorted(PKG.rglob("*.py")):
        layer = _layer(path)
        for mod in _imports(path):
            parts = mod.split(".")
            if parts[0] == "codebase_analyzer" and len(parts) > 1 and parts[1] in FORBIDDEN.get(layer, ()):
                violations.append(f"{path.relative_to(PKG)} ({layer}) imports {mod}")
    assert violations == []


def test_provider_sdks_are_imported_only_in_llm_providers():
    providers = PKG / "llm" / "providers"
    violations = [
        f"{path.relative_to(PKG)} imports {mod}"
        for path in sorted(PKG.rglob("*.py"))
        if providers not in path.parents
        for mod in _imports(path)
        if mod.startswith(PROVIDER_SDKS)
    ]
    assert violations == []


def test_the_rules_see_real_imports():
    # Guard against a vacuous pass: the parser must find the imports this file relies on.
    assert "codebase_analyzer.llm.providers" in _imports(PKG / "llm" / "client.py")
    assert any(m.startswith("langchain_google_genai") for m in _imports(PKG / "llm" / "providers" / "google_genai.py"))
