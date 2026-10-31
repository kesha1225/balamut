"""Mutant enumeration (balamut.core.mutants)."""

from __future__ import annotations

import ast
import textwrap

import pytest

from balamut.config import QueueFamily
from balamut.core.mutants import (
    ApplyMechanism,
    Mutant,
    apply_mutant,
    is_none_constant,
    non_body_node_ids,
)
from balamut.core.triage import Bucket, classify
from balamut.operators.concurrency import enumerate_concurrency
from balamut.operators.extreme import deletable_statement, enumerate_extreme
from balamut.operators.facts import EnumContext, effective_mechanism, mutant_mechanism
from balamut.operators.fault import FaultRaiseAt, enumerate_fault
from balamut.operators.kit import node_is
from balamut.operators.oracle import OracleFact, collectable_scope_ids, enumerate_oracle
from balamut.operators.src import enumerate_defaults, enumerate_mutants
from balamut.runtime.fault import FAULT_RAISE_NAME, InjectedFaultError, fault_globals, fault_raise

JOBS = QueueFamily(
    model="Job",
    family="jobs",
    fence=("attempts", "locked_by", "revision"),
    due="run_after",
    lease="lease_until",
    backstop="attempts",
    progress="required_revision",
)


def _mutset(src: str) -> set[tuple[str, str]]:
    return {(mutant.operator, mutant.mutated) for mutant in enumerate_mutants("t.py", textwrap.dedent(src))}


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("if a < b:\n    pass", ("lang:compare:Lt->LtE", "a <= b")),
        ("if a == b:\n    pass", ("lang:compare:Eq->NotEq", "a != b")),
        ("if a is None:\n    pass", ("lang:compare:Is->IsNot", "a is not None")),
        ("if a in xs:\n    pass", ("lang:compare:In->NotIn", "a not in xs")),
        ("x = 5", ("lang:const:num:5->6", "6")),
        ("x = True", ("lang:const:bool:True->False", "False")),
        ("y = a + b", ("lang:binop:Add->Sub", "a - b")),
        ("y = a * b", ("lang:binop:Mult->Div", "a / b")),
        ("z = a and b", ("lang:boolop:And->Or", "a or b")),
        ("z = not flag", ("lang:unary:not-removal", "flag")),
        ("x += 1", ("lang:augassign:Add->Sub", "x -= 1")),
        ("m = min(a, b)", ("std:builtins:min->max", "max(a, b)")),
        ("m = max(a, b)", ("std:builtins:max->min", "min(a, b)")),
        ("ok = all(xs)", ("std:builtins:all->any", "any(xs)")),
        ("ok = any(xs)", ("std:builtins:any->all", "all(xs)")),
        ("ok = s.startswith(p)", ("std:str:startswith->endswith", "s.endswith(p)")),
        ("ok = s.lstrip()", ("std:str:lstrip->rstrip", "s.rstrip()")),
        ("y = round(x, 2)", ("std:builtins:round-drop", "x")),
        ("y = amount.quantize(cent)", ("std:decimal:quantize-drop", "amount")),
        ("y = a & b", ("lang:binop:BitAnd->BitOr", "a | b")),
        ("y = a << b", ("lang:binop:LShift->RShift", "a >> b")),
    ],
)
def test_operator_produces_expected_mutant(src: str, expected: tuple[str, str]) -> None:
    assert expected in _mutset(src)


def test_call_min_max_swap_including_nested_clamp() -> None:
    # `max(lo, min(hi, x))`: both the outer and the inner call are separate sites.
    ops = _mutset("y = max(lo, min(hi, x))")
    assert ("std:builtins:max->min", "min(lo, min(hi, x))") in ops
    assert ("std:builtins:min->max", "max(hi, x)") in ops


def test_call_swap_only_bare_builtin_not_method() -> None:
    # `.min()` is a receiver-specific method, not the builtin: the call family must not touch it.
    operators = {operator for operator, _ in _mutset("y = series.min()")}
    assert "std:builtins:min->max" not in operators


