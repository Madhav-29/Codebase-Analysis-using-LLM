"""End-to-end pipeline on a small repo with the fake model: assembly and coverage."""

import pytest

from codebase_analyzer.analysis.facts import build_facts
from codebase_analyzer.config import Settings
from codebase_analyzer.errors import CoverageError
from codebase_analyzer.llm.client import StructuredRunner
from codebase_analyzer.pipeline.assemble import verify_coverage
from codebase_analyzer.pipeline.map_stage import select_paths
from codebase_analyzer.pipeline.run import run_analysis
from codebase_analyzer.schema.responses import ClassUnitResponse

from ..fakes import FakeStructuredModel, class_payload, fake_provider, smart_responder

FILES = {
    "com/acme/shop/order/controller/OrderController.java": None,  # filled from fixture
    "com/acme/shop/order/dto/OrderDto.java":
        "package com.acme.shop.order.dto;\n@Getter\npublic class OrderDto { private Integer id; }\n",
    "com/acme/shop/order/dto/OrderLine.java":
        "package com.acme.shop.order.dto;\n@Getter\npublic class OrderLine { private Integer qty; }\n",
    "com/acme/shop/order/service/OrderService.java":
        "package com.acme.shop.order.service;\npublic interface OrderService { java.util.List<Object> findAll(); }\n",
}


def _run(make_repo, fixture_source, responder=smart_responder, max_classes=None):
    files = dict(FILES)
    files["com/acme/shop/order/controller/OrderController.java"] = fixture_source("OrderController.java")
    settings = Settings()
    facts = build_facts(make_repo(files, context_path="/api"), settings)
    fake = FakeStructuredModel(responder)
    runner = StructuredRunner(fake_provider(fake), settings, None, "v")
    out = run_analysis(facts, settings, runner, selected=select_paths(facts, max_classes), max_classes=max_classes)
    return facts, out, fake


def test_full_pipeline_output_is_complete_and_labeled(make_repo, fixture_source):
    facts, out, _ = _run(make_repo, fixture_source)
    methods = {m.id: m for c in out.classes for m in c.methods}
    assert set(methods) == {mf.id for mf in facts.members()}
    assert out.coverage.methods_undescribed == [] and out.coverage.units_failed == []

    ctrl = next(c for c in out.classes if c.fqn.endswith("OrderController"))
    by_name = {m.name: m for m in ctrl.methods}
    assert by_name["list"].description_source == "llm"
    assert by_name["getOrderService"].trivial and by_name["getOrderService"].description_source == "deterministic"
    assert by_name["list"].complexity.cyclomatic == 2  # facts come from the parser/lizard, not the LLM
    assert ("GET", "/api/orders/all") in {(e.http_method, e.path) for e in out.endpoints}
    assert all(e.source == "deterministic" for e in out.endpoints)
    assert out.project_overview.status == "ok" and out.project_overview.source == "llm"
    assert {f.source for f in out.findings} >= {"llm"}
    # the two small DTOs from the same package were packed into one call
    assert len(out.coverage.packs) == 1 and len(out.coverage.packs[0].classes) == 2


def test_class_missing_from_pack_is_retried_individually(make_repo, fixture_source):
    def responder(schema, user, n):
        if schema is ClassUnitResponse and "OrderLine" in user and "OrderDto" in user:
            return class_payload(user, drop_types={"OrderLine"})  # pack answer omits one class
        return smart_responder(schema, user, n)

    _, out, fake = _run(make_repo, fixture_source, responder)
    line = next(c for c in out.classes if c.fqn.endswith("OrderLine"))
    assert line.summary_source == "llm" and line.analysis_unit.startswith("class:")
    assert any(schema is ClassUnitResponse and "OrderLine" in u and "OrderDto" not in u for schema, u in fake.calls)


def test_failed_unit_is_recorded_not_fabricated(make_repo, fixture_source):
    def responder(schema, user, n):
        if schema is ClassUnitResponse and "OrderController" in user:
            return {"classes": []}  # never valid for this unit
        return smart_responder(schema, user, n)

    _, out, _ = _run(make_repo, fixture_source, responder)
    ctrl = next(c for c in out.classes if c.fqn.endswith(".OrderController"))
    assert ctrl.summary is None and ctrl.summary_source == "failed"
    assert any(m.description_source == "failed" and m.description is None for m in ctrl.methods)
    assert any("OrderController" in f.unit for f in out.coverage.units_failed)
    assert set(out.coverage.methods_undescribed) == {m.id for c in out.classes for m in c.methods if m.description is None}


