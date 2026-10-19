"""The report renders deterministically; the evaluation sheet shows each description next to its source."""

import pytest
import yaml

from codebase_analyzer.errors import ConfigError
from codebase_analyzer.evaluation import (
    description_scores,
    finding_scores,
    load_finding_reviews,
    load_golden,
    review_sheet,
    unmatched_reviews,
)
from codebase_analyzer.report import render_report

from .test_pipeline import _run

CONTROLLER = "src/main/java/com/acme/shop/order/controller/OrderController.java"


def test_report_is_deterministic_and_covers_every_section(make_repo, fixture_source):
    _, out, _ = _run(make_repo, fixture_source)
    report = render_report(out)
    assert report == render_report(out)  # same JSON, same report
    for heading in (
        "## Overview",
        "## Run",
        "## Coverage",
        "## Domains",
        "## Endpoints (",
        "## Complexity",
        "## Findings",
    ):
        assert heading in report
    assert "| GET | `/api/orders/all` | `OrderController.list` |" in report
    assert "prices not included" in report


def _golden(tmp_path, verdict="correct"):
    path = tmp_path / "golden.yaml"
    data = {
        "rubric": {"correct": "c", "partially_correct": "p", "incorrect": "i"},
        "methods": [
            {
                "file": CONTROLLER,
                "method": "list",
                "line": 16,
                "verdicts": {"fake-model": {"verdict": verdict, "note": "covers both branches"}},
            },
            {"file": CONTROLLER, "method": "delete", "line": 30},
        ],
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_review_sheet_puts_each_description_next_to_its_source(make_repo, fixture_source, tmp_path):
    facts, out, _ = _run(make_repo, fixture_source)
    rubric, golden = load_golden(_golden(tmp_path))
    sheet = review_sheet(out, rubric, golden, facts.root)
    assert "**Description** (llm): Describes" in sheet  # the fake model's description
    assert "if (status == null) {" in sheet  # the real source lines of `list`
    assert "**Verdict:** correct: covers both branches" in sheet
    assert "_not reviewed for this model_" in sheet  # `delete` has no verdict yet
    assert description_scores(golden, "fake-model") == {"correct": 1}


def test_invalid_verdicts_are_rejected_with_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="'great' is not one of correct"):
        load_golden(_golden(tmp_path, verdict="great"))


def test_finding_reviews_are_scored_and_checked_against_the_analysis(make_repo, fixture_source, tmp_path):
    _, out, _ = _run(make_repo, fixture_source)
    real = out.findings[0]
    path = tmp_path / "findings.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "fake-model": {
                        "findings": [
                            {"scope": real.scope, "title": real.title, "category": "valid_improvement", "note": "n"},
                            {
                                "scope": "x.Gone",
                                "title": "not in this analysis",
                                "category": "false_positive",
                                "note": "n",
                            },
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    reviews = load_finding_reviews(path)["fake-model"]
    assert finding_scores(reviews) == {"valid_improvement": 1, "false_positive": 1}
    assert unmatched_reviews(out, reviews) == ["x.Gone: not in this analysis"]
