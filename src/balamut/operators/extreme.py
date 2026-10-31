"""The extreme kind: statement deletion.

A surviving ``extreme:sdl`` mutant is a pseudo-tested statement, executed by a covering test but
with its value or effect never checked (Niedermayr et al., "Will my tests tell me if I break
this code?"). Only value/effect statements in function bodies are deleted; call statements
belong to ``delete:call``, so ``src,extreme`` together never duplicate a site.

The kind is a whole-file phase only for its emission order (function x top-statement x suite
position); the logic itself is its own domain data: the ``suite_statements`` traversal and the
``deletable_statement`` matcher below.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, replace

from balamut.core.mutants import Mutant
from balamut.core.triage import Bucket
from balamut.operators.facts import EnumContext, OperatorKind, enumerate_facts
from balamut.operators.kit import Library, Matcher, Replace, context, is_none_constant, operator_name


def _sdl_function_nodes(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function, nested ones included: a nested body is patched through its top-level ancestor."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]


def suite_statements(top: ast.stmt) -> Iterator[ast.stmt]:
    """A top-level statement and, recursively, the statements of its control-flow suites (not
    nested def/class bodies — those are enumerated on their own)."""
    yield top
    yield from _nested_suite_statements(top)


def _nested_suite_statements(stmt: ast.stmt) -> Iterator[ast.stmt]:
    match stmt:
        case (
            ast.If(body=body, orelse=orelse)
            | ast.For(body=body, orelse=orelse)
            | ast.AsyncFor(body=body, orelse=orelse)
            | ast.While(body=body, orelse=orelse)
        ):
            yield from _flatten_suites(body, orelse)
        case ast.With(body=body) | ast.AsyncWith(body=body):
            yield from _flatten_suites(body)
        case (
            ast.Try(body=body, handlers=handlers, orelse=orelse, finalbody=finalbody)
            | ast.TryStar(body=body, handlers=handlers, orelse=orelse, finalbody=finalbody)
        ):
            yield from _flatten_suites(body, orelse, finalbody, *(handler.body for handler in handlers))
        case ast.Match(cases=cases):
            yield from _flatten_suites(*(case.body for case in cases))
        case _:
            return


def _flatten_suites(*suites: list[ast.stmt]) -> Iterator[ast.stmt]:
    for suite in suites:
        for statement in suite:
            yield statement
            yield from _nested_suite_statements(statement)


@dataclass(frozen=True, slots=True)
class DeletableStatement(Matcher):
    """A statement whose deletion is a meaningful SDL mutant.

    A bare ``return``/``return None`` is deleted only outside the tail position (a guard before
    side effects); in the tail its deletion is equivalent to falling through.
    """

    allow_bare_return: bool

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        match node:
            case ast.AnnAssign(value=None):
                return False
            case ast.Return(value=None):
                return self.allow_bare_return
            case ast.Return(value=value) if value is not None and is_none_constant(value):
                return self.allow_bare_return
            case ast.Assign() | ast.AugAssign() | ast.AnnAssign() | ast.Return() | ast.Raise() | ast.Delete():
                return True
            case _:
                return False


def deletable_statement(*, allow_bare_return: bool) -> DeletableStatement:
    return DeletableStatement(allow_bare_return=allow_bare_return)


@dataclass(frozen=True, slots=True)
class ExtremeFact:
    """The extreme kind: a whole-file phase (the deletion order needs the suite structure)."""

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.HIGH  # a pseudo-tested statement: its effect is never checked

    def enumerate(self, path: str, source: str) -> list[Mutant]:
        tree = ast.parse(source)
        ctx = context(path, source)
        for func in _sdl_function_nodes(tree):
            body = func.body
            for idx, top in enumerate(body):
                # A bare return is deleted only in a non-final top-level statement: there reachable code
                # follows. Guards nested in the final statement are skipped (sound, at the cost of a few
                # missed sites).
                fact = Replace(
                    match=deletable_statement(allow_bare_return=idx < len(body) - 1),
                    text="pass",
                    operator=operator_name(Library.LANG, "statement", "delete"),
                )
                for statement in suite_statements(top):
                    ctx.node = statement
                    fact.sites(ctx)
        return [replace(mutant, fact=self) for mutant in ctx.mutants]


def enumerate_extreme(path: str, source: str) -> list[Mutant]:
    """Statement-deletion mutants: a value/effect statement of a function body becomes ``pass``."""
    return enumerate_facts(path, source, (extreme_kind,), family="extreme")


extreme_kind: OperatorKind = (ExtremeFact(),)
