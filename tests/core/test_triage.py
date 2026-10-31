"""Bucketing of survivors (balamut.core.triage) and the semantic tagger.

The bucket is a fact-declared datum: no operator-string dispatch, no source heuristics. The
semantic classes are the token layer plus the classes the fact recorded on the mutant at
emission.
"""

from __future__ import annotations

import ast

import pytest
from capability import Capability

from balamut.config import DEFAULT_SEMANTIC, QueueFamily
from balamut.core import triage
from balamut.core.mutants import Mutant
from balamut.core.semantic import semantic_classes
from balamut.core.source_spans import line_byte_starts
from balamut.core.triage import (
    Bucket,
    ColdKind,
    _is_type_alias_annotation,
    _line_contains,
    _statement_for_site,
    classify,
    cold_kind,
)
from balamut.operators.concurrency import LockDrop, enumerate_concurrency
from balamut.operators.facts import enumerate_facts
from balamut.operators.fault import FaultReturnSwap, enumerate_fault
from balamut.operators.src import enumerate_mutants


def _mut(operator: str, original: str = "x", fact: Capability | None = None) -> Mutant:
    return Mutant("f.py", 1, 0, 1, original, "pass", operator, fact=fact)


@pytest.mark.parametrize(
    "operator",
    [
        "compare:Eq->NotEq",
        "delete:call",
        "extreme:sdl",
        "concurrency:general:asynclock-drop",
        "totally-new-operator",
    ],
)
def test_factless_mutant_is_review(operator: str) -> None:
    # Without a fact there is no declaration: the conservative bucket is REVIEW, never a guess
    # from the operator string.
    assert classify(_mut(operator)) is Bucket.REVIEW


def test_src_kind_buckets_are_declared_by_the_facts() -> None:
    source = "def f(a, b, x=1):\n    if a < b:\n        return True\n    x += 2\n    round(x, 2)\n    return not a\n"
    mutants = {m.operator: m for m in enumerate_mutants("f.py", source)}
    assert classify(mutants["lang:compare:Lt->LtE"]) is Bucket.HIGH
    assert classify(mutants["lang:return:None"]) is Bucket.HIGH
    assert classify(mutants["lang:const:bool:True->False"]) is Bucket.HIGH
    assert classify(mutants["lang:augassign:Add->Sub"]) is Bucket.REVIEW
    assert classify(mutants["lang:unary:not-removal"]) is Bucket.REVIEW
    assert classify(mutants["lang:delete:call"]) is Bucket.HIGH
    assert classify(mutants["lang:const:num:2->3"]) is Bucket.REVIEW
    assert classify(mutants["lang:const:num:1->2"]) is Bucket.HIGH


def test_kind_facts_declare_their_buckets() -> None:
    # One representative real mutant per kind, classified through its producing fact.
    assert classify(_mut("x", fact=LockDrop())) is Bucket.HIGH
    fault = FaultReturnSwap()
    assert classify(_mut("x", fact=fault)) is Bucket.HIGH
    source = "async def f(s):\n    await s.commit()\n"
    commit = next(m for m in enumerate_fault("f.py", source) if m.operator == "sa:session:raise-at:commit")
    assert classify(commit) is Bucket.HIGH
    from balamut.operators.db import enumerate_db
    from balamut.operators.extreme import enumerate_extreme
    from balamut.operators.oracle import enumerate_oracle

    db_source = "def q(stmt):\n    return stmt.where(A).limit(10)\n"
    limit = next(m for m in enumerate_db("f.py", db_source) if m.operator == "sa:limit-drop")
    assert classify(limit) is Bucket.HIGH
    extreme_source = "def f():\n    x = 1\n"
    sdl = next(iter(enumerate_extreme("f.py", extreme_source)))
    assert classify(sdl) is Bucket.HIGH
    oracle_source = "def test_x():\n    assert a == b\n"
    negate = next(iter(enumerate_oracle("f_test.py", oracle_source)))
    assert classify(negate) is Bucket.REVIEW


def test_cold_kind_distinguishes_type_schema_and_import_runtime() -> None:
    assert cold_kind("value: int\n", line=1) is ColdKind.TYPE_ONLY
    assert cold_kind("value: int = 10\n", line=1) is ColdKind.SCHEMA_DEFAULT
    assert cold_kind("DEFAULT_LIMIT = 10\n", line=1) is ColdKind.IMPORT_RUNTIME


def test_cold_kind_type_alias_statement_is_type_only() -> None:
    assert cold_kind("type Alias = int\n", line=1) is ColdKind.TYPE_ONLY


def test_line_contains_falls_back_to_lineno_without_end_lineno() -> None:
    node = ast.parse("x = 1").body[0]
    node.end_lineno = None
    assert _line_contains(node, 1) is True
    assert _line_contains(node, 9) is False


def test_statement_for_site_requires_both_bytes_when_partial() -> None:
    source = "value = 1\n"
    tree = ast.parse(source)
    starts = line_byte_starts(source)
    assert _statement_for_site(tree, starts, line=1, start_byte=None, end_byte=0) is None


def test_statement_for_site_allows_zero_start_byte() -> None:
    source = "value = 1\n"
    tree = ast.parse(source)
    starts = line_byte_starts(source)
    assert _statement_for_site(tree, starts, line=1, start_byte=0, end_byte=9) is not None


def test_statement_for_site_allows_equal_start_and_end_bytes() -> None:
    source = "value = 1\n"
    tree = ast.parse(source)
    starts = line_byte_starts(source)
    assert _statement_for_site(tree, starts, line=1, start_byte=5, end_byte=5) is not None


