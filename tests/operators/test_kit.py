"""The operator kit: composable Lego pieces for authoring facts (balamut.operators.kit)."""

from __future__ import annotations

import ast

from balamut.core.mutants import Mutant, apply_mutant
from balamut.operators.facts import EnumContext, OperatorKind, enumerate_facts
from balamut.operators.kit import (
    AttrSwap,
    CallToArgument,
    EmptyCollection,
    HoistFirstSuiteStatement,
    Matcher,
    RegexRuleSite,
    RegionRewrite,
    Replace,
    Rewrite,
    SetFirstLiteralArg,
    assignment_target,
    attribute,
    call_of,
    const_of,
    expr_value,
    first_in_suite,
    last_in_suite,
    named,
    node_is,
    non_empty_collection,
    result_is_used,
    value_of,
)


def _applied(source: str, kind: OperatorKind) -> list[str]:
    return [apply_mutant(source, mutant) for mutant in enumerate_facts("t.py", source, (kind,))]


def test_matchers_compose() -> None:
    source = "def f(a):\n    return round(a, 2)\n"
    ctx = EnumContext("t.py", source.encode("utf-8"), [0], frozenset())
    tree = ast.parse(source)
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))

    assert call_of(func="round").matches(call, ctx)
    assert call_of(func="max").matches(call, ctx) is False
    assert (call_of(func="round") & call_of(min_args=2)).matches(call, ctx)
    assert (call_of(func="max") | call_of(func="round")).matches(call, ctx)
    assert (~call_of(func="max")).matches(call, ctx)


class _Probe(Matcher):
    """A matcher recording its call order, so operand order is observable."""

    def __init__(self, result: bool, seen: list[str], name: str) -> None:
        self.result = result
        self.seen = seen
        self.name = name

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        self.seen.append(self.name)
        return self.result


def test_and_short_circuits_left_to_right() -> None:
    node = ast.parse("x = 1\n").body[0]
    ctx = EnumContext("t.py", b"x = 1\n", [0], frozenset())
    seen: list[str] = []
    left = _Probe(False, seen, "left")
    right = _Probe(True, seen, "right")

    assert (left & right).matches(node, ctx) is False
    assert seen == ["left"]  # right never evaluated: left is False


def test_or_short_circuits_left_to_right() -> None:
    node = ast.parse("x = 1\n").body[0]
    ctx = EnumContext("t.py", b"x = 1\n", [0], frozenset())
    seen: list[str] = []
    left = _Probe(True, seen, "left")
    right = _Probe(False, seen, "right")

    assert (left | right).matches(node, ctx) is True
    assert seen == ["left"]  # right never evaluated: left is True


def test_not_matches_passes_the_node_not_the_context() -> None:
    tree = ast.parse("x = 1\n")
    ctx = EnumContext("t.py", b"x = 1\n", [0], frozenset())
    name = next(node for node in ast.walk(tree) if isinstance(node, ast.Name))

    assert (~node_is(ast.Name)).matches(name, ctx) is False


def test_result_is_used_sees_the_parent() -> None:
    source = "def f():\n    x = g()\n    h()\n    return x\n"
    tree = ast.parse(source)
    ctx = EnumContext("t.py", source.encode("utf-8"), [0], frozenset())
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    ctx.parents = parents

    assigned = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "g"
    )
    bare = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "h"
    )
    assert result_is_used().matches(assigned, ctx)
    assert not result_is_used().matches(bare, ctx)


def test_replace_splices_fixed_text() -> None:
    source = "def f():\n    label = 'hello'\n"
    kind = (Replace(match=const_of(str, non_empty=True), text='""', operator="x"),)

    assert _applied(source, kind) == ['def f():\n    label = ""\n']


def test_rewrite_with_arg_propagation() -> None:
    source = "def f():\n    return max(a, b)\n"
    kind = (Rewrite(match=call_of() & result_is_used(), transform=CallToArgument(), operator="x"),)

    assert _applied(source, kind) == ["def f():\n    return a\n", "def f():\n    return b\n"]


def test_attr_swap_and_empty_collection() -> None:
    swap = (
        Rewrite(
            match=call_of(attr="notify_all"),
            transform=AttrSwap(swaps=(("notify_all", "notify"),)),
            operator="x",
        ),
    )
    assert _applied("def f(c):\n    c.notify_all()\n", swap) == ["def f(c):\n    c.notify()\n"]

    empty = (Rewrite(match=node_is(ast.List, ast.Dict, ast.Set), transform=EmptyCollection(), operator="x"),)
    assert _applied("def f():\n    return [1, 2]\n", empty) == ["def f():\n    return []\n"]
    assert _applied("def f():\n    return {1, 2}\n", empty) == ["def f():\n    return set()\n"]


