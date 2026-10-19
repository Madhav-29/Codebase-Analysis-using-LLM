from codebase_analyzer.analysis.checks import deterministic_findings
from codebase_analyzer.analysis.facts import build_facts
from codebase_analyzer.config import Settings

README = """# Shop API
Supports CRUD operations on:
- /api/orders
- /api/customers
"""


def _findings(make_repo, fixture_source, readme):
    repo = make_repo(
        {"com/acme/shop/order/controller/OrderController.java": fixture_source("OrderController.java")},
        context_path="/api",
    )
    (repo / "README.md").write_text(readme, encoding="utf-8")
    return {f.title: f for f in deterministic_findings(build_facts(repo, Settings()), Settings())}


def test_readme_endpoint_claims_are_checked_against_controller_mappings(make_repo, fixture_source):
    findings = _findings(make_repo, fixture_source, README)

    listed = findings["README endpoint list differs from controller mappings"]
    assert "/api/customers" in listed.detail and "no controller maps" in listed.detail
    assert listed.source == "deterministic" and listed.category == "documentation_mismatch"
    # evidence points at the README lines that mention the API base path
    assert [(e.file, e.line_start, e.line_end) for e in listed.evidence] == [("README.md", 3, 4)]

    crud = findings["README claims CRUD support; some listed resources lack HTTP verbs"]
    assert "/api/orders: POST" in crud.detail  # the fixture maps GET, PUT, PATCH, DELETE but no POST


def test_no_readme_mismatch_when_claims_match_the_code(make_repo, fixture_source):
    findings = _findings(make_repo, fixture_source, "# Shop API\n- /api/orders\n")
    assert not any(f.category == "documentation_mismatch" for f in findings.values())
