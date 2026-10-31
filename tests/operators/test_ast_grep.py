"""Mutation operators authored as ast-grep rules (balamut.operators.ast_grep)."""

from __future__ import annotations

import ast
import importlib
import textwrap

from balamut.core.mutants import (
    ApplyMechanism,
    Mutant,
    apply_mutant,
)
from balamut.core.triage import Bucket, classify
from balamut.operators import ast_grep as ast_grep_module
from balamut.operators.ast_grep import CHAIN_OPERATORS, DB_OPERATORS, AstGrepOperator, enumerate_ast_grep_operators
from balamut.operators.concurrency import enumerate_concurrency
from balamut.operators.db import ChainRulesFact, DbRulesFact, enumerate_chain, enumerate_db
from balamut.operators.facts import mutant_mechanism


def _muts(src: str, operators: tuple[AstGrepOperator, ...]) -> list[tuple[str, str, str]]:
    return [
        (m.operator, m.original, m.mutated)
        for m in enumerate_ast_grep_operators("t.py", textwrap.dedent(src), operators)
    ]


def test_db_limit_drop_snaps_chain() -> None:
    assert ("sa:limit-drop", "select(Q).limit(10)", "select(Q)") in _muts(
        "def f():\n    return select(Q).limit(10)\n", DB_OPERATORS
    )


def test_db_order_by_drop_snaps_chain() -> None:
    assert ("sa:order-by-drop", "stmt.order_by(X.asc())", "stmt") in _muts(
        "def f():\n    return stmt.order_by(X.asc())\n", DB_OPERATORS
    )


def test_db_order_direction_flips_both_ways() -> None:
    mutants = _muts("def f():\n    return stmt.order_by(A.asc(), B.desc())\n", DB_OPERATORS)

    assert ("sa:asc-to-desc", "A.asc()", "A.desc()") in mutants
    assert ("sa:desc-to-asc", "B.desc()", "B.asc()") in mutants


def test_db_zero_arg_distinct_drop() -> None:
    assert ("sa:distinct-drop", "stmt.distinct()", "stmt") in _muts(
        "def f():\n    return stmt.distinct()\n", DB_OPERATORS
    )


def test_db_conflict_update_becomes_do_nothing() -> None:
    assert (
        "sa:on-conflict-update-to-nothing",
        "stmt.on_conflict_do_update(index_elements=[A], set_={A: value})",
        "stmt.on_conflict_do_nothing()",
    ) in _muts(
        "def f(stmt, value):\n    return stmt.on_conflict_do_update(index_elements=[A], set_={A: value})\n",
        DB_OPERATORS,
    )


def test_db_limit_drop_keeps_chained_offset() -> None:
    """Only the ``limit`` link is removed: the span is ``q.limit(1)`` and ``.offset(2)`` survives the splice."""
    src = "def f():\n    return q.limit(1).offset(2)\n"
    (mutant,) = enumerate_ast_grep_operators("t.py", src, DB_OPERATORS)
    assert apply_mutant(src, mutant) == "def f():\n    return q.offset(2)\n"


def test_metavar_substitution_uses_captured_text() -> None:
    op = (AstGrepOperator(name="sa:limit-drop", rule={"pattern": "$C.limit($$$A)"}, rewrite="$C"),)
    (mutated,) = [
        m.mutated for m in enumerate_ast_grep_operators("t.py", "def f():\n    return build(a, b).limit(n)\n", op)
    ]
    assert mutated == "build(a, b)"


def test_uncaptured_metavariable_is_left_verbatim() -> None:
    op = (AstGrepOperator(name="sa:limit-drop", rule={"pattern": "$C.limit($$$A)"}, rewrite="$C.$MISSING"),)
    (mutated,) = [m.mutated for m in enumerate_ast_grep_operators("t.py", "def f():\n    return q.limit(1)\n", op)]
    assert mutated == "q.$MISSING"


