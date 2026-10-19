from codebase_analyzer.planning.chunking import plan_units
from codebase_analyzer.config import Settings, TokenBudget
from codebase_analyzer.analysis.facts import build_facts

SEPARATOR = "\n    ...\n"  # between the skeleton and the member bodies of a split part


def _method(i: int, lines: int = 8) -> str:
    body = "\n".join(f"        total += value * {j} + {i};" for j in range(lines))
    return f"""
    public int compute{i}(int value) {{
        int total = 0;
        if (value > {i}) {{ total = {i}; }}
{body}
        return total;
    }}
"""


def _big_class(n_methods: int, giant_lines: int = 0) -> str:
    giant = _method(999, giant_lines) if giant_lines else ""
    return ("package com.acme.calc;\n\npublic class Calculator {\n    private int state;\n"
            + "".join(_method(i) for i in range(n_methods)) + giant + "}\n")


def _small_settings(request_tokens: int) -> Settings:
    s = Settings()
    s.budget = TokenBudget(request_tokens=request_tokens, output_reserve=500, safety_margin_ratio=0.1)
    return s


def _body_lines(unit) -> list[int]:
    body = unit.prompt.blocks[0].code.split(SEPARATOR, 1)[1]
    return [int(line.split("|", 1)[0]) for line in body.splitlines()]


def _plan(root, settings):
    facts = build_facts(root, settings)
    return facts, plan_units(facts, settings, sorted(f.path for f in facts.files))


def test_oversized_class_splits_at_member_boundaries(make_repo):
    settings = _small_settings(4_000)
    facts, plan = _plan(make_repo({"com/acme/calc/Calculator.java": _big_class(30)}), settings)

    assert not plan.failed
    assert len(plan.units) > 1 and all(u.kind == "split" for u in plan.units)
    assert all(u.est_tokens <= settings.budget.input_limit for u in plan.units)

    members = list(facts.members())
    # every member that needs a description is requested in exactly one part
    requested = [mid for u in plan.units for mid in u.member_keys.values()]
    assert sorted(requested) == sorted(m.id for m in members if m.needs_llm)
    # nothing truncated: every member line is shown exactly once across parts
    expected = sorted(n for m in members for n in range(m.member.line_start, m.member.line_end + 1))
    assert sorted(n for u in plan.units for n in _body_lines(u)) == expected
    # each part starts on a member boundary and repeats the skeleton
    starts = {m.member.line_start for m in members}
    for u in plan.units:
        assert _body_lines(u)[0] in starts
        assert "public class Calculator" in u.user_text and "private int state" in u.user_text


def test_single_oversized_member_is_segmented_and_recorded(make_repo):
    settings = _small_settings(4_000)
    facts, plan = _plan(make_repo({"com/acme/calc/Calculator.java": _big_class(2, giant_lines=400)}), settings)

    giant = next(m for m in facts.members() if m.member.name == "compute999")
    segment_units = [u for u in plan.units if giant.id in u.member_keys.values()]
    assert len(segment_units) > 1
    assert all(giant.id in u.segmented_members for u in plan.units)
    labels = [r.segment for u in segment_units for r in u.prompt.blocks[0].members]
    assert labels and all(lbl and lbl.startswith("segment ") for lbl in labels)
    assert all(u.est_tokens <= settings.budget.input_limit for u in plan.units)
    # segments partition the giant member's lines with no gaps or overlap
    seg_lines = sorted(n for u in segment_units for n in _body_lines(u))
    assert seg_lines == list(range(giant.member.line_start, giant.member.line_end + 1))


def test_file_that_cannot_fit_is_reported_not_truncated(make_repo):
    fields = "\n".join(f"    private int field{i};" for i in range(400))  # skeleton alone is too large
    src = f"package com.acme.big;\npublic class Big {{\n{fields}\n    int f() {{ return 1 + field0; }}\n}}\n"
    settings = _small_settings(3_000)
    _, plan = _plan(make_repo({"com/acme/big/Big.java": src}), settings)
    assert plan.units == []
    assert plan.failed and "skeleton" in plan.failed[0][1]


def test_small_classes_are_packed_within_package_only(make_repo):
    files = {f"com/acme/a/dto/Dto{i}.java": f"package com.acme.a.dto;\n@Getter\npublic class Dto{i} {{ private int x; }}\n"
             for i in range(10)}
    files["com/acme/a/other/Lonely.java"] = "package com.acme.a.other;\npublic class Lonely { private int y; }\n"
    settings = Settings()
    _, plan = _plan(make_repo(files), settings)

    packs = [u for u in plan.units if u.kind == "pack"]
    assert sorted(len(p.files) for p in packs) == [2, 8]  # capped at max_classes
    assert all(all("/dto/" in f for f in p.files) for p in packs)
    lonely = [u for u in plan.units if u.files == ["src/main/java/com/acme/a/other/Lonely.java"]]
    assert lonely and lonely[0].kind == "class"
    assert all(u.est_tokens <= settings.budget.input_limit for u in plan.units)


def test_complex_classes_are_never_packed(make_repo):
    branches = " ".join(f"if (v == {i}) return {i};" for i in range(10))
    files = {
        "com/acme/b/Simple.java": "package com.acme.b;\npublic class Simple { int x; }\n",
        "com/acme/b/Branchy.java": f"package com.acme.b;\npublic class Branchy {{ int f(int v) {{ {branches} return 0; }} }}\n",
    }
    _, plan = _plan(make_repo(files), Settings())
    assert {u.kind for u in plan.units} == {"class"}
