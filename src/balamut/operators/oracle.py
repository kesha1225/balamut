"""The oracle kind: mutations of the tests themselves, so a test that cannot fail shows up.

- ``oracle:negate``: ``assert X`` -> ``assert not (X)``. A reached true assert fails (killed); a survivor
  means the condition is false or unreachable: a degenerate or dead assertion. ``assert <const>`` is skipped.
- ``oracle:raises``: the expected type of ``pytest.raises`` becomes a foreign sentinel.
- ``oracle:boundary``: ``assert x <= C`` -> ``assert x < C``. A survivor means no test produces ``x == C``:
  the boundary is never exercised. Equivalence-prone, so it triages as REVIEW.

The kind is three walk facts over the collectable-test scope: a Prepare pass computes, per file,
the ids of the assertions and ``pytest.raises`` calls inside collectable tests, and the facts'
matchers select the scope through the prepared sets.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

from balamut.core.mutants import BcEdit, Mutant
from balamut.core.triage import Bucket
from balamut.operators.facts import EnumContext, OperatorKind, enumerate_facts
from balamut.operators.kit import (
    Library,
    Matcher,
    Rewrite,
    SiteFinder,
    call_of,
    node_is,
    operator_name,
)

ORACLE_UNRAISED_NAME = "balamut_oracle_unraised"

type OracleGlobals = dict[str, object]

_ASSERTS_KEY = "oracle:assert-scope"
_RAISES_KEY = "oracle:raises-scope"


class _OracleUnraisedError(Exception):
    """A sentinel no code ever raises.

    ``oracle:raises`` swaps the expected type in ``pytest.raises(X)`` for it: the real exception no longer
    matches, propagates and fails the test (killed: the expectation is alive). A green test means the block
    raised nothing incompatible: a degenerate or suspicious expectation."""


def oracle_globals() -> OracleGlobals:
    return {ORACLE_UNRAISED_NAME: _OracleUnraisedError}


@dataclass(frozen=True, slots=True)
class _InPrepared(Matcher):
    """A node whose id sits in the prepared test-scope set (the kind's Prepare pass)."""

    key: str

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        ids = ctx.prepared(self.key)
        return isinstance(ids, frozenset) and id(node) in ids


def collectable_scope_ids(tree: ast.Module, matcher: Matcher, ctx: EnumContext) -> frozenset[int]:
    """Ids of nodes matching ``matcher`` inside collectable pytest tests: module-level ``test_*``
    functions or ``test_*`` methods of ``Test*`` classes."""
    ids: set[int] = set()
    for node in tree.body:
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() if node.name.startswith("test_"):
                ids.update(id(child) for child in ast.walk(node) if matcher.matches(child, ctx))
            case ast.ClassDef() if node.name.startswith("Test"):
                for method in node.body:
                    if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef) and method.name.startswith("test_"):
                        ids.update(id(child) for child in ast.walk(method) if matcher.matches(child, ctx))
            case _:
                pass
    return frozenset(ids)


def _prepare_scope(ctx: EnumContext, tree: ast.Module, key: str, matcher: Matcher) -> EnumContext:
    """Remember the ids of ``matcher`` inside collectable tests under ``key`` (once per file)."""
    if ctx.prepared(key) is None:
        ctx.remember(key, collectable_scope_ids(tree, matcher, ctx))
    return ctx


@dataclass(frozen=True, slots=True)
class _NegateFind(SiteFinder):
    """(the assert's test, ``not (...)``); a constant test is skipped (never a boundary)."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Assert) and not isinstance(node.test, ast.Constant):
            yield node.test, f"not ({ast.unparse(node.test)})", None, None, None


@dataclass(frozen=True, slots=True)
class _RaisesFind(SiteFinder):
    """(the expected exception, the sentinel) of a ``pytest.raises`` call."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Call) and node.args and not isinstance(node.args[0], ast.Starred):
            yield node.args[0], ORACLE_UNRAISED_NAME, None, None, None


