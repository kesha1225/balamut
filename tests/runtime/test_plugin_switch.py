"""Switching mutants inside a persistent worker (balamut.runtime.plugin._apply_or_switch).

A persistent worker reuses one process: a new mutant file must restore the previous mutant before the new
request's tests run, including the early exits on a missing, unreadable or broken spec. Otherwise the tests
would run against the stale ``__code__``/``__defaults__`` patch.
"""

from __future__ import annotations

import json
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from balamut.runtime import plugin
from balamut.runtime.worker_reset import WorkerBaseline
from bm_support.plugin_fakes import FakeItem, FakeSession


def _item_with_applied_prev(restored: list[bool]) -> FakeItem:
    session = FakeSession()

    def _restore() -> None:
        restored.append(True)

    session.stash[plugin._STATE] = plugin._AppliedMutant("old-mutant-raw", _restore)
    return FakeItem(session)


def test_switch_restores_prev_when_mutant_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    restored: list[bool] = []
    item = _item_with_applied_prev(restored)
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(tmp_path / "gone.json"))
    plugin._apply_or_switch(cast("pytest.Item", item))
    assert restored == [True]
    assert item.session.stash.get(plugin._STATE, None) is None


def test_switch_restores_prev_when_mutant_file_unreadable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    restored: list[bool] = []
    item = _item_with_applied_prev(restored)
    bad = tmp_path / "mutant_is_a_dir"
    bad.mkdir()  # read_text on a directory raises an OSError
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(bad))
    plugin._apply_or_switch(cast("pytest.Item", item))
    assert restored == [True]
    assert item.session.stash.get(plugin._STATE, None) is None


def test_switch_marks_leaked_import_target_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A module imported by a previous mutant's test body (in sys.modules but not in the baseline) is not mutated:
    # in a fresh fork it would not be there, and mutating stale import state gives false verdicts. It is not
    # unloaded either: re-importing a C extension can crash, a partial Python re-import breaks identity.
    session = FakeSession()
    item = cast("pytest.Item", FakeItem(session))
    leaked = "bm_leaked_import_mod"
    sys.modules.pop(leaked, None)

    spec_a = tmp_path / "a.json"
    spec_a.write_text(
        json.dumps({"module": "bm_absent_mod_zzz", "qualname": "f", "kind": "src", "mutated_source": "x = 1"}),
        encoding="utf-8",
    )
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec_a))
    plugin._apply_or_switch(item)  # takes the baseline without the leaked module

    sys.modules[leaked] = types.ModuleType(leaked)
    try:
        spec_b = tmp_path / "b.json"
        spec_b.write_text(
            json.dumps(
                {"module": leaked, "qualname": "g", "kind": "src", "mutated_source": "def g():\n    return 1\n"}
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec_b))
        plugin._apply_or_switch(item)
        assert Path(f"{spec_b}{plugin.UNSUPPORTED_MARKER_SUFFIX}").exists()
        assert leaked in sys.modules
    finally:
        sys.modules.pop(leaked, None)


def test_switch_keeps_prev_applied_for_same_request_without_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # One request token is one mutant lease: between its items the patch and the process state stay in place.
    restored: list[bool] = []
    resets: list[WorkerBaseline] = []
    session = FakeSession()
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    session.stash[plugin._STATE] = plugin._AppliedMutant("same-raw", lambda: restored.append(True))
    spec = tmp_path / "m.json"
    spec.write_text("same-raw", encoding="utf-8")
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    monkeypatch.setattr(plugin, "reset_to_baseline", resets.append)
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert restored == []
    assert resets == []


def test_switch_uses_request_token_as_boundary_for_same_mutant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    restored: list[bool] = []
    resets: list[WorkerBaseline] = []
    session = FakeSession()
    baseline = WorkerBaseline(frozenset(sys.modules), ())
    session.stash[plugin._BASELINE] = baseline
    base_spec = {
        "module": "bm_absent_mod_zzz",
        "qualname": "f",
        "kind": "src",
        "mutated_source": "def f():\n    return 1\n",
    }
    previous_raw = json.dumps({**base_spec, "request_token": "lease-a"})
    session.stash[plugin._STATE] = plugin._AppliedMutant(previous_raw, lambda: restored.append(True))
    spec = tmp_path / "m.json"
    spec.write_text(json.dumps({**base_spec, "request_token": "lease-b"}), encoding="utf-8")
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    monkeypatch.setattr(plugin, "reset_to_baseline", resets.append)
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert restored == [True]
    assert resets == [baseline]


