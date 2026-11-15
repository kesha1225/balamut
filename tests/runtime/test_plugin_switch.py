"""Switching mutants inside a persistent worker (balamut.runtime.plugin._apply_or_switch).

A persistent worker reuses one process: each test item runs against the mutant of its lease and
``pytest_runtest_protocol`` reverts it after the item, and a new mutant file starts a new lease from the baseline,
including the early exits on a missing, unreadable or broken spec.
"""

from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import sys
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from balamut.runtime import plugin
from balamut.runtime.reloads import Follower, ReloadLog, reload_log
from balamut.runtime.worker_reset import WorkerBaseline
from bm_support.plugin_fakes import FakeItem, FakeSession


def _session_with_lease(applied: list[str]) -> FakeSession:
    session = FakeSession()

    def apply() -> Callable[[], None]:
        applied.append("apply")
        return lambda: applied.append("revert")

    session.stash[plugin._STATE] = plugin._Lease("old-mutant-raw", apply)
    return session


def test_switch_drops_the_lease_when_mutant_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    applied: list[str] = []
    session = _session_with_lease(applied)
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(tmp_path / "gone.json"))
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert applied == []
    assert session.stash.get(plugin._STATE, None) is None
    assert session.stash.get(plugin._ITEM, None) is None


def test_switch_drops_the_lease_when_mutant_file_unreadable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    applied: list[str] = []
    session = _session_with_lease(applied)
    bad = tmp_path / "mutant_is_a_dir"
    bad.mkdir()  # read_text on a directory raises an OSError
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(bad))
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert applied == []
    assert session.stash.get(plugin._STATE, None) is None


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

    leaked_module = types.ModuleType(leaked)
    exec(compile("def g():\n    return 0\n", f"{leaked}.py", "exec"), vars(leaked_module))
    sys.modules[leaked] = leaked_module
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
        assert leaked_module.g() == 0  # the baseline of the first lease still stands: the spec would apply
        assert leaked in sys.modules
    finally:
        sys.modules.pop(leaked, None)


def test_switch_applies_the_same_lease_again_without_reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # One request token is one mutant lease: its next item gets the mutant again over the state the lease left.
    applied: list[str] = []
    resets: list[WorkerBaseline] = []
    session = FakeSession()
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    session.stash[plugin._STATE] = plugin._Lease("same-raw", lambda: lambda: applied.append("revert"))
    spec = tmp_path / "m.json"
    spec.write_text("same-raw", encoding="utf-8")
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    monkeypatch.setattr(plugin, "reset_to_baseline", resets.append)
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert resets == []
    revert = session.stash.get(plugin._ITEM, None)
    assert revert is not None
    revert()
    assert applied == ["revert"]


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
    session.stash[plugin._STATE] = plugin._Lease(previous_raw, lambda: lambda: restored.append(True))
    spec = tmp_path / "m.json"
    spec.write_text(json.dumps({**base_spec, "request_token": "lease-b"}), encoding="utf-8")
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    monkeypatch.setattr(plugin, "reset_to_baseline", resets.append)
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert restored == []  # the previous lease is not applied again
    assert resets == [baseline]
    lease = session.stash.get(plugin._STATE, None)
    assert lease is not None
    assert lease.key == json.dumps({**base_spec, "request_token": "lease-b"})


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
    assert session.stash.get(plugin._STATE, None) == plugin._Lease("{broken", plugin._noop_apply)
    assert session.stash.get(plugin._ITEM, None) is plugin._noop_restore


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
    session.stash[plugin._STATE] = plugin._Lease("old", plugin._noop_apply)
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


def test_switch_unprepared_mutant_marks_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _module, session = _real_module_in_baseline()
    try:

        def prepare_none(
            _module: types.ModuleType,
            _spec: plugin.MutantSpec,
            *,
            follow: Callable[[Follower | None], None] | None = None,
        ) -> plugin.Apply | None:
            del follow
            return None

        monkeypatch.setattr(plugin, "prepare_mutation", prepare_none)
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
        lease = session.stash.get(plugin._STATE, None)
        assert lease is not None
        assert lease.apply is plugin._noop_apply
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