def test_statement_for_site_returns_innermost_when_spans_share_start() -> None:
    inner = ast.Pass(lineno=1, col_offset=0, end_lineno=1, end_col_offset=4)
    outer = ast.If(
        test=ast.Constant(value=True, lineno=1, col_offset=0, end_lineno=1, end_col_offset=4),
        body=[inner],
        orelse=[],
        lineno=1,
        col_offset=0,
        end_lineno=1,
        end_col_offset=10,
    )
    tree = ast.Module(body=[outer], type_ignores=[])
    assert _statement_for_site(tree, [0], line=1, start_byte=None, end_byte=None) is inner


def test_is_type_alias_annotation_true_only_for_type_alias() -> None:
    assert _is_type_alias_annotation(ast.Name(id="TypeAlias")) is True
    assert _is_type_alias_annotation(ast.Attribute(value=ast.Name(id="typing"), attr="TypeAlias")) is True
    assert _is_type_alias_annotation(ast.Name(id="NotTypeAlias")) is False
    assert _is_type_alias_annotation(ast.Attribute(value=ast.Name(id="typing"), attr="Literal")) is False


def test_annotated_type_alias_is_type_only() -> None:
    source = "from typing import Literal, TypeAlias\nAlias: TypeAlias = Literal[1]\n"
    assert cold_kind(source, line=2) is ColdKind.TYPE_ONLY


def test_cold_kind_uses_exact_byte_span_for_same_line_statements() -> None:
    source = "SCHEMA: int = 10; RUNTIME = 20\n"
    tree = ast.parse(source)
    schema_assign = tree.body[0]
    runtime_assign = tree.body[1]
    assert isinstance(schema_assign, ast.AnnAssign)
    assert isinstance(runtime_assign, ast.Assign)
    assert (
        cold_kind(
            source,
            line=1,
            start_byte=schema_assign.col_offset,
            end_byte=runtime_assign.col_offset,
        )
        is ColdKind.UNATTRIBUTED
    )
    assert (
        cold_kind(
            source,
            line=1,
            start_byte=runtime_assign.col_offset,
            end_byte=runtime_assign.end_col_offset,
        )
        is ColdKind.IMPORT_RUNTIME
    )


@pytest.mark.parametrize("source", [None, "class Broken("])
def test_cold_kind_is_unattributed_without_reliable_ast(source: str | None) -> None:
    assert cold_kind(source, line=1) is ColdKind.UNATTRIBUTED


def _mutant(original: str, operator: str, fact: Capability | None = None) -> Mutant:
    return Mutant("f.py", 1, 0, len(original), original, "x", operator, fact=fact)


def test_semantic_classes_tag_on_explicit_tokens() -> None:
    assert semantic_classes(_mutant("user.tenant_id is not None", "compare:Eq->NotEq")) == ["tenant"]
    assert semantic_classes(_mutant("token.expires_at <= utcnow()", "compare:LtE->Lt")) == ["time"]
    assert "auth" in semantic_classes(_mutant("raise HTTPException(status_code=403, detail='x')", "delete:call"))
    assert "transaction" in semantic_classes(_mutant("await session.commit()", "fault:raise-at:commit"))
    assert semantic_classes(_mutant("statement.limit(page_size)", "db:limit-drop")) == ["pagination"]


def test_semantic_classes_untagged_without_tokens() -> None:
    assert semantic_classes(_mutant("a + b", "binop:Add->Sub")) == []
    assert semantic_classes(_mutant("len(items) > page_size", "compare:Gt->GtE")) == []


def test_semantic_classes_recorded_at_emission() -> None:
    # The class travels on the mutant: the fact recorded it at emission, no operator parsing.
    lock_source = "async def f():\n    async with self._lock:\n        await g()\n"
    lock = next(iter(enumerate_concurrency("f.py", lock_source)))
    assert "std:asyncio" in semantic_classes(lock)

    queues = (QueueFamily(model="Job", family="jobs", fence=("token",)),)
    fence_source = "def claim(token):\n    return Job.token == token\n"
    fence = next(iter(enumerate_concurrency("f.py", fence_source, queues=queues)))
    assert "project:jobs" in semantic_classes(fence)

    fault = FaultReturnSwap(return_swap=frozenset({"refund"}))
    fault_source = "async def pay(s):\n    return await s.refund(1)\n"
    swap = next(iter(enumerate_facts("f.py", fault_source, ((fault,),))))
    assert "idempotency" in semantic_classes(swap)
    raise_source = "async def pay(s):\n    await s.commit()\n"
    raised = next(iter(enumerate_fault("f.py", raise_source)))
    assert "idempotency" not in semantic_classes(raised)


def test_semantic_patterns_are_configurable() -> None:
    patterns = (*DEFAULT_SEMANTIC, ("billing", ("invoice",)))
    assert semantic_classes(_mutant("invoice.total", "binop:Mult->Div"), patterns=patterns) == ["billing"]
    assert semantic_classes(_mutant("invoice.total", "binop:Mult->Div")) == []


def test_parse_module_import_still_supports_cold_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = triage.parse_module

    def counting(source: str) -> ast.Module:
        nonlocal calls
        calls += 1
        return original(source)

    monkeypatch.setattr(triage, "parse_module", counting)
    assert cold_kind("DEFAULT_LIMIT = 10\n", line=1) is ColdKind.IMPORT_RUNTIME
    assert calls == 1