def test_identity_rewrite_yields_no_mutant() -> None:
    op = (AstGrepOperator(name="sa:noop", rule={"pattern": "$C.limit($N)"}, rewrite="$C.limit($N)"),)
    assert enumerate_ast_grep_operators("t.py", "def f():\n    return q.limit(1)\n", op) == []


def test_noop_first_match_does_not_skip_later_matches() -> None:
    # A first match whose rewrite is a no-op must be skipped (continue), never abort the whole
    # match loop (break) and lose the later real mutant.
    op = (AstGrepOperator(name="x", rule={"pattern": "$C.limit($$$A)"}, rewrite="q.limit(1)"),)
    src = "def f():\n    return q.limit(1) + r.limit(2)\n"
    mutants = enumerate_ast_grep_operators("t.py", src, op)

    assert len(mutants) == 1
    assert mutants[0].original == "r.limit(2)"
    assert mutants[0].mutated == "q.limit(1)"


def test_kind_bucket_mechanism() -> None:
    assert next(iter(enumerate_db("t.py", "def f():\n    return q.limit(1)\n"))).family == "db"
    mutant = Mutant("t.py", 1, 0, 1, "q.limit(1)", "q", "sa:limit-drop")
    assert mutant_mechanism(mutant) is ApplyMechanism.CODE
    assert classify(Mutant("t.py", 1, 0, 1, "q.limit(1)", "q", "sa:limit-drop", fact=DbRulesFact())) is Bucket.HIGH


def test_reexpressed_skiplock_matches_handrolled() -> None:
    """The hand-rolled ``sa:skiplock-drop`` re-expressed as an ast-grep rule gives the same mutants."""
    src = (
        "async def claim(session):\n"
        "    return await session.execute(select(Q).limit(1).with_for_update(skip_locked=True))\n"
    )
    handrolled = sorted(
        (m.start_byte, m.end_byte, m.mutated, m.operator)
        for m in enumerate_concurrency("t.py", src)
        if m.operator == "sa:skiplock-drop"
    )
    skiplock_op = (
        AstGrepOperator(name="sa:skiplock-drop", rule={"pattern": "$C.with_for_update($$$A)"}, rewrite="$C"),
    )
    astgrep = sorted(
        (m.start_byte, m.end_byte, m.mutated, m.operator)
        for m in enumerate_ast_grep_operators("t.py", src, skiplock_op)
    )
    assert handrolled
    assert astgrep == handrolled


def test_byte_precise_splice_with_multibyte_text_before_site() -> None:
    """ast-grep reports character offsets; the splice must convert them to UTF-8 bytes."""
    src = "def f():\n    # επιλογή γραμμών ✓\n    return q.limit(10)\n"
    (mutant,) = enumerate_ast_grep_operators("t.py", src, DB_OPERATORS)
    assert mutant.original == "q.limit(10)"
    assert mutant.lineno == 3
    assert apply_mutant(src, mutant) == "def f():\n    # επιλογή γραμμών ✓\n    return q\n"


def test_multiline_match_records_its_last_line() -> None:
    src = "def f():\n    return q.limit(\n        10,\n    )\n"
    (mutant,) = enumerate_ast_grep_operators("t.py", src, DB_OPERATORS)
    assert (mutant.lineno, mutant.site_end_lineno) == (2, 4)


def test_chain_astimezone_and_now_tz_drop() -> None:
    assert ("std:datetime:astimezone-drop", "dt.astimezone(UTC)", "dt") in _muts(
        "def f():\n    return dt.astimezone(UTC)\n", CHAIN_OPERATORS
    )
    assert ("std:datetime:now-tz-drop", "datetime.now(UTC)", "datetime.now()") in _muts(
        "def f():\n    return datetime.now(UTC)\n", CHAIN_OPERATORS
    )


def test_chain_filter_drop() -> None:
    assert ("sa:filter-drop", "q.filter(X.active)", "q") in _muts(
        "def f():\n    return q.filter(X.active).all()\n", CHAIN_OPERATORS
    )


