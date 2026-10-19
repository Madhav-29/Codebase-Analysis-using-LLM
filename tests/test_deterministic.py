from codebase_analyzer.deterministic import describe_abstract, describe_trivial, framework_notes, implementations_index
from codebase_analyzer.grouping import base_package, domain_for
from codebase_analyzer.java_parser import parse_java

SRC = """package p;
@Getter @Builder @RequiredArgsConstructor
public class Foo implements FooService {
    private final Repo repo;
    private String name;
    public String getName() { return this.name; }
    public void setName(String name) { this.name = name; }
    public Foo(Repo repo, String name) { this.repo = repo; this.name = name; }
    public Foo() { super(); }
    public List<X> find(Integer id) { return repo.findById(id); }
    public List<X> findOther(Integer id) { return repo.findById(id + 1); }
    public String getUpper() { return name.toUpperCase(); }
    @Override public boolean equals(Object o) { if (this == o) return true; return false; }
    @Override public String toString() { return "Foo"; }
}
interface FooService { List<X> find(Integer id); List<X> unimplemented(); }
@Mapper interface FooMapper { FooDto toDto(Foo foo); }
interface FooRepository extends JpaRepository<Foo, Integer> { List<Foo> findAllByNameContaining(String n); List<Foo> custom(); }
"""


def _setup():
    jf = parse_java(SRC)
    types = {t.name: t for t in jf.all_types()}
    return jf, types, implementations_index(list(types.values()))


def test_trivial_members_are_detected():
    _, types, _ = _setup()
    foo = types["Foo"]
    m = {f"{x.name}/{len(x.parameters)}": x for x in foo.members}
    trivial = {k for k, x in m.items() if (d := describe_trivial(x, foo)) and d.trivial}
    assert trivial == {"getName/0", "setName/1", "Foo/2", "Foo/0", "find/1", "equals/1", "toString/0"}
    # computed values and non-parameter arguments are not trivial
    assert describe_trivial(m["getUpper/0"], foo) is None
    assert describe_trivial(m["findOther/1"], foo) is None


def test_abstract_members_described_from_framework_or_implementation():
    _, types, impls = _setup()
    svc, mapper, repo = types["FooService"], types["FooMapper"], types["FooRepository"]
    find, unimpl = svc.members
    assert "implemented by Foo.find" in describe_abstract(find, svc, impls).description
    assert describe_abstract(unimpl, svc, impls) is None  # left for the LLM
    assert "MapStruct" in describe_abstract(mapper.members[0], mapper, impls).description
    derived, custom = repo.members
    assert "derived query" in describe_abstract(derived, repo, impls).description
    assert describe_abstract(custom, repo, impls) is None


def test_framework_notes_do_not_invent_methods():
    jf, types, _ = _setup()
    notes = framework_notes(types["Foo"], jf)
    assert any("@Getter" in n for n in notes) and any("@Builder" in n for n in notes)
    assert any("JpaRepository" in n for n in framework_notes(types["FooRepository"], jf))
    assert any("MapStruct" in n for n in framework_notes(types["FooMapper"], jf))
    assert all(m.name != "getRepo" for m in types["Foo"].members)  # Lombok getters are not invented


def test_domain_grouping():
    base = base_package(["com.ex.app.services.catalog.controller", "com.ex.app.common.util", "com.ex.app"])
    assert base == "com.ex.app"
    assert domain_for("com.ex.app.services.catalog.controller", base) == "catalog"
    assert domain_for("com.ex.app.common.util", base) == "common"
    assert domain_for("com.ex.app", base) == "application"