@dataclass(frozen=True, slots=True)
class _BoundaryFind(SiteFinder):
    """(the compare, its tightened form): exactly one ``<=``/``>=`` becomes strict.

    Chains and other operators are skipped (a chain has no single boundary, and loosening a strict
    comparison always survives)."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Assert):
            return
        for sub in ast.walk(node.test):
            if not isinstance(sub, ast.Compare) or len(sub.ops) != 1:
                continue
            tightened: list[ast.cmpop] | None = None
            match sub.ops[0]:
                case ast.LtE():
                    tightened = [ast.Lt()]
                case ast.GtE():
                    tightened = [ast.Gt()]
                case _:
                    pass
            if tightened is not None:
                mutated = ast.unparse(ast.Compare(left=sub.left, ops=tightened, comparators=sub.comparators))
                yield sub, mutated, None, None, None


@dataclass(frozen=True, slots=True, init=False)
class OracleFact:
    """The oracle kind's marker: bare-constructable (``OracleFact()``), so tests and the engine
    carry it as a fact (``family``/``self_covered``); ``enumerate`` runs the whole kind.

    The covering test of each mutant is its own function: coverage does not cover test files, so
    the engine selects the covering test by qualname."""

    self_covered: ClassVar[bool] = True

    def __init__(self) -> None:
        pass

    def enumerate(self, path: str, source: str) -> list[Mutant]:
        return enumerate_facts(path, source, (oracle_kind,), family="oracle")


@dataclass(frozen=True, slots=True, init=False)
class OracleWalkFact(Rewrite):
    """The shared shape of the oracle walk facts: the kind's markers, spread over the walk."""

    self_covered: ClassVar[bool] = True

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.REVIEW  # a weakened assertion: equivalence-prone by construction


@dataclass(frozen=True, slots=True)
class AssertNegation(OracleWalkFact):
    """The ``oracle:negate`` walk fact."""

    match: Matcher = node_is(ast.Assert) & _InPrepared(_ASSERTS_KEY)
    transform: SiteFinder = _NegateFind()
    operator: str = operator_name(Library.LANG, "assert", "negate")

    def prepare(self, ctx: EnumContext, tree: ast.Module) -> EnumContext:
        return _prepare_scope(ctx, tree, _ASSERTS_KEY, node_is(ast.Assert))


@dataclass(frozen=True, slots=True)
class RaisesExc(OracleWalkFact):
    """The ``oracle:raises`` walk fact."""

    match: Matcher = node_is(ast.Call) & _InPrepared(_RAISES_KEY)
    transform: SiteFinder = _RaisesFind()
    operator: str = operator_name(Library.PYTEST, "raises")

    def prepare(self, ctx: EnumContext, tree: ast.Module) -> EnumContext:
        matcher = call_of(attr="raises", receiver="pytest", min_args=1) | call_of(func="raises", min_args=1)
        return _prepare_scope(ctx, tree, _RAISES_KEY, matcher)


@dataclass(frozen=True, slots=True)
class AssertBoundaryTighten(OracleWalkFact):
    """The ``oracle:boundary`` walk fact."""

    match: Matcher = node_is(ast.Assert) & _InPrepared(_ASSERTS_KEY)
    transform: SiteFinder = _BoundaryFind()
    operator: str = operator_name(Library.LANG, "assert", "boundary")

    def prepare(self, ctx: EnumContext, tree: ast.Module) -> EnumContext:
        return _prepare_scope(ctx, tree, _ASSERTS_KEY, node_is(ast.Assert))


oracle_kind: OperatorKind = (AssertNegation(), RaisesExc(), AssertBoundaryTighten())


def enumerate_oracle(path: str, source: str) -> list[Mutant]:
    """Oracle mutants: mutate the test's own assertions (the covering test is the test itself)."""
    return OracleFact().enumerate(path, source)
