"""The oracle kind: mutations of the tests themselves (balamut.operators.oracle)."""

from __future__ import annotations

import ast
import importlib
import textwrap

from balamut.operators import oracle as oracle_module
from balamut.operators.facts import EnumContext
from balamut.operators.kit import And
from balamut.operators.oracle import _RAISES_KEY, RaisesExc, enumerate_oracle


def test_oracle_excludes_non_test_methods_in_test_class() -> None:
    # A `Test*` class may hold helper methods alongside `test_*`; only the latter are collectable.
    src = (
        "class TestThing:\n"
        "    def helper(self):\n"
        "        assert x > 0\n"
        "    def test_real(self):\n"
        "        assert value == 42\n"
    )
    muts = {m.mutated for m in enumerate_oracle("x_test.py", src)}

    assert "not (x > 0)" not in muts
    assert "not (value == 42)" in muts


def test_oracle_raises_skips_starred_exception() -> None:
    # A `pytest.raises(*errors)` has no concrete exception literal to swap for the sentinel.
    src = "import pytest\ndef test_x():\n    with pytest.raises(*errors):\n        boom()\n"
    raises = [m for m in enumerate_oracle("x_test.py", src) if m.operator == "pytest:raises"]

    assert raises == []


def test_oracle_class_body_reexecutes() -> None:
    # Class-body markers (self_covered, the composed matchers) run only at import; a reload inside
    # the test body re-runs them so the coverage substrate attributes the lines to this test.
    importlib.reload(oracle_module)
    assert oracle_module.OracleFact.self_covered is True
    assert oracle_module.OracleWalkFact.self_covered is True
    assert isinstance(oracle_module.AssertNegation().match, And)
    assert isinstance(oracle_module.RaisesExc().match, And)
    assert isinstance(oracle_module.AssertBoundaryTighten().match, And)


def test_raises_exc_prepare_matcher_contract() -> None:
    # The RaisesExc matcher is `call_of(attr="raises", receiver="pytest", min_args=1)
    # | call_of(func="raises", min_args=1)`: only an arg-ful `pytest.raises(...)` is collected.
    src = textwrap.dedent(
        """\
        import pytest
        def test_x():
            with pytest.raises(ValueError):
                boom()
            with other.raises(TypeError):
                boom()
            with pytest.not_raises(KeyError):
                boom()
            with pytest.raises():
                boom()
            with raises():
                boom()
        """
    )
    tree = ast.parse(src)
    ctx = EnumContext("x_test.py", src.encode("utf-8"), [0], frozenset())
    RaisesExc().prepare(ctx, tree)
    ids = ctx.prepared(_RAISES_KEY)
    assert isinstance(ids, frozenset)

    def receiver_attr(call: ast.Call, receiver: str, attr: str) -> bool:
        func = call.func
        return (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == receiver
            and func.attr == attr
        )

    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    good = next(c for c in calls if receiver_attr(c, "pytest", "raises") and len(c.args) == 1)
    assert id(good) in ids

    other = next(c for c in calls if receiver_attr(c, "other", "raises"))
    assert id(other) not in ids  # receiver must be pytest

    not_raises = next(c for c in calls if receiver_attr(c, "pytest", "not_raises"))
    assert id(not_raises) not in ids  # attribute must be "raises"

    zero_arg = next(c for c in calls if receiver_attr(c, "pytest", "raises") and len(c.args) == 0)
    assert id(zero_arg) not in ids  # min_args=1

    bare = next(c for c in calls if isinstance(c.func, ast.Name) and c.func.id == "raises" and len(c.args) == 0)
    assert id(bare) not in ids  # bare raises() also needs an argument
