"""The committed artifacts stay in sync with the models that define them."""

import json
from pathlib import Path

from codebase_analyzer.schema.output import AnalysisOutput

ROOT = Path(__file__).resolve().parents[2]


def test_committed_json_schema_matches_the_models():
    committed = json.loads((ROOT / "schemas" / "analysis.schema.json").read_text(encoding="utf-8"))
    assert committed == AnalysisOutput.model_json_schema(), "run: codebase-analyzer schema"


def test_committed_outputs_validate_against_the_current_schema():
    outputs = sorted((ROOT / "output").rglob("analysis*.json"))
    assert outputs, "expected committed analysis outputs"
    for path in outputs:
        result = AnalysisOutput.model_validate_json(path.read_text(encoding="utf-8"))
        assert result.coverage.methods_total == sum(len(c.methods) for c in result.classes)