def test_empty_baseline_request_records_a_boundary_for_the_first_mutant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = FakeSession()
    item = cast("pytest.Item", FakeItem(session))
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    spec = tmp_path / "m.json"
    spec.write_text("", encoding="utf-8")
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    plugin._apply_or_switch(item)
    state = session.stash.get(plugin._STATE, None)
    assert state is not None
    assert not state.key


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        '"scalar"',
        '{"module": "m", "qualname": "f", "kind": "src"}',
        '{"module": 1, "qualname": "f", "kind": "src", "mutated_source": ""}',
        '{"module": "m", "qualname": "f", "kind": "src", "mutated_source": "", "default_index": true}',
        '{"module": "m", "qualname": "f", "kind": "src", "mutated_source": "", "bc_kind": 5}',
        '{"module": "m", "qualname": "f", "kind": "src", "mutated_source": "", "bc_col": "x"}',
        '{"module": "m", "qualname": "f", "kind": "src", "mutated_source": "", "touch_path": 3}',
        '{"module": "m", "qualname": "f", "kind": "src", "mutated_source": "", "self_covered": 1}',
    ],
)
def test_untrusted_spec_is_rejected_without_crashing(raw: str) -> None:
    assert plugin.parse_mutant_spec(raw) is None


def test_broken_spec_marks_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = FakeSession()
    spec = tmp_path / "m.json"
    spec.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert Path(f"{spec}{plugin.UNSUPPORTED_MARKER_SUFFIX}").exists()
    state = session.stash.get(plugin._STATE, None)
    assert state is not None
    assert state.key == "{broken"
    assert state.restore is plugin._noop_restore


_SELF_COVERED_MODULE = types.ModuleType("bm_self_covered_mod")


class _SelfCoveredItem(pytest.Function):
    """A pytest.Function stand-in whose ``module`` is a fixed module (the real one derives from the collection tree)."""

    @property
    def module(self) -> types.ModuleType:
        return _SELF_COVERED_MODULE


def test_target_module_requires_function_item_for_self_covered() -> None:
    item = cast("pytest.Item", FakeItem(FakeSession()))
    baseline = WorkerBaseline(frozenset(sys.modules), ())
    spec = {
        "module": "bm_absent_mod_zzz",
        "qualname": "f",
        "kind": "src",
        "mutated_source": "",
        "self_covered": True,
    }
    assert plugin._target_module(item, cast("plugin.MutantSpec", spec), baseline) is None


def test_target_module_self_covered_returns_collected_module() -> None:
    item = _SelfCoveredItem.__new__(_SelfCoveredItem)
    baseline = WorkerBaseline(frozenset(sys.modules), ())
    spec = {"module": "bm_any", "qualname": "f", "kind": "src", "mutated_source": "", "self_covered": True}
    assert (
        plugin._target_module(cast("pytest.Item", item), cast("plugin.MutantSpec", spec), baseline)
        is _SELF_COVERED_MODULE
    )


def test_target_module_self_covered_flag_is_identity() -> None:
    item = _SelfCoveredItem.__new__(_SelfCoveredItem)
    baseline = WorkerBaseline(frozenset(sys.modules), ())
    spec = {
        "module": "bm_absent_mod_zzz",
        "qualname": "f",
        "kind": "src",
        "mutated_source": "",
        "self_covered": False,
    }
    assert plugin._target_module(cast("pytest.Item", item), cast("plugin.MutantSpec", spec), baseline) is None


def test_target_module_returns_baseline_module() -> None:
    module = types.ModuleType("bm_normal_mod")
    sys.modules["bm_normal_mod"] = module
    try:
        item = cast("pytest.Item", FakeItem(FakeSession()))
        baseline = WorkerBaseline(frozenset({"bm_normal_mod"}), ())
        spec = {"module": "bm_normal_mod", "qualname": "f", "kind": "src", "mutated_source": ""}
        assert plugin._target_module(item, cast("plugin.MutantSpec", spec), baseline) is module
    finally:
        sys.modules.pop("bm_normal_mod", None)