def test_method_swap_table_and_conservative() -> None:
    assert ("std:str:find->rfind", "s.rfind(x)") in _mutset("y = s.find(x)")
    assert ("std:str:rindex->index", "s.index(x)") in _mutset("y = s.rindex(x)")
    # An unknown method is left alone: only known directional pairs are swapped.
    assert not any(op.startswith("std:str:") for op, _ in _mutset("y = obj.compute(a, b)"))


def test_normalizer_drop_table_and_conservative() -> None:
    assert ("std:builtins:round-drop", "self.total") in _mutset("y = round(self.total)")
    assert ("std:decimal:quantize-drop", "self.total") in _mutset("y = self.total.quantize(cent, rounding=mode)")
    # No positional operand, a starred one, or a callee outside the table: nothing to stand in for the call.
    for src in ("y = round()", "y = round(*xs)", "y = round(number=x)", "y = quantize(x)", "y = obj.compute(x)"):
        assert not any(op.endswith("-drop") for op, _ in _mutset(src))


def test_normalizer_drop_keeps_operand_grouping() -> None:
    # `ast.unparse` drops grouping parentheses: a bare `a + b` spliced over `round(a + b, 2) * 3` would bind as
    # `a + b * 3`. A non-atomic operand stands in for the call parenthesized.
    src = "def f(a, b, cent):\n    return round(a + b, 2) * 3 + (a * b).quantize(cent, rounding=MODE) ** 2\n"
    applied = {
        m.operator: apply_mutant(src, m)
        for m in enumerate_mutants("t.py", src)
        if m.operator in {"std:builtins:round-drop", "std:decimal:quantize-drop"}
    }
    assert applied == {
        "std:builtins:round-drop": (
            "def f(a, b, cent):\n    return (a + b) * 3 + (a * b).quantize(cent, rounding=MODE) ** 2\n"
        ),
        "std:decimal:quantize-drop": "def f(a, b, cent):\n    return round(a + b, 2) * 3 + (a * b) ** 2\n",
    }


def test_arith_identity_swaps_suppressed_as_equivalent() -> None:
    # A swap with a right-hand identity operand yields the same value: such a mutant is equivalent.
    assert "lang:binop:Add->Sub" not in {op for op, _ in _mutset("y = a + 0")}
    assert "lang:augassign:Add->Sub" not in {op for op, _ in _mutset("x += 0")}
    # `a * 1` is an int while `a / 1` is a float: a real int/float contract, so it is kept.
    assert "lang:binop:Mult->Div" in {op for op, _ in _mutset("y = a * 1")}
    # A left-hand zero is not an identity (`0 - a` is `-a`).
    assert "lang:binop:Add->Sub" in {op for op, _ in _mutset("y = a + b")}
    assert "lang:binop:Add->Sub" in {op for op, _ in _mutset("y = 0 + a")}
    assert "lang:augassign:Add->Sub" in {op for op, _ in _mutset("x += y")}


def test_bitwise_swaps() -> None:
    assert ("lang:binop:BitOr->BitAnd", "a & b") in _mutset("y = a | b")
    assert ("lang:binop:BitXor->BitAnd", "a & b") in _mutset("y = a ^ b")
    assert ("lang:augassign:BitAnd->BitOr", "x |= y") in _mutset("x &= y")


def test_break_continue_swap() -> None:
    assert ("lang:control:break->continue", "continue") in _mutset("for x in xs:\n    break")
    assert ("lang:control:continue->break", "break") in _mutset("for x in xs:\n    continue")


def test_return_value_to_none() -> None:
    assert ("lang:return:None", "return None") in _mutset("def f():\n    return value")


def test_delete_call_statement_to_pass() -> None:
    assert ("lang:delete:call", "pass") in _mutset("def f():\n    session.flush()")


def test_delete_await_call_statement() -> None:
    assert ("lang:delete:call", "pass") in _mutset("async def f():\n    await session.flush()")


def test_docstring_is_not_deleted() -> None:
    operators = {operator for operator, _ in _mutset('def f():\n    "docstring"\n    return 1')}
    assert "lang:delete:call" not in operators


def test_call_with_used_return_is_not_deleted() -> None:
    operators = {operator for operator, _ in _mutset("def f():\n    x = compute()\n    return x")}
    assert "lang:delete:call" not in operators


