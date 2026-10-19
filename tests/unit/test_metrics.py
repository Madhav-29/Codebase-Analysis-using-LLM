from codebase_analyzer.analysis.java_parser import parse_java
from codebase_analyzer.analysis.metrics import complexity_band, member_complexity


def test_metrics_join_is_per_member_even_where_file_level_lizard_fails(fixture_source):
    # lizard's whole-file parse returns no functions for this fixture; per-member
    # analysis must still produce metrics for every member with a body.
    jf = parse_java(fixture_source("OrderController.java"))
    members = {m.name: m for t in jf.all_types() for m in t.members}
    lst = member_complexity(members["list"])
    assert lst.cyclomatic == 2  # one `if`
    assert lst.parameter_count == 1
    assert member_complexity(members["update"]).parameter_count == 2


def test_abstract_member_has_no_metrics(fixture_source):
    jf = parse_java(fixture_source("Types.java"))
    repo = next(t for t in jf.all_types() if t.name == "Repo")
    abstract = next(m for m in repo.members if m.name == "findAllByName")
    assert member_complexity(abstract) is None


def test_complexity_bands():
    th = {"moderate": 6, "high": 11, "very_high": 21}
    assert [complexity_band(c, th) for c in (1, 6, 11, 21)] == ["low", "moderate", "high", "very_high"]