def test_regex_rule_site_rewrites_the_pattern() -> None:
    kind = (
        Rewrite(
            match=call_of(attr=frozenset({"match"}), receiver="re"),
            transform=RegexRuleSite(rules=((r"(?<!\\)\^", ""), (r"\d", r"\D"))),
            operator="x",
        ),
    )
    source = "import re\ndef f(s):\n    return re.match(r'^\\d+', s)\n"
    # The rewritten literal uses repr quoting: '\\d+' is the non-raw spelling of the same pattern.
    assert _applied(source, kind) == ["import re\ndef f(s):\n    return re.match('\\\\d+', s)\n"]


def test_regex_rule_site_hides_the_compiled_field() -> None:
    # `_compiled = field(init=False, repr=False)`: the field stays out of the constructor (a
    # RegexRuleSite builds from ``rules`` only) and out of the repr. Reloading kit would re-run the
    # class body, but kit's enums/classes are imported by every other operator module, so a reload
    # here would change their identities and break the suite.
    site = RegexRuleSite(rules=(("a", "b"),))
    assert site._compiled is not None
    assert "_compiled" not in repr(site)


def test_first_literal_arg_rewrites_only_the_literal() -> None:
    kind = (
        Rewrite(
            match=call_of(attr=frozenset({"sleep"})),
            transform=SetFirstLiteralArg(text="0"),
            operator="x",
        ),
    )
    source = "def f():\n    time.sleep(5)\n    time.sleep(timeout)\n"
    assert _applied(source, kind) == ["def f():\n    time.sleep(0)\n    time.sleep(timeout)\n"]

    # The host shape is the matcher's job: with a looser matcher the same piece fits any call whose
    # first literal argument matters (bounds, limits, backoff).
    kind = (Rewrite(match=call_of(func="clamp"), transform=SetFirstLiteralArg(text="0"), operator="x"),)
    assert _applied("def f():\n    return clamp(5, lo, hi)\n", kind) == ["def f():\n    return clamp(0, lo, hi)\n"]


def _rows(mutants: list[Mutant]) -> list[tuple[int, int, str, str, str]]:
    return [(m.start_byte, m.end_byte, m.original, m.mutated, m.operator) for m in mutants]


def test_kit_generic_pieces_cover_the_precision_shapes() -> None:
    """The generic pieces rebuild the classic authoring shapes: scaled literal, dropped argument,
    dropped keyword, numeric shift plus boolean flip."""
    from balamut.operators.kit import DropArgument, DropKeyword, FlipBool, ScaleLiteral, ShiftNumber

    def rows(source: str, kind: OperatorKind) -> list[tuple[int, int, str, str, str]]:
        return _rows(enumerate_facts("t.py", source, (kind,)))

    assert rows(
        "def f():\n    return round(x, 2)\n",
        (Rewrite(match=call_of(func="round"), transform=ScaleLiteral(scale=10, index=1), operator="p:x"),),
    ) == [(29, 30, "2", "20", "p:x")]

    assert rows(
        "def f():\n    return clamp(x, 0, 100)\n",
        (Rewrite(match=call_of(func="clamp"), transform=DropArgument(index=1), operator="p:x"),),
    ) == [(29, 32, "0, ", "", "p:x")]

    assert rows(
        "def f():\n    return fetch(url, timeout=5)\n",
        (Rewrite(match=call_of(min_args=0), transform=DropKeyword(name="timeout"), operator="p:x"),),
    ) == [(29, 40, ", timeout=5", "", "p:x")]

    assert rows(
        "def f():\n    return 3 if True else 0\n",
        (
            Rewrite(match=const_of(int), transform=ShiftNumber(delta=1), operator="p:x"),
            Rewrite(match=const_of(bool), transform=FlipBool(), operator="p:x"),
        ),
    ) == [
        (25, 29, "True", "False", "p:x"),
        (20, 21, "3", "4", "p:x"),
        (35, 36, "0", "1", "p:x"),
    ]


def test_named_attribute_and_collection_matchers() -> None:
    source = "def f():\n    x = 1\n    y = a.b\n    z = [1]\n    w = {}\n"
    tree = ast.parse(source)
    ctx = EnumContext("t.py", source.encode("utf-8"), [0], frozenset())

    names = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "x"]
    assert named("x", "y").matches(names[0], ctx)
    attributes = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute)]
    assert attribute("b").matches(attributes[0], ctx)
    collections = [node for node in ast.walk(tree) if isinstance(node, ast.List)]
    assert non_empty_collection().matches(collections[0], ctx)
    empty = next(node for node in ast.walk(tree) if isinstance(node, ast.Dict))
    assert not non_empty_collection().matches(empty, ctx)


def test_value_of_unwraps_await_for_expression_statements() -> None:
    source = "async def f(q):\n    await q.join()\n"
    tree = ast.parse(source)
    ctx = EnumContext("t.py", source.encode("utf-8"), [0], frozenset())
    statement = next(node for node in ast.walk(tree) if isinstance(node, ast.Expr))
    assert (node_is(ast.Expr) & value_of(call_of(attr=frozenset({"join"})))).matches(statement, ctx)
    assert not value_of(call_of(attr=frozenset({"join"})), through_await=False).matches(statement, ctx)