def test_return_none_is_not_mutated_to_none() -> None:
    operators = {operator for operator, _ in _mutset("def f():\n    return None")}
    assert "lang:return:None" not in operators


def test_default_arg_gets_defaults_mutant_separate_from_body_const() -> None:
    mutants = enumerate_mutants("t.py", "def f(x=1):\n    y = 1\n    return y")
    assert any(m.operator == "lang:const:num:1->2" and m.default_index is None for m in mutants)
    assert any(m.operator == "lang:const:num:1->2" and m.default_index == 0 for m in mutants)


def test_enumerate_defaults_positional_literal_with_body_lineno() -> None:
    muts = enumerate_defaults("t.py", "def f(a, b=2, c=3):\n    return a\n")
    ops = {(m.operator, m.mutated) for m in muts}
    assert ("lang:const:num:2->3", "3") in ops
    assert ("lang:const:num:3->4", "4") in ops
    for m in muts:
        assert m.lineno == 2  # covering is selected by callers: the first body line
        assert m.site_lineno == 1  # the diff scope is the `def` line, where the default lives


def test_enumerate_defaults_skips_nested_and_nonliteral() -> None:
    assert enumerate_defaults("t.py", "def o():\n    def i(x=1):\n        return x\n    return i\n") == []
    assert enumerate_defaults("t.py", "def f(x=compute()):\n    return x\n") == []


def test_enumerate_defaults_skips_docstring_for_covering_line() -> None:
    # A docstring line is never marked by coverage on a call; selecting it would yield a false no_coverage.
    muts = enumerate_defaults("t.py", 'def f(x=1):\n    """doc"""\n    return x\n')
    assert muts
    for m in muts:
        assert m.lineno == 3
        assert m.site_lineno == 1


def test_default_mutant_mechanism_is_defaults() -> None:
    mutants = enumerate_mutants("t.py", "def f(x=1):\n    return x\n")
    assert mutant_mechanism(next(m for m in mutants if m.default_index is not None)) is ApplyMechanism.DEFAULTS


def test_add_zero_still_suppressed_true_identity() -> None:
    ops = {m.operator for m in enumerate_mutants("t.py", "def f(x):\n    return x + 0\n")}
    assert "lang:binop:Add->Sub" not in ops


def test_multiline_mutant_carries_span_end_line() -> None:
    src = "def f(a, b):\n    return (\n        a\n        + b\n    )\n"
    binops = [m for m in enumerate_mutants("t.py", src) if m.operator.startswith("lang:binop")]
    assert binops
    assert any(m.site_end_lineno > m.site_lineno for m in binops)


def test_index_method_not_swapped_to_missing_rindex() -> None:
    # list/tuple have `.index()` but no `.rindex()`: the swap would "kill" through AttributeError only.
    ops = {m.operator for m in enumerate_mutants("t.py", "def f(xs):\n    return xs.index(3)\n")}
    assert not any("rindex" in op for op in ops)


def test_decorator_and_annotation_constants_excluded() -> None:
    src = "import typing\n@retry(3)\ndef f(x: typing.Literal[5]) -> None:\n    pass"
    operators = {operator for operator, _ in _mutset(src)}
    assert "lang:const:num:3->4" not in operators
    assert "lang:const:num:5->6" not in operators


def test_class_decorator_arguments_excluded() -> None:
    # A class decorator runs at import time, outside any function body: no in-memory patch can reach it.
    src = "from dataclasses import dataclass\n@dataclass(frozen=True, slots=True)\nclass Snapshot:\n    value: int\n"
    assert "lang:const:bool:True->False" not in {operator for operator, _ in _mutset(src)}


def test_local_annotated_assignment_type_expression_is_excluded() -> None:
    src = "def f(value):\n    result: int | str = value\n    return result\n"
    assert "lang:binop:BitOr->BitAnd" not in {mutant.operator for mutant in enumerate_mutants("t.py", src)}