def test_target_module_requires_baseline_module() -> None:
    module = types.ModuleType("bm_leaked_mod")
    sys.modules["bm_leaked_mod"] = module
    try:
        item = cast("pytest.Item", FakeItem(FakeSession()))
        baseline = WorkerBaseline(frozenset(), ())
        spec = {"module": "bm_leaked_mod", "qualname": "f", "kind": "src", "mutated_source": ""}
        assert plugin._target_module(item, cast("plugin.MutantSpec", spec), baseline) is None
    finally:
        sys.modules.pop("bm_leaked_mod", None)


def test_switch_missing_file_resets_to_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    resets: list[WorkerBaseline] = []
    baseline = WorkerBaseline(frozenset(sys.modules), ())
    session = FakeSession()
    session.stash[plugin._BASELINE] = baseline
    session.stash[plugin._STATE] = plugin._AppliedMutant("old", lambda: None)
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(tmp_path / "gone.json"))
    monkeypatch.setattr(plugin, "reset_to_baseline", resets.append)
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert resets == [baseline]


def test_switch_unreadable_spec_marks_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = FakeSession()
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    bad = tmp_path / "mutant_is_a_dir"
    bad.mkdir()
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(bad))
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert Path(f"{bad}{plugin.UNSUPPORTED_MARKER_SUFFIX}").exists()


def _real_module_in_baseline() -> tuple[types.ModuleType, FakeSession]:
    module = types.ModuleType("bm_real_mod")
    exec(compile("x = 1\n", "bm_real_mod.py", "exec"), vars(module))
    sys.modules["bm_real_mod"] = module
    session = FakeSession()
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    return module, session