def test_suite_position_matchers() -> None:
    source = "def f():\n    a = 1\n    b = 2\n    if a:\n        c = 3\n        d = 4\n"
    tree = ast.parse(source)
    ctx = EnumContext("t.py", source.encode("utf-8"), [0], frozenset())
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    ctx.parents = parents

    assigns = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    by_name = {target.id: stmt for stmt in assigns for target in stmt.targets if isinstance(target, ast.Name)}
    assert first_in_suite().matches(by_name["a"], ctx)
    assert not first_in_suite().matches(by_name["b"], ctx)
    assert not last_in_suite().matches(by_name["b"], ctx)  # the if statement follows
    assert first_in_suite().matches(by_name["c"], ctx)
    assert last_in_suite().matches(by_name["d"], ctx)
    assert ctx.suite_index(by_name["b"]) == 1


def test_assignment_target_and_const_values() -> None:
    source = "def f():\n    x = 1\n    y = 0\n    z = x\n"
    tree = ast.parse(source)
    ctx = EnumContext("t.py", source.encode("utf-8"), [0], frozenset())
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    ctx.parents = parents

    names = {node.id: node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id in {"y", "z"}}
    assert assignment_target().matches(names["y"], ctx)
    assert assignment_target().matches(names["z"], ctx)
    x_names = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "x"]
    assert sum(assignment_target().matches(node, ctx) for node in x_names) == 1  # only the target x
    constants = [node for node in ast.walk(tree) if isinstance(node, ast.Constant)]
    assert const_of(values=(1,)).matches(constants[0], ctx)
    assert const_of(values=(1,)).matches(constants[1], ctx) is False
    assert const_of(values=(0,)).matches(constants[1], ctx)


def test_binary_operand_and_unary_and_await_finders() -> None:
    from balamut.operators.kit import BinaryOperand, RemoveAwait, UnaryOperand

    aod = (Rewrite(match=node_is(ast.BinOp), transform=BinaryOperand(side="left"), operator="x"),)
    assert _applied("def f():\n    return a + b\n", aod) == ["def f():\n    return a\n"]
    aod = (Rewrite(match=node_is(ast.BinOp), transform=BinaryOperand(side="right"), operator="x"),)
    assert _applied("def f():\n    return a + b\n", aod) == ["def f():\n    return b\n"]

    unary = (Rewrite(match=node_is(ast.UnaryOp), transform=UnaryOperand(), operator="x"),)
    assert _applied("def f():\n    return -x\n", unary) == ["def f():\n    return x\n"]
    assert _applied("def f():\n    return -(a + b)\n", unary) == ["def f():\n    return (a + b)\n"]

    awaitless = (Rewrite(match=node_is(ast.Await), transform=RemoveAwait(), operator="x"),)
    assert _applied("async def f():\n    x = await g()\n", awaitless) == ["async def f():\n    x = g()\n"]


def test_swap_call_args_and_naked_receiver() -> None:
    from balamut.operators.kit import NakedReceiver, SwapCallArgs

    esp = (Rewrite(match=call_of(min_args=2), transform=SwapCallArgs(first=0, second=1), operator="x"),)
    assert _applied("def f():\n    return g(a, b)\n", esp) == ["def f():\n    return g(b, a)\n"]

    naked = (Rewrite(match=call_of(min_args=0), transform=NakedReceiver(), operator="x"),)
    assert _applied("def f():\n    return a.b()\n", naked) == ["def f():\n    return a\n"]


def test_hoist_first_suite_statement_keeps_indentation() -> None:
    kind = (RegionRewrite(match=node_is(ast.With), transform=HoistFirstSuiteStatement(), operator="x"),)
    source = "def f(lock):\n    with lock:\n        work()\n        more()\n"
    assert _applied(source, kind) == ["def f(lock):\n    work()\n    with lock:\n        more()\n\n"]
    nested = "def f(lock):\n    if ok:\n        with lock:\n            first()\n            rest()\n"
    assert _applied(nested, kind) == [
        "def f(lock):\n    if ok:\n        first()\n        with lock:\n            rest()\n\n"
    ]


def test_expr_value_unwraps_awaited_calls() -> None:
    matcher = expr_value(call_of(attr=frozenset({"sleep", "join"})))
    source = "async def f(q):\n    await q.join()\n    x = q.join()\n"
    tree = ast.parse(source)
    ctx = EnumContext("t.py", source.encode("utf-8"), [0], frozenset())
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    ctx.parents = parents

    statements = [node for node in ast.walk(tree) if isinstance(node, ast.Expr)]
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    assert matcher.matches(statements[0], ctx)
    assert not matcher.matches(assignments[0], ctx)