def test_apply_mutant_byte_precise_with_multibyte_text_before_site() -> None:
    src = 'x = "h\u00e9llo w\u00f6rld \u2603"\nif a < b:\n    y = 1\n'
    compare = next(m for m in enumerate_mutants("t.py", src) if m.operator.startswith("lang:compare"))
    assert apply_mutant(src, compare) == 'x = "h\u00e9llo w\u00f6rld \u2603"\nif a <= b:\n    y = 1\n'


def test_every_mutant_reparses_to_valid_python() -> None:
    src = textwrap.dedent("""
        def settle(amount, attempts, max_attempts):
            if attempts >= max_attempts and amount > 0:
                return amount + 1
            total = 0
            for x in items:
                total += x * 2
            return total is None
    """)
    mutants = enumerate_mutants("t.py", src)
    assert len(mutants) > 5
    for mutant in mutants:
        ast.parse(apply_mutant(src, mutant))


def test_numeric_literal_before_attribute_access_is_parenthesized() -> None:
    src = "def f():\n    return 0xff.real\n"
    const = next(m for m in enumerate_mutants("t.py", src) if m.operator.startswith("lang:const:num"))
    assert const.mutated == "(256)"
    ast.parse(apply_mutant(src, const))


def test_mutants_carry_lineno_for_coverage_selection() -> None:
    src = "a = 1\nb = 2\nif a < b:\n    pass\n"
    compare = next(m for m in enumerate_mutants("t.py", src) if m.operator.startswith("lang:compare"))
    assert compare.lineno == 3


def test_mutants_carry_the_family_of_their_fact() -> None:
    src_mutants = enumerate_mutants("t.py", "def f(a, b):\n    return a < b\n")
    assert src_mutants
    for mutant in src_mutants:
        assert mutant.family == "src"


def test_mechanism_comes_from_the_fact_code_by_default() -> None:
    for mutant in enumerate_mutants("t.py", "def f(a, b):\n    return a < b + 1\n"):
        assert mutant_mechanism(mutant) is ApplyMechanism.CODE
    defaults = enumerate_mutants("t.py", "def f(x=1):\n    return x\n")
    default_mutants = [mutant for mutant in defaults if mutant.default_index is not None]
    assert default_mutants
    assert all(mutant_mechanism(mutant) is ApplyMechanism.DEFAULTS for mutant in default_mutants)


def test_effective_mechanism_code_mutant_outside_function_body_is_cold() -> None:
    mutant = Mutant("t.py", 1, 0, 1, "5", "6", "lang:const:num:5->6")
    assert effective_mechanism(mutant, code_reachable=True) is ApplyMechanism.CODE
    assert effective_mechanism(mutant, code_reachable=False) is ApplyMechanism.COLD


def test_mutant_site_lineno_and_end_lineno_default_to_lineno() -> None:
    # A mutant without an explicit site span defaults both ends to the mutation's own line.
    mutant = Mutant("t.py", 5, 0, 1, "x", "y", "op")
    assert mutant.site_lineno == 5
    assert mutant.site_end_lineno == 5


def test_mutant_explicit_site_lineno_keeps_end_lineno_default() -> None:
    mutant = Mutant("t.py", 5, 0, 1, "x", "y", "op", site_lineno=3)
    assert mutant.site_lineno == 3
    assert mutant.site_end_lineno == 3


def test_is_none_constant_distinguishes_none_from_other_nodes() -> None:
    assert is_none_constant(ast.Constant(value=None)) is True
    assert is_none_constant(ast.Constant(value=0)) is False
    assert is_none_constant(ast.Name(id="x")) is False


def test_non_body_node_ids_includes_defaults_and_annotations() -> None:
    tree = ast.parse("def f(x=1, y=2) -> int:\n    return x\n")
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    ids = non_body_node_ids(tree)
    # Default arguments and the return annotation live outside the body.
    assert id(fn.args.defaults[0]) in ids
    assert id(fn.args.defaults[1]) in ids
    assert id(fn.returns) in ids
    # The returned name is inside the body.
    ret = next(n for n in ast.walk(fn) if isinstance(n, ast.Return))
    assert id(ret.value) not in ids