def test_switch_unsupported_apply_marks_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _module, session = _real_module_in_baseline()
    try:
        spec = tmp_path / "m.json"
        spec.write_text(
            json.dumps(
                {
                    "module": "bm_real_mod",
                    "qualname": "ghost",
                    "kind": "src",
                    "mutated_source": "def ghost():\n    return 0\n",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
        plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
        assert Path(f"{spec}{plugin.UNSUPPORTED_MARKER_SUFFIX}").exists()
    finally:
        sys.modules.pop("bm_real_mod", None)


def test_switch_none_restore_marks_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _module, session = _real_module_in_baseline()
    try:

        def apply_applied(
            _module: types.ModuleType, _spec: plugin.MutantSpec
        ) -> tuple[plugin.ApplyOutcome, Callable[[], None] | None]:
            return (plugin.ApplyOutcome.APPLIED, None)

        monkeypatch.setattr(plugin, "apply_mutation_reversible", apply_applied)
        spec = tmp_path / "m.json"
        spec.write_text(
            json.dumps(
                {"module": "bm_real_mod", "qualname": "f", "kind": "src", "mutated_source": "def f():\n    return 1\n"}
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
        plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
        assert Path(f"{spec}{plugin.UNSUPPORTED_MARKER_SUFFIX}").exists()
        state = session.stash.get(plugin._STATE, None)
        assert state is not None
        assert state.restore is plugin._noop_restore
    finally:
        sys.modules.pop("bm_real_mod", None)


def test_switch_unsupported_outcome_marks_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _module, session = _real_module_in_baseline()
    try:

        def apply_unsupported(
            _module: types.ModuleType, _spec: plugin.MutantSpec
        ) -> tuple[plugin.ApplyOutcome, Callable[[], None] | None]:
            return (plugin.ApplyOutcome.UNSUPPORTED, lambda: None)

        monkeypatch.setattr(plugin, "apply_mutation_reversible", apply_unsupported)
        spec = tmp_path / "m.json"
        spec.write_text(
            json.dumps(
                {"module": "bm_real_mod", "qualname": "f", "kind": "src", "mutated_source": "def f():\n    return 1\n"}
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
        plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
        assert Path(f"{spec}{plugin.UNSUPPORTED_MARKER_SUFFIX}").exists()
    finally:
        sys.modules.pop("bm_real_mod", None)


def test_runtest_setup_applies_or_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[object] = []
    monkeypatch.setattr(plugin, "_apply_or_switch", called.append)
    item = cast("pytest.Item", FakeItem(FakeSession()))
    plugin.pytest_runtest_setup(item)
    assert called == [item]


def test_runtest_protocol_yields_to_inner_protocol() -> None:
    item = cast("pytest.Item", FakeItem(FakeSession()))
    wrapper = plugin.pytest_runtest_protocol(item)
    next(wrapper)
    with pytest.raises(StopIteration) as exc:
        wrapper.send(True)
    assert exc.value.value is True


def test_runtest_setup_scrubs_transport_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(plugin.MUTANT_FILE_ENV, raising=False)
    for key in plugin._OWNED_TRANSPORT_ENV:
        monkeypatch.setenv(key, "1")
    item = cast("pytest.Item", FakeItem(FakeSession()))
    plugin.pytest_runtest_setup(item)
    for key in plugin._OWNED_TRANSPORT_ENV:
        assert key not in os.environ


def test_switch_treats_a_valid_spec_with_original_deps_path_as_a_mutant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The `and` guard separates a real mutant from a paired original: only a spec that fails to parse as a mutant
    # is treated as an original. A mutant spec carrying an extra `original_deps_path` key is still a mutant.
    session = FakeSession()
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    spec = tmp_path / "m.json"
    spec.write_text(
        json.dumps(
            {
                "module": "bm_absent_mod_zzz",
                "qualname": "f",
                "kind": "src",
                "mutated_source": "def f():\n    return 1\n",
                "original_deps_path": str(tmp_path / "deps.json"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert Path(f"{spec}{plugin.UNSUPPORTED_MARKER_SUFFIX}").exists()


def test_switch_does_not_apply_to_a_missing_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = FakeSession()
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    calls: list[object] = []

    def apply_spy(module: object, _spec: plugin.MutantSpec) -> tuple[plugin.ApplyOutcome, Callable[[], None] | None]:
        calls.append(module)
        return (plugin.ApplyOutcome.UNSUPPORTED, None)

    monkeypatch.setattr(plugin, "apply_mutation_reversible", apply_spy)
    spec = tmp_path / "m.json"
    spec.write_text(
        json.dumps(
            {
                "module": "bm_absent_mod_zzz",
                "qualname": "f",
                "kind": "src",
                "mutated_source": "def f():\n    return 1\n",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert calls == []
    assert Path(f"{spec}{plugin.UNSUPPORTED_MARKER_SUFFIX}").exists()


def test_switch_missing_file_marks_a_lease_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = FakeSession()
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    leases: list[tuple[object, object]] = []

    def lease(session: pytest.Session, target: str | None) -> None:
        leases.append((session, target))

    def reset(_baseline: WorkerBaseline) -> None:
        return None

    monkeypatch.setattr(plugin, "_deps_lease", lease)
    monkeypatch.setattr(plugin, "reset_to_baseline", reset)
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(tmp_path / "gone.json"))
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert leases == [(session, None)]


def test_switch_unreadable_spec_marks_a_lease_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = FakeSession()
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    leases: list[tuple[object, object]] = []

    def lease(session: pytest.Session, target: str | None) -> None:
        leases.append((session, target))

    def reset(_baseline: WorkerBaseline) -> None:
        return None

    monkeypatch.setattr(plugin, "_deps_lease", lease)
    monkeypatch.setattr(plugin, "reset_to_baseline", reset)
    bad = tmp_path / "mutant_is_a_dir"
    bad.mkdir()
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(bad))
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert leases == [(session, None)]


def test_switch_applies_mutant_and_stores_restore(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The successful apply path: the applied state holds the raw spec text as its key and a callable restore, so
    # the next item of the same lease is skipped and the next lease can revert the patch.
    module = types.ModuleType("bm_apply_mod")
    exec(compile("def f():\n    return 1\n", "bm_apply_mod.py", "exec"), vars(module))
    sys.modules["bm_apply_mod"] = module
    try:
        session = FakeSession()
        session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
        spec = tmp_path / "m.json"
        raw = json.dumps(
            {"module": "bm_apply_mod", "qualname": "f", "kind": "src", "mutated_source": "def f():\n    return 2\n"}
        )
        spec.write_text(raw, encoding="utf-8")
        monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
        plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
        state = session.stash.get(plugin._STATE, None)
        assert state is not None
        assert state.key == raw
        assert callable(state.restore)
        assert module.f() == 2
        state.restore()
        assert module.f() == 1
    finally:
        sys.modules.pop("bm_apply_mod", None)
