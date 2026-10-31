"""The concurrency kind: dropped locks, SKIP LOCKED, conflict policies, visibility shifts and the
configured queue claim/settle predicates.

Each fact is an ordinary ``Rewrite`` over one domain site finder; the facts carry the schedule-fuzz
and isolated-lane markers and the queue operators' semantic classes. The configured queue families
travel with ``QueueCompareDrop`` as plain data, as before.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from typing import ClassVar

from balamut.config import QueueFamily
from balamut.core.mutants import BcEdit, Mutant
from balamut.core.triage import Bucket
from balamut.operators.facts import EnumContext, OperatorKind, enumerate_facts
from balamut.operators.kit import Library, Matcher, Rewrite, SiteFinder, node_is, operator_name

FAMILY = "concurrency"

# The queue-semantics operators have no library: they name the project's own domain.
QUEUE_NAMESPACE = "project:queue"

type QueueIndex = dict[str, QueueFamily]  # queue model name -> its configured family


@cache
def _by_model(queues: tuple[QueueFamily, ...]) -> QueueIndex:
    """The configured families keyed by their ORM model name (cached: the config rarely changes)."""
    return {queue.model: queue for queue in queues}


# --- Domain site finders (``(target, text, span, operator)`` per the kit protocol) ----------------


@dataclass(frozen=True, slots=True)
class LockContextSite:
    """(the context expression, ``nullcontext()``) for lock-shaped with contexts; the operator
    names the shape (asynclock vs advisory)."""

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                matched = _lock_context_operator(item.context_expr)
                if matched is not None:
                    operator, _cls = matched
                    yield item.context_expr, "nullcontext()", None, operator, None

    def site_classes(self, node: ast.AST, ctx: EnumContext) -> tuple[str, ...]:
        if not isinstance(node, ast.With | ast.AsyncWith):
            return ()
        classes = {
            cls
            for item in node.items
            for matched in (_lock_context_operator(item.context_expr),)
            if matched is not None
            for cls in (matched[1],)
        }
        return tuple(sorted(classes))


@dataclass(frozen=True, slots=True)
class ChainCallDropSite:
    """(the call, the chain without the dropped link) for the given chain-method attributes."""

    attrs: tuple[tuple[str, str], ...]  # (method name, operator name)

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            for attr, operator in self.attrs:
                if node.func.attr == attr:
                    yield node, ast.unparse(node.func.value), None, operator, None

    def site_classes(self, node: ast.AST, ctx: EnumContext) -> tuple[str, ...]:
        return (Library.SA.value,)


@dataclass(frozen=True, slots=True)
class NowShiftSite:
    """``func.now() + <ttl>`` -> ``func.now()``: the visibility window drops to zero."""

    operator: str = operator_name(Library.STD, "datetime", "now-shift-collapse")

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and isinstance(node.left, ast.Call)
            and isinstance(node.left.func, ast.Attribute)
            and node.left.func.attr == "now"
        ):
            yield node, "func.now()", None, self.operator, None

    def site_classes(self, node: ast.AST, ctx: EnumContext) -> tuple[str, ...]:
        return (operator_name(Library.STD, "datetime"),)


@dataclass(frozen=True, slots=True)
class QueueCompareSite:
    """A configured queue claim/settle predicate collapses into a tautology (``col == col``)."""

    queues: tuple[QueueFamily, ...] = ()

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            return
        column = _queue_column(node.left, _by_model(self.queues))
        if column is None:
            return
        queue, col = column
        operator = _queue_compare_operator(queue, col, node.ops[0], node.comparators[0])
        if operator is not None:
            yield node, f"{queue.model}.{col} == {queue.model}.{col}", None, operator, None

    def site_classes(self, node: ast.AST, ctx: EnumContext) -> tuple[str, ...]:
        if not isinstance(node, ast.Compare):
            return ()
        column = _queue_column(node.left, _by_model(self.queues))
        if column is None:
            return ()
        return (operator_name("project", column[0].family),)


def _lock_context_operator(expr: ast.expr) -> tuple[str, str] | None:
    """Is this ``with`` context a lock? Exact shapes only: ``*.lock``, ``*_lock`` names, ``Lock()``, advisory.

    The value is ``(operator, semantic class)``: the operator names the library the lock comes from."""
    match expr:
        case ast.Attribute(attr=("lock" | "_lock" | "_mutex")):
            return operator_name(Library.STD, "asyncio", "lock-drop"), operator_name(Library.STD, "asyncio")
        case ast.Name(id=name) if name in {"lock", "mutex"} or name.endswith(("_lock", "_mutex")):
            return operator_name(Library.STD, "asyncio", "lock-drop"), operator_name(Library.STD, "asyncio")
        case ast.Call(func=ast.Name(id="Lock") | ast.Attribute(attr="Lock")):
            return operator_name(Library.STD, "asyncio", "lock-drop"), operator_name(Library.STD, "asyncio")
        case ast.Call(func=ast.Name(id=("advisory_xact_lock" | "advisory_lock"))):
            return operator_name(Library.SA, "advisory-drop"), Library.SA.value
        case ast.Call(func=ast.Attribute(attr=("advisory_xact_lock" | "advisory_lock"))):
            return operator_name(Library.SA, "advisory-drop"), Library.SA.value
        case _:
            return None


def _queue_column(node: ast.expr, by_model: QueueIndex) -> tuple[QueueFamily, str] | None:
    """``<QueueModel>.<column>`` -> (family, column); anything else -> None."""
    match node:
        case ast.Attribute(value=ast.Name(id=model), attr=col) if model in by_model:
            return by_model[model], col
        case _:
            return None


def _queue_compare_operator(queue: QueueFamily, col: str, op: ast.cmpop, comparator: ast.expr) -> str | None:
    """The queue-semantics operator for ``<Model>.<col> <op> <comparator>``, or None.

    A fence compares with a variable (the fencing token), never with a literal: ``attempts == 0`` is not one."""
    non_literal = not isinstance(comparator, ast.Constant)
    family = queue.family
    if col in queue.fence and isinstance(op, ast.Eq) and non_literal:
        return operator_name(QUEUE_NAMESPACE, family, "fence-drop")
    if col == queue.backstop and isinstance(op, ast.Lt):
        return operator_name(QUEUE_NAMESPACE, family, "backstop-drop")
    if col == queue.due and isinstance(op, ast.LtE):
        return operator_name(QUEUE_NAMESPACE, family, "due-filter-drop")
    if col == queue.lease and isinstance(op, ast.Lt):
        return operator_name(QUEUE_NAMESPACE, family, "lease-recovery-drop")
    if col == queue.progress and isinstance(op, ast.Gt):
        return operator_name(QUEUE_NAMESPACE, family, "progress-drop")
    return None


# --- The facts -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, init=False)
class ConcurrencyFact(Rewrite):
    """A concurrency fact: its mutants opt into schedule-fuzz yield injection and the isolated
    single-worker lane through the marker attributes."""

    fuzz: ClassVar[bool] = True
    isolated_lane: ClassVar[bool] = True

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.HIGH  # a dropped lock or a narrowed claim window: races matter


@dataclass(frozen=True, slots=True)
class LockDrop(ConcurrencyFact):
    """A ``with``/``async with`` lock context becomes ``nullcontext()`` (the body keeps its shape)."""

    match: Matcher = field(default_factory=lambda: node_is(ast.With, ast.AsyncWith))
    transform: SiteFinder = field(default_factory=LockContextSite)
    operator: str | None = None


@dataclass(frozen=True, slots=True)
class SkipLockDrop(ConcurrencyFact):
    """``.with_for_update(...)`` is dropped from the chain."""

    match: Matcher = field(default_factory=lambda: node_is(ast.Call))
    transform: SiteFinder = field(
        default_factory=lambda: ChainCallDropSite((("with_for_update", operator_name(Library.SA, "skiplock-drop")),))
    )
    operator: str | None = None


@dataclass(frozen=True, slots=True)
class OnConflictDrop(ConcurrencyFact):
    """``.on_conflict_do_nothing()`` / ``.on_conflict_do_update(...)`` is dropped from the chain."""

    match: Matcher = field(default_factory=lambda: node_is(ast.Call))
    transform: SiteFinder = field(
        default_factory=lambda: ChainCallDropSite(
            (
                ("on_conflict_do_update", operator_name(Library.SA, "onconflict-drop")),
                ("on_conflict_do_nothing", operator_name(Library.SA, "onconflict-drop")),
            )
        )
    )
    operator: str | None = None


@dataclass(frozen=True, slots=True)
class NowShiftCollapse(ConcurrencyFact):
    """``func.now() + <ttl>`` -> ``func.now()``: the visibility window drops to zero."""

    match: Matcher = field(default_factory=lambda: node_is(ast.BinOp))
    transform: SiteFinder = field(default_factory=NowShiftSite)
    operator: str | None = None


@dataclass(frozen=True, slots=True, init=False)
class QueueCompareDrop(ConcurrencyFact):
    """A configured queue claim/settle predicate collapses into a tautology (``col == col``).

    The positional constructor takes the queue families directly (``QueueCompareDrop(queues)``),
    the shape the engine's config and the tests use."""

    queues: tuple[QueueFamily, ...] = ()

    def __init__(self, queues: tuple[QueueFamily, ...] = ()) -> None:
        object.__setattr__(self, "match", node_is(ast.Compare))
        object.__setattr__(self, "transform", QueueCompareSite(queues))
        object.__setattr__(self, "operator", None)
        object.__setattr__(self, "queues", queues)


concurrency_kind: OperatorKind = (LockDrop(), SkipLockDrop(), OnConflictDrop(), NowShiftCollapse(), QueueCompareDrop())


def enumerate_concurrency(path: str, source: str, *, queues: tuple[QueueFamily, ...] = ()) -> list[Mutant]:
    """Concurrency mutants: lock removal that keeps the structure intact.

    A lock context becomes ``nullcontext()`` (the ``with`` body keeps its indentation, a clean splice) and
    ``.with_for_update(...)`` is dropped from the chain. Configured queue families add queue-semantics
    operators. A survivor means no covering test detects the race."""
    kind: OperatorKind = (LockDrop(), SkipLockDrop(), OnConflictDrop(), NowShiftCollapse(), QueueCompareDrop(queues))
    return enumerate_facts(path, source, (kind,), family=FAMILY)