def test_non_body_node_ids_includes_lambda_defaults() -> None:
    tree = ast.parse("f = lambda x=1: x\n")
    lam = next(n for n in ast.walk(tree) if isinstance(n, ast.Lambda))
    ids = non_body_node_ids(tree)
    # A lambda default is evaluated at definition time, outside any body.
    assert id(lam.args.defaults[0]) in ids


def test_apply_mechanism_values_are_open_and_fact_declared() -> None:
    assert FaultRaiseAt().mechanism() is ApplyMechanism.CODE
    assert {m.value for m in ApplyMechanism} == {"code", "defaults", "cold"}


def test_enumerate_concurrency_skiplock_advisory_lock() -> None:
    src = textwrap.dedent("""
        async def claim(session):
            row = await session.execute(select(Q).limit(1).with_for_update(skip_locked=True))
            async with open_session() as s, advisory_xact_lock(s, Lock.X, k):
                async with self._lock:
                    await s.flush()
            return row
    """)
    ops = {(m.operator, m.mutated) for m in enumerate_concurrency("t.py", src)}
    assert any(op == "sa:skiplock-drop" and "with_for_update" not in mut for op, mut in ops)
    assert ("sa:advisory-drop", "nullcontext()") in ops
    assert ("std:asyncio:lock-drop", "nullcontext()") in ops
    for mutant in enumerate_concurrency("t.py", src):
        assert mutant.family == "concurrency"


def test_enumerate_concurrency_ignores_plain_code() -> None:
    assert enumerate_concurrency("t.py", "def f(a, b):\n    return a + b\n") == []


def test_enumerate_concurrency_name_bound_lock() -> None:
    src = "async def f(state_lock):\n    async with state_lock:\n        await g()\n"
    assert "std:asyncio:lock-drop" in {m.operator for m in enumerate_concurrency("t.py", src)}
    assert enumerate_concurrency("t.py", "async def f(cm):\n    async with cm:\n        await g()\n") == []


def test_enumerate_concurrency_queue_family_operators() -> None:
    # Queue-semantics operators collapse a claim/settle predicate into a tautology (`col == col`).
    src = textwrap.dedent("""
        def claim(session, worker_id, lease_td):
            cid = session.execute(
                select(Job.id)
                .where(
                    Job.run_after <= func.now(),
                    Job.required_revision > Job.processed_revision,
                    or_(Job.max_attempts.is_(None), Job.attempts < Job.max_attempts),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            session.execute(
                update(Job)
                .where(Job.id == cid)
                .values(lease_until=func.now() + lease_td, locked_by=worker_id)
            )

        def finish(session, task):
            session.execute(
                update(Job)
                .where(Job.locked_by == task.worker_id, Job.revision == task.revision, Job.attempts == task.attempts)
                .values(status="idle")
            )

        def recover(session):
            session.execute(update(Job).where(Job.lease_until < func.now()).values(status="pending"))

        def enqueue(session, k):
            session.execute(
                pg_insert(Job).values(kind=k).on_conflict_do_nothing(index_elements=["kind"]).returning(Job.id)
            )
    """)
    ops = {(m.operator, m.mutated) for m in enumerate_concurrency("t.py", src, queues=(JOBS,))}
    assert ("project:queue:jobs:due-filter-drop", "Job.run_after == Job.run_after") in ops
    assert ("project:queue:jobs:progress-drop", "Job.required_revision == Job.required_revision") in ops
    assert ("project:queue:jobs:backstop-drop", "Job.attempts == Job.attempts") in ops
    assert ("project:queue:jobs:fence-drop", "Job.locked_by == Job.locked_by") in ops
    assert ("project:queue:jobs:fence-drop", "Job.revision == Job.revision") in ops
    assert ("project:queue:jobs:fence-drop", "Job.attempts == Job.attempts") in ops
    assert ("project:queue:jobs:lease-recovery-drop", "Job.lease_until == Job.lease_until") in ops
    assert ("std:datetime:now-shift-collapse", "func.now()") in ops
    assert any(op == "sa:onconflict-drop" and "on_conflict" not in mut for op, mut in ops)
    assert any(op == "sa:skiplock-drop" for op, _ in ops)
    for mutant in enumerate_concurrency("t.py", src, queues=(JOBS,)):
        assert mutant.family == "concurrency"
        ast.parse(apply_mutant(src, mutant))