def test_max_classes_keeps_all_facts(make_repo, fixture_source):
    facts, out, _ = _run(make_repo, fixture_source, max_classes=1)
    assert len(out.classes) == len(facts.types)  # facts for every type, LLM content only for one file
    assert sum(c.summary_source == "llm" for c in out.classes) >= 1
    assert any(c.summary_source == "not_analyzed" for c in out.classes)
    assert any(s.reason.startswith("not selected") for s in out.coverage.files_skipped)


def test_coverage_check_fails_loudly_when_a_method_is_missing(make_repo, fixture_source):
    facts, out, _ = _run(make_repo, fixture_source)
    broken = out.model_copy(deep=True)
    victim = next(c for c in broken.classes if c.methods)
    victim.methods.pop()
    with pytest.raises(CoverageError, match="members missing"):
        verify_coverage(broken, facts)


def test_secret_values_are_masked_before_reaching_the_llm():
    from codebase_analyzer.pipeline.reduce_stage import mask_secrets

    text = "app:\n  jwt:\n    signing-key: c2VjcmV0LXZhbHVl\n  datasource:\n    password: hunter2\n  host: localhost\n"
    masked = mask_secrets(text)
    assert "c2VjcmV0LXZhbHVl" not in masked and "hunter2" not in masked
    assert "host: localhost" in masked and "signing-key:" in masked  # key names stay visible


def test_reduce_evidence_must_cite_real_lines_of_input_files():
    from codebase_analyzer.pipeline.reduce_stage import _reduce_findings
    from codebase_analyzer.schema.responses import LLMEvidence, LLMReducedFinding

    def finding(file, start, end):
        return LLMReducedFinding(category="security", severity="low", title=f"{file}:{start}", detail="d",
                                 evidence=[LLMEvidence(file=file, line_start=start, line_end=end)])

    dropped: list[str] = []
    kept = _reduce_findings([finding("a.yaml", 3, 4), finding("a.yaml", 50, 51), finding("other.java", 1, 1)],
                            "project", {"a.yaml": 10}, dropped)
    assert [f.title for f in kept] == ["a.yaml:3"] and len(dropped) == 2


def test_llm_finding_duplicating_a_deterministic_one_is_dropped_and_recorded():
    from codebase_analyzer.pipeline.reduce_stage import drop_duplicates_of
    from codebase_analyzer.schema.output import Evidence, FindingOut

    def f(category, file, source):
        return FindingOut(category=category, severity="low", title=f"{category}@{file}", detail="d", scope="project",
                          evidence=[Evidence(file=file, line_start=1, line_end=2)], source=source)

    det = [f("documentation_mismatch", "README.md", "deterministic")]
    dropped: list[str] = []
    kept = drop_duplicates_of(det, [f("documentation_mismatch", "README.md", "llm"), f("security", "README.md", "llm")],
                              dropped)
    assert [k.category for k in kept] == ["security"] and len(dropped) == 1


def test_key_methods_index_endpoint_handlers_and_skips_trivial_members(make_repo, fixture_source):
    _, out, _ = _run(make_repo, fixture_source)
    keys = {k.signature.split("(")[0].rsplit(" ", 1)[-1]: k for k in out.key_methods}
    assert "endpoint_handler" in keys["list"].reasons
    assert set(keys["list"].endpoints) == {"GET /api/orders", "GET /api/orders/all"}
    assert "getOrderService" not in keys  # trivial getter
    ids = {m.id for c in out.classes for m in c.methods}
    assert all(k.id in ids for k in out.key_methods)


def test_prompt_member_keys_never_leak_into_output(make_repo, fixture_source):
    import re

    def responder(schema, user, n):
        payload = smart_responder(schema, user, n)
        if schema is ClassUnitResponse:
            for cls in payload["classes"]:
                if cls["methods"]:
                    key = cls["methods"][0]["member_key"]
                    cls["findings"][0]["detail"] = f"In method {key}, the input is not validated."
                    cls["methods"][0]["description"] = f"Like {key}, returns orders."
        return payload

    _, out, _ = _run(make_repo, fixture_source, responder)
    texts = [f.detail for f in out.findings] + [m.description or "" for c in out.classes for m in c.methods]
    assert not any(re.search(r"\bm\d+\b", t) for t in texts)
    assert any(re.search(r"In method \w+\(\), the input", t) for t in texts)
