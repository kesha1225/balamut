"""The fault kind: an awaited dependency call raises (``fault:raise-at``) or answers the wrong
way (``fault:return-swap``).

Both facts are walk facts: the discarded-await set (a bare ``await`` statement throws its result
away, making a return-swap observably equivalent) needs the whole tree, so the return-swap fact
computes it once per file through the ``Prepare`` protocol and stores it on the context.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from balamut.core.mutants import ApplyMechanism, Mutant
from balamut.core.triage import Bucket
from balamut.operators.facts import EnumContext, OperatorKind, enumerate_facts
from balamut.operators.kit import Library, operator_name
from balamut.runtime.fault import FAULT_RAISE_NAME

DISCARDED_AWAITS_KEY = "fault.discarded-awaits"

FAMILY = "fault"

DEFAULT_FAULT_RAISE_AT = frozenset({"commit", "flush", "rollback"})


@dataclass(frozen=True, slots=True)
class FaultReturnSwap:
    """``await gw.refund(...)`` -> ``not (await gw.refund(...))``: the provider answers "not done".

    Skipped for discarded awaits (a bare ``await`` statement throws the result away, so the swap
    would be observably equivalent). The discarded set comes from the per-file ``Prepare`` pass.
    """

    return_swap: frozenset[str] = frozenset()
    namespace: Library | str = Library.SA

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.HIGH  # a dependency answering "not done": failure handling matters

    def prepare(self, ctx: EnumContext, tree: ast.Module) -> EnumContext:
        discarded = frozenset(
            id(statement.value)
            for statement in ast.walk(tree)
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Await)
        )
        ctx.remember(DISCARDED_AWAITS_KEY, discarded)
        return ctx

    def sites(self, ctx: EnumContext) -> EnumContext:
        node = ctx.node
        if node is None or not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            return ctx
        call = node.value
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in self.return_swap:
            return ctx
        discarded = ctx.prepared(DISCARDED_AWAITS_KEY)
        if isinstance(discarded, frozenset) and id(node) in discarded:
            return ctx
        attr = call.func.attr
        ctx.emit(
            node,
            f"not ({ast.unparse(node)})",
            operator_name(self.namespace, "session", f"return-swap:{attr}"),
            fact=self,
            classes=("idempotency",),
        )
        return ctx

    def mechanism(self) -> ApplyMechanism:
        return ApplyMechanism.CODE


@dataclass(frozen=True, slots=True)
class FaultRaiseAt:
    """``await session.commit()`` -> ``await balamut_fault_raise('commit')``: the call fails."""

    raise_at: frozenset[str] = DEFAULT_FAULT_RAISE_AT
    namespace: Library | str = Library.SA

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.HIGH  # a dependency failure: compensation and rollback matter

    def sites(self, ctx: EnumContext) -> EnumContext:
        node = ctx.node
        if node is None or not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            return ctx
        call = node.value
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in self.raise_at:
            return ctx
        attr = call.func.attr
        ctx.emit(
            call,
            f"{FAULT_RAISE_NAME}({attr!r})",
            operator_name(self.namespace, "session", f"raise-at:{attr}"),
            fact=self,
        )
        return ctx

    def mechanism(self) -> ApplyMechanism:
        return ApplyMechanism.CODE


fault_kind: OperatorKind = (FaultReturnSwap(), FaultRaiseAt())


def enumerate_fault(
    path: str,
    source: str,
    *,
    raise_at: frozenset[str] = DEFAULT_FAULT_RAISE_AT,
    return_swap: frozenset[str] = frozenset(),
) -> list[Mutant]:
    """Fault mutants at recognized *awaited* call sites (exact method names).

    ``return-swap`` (methods returning a "done?" bool) makes the provider answer "not done", so the recheck or
    compensation runs without an exception. ``raise-at`` makes the call fail with a generic injected error:
    every return-swap method can also fail, so it is raise-at eligible too."""
    kind: OperatorKind = (FaultReturnSwap(return_swap=return_swap), FaultRaiseAt(raise_at=raise_at | return_swap))
    return enumerate_facts(path, source, (kind,), family=FAMILY)
