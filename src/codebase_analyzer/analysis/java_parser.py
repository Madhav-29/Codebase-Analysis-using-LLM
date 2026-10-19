"""Structural Java parsing with tree-sitter.

Extracts packages, imports, type declarations (including nested types),
fields, methods, constructors, annotations, modifiers, generics and line ranges.
Everything here is a deterministic fact; nothing is inferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import tree_sitter_java
from tree_sitter import Language, Node, Parser

_PARSER = Parser(Language(tree_sitter_java.language()))

TYPE_NODES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
MEMBER_NODES = {
    "method_declaration": "method",
    "constructor_declaration": "constructor",
    "compact_constructor_declaration": "constructor",
    "annotation_type_element_declaration": "method",
}


@dataclass
class Annotation:
    name: str  # simple name, e.g. "GetMapping"
    text: str  # source text, e.g. '@GetMapping(path = "/{id}")'
    # Argument values as source text, keyed by element name ("value" for the
    # single unnamed argument). Array initializers become lists.
    arguments: dict[str, str | list[str]] = field(default_factory=dict)


@dataclass
class Parameter:
    name: str
    type: str
    annotations: list[Annotation] = field(default_factory=list)
    varargs: bool = False


@dataclass
class FieldDecl:
    name: str
    type: str
    modifiers: list[str]
    annotations: list[Annotation]
    line: int


@dataclass
class Member:
    kind: str  # method | constructor
    name: str
    signature: str
    modifiers: list[str]
    annotations: list[Annotation]
    parameters: list[Parameter]
    return_type: str | None  # None for constructors
    type_parameters: str | None
    throws: list[str]
    line_start: int
    line_end: int
    source: str  # full declaration text including annotations
    body: str | None  # None for abstract/interface declarations


@dataclass
class TypeDecl:
    name: str
    fqn: str
    kind: str
    modifiers: list[str]
    annotations: list[Annotation]
    extends: list[str]
    implements: list[str]
    type_parameters: str | None
    line_start: int
    line_end: int
    header: str  # declaration line(s) up to the opening brace
    fields: list[FieldDecl] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)
    nested: list[TypeDecl] = field(default_factory=list)
    enum_constants: list[str] = field(default_factory=list)
    record_components: list[Parameter] = field(default_factory=list)

    def walk(self):
        """Yield this type and all nested types, depth first."""
        yield self
        for n in self.nested:
            yield from n.walk()


@dataclass
class JavaFile:
    path: str
    package: str
    imports: list[str]
    types: list[TypeDecl]
    source: str
    line_count: int
    has_errors: bool  # tree-sitter reported a syntax error somewhere

    def all_types(self):
        for t in self.types:
            yield from t.walk()


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8") if node is not None else ""


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _end_line(node: Node) -> int:
    return node.end_point[0] + 1


def _modifiers(node: Node) -> tuple[list[str], list[Annotation]]:
    mods: list[str] = []
    anns: list[Annotation] = []
    for child in node.children:
        if child.type != "modifiers":
            continue
        for m in child.children:
            if m.type in ("annotation", "marker_annotation"):
                anns.append(_annotation(m))
            else:
                mods.append(_text(m))
    return mods, anns


def _annotation_value(node: Node) -> str | list[str]:
    if node.type == "element_value_array_initializer":
        return [_text(c) for c in node.named_children]
    return _text(node)


def _annotation(node: Node) -> Annotation:
    name = _text(node.child_by_field_name("name")).rsplit(".", 1)[-1]
    args: dict[str, str | list[str]] = {}
    arg_list = node.child_by_field_name("arguments")
    if arg_list is not None:
        for arg in arg_list.named_children:
            if arg.type == "element_value_pair":
                args[_text(arg.child_by_field_name("key"))] = _annotation_value(arg.child_by_field_name("value"))
            else:
                args["value"] = _annotation_value(arg)
    return Annotation(name=name, text=_text(node), arguments=args)


def _parameters(params_node: Node | None) -> list[Parameter]:
    result: list[Parameter] = []
    if params_node is None:
        return result
    for p in params_node.named_children:
        _, anns = _modifiers(p)
        if p.type == "formal_parameter":
            result.append(Parameter(_text(p.child_by_field_name("name")), _text(p.child_by_field_name("type")), anns))
        elif p.type == "spread_parameter":
            type_node = next(c for c in p.named_children if c.type not in ("modifiers", "variable_declarator"))
            declarator = next(c for c in p.named_children if c.type == "variable_declarator")
            result.append(Parameter(_text(declarator.child_by_field_name("name")), _text(type_node), anns, varargs=True))
    return result


def _member(node: Node, kind: str) -> Member:
    mods, anns = _modifiers(node)
    name = _text(node.child_by_field_name("name"))
    tparams = node.child_by_field_name("type_parameters")
    ret = node.child_by_field_name("type")
    params = _parameters(node.child_by_field_name("parameters"))
    throws_node = next((c for c in node.children if c.type == "throws"), None)
    throws = [_text(c) for c in throws_node.named_children] if throws_node else []
    body = node.child_by_field_name("body")

    param_text = ", ".join(f"{p.type}{'...' if p.varargs else ''} {p.name}" for p in params)
    parts = [*mods, _text(tparams) if tparams else "", _text(ret) if ret else "", f"{name}({param_text})"]
    signature = " ".join(p for p in parts if p)
    if throws:
        signature += " throws " + ", ".join(throws)
    return Member(
        kind=kind,
        name=name,
        signature=signature,
        modifiers=mods,
        annotations=anns,
        parameters=params,
        return_type=_text(ret) if ret else None,
        type_parameters=_text(tparams) if tparams else None,
        throws=throws,
        line_start=_line(node),
        line_end=_end_line(node),
        source=_text(node),
        body=_text(body) if body is not None else None,
    )


def _type_list(node: Node | None) -> list[str]:
    if node is None:
        return []
    type_list = next((c for c in node.named_children if c.type == "type_list"), None)
    return [_text(c) for c in (type_list.named_children if type_list else node.named_children)]


def _type_decl(node: Node, prefix: str) -> TypeDecl:
    kind = TYPE_NODES[node.type]
    mods, anns = _modifiers(node)
    name = _text(node.child_by_field_name("name"))
    fqn = f"{prefix}.{name}" if prefix else name
    body = node.child_by_field_name("body")
    tparams = node.child_by_field_name("type_parameters")

    if kind == "interface":
        extends = _type_list(next((c for c in node.children if c.type == "extends_interfaces"), None))
        implements: list[str] = []
    else:
        superclass = node.child_by_field_name("superclass")
        extends = [_text(superclass.named_children[0])] if superclass else []
        implements = _type_list(node.child_by_field_name("interfaces"))

    header_end = body.start_byte if body is not None else node.end_byte
    header = node.text[: header_end - node.start_byte].decode("utf-8").strip()

    decl = TypeDecl(
        name=name, fqn=fqn, kind=kind, modifiers=mods, annotations=anns,
        extends=extends, implements=implements,
        type_parameters=_text(tparams) if tparams else None,
        line_start=_line(node), line_end=_end_line(node), header=header,
    )
    if kind == "record":
        decl.record_components = _parameters(node.child_by_field_name("parameters"))
    if body is not None:
        _collect_body(body, decl)
    return decl


def _collect_body(body: Node, decl: TypeDecl) -> None:
    for child in body.named_children:
        if child.type == "enum_constant":
            decl.enum_constants.append(_text(child.child_by_field_name("name")))
        elif child.type == "enum_body_declarations":
            _collect_body(child, decl)
        elif child.type in ("field_declaration", "constant_declaration"):
            mods, anns = _modifiers(child)
            ftype = _text(child.child_by_field_name("type"))
            for d in child.children_by_field_name("declarator"):
                decl.fields.append(FieldDecl(_text(d.child_by_field_name("name")), ftype, mods, anns, _line(child)))
        elif child.type in MEMBER_NODES:
            decl.members.append(_member(child, MEMBER_NODES[child.type]))
        elif child.type in TYPE_NODES:
            decl.nested.append(_type_decl(child, decl.fqn))


def parse_java(source: str, path: str = "<memory>") -> JavaFile:
    tree = _PARSER.parse(source.encode("utf-8"))
    root = tree.root_node
    package = ""
    imports: list[str] = []
    types: list[TypeDecl] = []
    for child in root.named_children:
        if child.type == "package_declaration":
            package = _text(next(c for c in child.named_children if "identifier" in c.type))
        elif child.type == "import_declaration":
            imports.append(_text(child).removeprefix("import").removesuffix(";").strip())
        elif child.type in TYPE_NODES:
            types.append(_type_decl(child, package))
    return JavaFile(
        path=path, package=package, imports=imports, types=types, source=source,
        line_count=source.count("\n") + (0 if source.endswith("\n") else 1),
        has_errors=root.has_error,
    )