def test_runtest_setup_lets_a_daemonic_worker_start_child_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest-fast marks its workers daemonic, and multiprocessing refuses to start a child of a daemonic process.
    monkeypatch.delenv(plugin.MUTANT_FILE_ENV, raising=False)
    process = multiprocessing.current_process()
    monkeypatch.setattr(process, "daemon", True)
    plugin.pytest_runtest_setup(cast("pytest.Item", FakeItem(FakeSession())))
    assert process.daemon is False


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

    def prepare_spy(
        module: types.ModuleType,
        _spec: plugin.MutantSpec,
        *,
        follow: Callable[[Follower | None], None] | None = None,
    ) -> plugin.Apply | None:
        del follow
        calls.append(module)
        return None

    monkeypatch.setattr(plugin, "prepare_mutation", prepare_spy)
    spec = tmp_path / "m.json"
    raw = json.dumps(
        {
            "module": "bm_absent_mod_zzz",
            "qualname": "f",
            "kind": "src",
            "mutated_source": "def f():\n    return 1\n",
        }
    )
    spec.write_text(raw, encoding="utf-8")
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert calls == []
    assert Path(f"{spec}{plugin.UNSUPPORTED_MARKER_SUFFIX}").exists()
    # The lease still has its boundary: the next item of it is recognized, the next lease resets after it.
    assert session.stash.get(plugin._STATE, None) == plugin._Lease(raw, plugin._noop_apply)


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


def test_switch_applies_mutant_and_stores_the_item_revert(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The successful apply path: the lease holds the raw spec text as its key, and the item's revert puts the
    # original back.
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
        lease = session.stash.get(plugin._STATE, None)
        assert lease is not None
        assert lease.key == raw
        revert = session.stash.get(plugin._ITEM, None)
        assert revert is not None
        assert module.f() == 2
        revert()
        assert module.f() == 1
    finally:
        sys.modules.pop("bm_apply_mod", None)


def test_runtest_protocol_reverts_the_mutant_of_its_item() -> None:
    reverted: list[bool] = []
    session = FakeSession()
    wrapper = plugin.pytest_runtest_protocol(cast("pytest.Item", FakeItem(session)))
    next(wrapper)
    session.stash[plugin._ITEM] = lambda: reverted.append(True)
    with pytest.raises(StopIteration):
        wrapper.send(True)
    assert reverted == [True]
    assert session.stash.get(plugin._ITEM, None) is None


def test_runtest_protocol_reverts_the_mutant_when_the_protocol_raises() -> None:
    reverted: list[bool] = []
    session = FakeSession()
    wrapper = plugin.pytest_runtest_protocol(cast("pytest.Item", FakeItem(session)))
    next(wrapper)
    session.stash[plugin._ITEM] = lambda: reverted.append(True)
    with pytest.raises(RuntimeError, match="item"):
        wrapper.throw(RuntimeError("item"))
    assert reverted == [True]


@dataclass
class _Config:
    stash: pytest.Stash = field(default_factory=pytest.Stash)
    cleanups: list[Callable[[], None]] = field(default_factory=list[Callable[[], None]])

    def add_cleanup(self, cleanup: Callable[[], None]) -> None:
        self.cleanups.append(cleanup)


def test_configure_routes_reload_through_the_log_until_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    replaced = importlib.reload
    monkeypatch.setattr(importlib, "reload", replaced)  # put back whatever happens below
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(tmp_path / "m.json"))
    config = _Config()

    plugin.pytest_configure(cast("pytest.Config", config))

    log = config.stash[plugin._RELOADS]
    assert importlib.reload is log.reload
    plugin.pytest_configure(cast("pytest.Config", config))
    assert importlib.reload is log.reload  # configured once per config
    for cleanup in config.cleanups:
        cleanup()
    assert importlib.reload is replaced


def test_configure_leaves_reload_alone_outside_a_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(plugin.MUTANT_FILE_ENV, raising=False)
    replaced = importlib.reload
    config = _Config()

    plugin.pytest_configure(cast("pytest.Config", config))

    assert importlib.reload is replaced
    assert plugin._RELOADS not in config.stash


