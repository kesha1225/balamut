"""The fault kind: awaited dependency failures (balamut.operators.fault)."""

from __future__ import annotations

from balamut.core.mutants import ApplyMechanism
from balamut.operators.fault import FaultReturnSwap, enumerate_fault


def test_enumerate_fault_ignores_awaited_bare_calls() -> None:
    # A bare awaited call (`await refund()`) has no attribute to match; it must be skipped, not
    # crash on `.attr` of a plain Name callee.
    src = "async def f():\n    await refund()\n"

    assert enumerate_fault("t.py", src, return_swap=frozenset({"refund"})) == []


def test_fault_return_swap_mechanism_is_code() -> None:
    assert FaultReturnSwap().mechanism() is ApplyMechanism.CODE
