"""The capability substrate: EnumContext.emit spans and the single-pass enumeration fold.

The contract every ported kind relies on: byte-exact UTF-8 spans, skip of no-op replacements,
site lines of multi-line targets, per-kind emission order, non-body exclusion and fact
propagation onto the mutant.
"""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass, replace

import pytest

from balamut.core.mutants import Mutant
from balamut.core.source_spans import line_byte_starts, node_byte_span
from balamut.operators import facts as facts_module
from balamut.operators.facts import (
    EnumContext,
    OperatorKind,
    _suite_candidates,
    enumerate_facts,
)


def _context(source: str) -> EnumContext:
    return EnumContext(
        path="mod.py",
        source_bytes=source.encode("utf-8"),
        line_starts=line_byte_starts(source),
        non_body=frozenset(),
    )


@dataclass(frozen=True, slots=True)
class ConstShift:
    """Replaces every integer constant with itself plus ``delta``."""

    delta: int

    def sites(self, ctx: EnumContext) -> EnumContext:
        node = ctx.node
        if node is not None and isinstance(node, ast.Constant) and isinstance(node.value, int):
            ctx.emit(node, str(node.value + self.delta), "const:num:shift", fact=self)
        return ctx


@dataclass(frozen=True, slots=True)
class ReturnNone:
    """Replaces ``return E`` with ``return None``."""

    def sites(self, ctx: EnumContext) -> EnumContext:
        node = ctx.node
        if node is not None and isinstance(node, ast.Return):
            ctx.emit(node, "return None", "return:None", fact=self)
        return ctx


@dataclass(frozen=True, slots=True)
class OnlyFamily:
    """A fact without Sites: the fold must skip it."""

    def family(self) -> str:
        return "custom"


def test_emit_splices_byte_exact_after_multibyte_text() -> None:
    source = 'def f():\n    label = "héllo"  # wörld\n    return 3\n'
    tree = ast.parse(source)
    ctx = _context(source)
    ctx.node = next(node for node in ast.walk(tree) if isinstance(node, ast.Return))
    ReturnNone().sites(ctx)
    (mutant,) = ctx.mutants

    start = source.encode("utf-8").index(b"return 3")
    assert (mutant.start_byte, mutant.end_byte) == (start, start + len("return 3"))
    assert mutant.original == "return 3"
    assert mutant.mutated == "return None"
    assert mutant.operator == "return:None"
    assert (mutant.site_lineno, mutant.site_end_lineno) == (3, 3)
    assert mutant.path == "mod.py"


def test_emit_skips_noop_replacement() -> None:
    source = "def f():\n    return 3\n"
    ctx = _context(source)
    ctx.node = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Return))
    ctx.emit(ctx.node, "return 3", "return:None")
    assert ctx.mutants == []


def test_emit_site_lines_span_a_multiline_target() -> None:
    source = "def f(values):\n    return sum(\n        values,\n    )\n"
    tree = ast.parse(source)
    ctx = _context(source)
    ctx.node = next(node for node in ast.walk(tree) if isinstance(node, ast.Return))
    ReturnNone().sites(ctx)
    (mutant,) = ctx.mutants

    assert mutant.lineno == 2
    assert mutant.site_end_lineno == 4


def test_emit_records_the_emitting_fact() -> None:
    source = "def f():\n    return 3\n"
    ctx = _context(source)
    fact = ReturnNone()
    ctx.node = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Return))
    fact.sites(ctx)
    assert ctx.mutants[0].fact is fact


def test_fold_orders_per_kind_and_skips_non_site_facts() -> None:
    source = "def f(x):\n    return x + 1\n"
    kinds: tuple[OperatorKind, ...] = ((ConstShift(1), OnlyFamily()), (ReturnNone(),))
    mutants = enumerate_facts("mod.py", source, kinds)

    assert [mutant.operator for mutant in mutants] == ["const:num:shift", "return:None"]


def test_fold_excludes_non_body_sites() -> None:
    source = "@dec(1)\ndef f(x=2):\n    return 3\n"

    @dataclass(frozen=True, slots=True)
    class BodyOnly(ConstShift):
        def sites(self, ctx: EnumContext) -> EnumContext:
            node = ctx.node
            if node is not None and id(node) in ctx.non_body:
                return ctx
            return super().sites(ctx)

    mutants = enumerate_facts("mod.py", source, ((BodyOnly(1),),))
    assert {mutant.original for mutant in mutants} == {"3"}


