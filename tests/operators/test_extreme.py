"""The extreme kind: statement deletion (balamut.operators.extreme)."""

from __future__ import annotations

import ast
import textwrap

from balamut.operators.extreme import deletable_statement, suite_statements
from balamut.operators.facts import EnumContext


def _ctx() -> EnumContext:
    return EnumContext("t.py", b"", [0], frozenset())


def _call_names(top: ast.stmt) -> list[str]:
    return [
        node.value.func.id
        for node in suite_statements(top)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
    ]


def test_annotation_only_assignment_is_not_deletable() -> None:
    node = ast.parse("x: int\n").body[0]  # an AnnAssign with value=None
    assert deletable_statement(allow_bare_return=False).matches(node, _ctx()) is False


def test_unmatched_statement_is_not_deletable() -> None:
    node = ast.parse("pass\n").body[0]  # a Pass hits the default case
    assert deletable_statement(allow_bare_return=False).matches(node, _ctx()) is False


def test_suite_statements_orders_if_body_before_else() -> None:
    top = ast.parse("if a:\n    b()\nelse:\n    c()\n").body[0]
    assert _call_names(top) == ["b", "c"]


def test_suite_statements_orders_try_body_orelse_finalbody_handlers() -> None:
    src = textwrap.dedent(
        """\
        try:
            a()
        except Exception:
            b()
        else:
            c()
        finally:
            d()
        """
    )
    top = ast.parse(src).body[0]
    assert _call_names(top) == ["a", "c", "d", "b"]
