from codebase_analyzer.analysis.java_parser import parse_java


def _members(jf, type_name):
    t = next(t for t in jf.all_types() if t.name == type_name)
    return t, {m.name: m for m in t.members}


def test_controller_type_facts(fixture_source):
    jf = parse_java(fixture_source("OrderController.java"), "OrderController.java")
    assert not jf.has_errors
    assert jf.package == "com.acme.shop.order.controller"
    assert jf.imports == ["java.util.List", "org.springframework.web.bind.annotation.*"]

    ctrl = jf.types[0]
    assert ctrl.fqn == "com.acme.shop.order.controller.OrderController"
    assert ctrl.type_parameters == "<T extends Number>"
    assert ctrl.extends == ["BaseController<T>"]
    assert ctrl.implements == ["Auditable", "Comparable<OrderController<T>>"]
    assert [a.name for a in ctrl.annotations] == ["RestController", "RequestMapping"]
    assert ctrl.annotations[1].arguments == {"value": '"/orders"'}
    # line range starts at the first annotation and ends at the closing brace
    assert (ctrl.line_start, ctrl.line_end) == (6, 47)
    assert [f.name for f in ctrl.fields] == ["orderService", "a", "b"]


def test_method_signatures_and_annotations(fixture_source):
    jf = parse_java(fixture_source("OrderController.java"))
    _, m = _members(jf, "OrderController")

    assert m["list"].signature == "public ResponseEntity<List<OrderDto>> list(String status)"
    assert (m["list"].line_start, m["list"].line_end) == (16, 23)
    assert m["list"].annotations[0].arguments == {"path": ['""', '"/all"']}
    assert m["list"].parameters[0].annotations[0].name == "RequestParam"

    upd = m["update"]
    assert upd.signature == "public <R> R update(Integer id, String... tags) throws IllegalStateException"
    assert upd.return_type == "R" and upd.type_parameters == "<R>"
    assert upd.parameters[1].varargs
    assert upd.annotations[0].arguments["method"] == ["RequestMethod.PUT", "RequestMethod.PATCH"]

    assert m["delete"].annotations[0].arguments == {"value": '"/{id}"'}
    assert m["OrderController"].kind == "constructor" and m["OrderController"].return_type is None


def test_nested_type_has_qualified_name(fixture_source):
    jf = parse_java(fixture_source("OrderController.java"))
    nested = jf.types[0].nested[0]
    assert nested.fqn == "com.acme.shop.order.controller.OrderController.Page"
    assert nested.modifiers == ["static"]


def test_record_enum_interface(fixture_source):
    jf = parse_java(fixture_source("Types.java"))
    assert not jf.has_errors
    money, mm = _members(jf, "Money")
    assert money.kind == "record"
    assert [(p.name, p.type) for p in money.record_components] == [("amount", "long"), ("currency", "String")]
    assert money.record_components[1].annotations[0].name == "NotNull"
    assert mm["Money"].kind == "constructor"  # compact constructor
    assert money.implements == ["Comparable<Money>"]

    status, sm = _members(jf, "Status")
    assert status.kind == "enum" and status.enum_constants == ["OPEN", "CLOSED"]
    assert [f.name for f in status.fields] == ["label"]
    assert sum(1 for x in status.members if x.kind == "constructor") == 2
    assert sm["label"].signature == "public String label()"

    repo, rm = _members(jf, "Repo")
    assert repo.kind == "interface"
    assert repo.extends == ["Base<E>", "Other"]
    assert rm["findAllByName"].body is None  # abstract declaration
    assert rm["exists"].body is not None and "default" in rm["exists"].modifiers