def test_queue_family_operators_need_configuration() -> None:
    src = "def f():\n    return Job.run_after <= func.now()\n"
    assert not any(":jobs:" in m.operator for m in enumerate_concurrency("t.py", src))


def test_enumerate_concurrency_conservative_on_non_queue_compares() -> None:
    # `attempts == 0` compares with a literal, not with a fencing token; `x <= y` is not a configured column.
    src = "def f():\n    return Job.attempts == 0 and x <= y\n"
    ops = {m.operator for m in enumerate_concurrency("t.py", src, queues=(JOBS,))}
    assert "project:queue:jobs:fence-drop" not in ops
    assert "project:queue:jobs:due-filter-drop" not in ops


def test_enumerate_fault_raise_at_and_return_swap() -> None:
    src = textwrap.dedent("""
        async def execute(session, gateway):
            await session.commit()
            if not await gateway.refund(tx, amount):
                raise PaymentError()
    """)
    mutants = enumerate_fault("t.py", src, return_swap=frozenset({"refund"}))
    ops = {(m.operator, m.mutated) for m in mutants}
    assert ("sa:session:raise-at:commit", f"{FAULT_RAISE_NAME}('commit')") in ops
    assert ("sa:session:raise-at:refund", f"{FAULT_RAISE_NAME}('refund')") in ops
    assert any(op == "sa:session:return-swap:refund" and mut.startswith("not (") for op, mut in ops)
    for mutant in mutants:
        assert mutant.family == "fault"


def test_enumerate_fault_respects_configured_method_sets() -> None:
    src = "async def f(s):\n    await s.save()\n    await s.commit()\n"
    ops = {m.operator for m in enumerate_fault("t.py", src, raise_at=frozenset({"save"}))}
    assert ops == {"sa:session:raise-at:save"}


def test_enumerate_fault_return_swap_skipped_when_result_discarded() -> None:
    # A bare `await gw.refund(...)` statement discards the bool: `not (await ...)` would be equivalent.
    src = "async def execute(gw):\n    await gw.refund(tx, amount)\n"
    ops = {(m.operator, m.mutated) for m in enumerate_fault("t.py", src, return_swap=frozenset({"refund"}))}
    assert ("sa:session:raise-at:refund", f"{FAULT_RAISE_NAME}('refund')") in ops
    assert not any(op.startswith("sa:session:return-swap") for op, _ in ops)


def test_enumerate_fault_return_swap_kept_when_result_assigned() -> None:
    src = "async def execute(gw):\n    ok = await gw.refund(tx, amount)\n    return ok\n"
    ops = {(m.operator, m.mutated) for m in enumerate_fault("t.py", src, return_swap=frozenset({"refund"}))}
    assert any(op == "sa:session:return-swap:refund" and mut.startswith("not (") for op, mut in ops)


def test_enumerate_fault_only_awaited_known_methods() -> None:
    assert enumerate_fault("t.py", "def f(s):\n    s.commit()\n    return s.save()\n") == []


def test_fault_raise_raises_injected() -> None:
    with pytest.raises(InjectedFaultError):
        fault_raise("commit")
    assert set(fault_globals()) == {FAULT_RAISE_NAME}


def test_extreme_deletable_and_oracle_test_scope() -> None:
    """The kind-owned rules: SDL's deletable statements live in extreme, pytest test scope in oracle."""
    source = "def f():\n    x = 1\n    return None\n"
    tree = ast.parse(source)
    ctx = EnumContext("t.py", source.encode("utf-8"), [0], frozenset())
    assigns = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign))
    returns = next(node for node in ast.walk(tree) if isinstance(node, ast.Return))
    assert deletable_statement(allow_bare_return=False).matches(assigns, ctx)
    assert not deletable_statement(allow_bare_return=False).matches(returns, ctx)
    assert deletable_statement(allow_bare_return=True).matches(returns, ctx)

    test_source = "def test_x():\n    assert 1 < 2\n\ndef helper():\n    assert 3 < 4\n"
    test_tree = ast.parse(test_source)
    test_ctx = EnumContext("t.py", test_source.encode("utf-8"), [0], frozenset())
    ids = collectable_scope_ids(test_tree, node_is(ast.Assert), test_ctx)
    matched = [id(node) for node in ast.walk(test_tree) if isinstance(node, ast.Assert)]
    assert ids == {matched[0]}