def test_chain_has_no_method_swap_onto_a_missing_method() -> None:
    """``.first()`` -> ``.last()`` is not offered: a result object without ``.last()`` would crash, not be caught."""
    ops = {op.name for op in CHAIN_OPERATORS}
    assert "chain:first-to-last" not in ops
    assert not _muts("def f():\n    return res.first()\n", CHAIN_OPERATORS)


def test_chain_kind_and_bucket() -> None:
    assert next(iter(enumerate_chain("t.py", "def f(q):\n    return q.filter(X)\n"))).family == "chain"
    chained = Mutant("t.py", 1, 0, 1, "q.filter(X)", "q", "sa:filter-drop", fact=ChainRulesFact())
    assert classify(chained) is Bucket.HIGH


def test_every_chain_mutant_reparses_to_valid_python() -> None:
    src = "def f(q, dt):\n    return q.filter(q.active).order_by(q.id).limit(3).first()\n"
    mutants = enumerate_ast_grep_operators("t.py", src, CHAIN_OPERATORS)
    assert mutants
    for mutant in mutants:
        ast.parse(apply_mutant(src, mutant))


def test_every_db_mutant_reparses_to_valid_python() -> None:
    src = textwrap.dedent("""
        def list_rows(session, limit, offset):
            stmt = select(Row).where(Row.active).order_by(Row.name.asc()).offset(offset).limit(limit)
            return session.execute(stmt).scalars().all()
    """)
    mutants = enumerate_ast_grep_operators("t.py", src, DB_OPERATORS)
    assert len(mutants) >= 2
    for mutant in mutants:
        ast.parse(apply_mutant(src, mutant))


def test_operator_tables_class_body_reexecutes() -> None:
    # The DB_OPERATORS / CHAIN_OPERATORS tables are built at import; a reload inside the test
    # body re-runs them so the coverage substrate attributes the lines to this test.
    importlib.reload(ast_grep_module)

    db_names = [op.name for op in ast_grep_module.DB_OPERATORS]
    assert db_names == [
        "sa:limit-drop",
        "sa:order-by-drop",
        "sa:asc-to-desc",
        "sa:desc-to-asc",
        "sa:distinct-drop",
        "sa:on-conflict-update-to-nothing",
    ]

    chain_names = [op.name for op in ast_grep_module.CHAIN_OPERATORS]
    assert chain_names == ["std:datetime:astimezone-drop", "std:datetime:now-tz-drop", "sa:filter-drop"]

    db = {op.name: (op.rule, op.rewrite) for op in ast_grep_module.DB_OPERATORS}
    assert db["sa:limit-drop"] == ({"pattern": "$C.limit($$$A)"}, "$C")
    assert db["sa:order-by-drop"] == ({"pattern": "$C.order_by($$$A)"}, "$C")
    assert db["sa:asc-to-desc"] == ({"pattern": "$C.asc()"}, "$C.desc()")
    assert db["sa:desc-to-asc"] == ({"pattern": "$C.desc()"}, "$C.asc()")
    assert db["sa:distinct-drop"] == ({"pattern": "$C.distinct()"}, "$C")
    assert db["sa:on-conflict-update-to-nothing"] == (
        {"pattern": "$C.on_conflict_do_update($$$A)"},
        "$C.on_conflict_do_nothing()",
    )

    chain = {op.name: (op.rule, op.rewrite) for op in ast_grep_module.CHAIN_OPERATORS}
    assert chain["std:datetime:astimezone-drop"] == ({"pattern": "$X.astimezone($$$A)"}, "$X")
    assert chain["std:datetime:now-tz-drop"] == ({"pattern": "$D.now($TZ)"}, "$D.now()")
    assert chain["sa:filter-drop"] == ({"pattern": "$C.filter($$$A)"}, "$C")
