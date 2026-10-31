"""Conservative bucketing of surviving mutants by static signal.

``LOW`` (probable noise) is assigned only by one narrow, high-precision rule: deleting a logging call. Every
ambiguous case (``const:num``, ``unary``, ``augassign``) is ``REVIEW`` rather than ``LOW``, so equivalence is
never declared statically and a real gap is never hidden. Decision, arithmetic and state-mutating mutations
are ``HIGH``.

This is prioritization, not a replacement for a human or model review: what to look at first and what is
almost certainly noise. Sort survivors by (bucket, -covering): many covering tests that still miss a mutant is
the strongest signal.
"""

from __future__ import annotations

import ast
from enum import Enum
from typing import Protocol, runtime_checkable

from balamut.core.astcache import parse_module
from balamut.core.mutants import Mutant
from balamut.core.source_spans import line_byte_starts, node_byte_span


class Bucket(Enum):
    HIGH = "high"  # decision / arithmetic / state mutation / return: look first
    REVIEW = "review"  # conservative default: const:num / unary / augassign, unknown
    LOW = "low"  # probable noise: a deleted logging call


class ColdKind(Enum):
    """Statically provable kind of a site outside the per-test runtime path."""

    TYPE_ONLY = "type_only"
    SCHEMA_DEFAULT = "schema_default"
    IMPORT_RUNTIME = "import_runtime"
    UNATTRIBUTED = "unattributed"


@runtime_checkable
class Bucketed(Protocol):
    """A fact declares the bucket of its mutants: a fixed priority level, no dispatch."""

    def bucket(self, mutant: Mutant) -> Bucket: ...


def classify(mutant: Mutant) -> Bucket:
    """The mutant's bucket: the producing fact's declaration; REVIEW for anything else.

    No operator-string dispatch and no source heuristics: a fact owns its bucket as data, and
    a hand-built or third-party mutant without one stays REVIEW."""
    fact = mutant.fact
    if isinstance(fact, Bucketed):
        return fact.bucket(mutant)
    return Bucket.REVIEW


def _line_contains(node: ast.stmt, line: int) -> bool:
    end_lineno = node.end_lineno or node.lineno
    return node.lineno <= line <= end_lineno


def _statement_for_site(
    tree: ast.Module,
    line_starts: list[int],
    *,
    line: int,
    start_byte: int | None,
    end_byte: int | None,
) -> ast.stmt | None:
    candidates = [node for node in ast.walk(tree) if isinstance(node, ast.stmt) and _line_contains(node, line)]
    positioned = [(node, span) for node in candidates if (span := node_byte_span(node, line_starts)) is not None]
    if start_byte is not None or end_byte is not None:
        if start_byte is None or end_byte is None or start_byte < 0 or end_byte < start_byte:
            return None
        containing = [(node, span) for node, span in positioned if span[0] <= start_byte and end_byte <= span[1]]
        if not containing:
            return None
        shortest = min(span[1] - span[0] for _node, span in containing)
        exact = [node for node, span in containing if span[1] - span[0] == shortest]
        return exact[0] if len(exact) == 1 else None

    leaves = [
        node
        for node, span in positioned
        if not any(
            other is not node and span != other_span and span[0] <= other_span[0] and other_span[1] <= span[1]
            for other, other_span in positioned
        )
    ]
    return leaves[0] if len(leaves) == 1 else None


def _is_type_alias_annotation(annotation: ast.expr) -> bool:
    match annotation:
        case ast.Name(id="TypeAlias") | ast.Attribute(attr="TypeAlias"):
            return True
        case _:
            return False


def cold_kind(
    source: str | None,
    *,
    line: int,
    start_byte: int | None = None,
    end_byte: int | None = None,
) -> ColdKind:
    """Classify a cold site by high-precision AST traits of the current source."""
    if source is None:
        return ColdKind.UNATTRIBUTED
    try:
        tree = parse_module(source)
    except SyntaxError:
        return ColdKind.UNATTRIBUTED
    statement = _statement_for_site(
        tree,
        line_byte_starts(source),
        line=line,
        start_byte=start_byte,
        end_byte=end_byte,
    )
    if statement is None:
        return ColdKind.UNATTRIBUTED
    if isinstance(statement, ast.AnnAssign):
        return (
            ColdKind.TYPE_ONLY
            if statement.value is None or _is_type_alias_annotation(statement.annotation)
            else ColdKind.SCHEMA_DEFAULT
        )
    if isinstance(statement, ast.TypeAlias):
        return ColdKind.TYPE_ONLY
    return ColdKind.IMPORT_RUNTIME
