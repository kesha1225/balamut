"""Query operators: drop one predicate or key of a composite DB contract (balamut.operators.db)."""

from __future__ import annotations

import ast
import importlib

import pytest

from balamut.core.mutants import ApplyMechanism, Mutant, apply_mutant
from balamut.core.source_spans import line_byte_starts
from balamut.core.triage import Bucket
from balamut.operators import db as db_module
from balamut.operators.db import (
    DEFAULT_KEY_KEYWORDS,
    ChainRulesFact,
    DbRewrite,
    DbRulesFact,
    DedupKeySite,
    enumerate_chain,
    enumerate_db,
    enumerate_db_ast_operators,
)
from balamut.operators.facts import EnumContext
from balamut.operators.kit import DropEachArgument, node_is


def _results(source: str, *, operator: str, key_keywords: frozenset[str] = DEFAULT_KEY_KEYWORDS) -> set[str]:
    return {
        apply_mutant(source, mutant)
        for mutant in enumerate_db_ast_operators("t.py", source, key_keywords=key_keywords)
        if mutant.operator == operator
    }


def test_where_drops_exactly_one_predicate() -> None:
    source = "def query(stmt):\n    return stmt.where(A, B, C)\n"

    assert _results(source, operator="sa:where-predicate-drop") == {
        "def query(stmt):\n    return stmt.where(B, C)\n",
        "def query(stmt):\n    return stmt.where(A, C)\n",
        "def query(stmt):\n    return stmt.where(A, B)\n",
    }


def test_single_predicate_is_left_alone() -> None:
    assert _results("def query(stmt):\n    return stmt.where(A)\n", operator="sa:where-predicate-drop") == set()


def test_filter_drops_exactly_one_predicate() -> None:
    source = "def query(stmt):\n    return stmt.filter(A, B)\n"

    assert _results(source, operator="sa:filter-predicate-drop") == {
        "def query(stmt):\n    return stmt.filter(B)\n",
        "def query(stmt):\n    return stmt.filter(A)\n",
    }


def test_group_by_and_distinct_drop_exactly_one_key() -> None:
    source = "def query(stmt):\n    return stmt.group_by(A, B).distinct(C, D)\n"

    assert _results(source, operator="sa:group-by-key-drop") == {
        "def query(stmt):\n    return stmt.group_by(B).distinct(C, D)\n",
        "def query(stmt):\n    return stmt.group_by(A).distinct(C, D)\n",
    }
    assert _results(source, operator="sa:distinct-key-drop") == {
        "def query(stmt):\n    return stmt.group_by(A, B).distinct(D)\n",
        "def query(stmt):\n    return stmt.group_by(A, B).distinct(C)\n",
    }


def test_multibyte_predicates_splice_on_byte_boundaries() -> None:
    source = 'def query(stmt):\n    return stmt.where(name == "αβ", city == "✓")\n'

    assert _results(source, operator="sa:where-predicate-drop") == {
        'def query(stmt):\n    return stmt.where(city == "✓")\n',
        'def query(stmt):\n    return stmt.where(name == "αβ")\n',
    }


def test_dedup_keyword_tuple_drops_one_element_and_stays_tuple() -> None:
    source = "def insert(stmt):\n    return stmt.run(dedup_keys=(A, B))\n"

    assert _results(source, operator="sa:dedup-key-drop") == {
        "def insert(stmt):\n    return stmt.run(dedup_keys=(B,))\n",
        "def insert(stmt):\n    return stmt.run(dedup_keys=(A,))\n",
    }


def test_longer_key_tuple_drops_one_element() -> None:
    source = "def insert(stmt):\n    return stmt.run(group_keys=(A, B, C))\n"

    assert _results(source, operator="sa:dedup-key-drop") == {
        "def insert(stmt):\n    return stmt.run(group_keys=(B, C))\n",
        "def insert(stmt):\n    return stmt.run(group_keys=(A, C))\n",
        "def insert(stmt):\n    return stmt.run(group_keys=(A, B))\n",
    }


@pytest.mark.parametrize(
    "keyword", ["monkey", "key", "group", "cache_key", "api_keys", "sort_key", "permission_groups"]
)
def test_tuple_key_operator_ignores_non_contract_keyword_names(keyword: str) -> None:
    source = f"def render():\n    return build({keyword}=(A, B))\n"

    assert _results(source, operator="sa:dedup-key-drop") == set()


