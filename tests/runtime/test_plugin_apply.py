"""Applying a mutant to a live module (balamut.runtime.plugin.apply_mutation), without a daemon.

A pure seam: build a module with exec, apply a spec, check the behaviour change or UNSUPPORTED.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from balamut.operators.src import enumerate_mutants
from balamut.runtime import plugin
from balamut.runtime.inmemory import resolve_function
from balamut.runtime.plugin import (
    FORCE_COMPILE_ENV,
    ApplyOutcome,
    MutantSpec,
    apply_mutation,
    apply_mutation_reversible,
)


def _module(name: str, src: str) -> types.ModuleType:
    module = types.ModuleType(name)
    exec(compile(src, f"{name}.py", "exec"), vars(module))
    return module


def _noop(*_args: object) -> None:
    return None


def _bc_spec(name: str, source: str, qualname: str, predicate: Callable[[str], bool], *, trap: str) -> MutantSpec:
    """A spec with the bytecode fields of the first matching mutant. ``trap`` is the mutated_source of the
    fallback path: if apply recompiles, the trap is what gets applied, which tells the two paths apart."""
    mutant = next(m for m in enumerate_mutants("t.py", source) if predicate(m.operator))
    assert mutant.bc is not None, mutant.operator
    line_start = source.encode("utf-8").rfind(b"\n", 0, mutant.start_byte) + 1
    return {
        "module": name,
        "qualname": qualname,
        "kind": "src",
        "mutated_source": trap,
        "bc_kind": mutant.bc.kind.value,
        "bc_source": mutant.bc.source,
        "bc_target": mutant.bc.target,
        "bc_lineno": mutant.lineno,
        "bc_col": mutant.start_byte - line_start,
        "bc_end_col": mutant.end_byte - line_start,
        "bc_mutated": mutant.bc.mutated,
    }


def test_apply_mutation_prefers_bytecode_edit_over_recompile() -> None:
    source = "def f(a):\n    return a + 10\n"
    module = _module("m_bc_pref", source)
    spec = _bc_spec("m_bc_pref", source, "f", lambda op: op.startswith("lang:const:num"), trap=source)
    assert apply_mutation(module, spec) is ApplyOutcome.APPLIED
    f = resolve_function(module, "f")
    assert f is not None
    assert f(5) != 15  # behaviour changed: the bytecode edit applied, not the identity trap


def test_apply_mutation_force_compile_env_disables_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    # The kill switch routes everything through compile(mutated_source); the identity trap then mutates nothing.
    monkeypatch.setenv(FORCE_COMPILE_ENV, "1")
    source = "def f(a):\n    return a + 10\n"
    module = _module("m_bc_force", source)
    spec = _bc_spec("m_bc_force", source, "f", lambda op: op.startswith("lang:const:num"), trap=source)
    assert apply_mutation(module, spec) is ApplyOutcome.APPLIED
    f = resolve_function(module, "f")
    assert f is not None
    assert f(5) == 15


def test_apply_mutation_falls_back_to_compile_when_bytecode_rejects() -> None:
    source = "def f(a, x):\n    return a * 2 ** x\n"  # Pow->Mult regroups: the bytecode edit refuses
    module = _module("m_bc_fb", source)
    spec = _bc_spec(
        "m_bc_fb",
        source,
        "f",
        lambda op: op.startswith("lang:binop:Pow->Mult"),
        trap="def f(a, x):\n    return a - x\n",
    )
    assert apply_mutation(module, spec) is ApplyOutcome.APPLIED
    f = resolve_function(module, "f")
    assert f is not None
    assert f(10, 3) == 7


def test_apply_mutation_code_patches_live_function() -> None:
    module = _module("m_apply_code", "def add(a, b):\n    return a + b\n")
    spec: MutantSpec = {
        "module": "m_apply_code",
        "qualname": "add",
        "mutated_source": "def add(a, b):\n    return a - b\n",
        "kind": "src",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.APPLIED
    add = resolve_function(module, "add")
    assert add is not None
    assert add(2, 3) == -1


def test_apply_mutation_missing_function_is_unsupported() -> None:
    module = _module("m_apply_miss", "x = 1\n")
    spec: MutantSpec = {
        "module": "m_apply_miss",
        "qualname": "ghost",
        "mutated_source": "def ghost():\n    return 0\n",
        "kind": "src",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.UNSUPPORTED


def test_apply_mutation_defaults_replaces_positional_default() -> None:
    module = _module("m_apply_def", "def f(x=1):\n    return x\n")
    spec: MutantSpec = {
        "module": "m_apply_def",
        "qualname": "f",
        "mutated_source": "def f(x=2):\n    return x\n",
        "kind": "src",
        "mechanism": "defaults",
        "default_index": 0,
        "default_literal": "2",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.APPLIED
    f = resolve_function(module, "f")
    assert f is not None
    assert f() == 2


def test_apply_mutation_defaults_out_of_range_is_unsupported() -> None:
    module = _module("m_apply_def2", "def f(x=1):\n    return x\n")
    spec: MutantSpec = {
        "module": "m_apply_def2",
        "qualname": "f",
        "mutated_source": "",
        "kind": "src",
        "mechanism": "defaults",
        "default_index": 5,
        "default_literal": "2",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.UNSUPPORTED


def test_apply_mutation_defaults_unparseable_literal_is_unsupported() -> None:
    module = _module("m_apply_def3", "def f(x=1):\n    return x\n")
    spec: MutantSpec = {
        "module": "m_apply_def3",
        "qualname": "f",
        "mutated_source": "",
        "kind": "src",
        "mechanism": "defaults",
        "default_index": 0,
        "default_literal": "compute()",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.UNSUPPORTED


def test_apply_mutation_reversible_restore_undoes_code_patch() -> None:
    module = _module("m_rev_code", "def f(a):\n    return a + 10\n")
    outcome, restore = apply_mutation_reversible(
        module,
        {"module": "m_rev_code", "qualname": "f", "kind": "src", "mutated_source": "def f(a):\n    return a - 10\n"},
    )
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    f = resolve_function(module, "f")
    assert f is not None
    assert f(5) == -5
    restore()
    assert f(5) == 15


def test_apply_mutation_marks_exact_nested_code_when_it_executes(tmp_path: Path) -> None:
    source = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    module = _module("m_touch_nested", source)
    marker = tmp_path / "touched"
    outcome, restore = apply_mutation_reversible(
        module,
        {
            "module": "m_touch_nested",
            "qualname": "outer",
            "kind": "src",
            "mutated_source": "def outer():\n    def inner():\n        return 2\n    return inner\n",
            "target_lineno": 3,
            "touch_path": str(marker),
        },
    )
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    outer = resolve_function(module, "outer")
    assert outer is not None
    inner = outer()
    assert not marker.exists()  # calling outer has not executed the mutated inner code object yet
    assert inner() == 2
    assert marker.read_text(encoding="utf-8") == "1"
    restore()
    marker.unlink()
    assert outer()() == 1
    assert not marker.exists()  # restore removed the local monitoring callback


def test_apply_mutation_marks_outer_site_on_line_shared_with_lambda(tmp_path: Path) -> None:
    source = "def outer(value):\n    return (lambda: 1, value + 1)\n"
    mutated_source = "def outer(value):\n    return (lambda: 1, value - 1)\n"
    target_col = source.splitlines()[1].encode("utf-8").index(b"value + 1")
    module = _module("m_touch_shared_line", source)
    marker = tmp_path / "touched"
    outcome, restore = apply_mutation_reversible(
        module,
        {
            "module": "m_touch_shared_line",
            "qualname": "outer",
            "kind": "src",
            "mutated_source": mutated_source,
            "target_lineno": 2,
            "target_col": target_col,
            "touch_path": str(marker),
        },
    )
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    outer = resolve_function(module, "outer")
    assert outer is not None
    _unused_lambda, result = outer(5)
    assert result == 4
    assert marker.read_text(encoding="utf-8") == "1"
    restore()


def test_apply_mutation_marks_an_edit_spanning_a_lambda_when_the_call_fails_first(tmp_path: Path) -> None:
    # Swapped arguments: the call raises before the lambda it contains ever runs, yet the mutated code did run.
    source = "def outer(parts):\n    return list(filter(lambda p: p is not None, parts))\n"
    mutated_source = "def outer(parts):\n    return list(filter(parts, lambda p: p is not None))\n"
    target_col = source.splitlines()[1].encode("utf-8").index(b"filter(")
    module = _module("m_touch_spanning", source)
    marker = tmp_path / "touched"
    outcome, restore = apply_mutation_reversible(
        module,
        {
            "module": "m_touch_spanning",
            "qualname": "outer",
            "kind": "src",
            "mutated_source": mutated_source,
            "target_lineno": 2,
            "target_col": target_col,
            "touch_path": str(marker),
        },
    )
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    outer = resolve_function(module, "outer")
    assert outer is not None
    with pytest.raises(TypeError):
        outer([1, None])
    assert marker.read_text(encoding="utf-8") == "1"
    restore()


def test_apply_mutation_marks_an_edit_inside_a_lambda_only_when_the_lambda_runs(tmp_path: Path) -> None:
    source = "def outer(parts):\n    return list(filter(lambda p: p is not None, parts))\n"
    mutated_source = "def outer(parts):\n    return list(filter(lambda p: p is None, parts))\n"
    target_col = source.splitlines()[1].encode("utf-8").index(b"p is not None")
    module = _module("m_touch_in_lambda", source)
    marker = tmp_path / "touched"
    outcome, restore = apply_mutation_reversible(
        module,
        {
            "module": "m_touch_in_lambda",
            "qualname": "outer",
            "kind": "src",
            "mutated_source": mutated_source,
            "target_lineno": 2,
            "target_col": target_col,
            "touch_path": str(marker),
        },
    )
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    outer = resolve_function(module, "outer")
    assert outer is not None
    assert outer([]) == []
    assert not marker.exists()  # the outer body ran, the mutated lambda body did not
    assert outer([1, None]) == [None]
    assert marker.read_text(encoding="utf-8") == "1"
    restore()


def test_apply_defaults_marks_function_call(tmp_path: Path) -> None:
    module = _module("m_touch_default", "def f(x=1):\n    return x\n")
    marker = tmp_path / "touched"
    outcome, restore = apply_mutation_reversible(
        module,
        {
            "module": "m_touch_default",
            "qualname": "f",
            "kind": "src",
            "mutated_source": "def f(x=2):\n    return x\n",
            "mechanism": "defaults",
            "default_index": 0,
            "default_literal": "2",
            "target_lineno": 1,
            "touch_path": str(marker),
        },
    )
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    assert not marker.exists()
    f = resolve_function(module, "f")
    assert f is not None
    assert f() == 2
    assert marker.exists()
    restore()


def test_apply_mutation_reversible_restore_clears_lru_cache() -> None:
    # A persistent worker runs many mutants in one process: a result cached under mutant A must not survive the
    # restore, or the next mutant would see A's value through the wrapper.
    module = _module("m_rev_lru", "import functools\n\n\n@functools.lru_cache\ndef f(a):\n    return a + 10\n")
    outcome, restore = apply_mutation_reversible(
        module,
        {
            "module": "m_rev_lru",
            "qualname": "f",
            "kind": "src",
            "mutated_source": "import functools\n\n\n@functools.lru_cache\ndef f(a):\n    return a - 10\n",
        },
    )
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    cached = module.f
    assert cached(5) == -5
    restore()
    assert cached(5) == 15


def test_apply_mutation_reversible_defaults_restore() -> None:
    module = _module("m_rev_def", "def f(x=1):\n    return x\n")
    spec: MutantSpec = {
        "module": "m_rev_def",
        "qualname": "f",
        "kind": "src",
        "mutated_source": "",
        "mechanism": "defaults",
        "default_index": 0,
        "default_literal": "2",
    }
    outcome, restore = apply_mutation_reversible(module, spec)
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    f = resolve_function(module, "f")
    assert f is not None
    assert f() == 2
    restore()
    assert f() == 1


def test_apply_mutation_reversible_unsupported_has_no_restore() -> None:
    module = _module("m_rev_uns", "x = 1\n")
    outcome, restore = apply_mutation_reversible(
        module,
        {"module": "m_rev_uns", "qualname": "ghost", "kind": "src", "mutated_source": "def ghost():\n    return 0\n"},
    )
    assert outcome is ApplyOutcome.UNSUPPORTED
    assert restore is None


def test_switching_mutants_restores_before_applying_next() -> None:
    module = _module("m_switch", "def f(a):\n    return a + 10\n")
    _, restore_a = apply_mutation_reversible(
        module,
        {"module": "m_switch", "qualname": "f", "kind": "src", "mutated_source": "def f(a):\n    return a - 10\n"},
    )
    f = resolve_function(module, "f")
    assert f is not None
    assert f(5) == -5
    assert restore_a is not None
    restore_a()
    _, restore_b = apply_mutation_reversible(
        module,
        {"module": "m_switch", "qualname": "f", "kind": "src", "mutated_source": "def f(a):\n    return a * 10\n"},
    )
    assert f(5) == 50  # B lands on the original, not composed with A
    assert restore_b is not None
    restore_b()
    assert f(5) == 15


def test_json_value_type_alias_materializes() -> None:
    value = plugin.JsonValue.__value__
    assert isinstance(value, types.UnionType)


def test_monitoring_tool_ids_are_reserved_slots() -> None:
    assert plugin._MONITORING_TOOL_IDS == (4, 3)


def test_codes_at_site_falls_back_to_root_when_no_line() -> None:
    def f() -> int:
        return 0

    code = f.__code__
    assert plugin._codes_at_site(code, None, None) == (code,)


def test_codes_at_site_falls_back_to_root_when_no_match() -> None:
    def f() -> int:
        return 0

    code = f.__code__
    assert plugin._codes_at_site(code, 999, None) == (code,)


def _outer_code(source: str) -> types.CodeType:
    return next(const for const in compile(source, "t.py", "exec").co_consts if isinstance(const, types.CodeType))


def test_codes_at_site_keeps_an_edit_spanning_a_lambda_on_the_enclosing_function() -> None:
    source = "def outer(parts):\n    return list(filter(parts, lambda p: p is not None))\n"
    code = _outer_code(source)

    picked = plugin._codes_at_site(code, 2, source.splitlines()[1].index("filter("))

    assert [c.co_qualname for c in picked] == ["outer"]


def test_codes_at_site_picks_the_lambda_holding_the_edit_start() -> None:
    source = "def outer(parts):\n    return list(filter(lambda p: p is not None, parts))\n"
    code = _outer_code(source)

    picked = plugin._codes_at_site(code, 2, source.splitlines()[1].index("p is not None"))

    assert [c.co_qualname for c in picked] == ["outer.<locals>.<lambda>"]


def test_codes_at_site_leaves_the_decorator_line_of_a_nested_function_to_the_outer_scope() -> None:
    # The nested code object's RESUME sits at column 0 of its first line: a zero-width position, not its source.
    source = "def outer(deco):\n    @deco\n    def inner():\n        return 1\n    return inner\n"
    code = _outer_code(source)

    assert [c.co_qualname for c in plugin._codes_at_site(code, 2, 5)] == ["outer"]
    assert [c.co_qualname for c in plugin._codes_at_site(code, 4, 15)] == ["outer.<locals>.inner"]


def test_codes_at_site_without_a_column_matches_by_line() -> None:
    source = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    code = _outer_code(source)

    assert [c.co_qualname for c in plugin._codes_at_site(code, 3, None)] == ["outer.<locals>.inner"]
    assert [c.co_qualname for c in plugin._codes_at_site(code, 4, None)] == ["outer"]


def test_arm_touch_marker_noop_without_path() -> None:
    def f() -> int:
        return 0

    assert plugin._arm_touch_marker((f.__code__,), None) is plugin._noop_restore


def test_arm_touch_marker_noop_when_all_tool_ids_taken(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def f() -> int:
        return 0

    def use_tool_id(_candidate: int, _name: str) -> None:
        raise ValueError("taken")

    monkeypatch.setattr(sys.monitoring, "use_tool_id", use_tool_id)
    assert plugin._arm_touch_marker((f.__code__,), str(tmp_path / "touched")) is plugin._noop_restore


def test_arm_touch_marker_continues_to_next_tool_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def f() -> int:
        return 0

    calls: list[int] = []
    taken = {4}

    def use_tool_id(candidate: int, _name: str) -> None:
        calls.append(candidate)
        if candidate in taken:
            raise ValueError("taken")

    monkeypatch.setattr(sys.monitoring, "use_tool_id", use_tool_id)
    monkeypatch.setattr(sys.monitoring, "register_callback", _noop)
    monkeypatch.setattr(sys.monitoring, "set_local_events", _noop)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", _noop)
    monkeypatch.setattr(sys.monitoring, "clear_tool_id", _noop)
    restore = plugin._arm_touch_marker((f.__code__,), str(tmp_path / "touched"))
    assert restore is not plugin._noop_restore
    assert calls == [4, 3]
    restore()


def test_arm_touch_marker_clears_tool_id_on_register_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def f() -> int:
        return 0

    cleared: list[int] = []
    freed: list[int] = []
    used: list[int] = []

    def use_tool_id(candidate: int, _name: str) -> None:
        used.append(candidate)

    def register_callback(*_args: object) -> None:
        raise ValueError("no free id")

    monkeypatch.setattr(sys.monitoring, "use_tool_id", use_tool_id)
    monkeypatch.setattr(sys.monitoring, "register_callback", register_callback)
    monkeypatch.setattr(sys.monitoring, "set_local_events", _noop)
    monkeypatch.setattr(sys.monitoring, "clear_tool_id", cleared.append)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", freed.append)
    restore = plugin._arm_touch_marker((f.__code__,), str(tmp_path / "touched"))
    assert restore is plugin._noop_restore
    assert cleared == used
    assert freed == used


def test_arm_touch_marker_disarm_unregisters_callback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def f() -> int:
        return 0

    registered: list[object] = []

    def register_callback(_tool_id: int, _event: object, func: object) -> None:
        registered.append(func)

    monkeypatch.setattr(sys.monitoring, "use_tool_id", _noop)
    monkeypatch.setattr(sys.monitoring, "register_callback", register_callback)
    monkeypatch.setattr(sys.monitoring, "set_local_events", _noop)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", _noop)
    monkeypatch.setattr(sys.monitoring, "clear_tool_id", _noop)
    disarm = plugin._arm_touch_marker((f.__code__,), str(tmp_path / "touched"))
    assert disarm is not None
    disarm()
    assert registered[-1] is None


def test_apply_mutation_nested_function_via_enclosing() -> None:
    module = _module("m_apply_nested", "def outer():\n    def inner():\n        return 1\n    return inner\n")
    spec: MutantSpec = {
        "module": "m_apply_nested",
        "qualname": "outer",
        "mutated_source": "def outer():\n    def inner():\n        return 2\n    return inner\n",
        "kind": "src",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.APPLIED
    outer = resolve_function(module, "outer")
    assert outer is not None
    assert outer()() == 2


def test_apply_mutation_refuses_a_foreign_definition_behind_an_unwrapped_decorator() -> None:
    # A decorator without functools.wraps leaves the wrapper in the module: compiling the mutated source finds the
    # body under the same qualname, but grafting it onto the wrapper would destroy the wrapper.
    source = (
        "def deco(fn):\n"
        "    def wrapper(*a):\n"
        "        return fn(*a)\n"
        "    return wrapper\n"
        "@deco\n"
        "def f(a):\n"
        "    return a + 1\n"
    )
    module = _module("m_unwrapped", source)
    spec: MutantSpec = {
        "module": "m_unwrapped",
        "qualname": "f",
        "kind": "src",
        "mutated_source": source.replace("a + 1", "a - 1"),
    }
    assert apply_mutation(module, spec) is ApplyOutcome.UNSUPPORTED
    assert module.f(1) == 2


def test_apply_mutation_defaults_missing_index_is_unsupported() -> None:
    module = _module("m_apply_def4", "def f(x=1):\n    return x\n")
    spec: MutantSpec = {
        "module": "m_apply_def4",
        "qualname": "f",
        "mutated_source": "",
        "kind": "src",
        "mechanism": "defaults",
        "default_literal": "2",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.UNSUPPORTED


def test_apply_mutation_compile_failure_is_unsupported() -> None:
    module = _module("m_apply_cf", "def f(a):\n    return a + 1\n")
    spec: MutantSpec = {
        "module": "m_apply_cf",
        "qualname": "f",
        "kind": "src",
        "mutated_source": "x = 1\n",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.UNSUPPORTED


def test_apply_mutation_patch_code_closure_rejection_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    # A body whose closure changed can no longer be grafted onto the live function (freevars/cellvars mismatch);
    # the guard reports UNSUPPORTED rather than crashing the worker.
    module = _module("m_apply_pc", "def f(a):\n    return a + 1\n")

    def patch_fails(_func: types.FunctionType, _new_code: types.CodeType) -> Callable[[], None]:
        raise ValueError("freevars mismatch")

    monkeypatch.setattr(plugin, "patch_code", patch_fails)
    spec: MutantSpec = {
        "module": "m_apply_pc",
        "qualname": "f",
        "kind": "src",
        "mutated_source": "def f(a):\n    return a - 1\n",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.UNSUPPORTED


def test_arm_touch_marker_callback_writes_the_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def f() -> int:
        return 0

    registered: list[object] = []

    def register_callback(_tool_id: int, _event: object, func: object) -> None:
        registered.append(func)

    monkeypatch.setattr(sys.monitoring, "use_tool_id", _noop)
    monkeypatch.setattr(sys.monitoring, "register_callback", register_callback)
    monkeypatch.setattr(sys.monitoring, "set_local_events", _noop)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", _noop)
    monkeypatch.setattr(sys.monitoring, "clear_tool_id", _noop)
    marker = tmp_path / "touched"
    restore = plugin._arm_touch_marker((f.__code__,), str(marker))
    assert restore is not None
    mark = registered[-1]
    assert callable(mark)
    assert mark(f.__code__, 0) is sys.monitoring.DISABLE
    assert marker.read_text(encoding="utf-8") == "1"
    restore()


def test_apply_defaults_restore_disarms_the_touch_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module("m_def_disarm", "def f(x=1):\n    return x\n")
    disarmed: list[bool] = []

    def disarm() -> None:
        disarmed.append(True)

    def arm(_codes: tuple[types.CodeType, ...], _path: str | None) -> Callable[[], None]:
        return disarm

    monkeypatch.setattr(plugin, "_arm_touch_marker", arm)
    spec: MutantSpec = {
        "module": "m_def_disarm",
        "qualname": "f",
        "mutated_source": "",
        "kind": "src",
        "mechanism": "defaults",
        "default_index": 0,
        "default_literal": "2",
    }
    outcome, restore = apply_mutation_reversible(module, spec)
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    restore()
    assert disarmed == [True]


def test_apply_defaults_restore_clears_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module("m_def_cc", "def f(x=1):\n    return x\n")
    func = resolve_function(module, "f")
    assert func is not None
    cleared: list[bool] = []

    def clear() -> None:
        cleared.append(True)

    def resolve(_module: types.ModuleType, _qualname: str) -> tuple[types.FunctionType | None, Callable[[], None]]:
        return (func, clear)

    def arm(_codes: tuple[types.CodeType, ...], _path: str | None) -> Callable[[], None]:
        return _noop

    monkeypatch.setattr(plugin, "resolve_with_cache_clear", resolve)
    monkeypatch.setattr(plugin, "_arm_touch_marker", arm)
    spec: MutantSpec = {
        "module": "m_def_cc",
        "qualname": "f",
        "mutated_source": "",
        "kind": "src",
        "mechanism": "defaults",
        "default_index": 0,
        "default_literal": "2",
    }
    outcome, restore = apply_mutation_reversible(module, spec)
    assert outcome is ApplyOutcome.APPLIED
    assert restore is not None
    restore()
    assert cleared == [True]


def test_arm_touch_marker_disarm_disables_local_events(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def f() -> int:
        return 0

    events: list[tuple[int, object, int]] = []

    def set_local_events(tool_id: int, code: types.CodeType, event: int) -> None:
        events.append((tool_id, code, event))

    monkeypatch.setattr(sys.monitoring, "use_tool_id", _noop)
    monkeypatch.setattr(sys.monitoring, "register_callback", _noop)
    monkeypatch.setattr(sys.monitoring, "set_local_events", set_local_events)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", _noop)
    monkeypatch.setattr(sys.monitoring, "clear_tool_id", _noop)
    disarm = plugin._arm_touch_marker((f.__code__,), str(tmp_path / "touched"))
    assert disarm is not None
    disarm()
    assert events == [
        (plugin._MONITORING_TOOL_IDS[0], f.__code__, sys.monitoring.events.PY_START),
        (plugin._MONITORING_TOOL_IDS[0], f.__code__, 0),
    ]


def test_apply_mutation_defaults_at_the_end_is_unsupported() -> None:
    # index == len(defaults) is out of range: the half-open check must reject it (a <= would index past the end).
    module = _module("m_apply_def5", "def f(x=1):\n    return x\n")
    spec: MutantSpec = {
        "module": "m_apply_def5",
        "qualname": "f",
        "mutated_source": "",
        "kind": "src",
        "mechanism": "defaults",
        "default_index": 1,
        "default_literal": "2",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.UNSUPPORTED


def test_apply_mutation_injects_nullcontext(monkeypatch: pytest.MonkeyPatch) -> None:
    # A lock-drop mutant references nullcontext; the plugin injects it into the module globals before recompiling.
    module = _module("m_inject", "def f():\n    return 1\n")
    spec: MutantSpec = {
        "module": "m_inject",
        "qualname": "f",
        "kind": "src",
        "mutated_source": "def f():\n    with nullcontext():\n        return 2\n",
    }
    assert apply_mutation(module, spec) is ApplyOutcome.APPLIED
    f = resolve_function(module, "f")
    assert f is not None
    assert f() == 2


def test_apply_mutation_bytecode_binop_uses_source_and_target() -> None:
    # A binop bytecode edit reads `bc_source`/`bc_target` (the precedence gate and the arg lookup). A const edit
    # ignores them, so the source/target path must be pinned with a binop: a wrong or absent value falls back to
    # the identity trap and the behaviour does not change.
    source = "def f(a, b):\n    return a + b\n"
    module = _module("m_bc_binop", source)
    spec = _bc_spec("m_bc_binop", source, "f", lambda op: op.startswith("lang:binop:Add"), trap=source)
    assert apply_mutation(module, spec) is ApplyOutcome.APPLIED
    f = resolve_function(module, "f")
    assert f is not None
    assert f(2, 3) == -1  # Add -> Sub applied via bytecode, not the identity trap
