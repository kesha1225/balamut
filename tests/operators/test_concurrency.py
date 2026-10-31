"""Concurrency site finders and facts (balamut.operators.concurrency)."""

from __future__ import annotations

import ast

from balamut.config import QueueFamily
from balamut.operators.concurrency import (
    LockContextSite,
    LockDrop,
    QueueCompareDrop,
    QueueCompareSite,
    _lock_context_operator,
    _queue_compare_operator,
    enumerate_concurrency,
)
from balamut.operators.facts import EnumContext

JOBS = QueueFamily(
    model="Job",
    family="jobs",
    fence=("attempts", "locked_by", "revision"),
    due="run_after",
    lease="lease_until",
    backstop="attempts",
    progress="required_revision",
)


def _ctx() -> EnumContext:
    return EnumContext(path="t.py", source_bytes=b"", line_starts=[0], non_body=frozenset())


def _eval(src: str) -> ast.expr:
    return ast.parse(src, mode="eval").body


def test_skiplock_mutant_records_sa_semantic_class() -> None:
    src = "def f(session):\n    return q.with_for_update(skip_locked=True)\n"
    (mutant,) = enumerate_concurrency("t.py", src)
    assert mutant.operator == "sa:skiplock-drop"
    assert mutant.semantic == ("sa",)


def test_now_shift_mutant_records_std_datetime_semantic_class() -> None:
    src = "def f():\n    return func.now() + timedelta(days=1)\n"
    (mutant,) = [m for m in enumerate_concurrency("t.py", src) if m.operator == "std:datetime:now-shift-collapse"]
    assert mutant.semantic == ("std:datetime",)


def test_lock_context_site_classes_empty_for_non_with() -> None:
    node = ast.parse("x = 1").body[0]
    assert LockContextSite().site_classes(node, _ctx()) == ()


def test_queue_compare_site_classes_empty_for_non_compare() -> None:
    node = ast.parse("x + 1").body[0]
    assert QueueCompareSite(queues=(JOBS,)).site_classes(node, _ctx()) == ()


def test_queue_compare_site_classes_empty_for_non_queue_column() -> None:
    parsed = ast.parse("x == y").body[0]
    assert isinstance(parsed, ast.Expr)
    node = parsed.value
    assert QueueCompareSite(queues=(JOBS,)).site_classes(node, _ctx()) == ()


def test_lock_context_operator_recognizes_lock_call() -> None:
    assert _lock_context_operator(_eval("Lock()")) == ("std:asyncio:lock-drop", "std:asyncio")


def test_lock_context_operator_recognizes_name_bound_lock() -> None:
    # A ``*_lock`` name bound to a lock (the ``ast.Name`` case) names the same asyncio operator.
    assert _lock_context_operator(_eval("state_lock")) == ("std:asyncio:lock-drop", "std:asyncio")


def test_now_shift_site_names_the_datetime_operator() -> None:
    import importlib

    from balamut.operators import concurrency as concurrency_module

    importlib.reload(concurrency_module)
    assert concurrency_module.NowShiftSite().operator == "std:datetime:now-shift-collapse"


def test_lock_context_operator_recognizes_advisory_attribute_call() -> None:
    assert _lock_context_operator(_eval("conn.advisory_lock(k)")) == ("sa:advisory-drop", "sa")


def test_queue_compare_operator_requires_the_matching_comparison_for_due() -> None:
    assert _queue_compare_operator(JOBS, "run_after", ast.Lt(), _eval("x")) is None


def test_queue_compare_operator_requires_the_matching_comparison_for_lease() -> None:
    assert _queue_compare_operator(JOBS, "lease_until", ast.LtE(), _eval("x")) is None


def test_queue_compare_requires_a_single_operator() -> None:
    src = "def f():\n    return Job.run_after <= func.now() <= other\n"
    ops = {m.operator for m in enumerate_concurrency("t.py", src, queues=(JOBS,))}
    assert "project:queue:jobs:due-filter-drop" not in ops


def test_queue_compare_drop_initializes_operator() -> None:
    assert QueueCompareDrop(queues=(JOBS,)).operator is None


def test_queue_compare_drop_keeps_queues() -> None:
    assert QueueCompareDrop(queues=(JOBS,)).queues == (JOBS,)


def test_concurrency_facts_opt_into_fuzz_and_isolated_lane() -> None:
    fact = LockDrop()
    assert fact.fuzz is True
    assert fact.isolated_lane is True