def test_key_keyword_vocabulary_is_configurable() -> None:
    source = "def insert(stmt):\n    return stmt.run(unique_by=(A, B))\n"

    assert _results(source, operator="sa:dedup-key-drop") == set()
    assert _results(source, operator="sa:dedup-key-drop", key_keywords=frozenset({"unique_by"})) == {
        "def insert(stmt):\n    return stmt.run(unique_by=(B,))\n",
        "def insert(stmt):\n    return stmt.run(unique_by=(A,))\n",
    }


def test_db_ast_mutants_all_reparse() -> None:
    source = "def query(stmt):\n    return stmt.where(A, B).group_by(C, D).distinct(E, F).run(group_keys=(G, H, I))\n"

    mutants: list[Mutant] = enumerate_db_ast_operators("t.py", source)

    assert mutants
    for mutant in mutants:
        ast.parse(apply_mutant(source, mutant))


def test_db_kind_combines_rule_and_ast_operators() -> None:
    source = "def query(stmt):\n    return stmt.where(A, B).limit(10)\n"

    operators = {mutant.operator for mutant in enumerate_db("t.py", source)}

    assert {"sa:limit-drop", "sa:where-predicate-drop"} <= operators


def test_chain_kind_uses_chain_rules_only() -> None:
    source = "def query(q, dt):\n    return q.filter(A).limit(3), dt.astimezone(UTC)\n"

    operators = {mutant.operator for mutant in enumerate_chain("t.py", source)}

    assert operators == {"sa:filter-drop", "std:datetime:astimezone-drop"}


def test_dedup_key_skips_tuple_with_missing_element_span() -> None:
    # A tuple with a valid span but one element missing its end position: the span check must
    # reject it (a pair splice needs every element byte). Under `or` -> `and` the guard falls
    # through and indexes an element that is not there.
    source = "x = f(keys=(A, B))\n"
    tree = ast.parse(source)
    value = next(node for node in ast.walk(tree) if isinstance(node, ast.Tuple))
    value.elts[1].end_lineno = None
    value.elts[1].end_col_offset = None
    ctx = EnumContext("t.py", source.encode("utf-8"), line_byte_starts(source), frozenset())

    assert list(DedupKeySite()._drop_tuple_elements(ctx, value)) == []


def test_longer_key_tuple_drop_spans_are_byte_exact() -> None:
    # The middle drop runs from the middle element to the next start (the element plus its
    # trailing separator), never from the previous end (the separator plus the element): the
    # splice byte span is part of the contract, not just the final text.
    source = "def insert(stmt):\n    return stmt.run(group_keys=(A, B, C))\n"
    originals = {
        mutant.original
        for mutant in enumerate_db_ast_operators("t.py", source)
        if mutant.operator == "sa:dedup-key-drop"
    }

    assert originals == {"A, ", "B, ", ", C"}


def test_db_rewrite_bucket_is_high() -> None:
    fact = DbRewrite(match=node_is(ast.Call), transform=DropEachArgument(), operator="sa:x")
    mutant = Mutant("t.py", 1, 0, 1, "a", "b", "sa:x")

    assert fact.bucket(mutant) is Bucket.HIGH


def test_db_rules_mechanism_is_code() -> None:
    assert DbRulesFact().mechanism() is ApplyMechanism.CODE


def test_chain_rules_mechanism_is_code() -> None:
    assert ChainRulesFact().mechanism() is ApplyMechanism.CODE


def test_dedup_key_site_class_body_reexecutes() -> None:
    # The class-body field runs only at import; a reload inside the test body re-runs it so the
    # coverage substrate attributes the line to this test (a COLD mutant reads as unsupported).
    importlib.reload(db_module)
    site = db_module.DedupKeySite()
    assert site._lower == frozenset(name.lower() for name in db_module.DEFAULT_KEY_KEYWORDS)
    assert "_lower" not in repr(site)


def test_db_ast_kind_names_the_group_by_and_distinct_operators() -> None:
    operators = [fact.operator for fact in db_module.db_ast_kind() if isinstance(fact, db_module.DbRewrite)]
    assert "sa:group-by-key-drop" in operators
    assert "sa:distinct-key-drop" in operators


def test_db_ast_operators_stamp_the_db_family() -> None:
    mutants = enumerate_db_ast_operators("t.py", "def query(stmt):\n    return stmt.where(A, B)\n")
    assert mutants
    assert all(mutant.family == "db" for mutant in mutants)


def test_chain_rules_stamp_the_fact() -> None:
    mutants = db_module.enumerate_chain("t.py", "def q():\n    return q.filter(A)\n")
    assert mutants
    assert all(isinstance(mutant.fact, db_module.ChainRulesFact) for mutant in mutants)