def test_fold_rejects_a_fact_replacing_the_context() -> None:
    source = "def f():\n    return 3\n"

    @dataclass(frozen=True, slots=True)
    class Bad:
        def sites(self, ctx: EnumContext) -> EnumContext:
            return replace(ctx)

    with pytest.raises(AssertionError, match="must return the context"):
        enumerate_facts("mod.py", source, ((Bad(),),))


def test_emit_accepts_a_span_override_for_removal_splices() -> None:
    source = "def q(stmt):\n    return stmt.where(A, B)\n"
    tree = ast.parse(source)
    ctx = _context(source)
    ctx.node = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
    arguments = ctx.node.args
    spans = [node_byte_span(argument, ctx.line_starts) for argument in arguments]
    first, second = arguments
    assert spans[0] is not None
    assert spans[1] is not None
    # The removal of the first item runs up to the next item's start: `A, ` goes away.
    ctx.emit(first, "", "db:where-predicate-drop", fact=None, span=(spans[0][0], spans[1][0]))
    (mutant,) = ctx.mutants

    assert (mutant.start_byte, mutant.end_byte) == (spans[0][0], spans[1][0])
    assert mutant.original == "A, "
    assert mutant.lineno == first.lineno
    # Removing the last item runs from the previous item's end: the separator goes with it.
    ctx.mutants.clear()
    ctx.emit(second, "", "db:where-predicate-drop", fact=None, span=(spans[0][1], spans[1][1]))
    assert ctx.mutants[0].original == ", B"


def test_mutant_fact_defaults_to_none() -> None:
    mutant = Mutant("mod.py", 1, 0, 1, "a", "b", "compare:Lt->LtE")
    assert mutant.fact is None


def _parent_context(source: str) -> tuple[ast.Module, EnumContext]:
    tree = ast.parse(source)
    ctx = _context(source)
    ctx.parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    return tree, ctx


def test_suite_of_finds_node_in_a_later_suite() -> None:
    source = "def f():\n    if a:\n        x = 1\n    else:\n        y = 2\n"
    tree, ctx = _parent_context(source)
    assigns = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    y = assigns[1]

    # The node lives in the orelse, the second candidate suite of an If.
    assert ctx.suite_of(y) is not None
    assert ctx.suite_index(y) == 0


def test_suite_candidates_covers_every_parent_kind() -> None:
    source = (
        "def f(x):\n"
        "    try:\n"
        "        a = 1\n"
        "    except ValueError:\n"
        "        b = 2\n"
        "    match x:\n"
        "        case 0:\n"
        "            c = 3\n"
    )
    tree = ast.parse(source)
    try_node = next(node for node in ast.walk(tree) if isinstance(node, ast.Try))
    handler = try_node.handlers[0]
    match_node = next(node for node in ast.walk(tree) if isinstance(node, ast.Match))

    # A Try yields its body, orelse, finalbody and every handler body.
    assert _suite_candidates(try_node)[0] == try_node.body
    # An ExceptHandler yields just its body.
    assert _suite_candidates(handler) == [handler.body]
    # A Match yields the body of every case.
    assert _suite_candidates(match_node) == [case.body for case in match_node.cases]
    # Anything else (here the module itself) yields nothing.
    assert _suite_candidates(ast.parse("pass\n")) == []


def test_sites_step_registry_dispatches_sites_facts() -> None:
    # `sites_step = by_protocol(Sites, _sites_apply)` is a module-level registry; a reload re-runs
    # it so the coverage substrate attributes the line to this test. The step must dispatch a fact
    # that implements Sites and skip one that does not.
    importlib.reload(facts_module)

    source = "def f():\n    return 3\n"
    tree = ast.parse(source)
    ret = next(node for node in ast.walk(tree) if isinstance(node, ast.Return))

    ctx = _context(source)
    ctx.node = ret
    assert facts_module.sites_step.fn(ReturnNone(), ctx) is ctx
    assert len(ctx.mutants) == 1

    ctx2 = _context(source)
    ctx2.node = ret
    assert facts_module.sites_step.fn(OnlyFamily(), ctx2) is ctx2
    assert ctx2.mutants == []
