from pathlib import Path

from codebase_analyzer.endpoints import join_path, read_context_path, resolve_endpoints
from codebase_analyzer.java_parser import parse_java


def _endpoints(fixture_source, context_path="/api/v1"):
    jf = parse_java(fixture_source("OrderController.java"), "OrderController.java")
    return {(e.http_method, e.path): e for e in resolve_endpoints([jf], context_path)}


def test_context_class_and_method_paths_are_combined(fixture_source):
    eps = _endpoints(fixture_source)
    assert set(eps) == {
        ("GET", "/api/v1/orders"),          # path = ""
        ("GET", "/api/v1/orders/all"),      # second entry of the path array
        ("PUT", "/api/v1/orders/{id}"),     # @RequestMapping(method = {PUT, PATCH})
        ("PATCH", "/api/v1/orders/{id}"),
        ("DELETE", "/api/v1/orders/{id}"),  # unnamed value argument
    }
    get = eps[("GET", "/api/v1/orders")]
    assert get.handler_method == "list" and get.line == 16
    assert get.security == ("@Secured(Roles.READ)",)
    assert not get.unresolved


def test_request_mapping_without_method_matches_any():
    jf = parse_java('@RestController class A { @RequestMapping("/x") void x() {} }')
    [ep] = resolve_endpoints([jf], "")
    assert (ep.http_method, ep.path) == ("ANY", "/x")


def test_non_literal_path_is_flagged_unresolved():
    jf = parse_java('@RestController @RequestMapping(Paths.BASE) class A { @GetMapping("/y") void y() {} }')
    [ep] = resolve_endpoints([jf], "")
    assert ep.path == "/{Paths.BASE}/y" and ep.unresolved


def test_non_controller_classes_are_ignored():
    jf = parse_java('@Service class A { @GetMapping("/y") void y() {} }')
    assert resolve_endpoints([jf], "") == []


def test_join_path_normalizes_slashes():
    assert join_path("/api/v1/", "/orders/", "") == "/api/v1/orders"
    assert join_path("", "", "") == "/"


def test_context_path_from_yaml(tmp_path: Path):
    (tmp_path / "application.yaml").write_text("---\nserver:\n  servlet:\n    context-path: /api/v1\n")
    assert read_context_path(tmp_path) == "/api/v1"
    assert read_context_path(tmp_path / "missing") == ""