@pytest.fixture
def reloadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    """``bm_reload_target`` importable from disk, so a real reload executes its body again."""
    (tmp_path / "bm_reload_target.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    module = importlib.import_module("bm_reload_target")
    try:
        yield module
    finally:
        sys.modules.pop("bm_reload_target", None)


def _run_item(session: FakeSession, check: Callable[[], None] = lambda: None) -> None:
    """One test item as a worker runs it: the protocol wrapper around the setup that applies the lease's mutant."""
    item = cast("pytest.Item", FakeItem(session))
    wrapper = plugin.pytest_runtest_protocol(item)
    next(wrapper)
    plugin._apply_or_switch(item)
    check()
    with pytest.raises(StopIteration):
        wrapper.send(True)


def _reload_session(spec_path: Path) -> tuple[FakeSession, ReloadLog]:
    session = FakeSession()
    log = reload_log(importlib.reload)
    session.config.stash[plugin._RELOADS] = log
    session.stash[plugin._BASELINE] = WorkerBaseline(frozenset(sys.modules), ())
    spec_path.write_text("", encoding="utf-8")
    return session, log


def test_a_reload_in_a_lease_takes_the_mutant_and_is_undone_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reloadable: types.ModuleType
) -> None:
    spec = tmp_path / "m.json"
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    session, log = _reload_session(spec)
    imported = reloadable.f  # what `from bm_reload_target import f` in another test file holds
    spec.write_text(
        json.dumps(
            {
                "module": "bm_reload_target",
                "qualname": "f",
                "kind": "src",
                "mutated_source": "def f():\n    return 2\n",
                "request_token": "lease-a",
            }
        ),
        encoding="utf-8",
    )
    reloaded: list[Callable[[], int]] = []

    def reload_in_the_test() -> None:
        log.reload(reloadable)
        reloaded.append(reloadable.f)
        assert reloadable.f is not imported
        assert (imported(), reloadable.f()) == (2, 2)  # both definitions run the mutant, as on an edited file

    _run_item(session, reload_in_the_test)
    assert (imported(), reloaded[0]()) == (1, 1)  # reverted after the item

    def both_mutated() -> None:
        assert (imported(), reloaded[0]()) == (2, 2)

    _run_item(session, both_mutated)  # the next item of the lease edits both definitions

    spec.write_text("", encoding="utf-8")
    _run_item(session)  # the boundary into the next lease

    assert reloadable.f is imported


def test_a_reload_at_collection_is_part_of_the_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reloadable: types.ModuleType
) -> None:
    spec = tmp_path / "m.json"
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    session, log = _reload_session(spec)
    del session.stash[plugin._BASELINE]
    log.reload(reloadable)
    collected = reloadable.f

    _run_item(session)  # the first item of the worker takes the baseline
    spec.write_text(json.dumps({"request_token": "next"}), encoding="utf-8")
    _run_item(session)

    assert reloadable.f is collected


def _mutant_spec(token: str) -> str:
    return json.dumps(
        {
            "module": "bm_reload_target",
            "qualname": "f",
            "kind": "src",
            "mutated_source": "def f():\n    return 2\n",
            "request_token": token,
        }
    )


def _rewrite(module: types.ModuleType, source: str) -> None:
    """Change the module's file, so the next reload executes ``source``."""
    assert module.__file__ is not None
    Path(module.__file__).write_text(source, encoding="utf-8")
    importlib.invalidate_caches()


def test_a_reload_after_the_item_runs_the_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reloadable: types.ModuleType
) -> None:
    # Between the items, and in the original lease of a paired kill, nothing must put the mutant back.
    spec = tmp_path / "m.json"
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    session, log = _reload_session(spec)
    spec.write_text(_mutant_spec("lease-a"), encoding="utf-8")
    _run_item(session)

    log.reload(reloadable)

    assert reloadable.f() == 1


def test_a_reload_that_drops_the_target_leaves_the_imported_definition_mutated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reloadable: types.ModuleType
) -> None:
    spec = tmp_path / "m.json"
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    session, log = _reload_session(spec)
    imported = reloadable.f
    spec.write_text(_mutant_spec("lease-a"), encoding="utf-8")

    def reload_without_f() -> None:
        _rewrite(reloadable, "f = None\n")
        log.reload(reloadable)
        assert imported() == 2

    _run_item(session, reload_without_f)
    assert imported() == 1


def test_a_reload_that_binds_another_function_leaves_it_unmutated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reloadable: types.ModuleType
) -> None:
    # The name now holds a definition compiled under another qualname: the edit does not fit it.
    spec = tmp_path / "m.json"
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    session, log = _reload_session(spec)
    imported = reloadable.f
    spec.write_text(_mutant_spec("lease-a"), encoding="utf-8")

    def reload_with_an_alias() -> None:
        _rewrite(reloadable, "def g():\n    return 0\n\n\nf = g\n")
        log.reload(reloadable)
        assert (imported(), reloadable.f()) == (2, 0)

    _run_item(session, reload_with_an_alias)
    assert (imported(), reloadable.f()) == (1, 0)


def test_a_reload_of_another_module_leaves_the_target_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reloadable: types.ModuleType
) -> None:
    _rewrite(reloadable, "from functools import lru_cache\n\n\n@lru_cache\ndef f():\n    return 1\n")
    target = importlib.reload(reloadable)
    (tmp_path / "bm_reload_other.py").write_text("X = 1\n", encoding="utf-8")
    other = importlib.import_module("bm_reload_other")
    spec = tmp_path / "m.json"
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))
    try:
        session, log = _reload_session(spec)
        spec.write_text(_mutant_spec("lease-a"), encoding="utf-8")

        def reload_the_other_module() -> None:
            assert target.f() == 2
            log.reload(other)
            assert target.f.cache_info().currsize == 1

        _run_item(session, reload_the_other_module)
    finally:
        sys.modules.pop("bm_reload_other", None)