def test_enumerate_oracle_negates_asserts() -> None:
    src = textwrap.dedent("""
        def test_x():
            assert a == b
            assert response.ok
            assert True
            with pytest.raises(ValueError):
                f()
    """)
    mutants = OracleFact().enumerate("x_test.py", src)
    ops = {(m.operator, m.mutated) for m in mutants}
    assert ("lang:assert:negate", "not (a == b)") in ops
    assert ("lang:assert:negate", "not (response.ok)") in ops
    originals = {m.original for m in mutants}
    assert "True" not in originals
    assert "f()" not in originals
    for mutant in mutants:
        assert mutant.family == "oracle"


def test_enumerate_oracle_excludes_helper_asserts() -> None:
    # Only collectable tests: module-level `test_*` and `test_*` methods of `Test*` classes.
    src = textwrap.dedent("""
        def _check(x):
            assert x > 0

        def test_real():
            assert value == 42

        class TestThing:
            def test_method(self):
                assert state.ready

        class Helper:
            def test_case(self):
                assert helper.ok
    """)
    muts = {m.mutated for m in enumerate_oracle("x_test.py", src)}
    assert "not (value == 42)" in muts
    assert "not (state.ready)" in muts
    assert "not (x > 0)" not in muts
    assert "not (helper.ok)" not in muts


def test_enumerate_oracle_raises_substitutes_foreign_exception_type() -> None:
    src = "import pytest\ndef test_x():\n    with pytest.raises(ValueError):\n        boom()\n"
    raises_muts = [m for m in enumerate_oracle("x_test.py", src) if m.operator == "pytest:raises"]
    assert len(raises_muts) == 1
    m = raises_muts[0]
    assert m.original == "ValueError"
    assert m.mutated == "balamut_oracle_unraised"
    applied = apply_mutant(src, m)
    assert "with pytest.raises(balamut_oracle_unraised):" in applied
    ast.parse(applied)


def test_enumerate_oracle_raises_from_import_form_and_excludes_helpers() -> None:
    from_import = "from pytest import raises\ndef test_y():\n    with raises(KeyError, match='x'):\n        boom()\n"
    ops = {(m.operator, m.original, m.mutated) for m in enumerate_oracle("x_test.py", from_import)}
    assert ("pytest:raises", "KeyError", "balamut_oracle_unraised") in ops
    helper = "import pytest\ndef _helper():\n    with pytest.raises(ValueError):\n        boom()\n"
    assert [m for m in enumerate_oracle("x_test.py", helper) if m.operator == "pytest:raises"] == []


def test_enumerate_oracle_boundary_tightens_le_and_ge() -> None:
    src = "def test_x():\n    assert result <= 10\n    assert count >= 1\n"
    muts = {(m.operator, m.original, m.mutated) for m in enumerate_oracle("x_test.py", src)}
    assert ("lang:assert:boundary", "result <= 10", "result < 10") in muts
    assert ("lang:assert:boundary", "count >= 1", "count > 1") in muts


def test_enumerate_oracle_boundary_skips_strict_chained_and_helpers() -> None:
    strict = "def test_x():\n    assert a < b\n    assert c > d\n"
    assert [m for m in enumerate_oracle("x_test.py", strict) if m.operator == "lang:assert:boundary"] == []
    chained = "def test_x():\n    assert 0 <= x <= 10\n"
    assert [m for m in enumerate_oracle("x_test.py", chained) if m.operator == "lang:assert:boundary"] == []
    helper = "def _check(x):\n    assert x <= 10\n"
    assert [m for m in enumerate_oracle("x_test.py", helper) if m.operator == "lang:assert:boundary"] == []


