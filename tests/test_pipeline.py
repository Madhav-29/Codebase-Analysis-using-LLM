"""End-to-end pipeline on a small repo with the fake model: assembly and coverage."""

import pytest

from codebase_analyzer.analyze import run_domain_reduce, run_map, run_project_reduce, select_paths
from codebase_analyzer.assemble import CoverageError, build_output, deterministic_findings, verify_coverage
from codebase_analyzer.config import Settings
from codebase_analyzer.facts import build_facts
from codebase_analyzer.llm import StructuredRunner
from codebase_analyzer.schema import ClassUnitResponse, Metadata, Usage

from .fakes import FakeStructuredModel, class_payload, smart_responder

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
    selected = select_paths(facts, max_classes)
    fake = FakeStructuredModel(responder)
    runner = StructuredRunner(fake, settings, cache=None)
    mo = run_map(facts, settings, runner, selected)
    domains, dfind = run_domain_reduce(facts, settings, runner, mo)
    det = deterministic_findings(facts, settings)
    project, pfind = run_project_reduce(facts, settings, runner, mo, domains, det)
    meta = Metadata(repo_url=None, commit_sha=None, analyzed_at="t", prompt_version="v", provider="fake",
                    model="fake", temperature=0.0, token_budget={}, packing={}, context_path=facts.context_path,
                    usage=Usage(), max_classes=max_classes)
    out = build_output(facts=facts, settings=settings, selected=selected, mo=mo, domains=domains, project=project,
                       findings=mo.findings + dfind + pfind + det, metadata=meta)
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
    from codebase_analyzer.analyze import mask_secrets

    text = "app:\n  jwt:\n    signing-key: c2VjcmV0LXZhbHVl\n  datasource:\n    password: hunter2\n  host: localhost\n"
    masked = mask_secrets(text)
    assert "c2VjcmV0LXZhbHVl" not in masked and "hunter2" not in masked
    assert "host: localhost" in masked and "signing-key:" in masked  # key names stay visible


def test_reduce_evidence_must_cite_real_lines_of_input_files():
    from codebase_analyzer.analyze import _reduce_findings
    from codebase_analyzer.schema import LLMEvidence, LLMReducedFinding

    def finding(file, start, end):
        return LLMReducedFinding(category="security", severity="low", title=f"{file}:{start}", detail="d",
                                 evidence=[LLMEvidence(file=file, line_start=start, line_end=end)])

    dropped: list[str] = []
    kept = _reduce_findings([finding("a.yaml", 3, 4), finding("a.yaml", 50, 51), finding("other.java", 1, 1)],
                            "project", {"a.yaml": 10}, dropped)
    assert [f.title for f in kept] == ["a.yaml:3"] and len(dropped) == 2