def test_enumerate_oracle_boundary_in_compound_assert_byte_precise() -> None:
    src = "def test_x():\n    assert x <= 10 and ok\n"
    muts = [mm for mm in enumerate_oracle("x_test.py", src) if mm.operator == "lang:assert:boundary"]
    assert len(muts) == 1
    m = muts[0]
    assert m.original == "x <= 10"
    assert m.mutated == "x < 10"
    assert apply_mutant(src, m) == "def test_x():\n    assert x < 10 and ok\n"


def _extreme_originals(src: str) -> set[str]:
    return {m.original for m in enumerate_extreme("t.py", textwrap.dedent(src))}


def test_enumerate_extreme_deletes_value_and_effect_statements() -> None:
    muts = enumerate_extreme("t.py", "def f():\n    total = compute()\n    total += 1\n    return total * 2\n")
    originals = {m.original for m in muts}
    assert "total = compute()" in originals
    assert "total += 1" in originals
    assert "return total * 2" in originals
    for m in muts:
        assert m.operator == "lang:statement:delete"
        assert m.mutated == "pass"


def test_enumerate_extreme_skips_module_level_statements() -> None:
    assert enumerate_extreme("t.py", "x = 1\ny = compute()\n") == []


def test_enumerate_extreme_skips_docstring_pass_bare_and_none_return() -> None:
    originals = _extreme_originals('def f():\n    "doc"\n    pass\n    x = 1\n    return None\n')
    assert "x = 1" in originals
    assert '"doc"' not in originals
    assert "pass" not in originals
    assert "return None" not in originals  # a tail `return None` is the same as falling through
    assert "return" not in _extreme_originals("def f():\n    x = 1\n    return\n")


def test_enumerate_extreme_mutates_guard_bare_return_not_tail() -> None:
    # A guard `return` with reachable code after it changes behaviour when removed.
    assert "return" in _extreme_originals("def f():\n    if bad:\n        return\n    process()\n")
    assert "return None" in _extreme_originals("def f():\n    if bad:\n        return None\n    process()\n")
    assert "return" not in _extreme_originals("def f():\n    process()\n    return\n")


def test_enumerate_extreme_skips_call_statement_delegated_to_src() -> None:
    # Call statements belong to `delete:call`; running `src,extreme` together must not duplicate them.
    assert enumerate_extreme("t.py", "def f():\n    session.flush()\n") == []
    assert enumerate_extreme("t.py", "async def f():\n    await session.flush()\n") == []


def test_enumerate_extreme_skips_annotation_only_keeps_annotated_assign() -> None:
    assert "x: int" not in _extreme_originals("def f():\n    x: int\n    return 1\n")
    assert "x: int = compute()" in _extreme_originals("def f():\n    x: int = compute()\n    return x\n")


def test_enumerate_extreme_recurses_suites_and_nested_defs() -> None:
    src = textwrap.dedent("""
        def outer():
            if cond:
                a = 1
            for x in xs:
                b = x
            with ctx():
                c = 2
            try:
                d = 3
            except Exception:
                e = 4
            def inner():
                nested = 5
                return nested
            return inner
    """)
    originals = _extreme_originals(src)
    assert {"a = 1", "b = x", "c = 2", "d = 3", "e = 4"} <= originals
    assert "nested = 5" in originals
    assert "return nested" in originals


def test_enumerate_extreme_kind_bucket_mechanism() -> None:
    assert next(iter(enumerate_extreme("t.py", "def f():\n    x = 1\n"))).family == "extreme"
    assert mutant_mechanism(Mutant("t.py", 2, 0, 1, "x = 1", "pass", "lang:statement:delete")) is ApplyMechanism.CODE
    assert classify(next(iter(enumerate_extreme("t.py", "def f():\n    x = 1\n")))) is Bucket.HIGH


def test_enumerate_extreme_reparses_valid_and_byte_precise() -> None:
    src = textwrap.dedent("""
        def settle(amount):
            total = amount * 2
            if total > 0:
                acc = total + 1
                return acc
            raise ValueError(total)
    """)
    muts = enumerate_extreme("t.py", src)
    assert len(muts) >= 4
    for m in muts:
        assert m.operator == "lang:statement:delete"
        ast.parse(apply_mutant(src, m))
    first = next(m for m in muts if m.original == "total = amount * 2")
    assert apply_mutant(src, first).splitlines()[2] == "    pass"
