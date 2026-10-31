"""The daemon plugin records the functions a mutant lease entered: the kill dependencies (see kill_deps)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from balamut.runtime import kill_deps, plugin
from bm_support.plugin_fakes import FakeItem, FakeSession

_SPEC = {"module": "bm_absent_mod_zzz", "qualname": "f", "kind": "src", "mutated_source": "def f():\n    return 1\n"}


def _nothing() -> None:
    return None


def _armed(monkeypatch: pytest.MonkeyPatch) -> tuple[list[kill_deps.DepsRecorder], list[bool]]:
    """Monitoring is replaced: test_kill_deps covers the recorder, this file only its wiring to leases."""
    armed: list[kill_deps.DepsRecorder] = []
    restarts: list[bool] = []

    def arm(recorder: kill_deps.DepsRecorder, **_kwargs: tuple[int, ...]) -> Callable[[], None] | None:
        armed.append(recorder)
        return _nothing

    def restart() -> None:
        restarts.append(True)

    monkeypatch.setattr(kill_deps, "monitoring_arm", arm)
    monkeypatch.setattr(kill_deps, "monitoring_restart", restart)
    return armed, restarts


def _lease(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str, deps_path: Path | None) -> None:
    spec = tmp_path / "mutant.json"
    fields = {**_SPEC, "request_token": token, **({"deps_path": str(deps_path)} if deps_path else {})}
    spec.write_text(json.dumps(fields), encoding="utf-8")
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec))


def _protocol(item: pytest.Item, phases: Callable[[], None] = _nothing) -> None:
    """An item through the plugin's wrapper: ``phases`` stands for setup/call/teardown between its entry and exit."""
    wrapper = plugin.pytest_runtest_protocol(item)
    next(wrapper)
    phases()
    with pytest.raises(StopIteration):
        wrapper.send(True)


def _daemon_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSession:
    """The daemon arms the recorder before the root conftest; workers inherit it through the fork."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(plugin.KILL_DEPS_ENV, "1")
    session = FakeSession()
    plugin.pytest_load_initial_conftests(cast("pytest.Config", session.config))
    return session


def test_recorder_is_armed_only_in_daemon_workers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    armed, _restarts = _armed(monkeypatch)
    monkeypatch.delenv(plugin.KILL_DEPS_ENV, raising=False)
    session = FakeSession()
    plugin.pytest_load_initial_conftests(cast("pytest.Config", session.config))
    _lease(monkeypatch, tmp_path, "lease-a", tmp_path / "mutant.json.deps")
    plugin._apply_or_switch(cast("pytest.Item", FakeItem(session)))
    assert armed == []
    assert plugin._deps_recorder(cast("pytest.Session", session)) is None


def test_recorder_is_armed_before_the_conftests_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The root conftest's application import builds state every lease reads: a recorder armed later would miss it."""
    armed, _restarts = _armed(monkeypatch)
    session = _daemon_worker(monkeypatch, tmp_path)
    plugin.pytest_load_initial_conftests(cast("pytest.Config", session.config))
    assert len(armed) == 1
    assert plugin._deps_recorder(cast("pytest.Session", session)) is armed[0]


def test_recorder_uses_the_configured_test_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    armed, _restarts = _armed(monkeypatch)
    monkeypatch.setenv(plugin.TEST_ROOTS_ENV, "checks")
    session = _daemon_worker(monkeypatch, tmp_path)
    [recorder] = armed
    # Called during collection, before any lease: a test-module helper is held by the import closure, not shared,
    # while a source helper builds state every lease reads.
    recorder.record(str(tmp_path / "checks/helpers.py"), "build")
    recorder.record(str(tmp_path / "app/settings.py"), "load")
    deps_path = tmp_path / "mutant.json.deps"
    _lease(monkeypatch, tmp_path, "lease-a", deps_path)
    item = cast("pytest.Item", FakeItem(session))
    plugin._apply_or_switch(item)
    _protocol(item)
    assert kill_deps.kill_deps_read(deps_path) == (("app/settings.py", "load"),)


def test_recorder_defaults_to_tests_root_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `os.environ.get(TEST_ROOTS_ENV, "tests")` must fall back to the default root when the env var is absent; a
    # dropped default would call `.split(",")` on None and crash the worker during collection.
    armed, _restarts = _armed(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(plugin.KILL_DEPS_ENV, "1")
    monkeypatch.delenv(plugin.TEST_ROOTS_ENV, raising=False)
    session = FakeSession()
    plugin.pytest_load_initial_conftests(cast("pytest.Config", session.config))
    [recorder] = armed
    assert recorder._test_prefixes == ("tests/",)


def test_lease_without_an_early_recorder_publishes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A recorder not armed before collection never saw the import state: such a kill keeps the file rule."""
    armed, _restarts = _armed(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(plugin.KILL_DEPS_ENV, "1")
    deps_path = tmp_path / "mutant.json.deps"
    _lease(monkeypatch, tmp_path, "lease-a", deps_path)
    item = cast("pytest.Item", FakeItem(FakeSession()))
    plugin._apply_or_switch(item)
    _protocol(item)
    assert armed == []
    assert not deps_path.exists()


def test_mutant_lease_publishes_what_it_entered_after_each_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    armed, restarts = _armed(monkeypatch)
    session = _daemon_worker(monkeypatch, tmp_path)
    deps_path = tmp_path / "mutant.json.deps"
    _lease(monkeypatch, tmp_path, "lease-a", deps_path)
    item = cast("pytest.Item", FakeItem(session))
    plugin._apply_or_switch(item)
    [recorder] = armed
    _protocol(item, lambda: recorder.record(str(tmp_path / "src/x.py"), "f"))
    assert restarts == [True]
    assert kill_deps.kill_deps_read(deps_path) == (("src/x.py", "f"),)


def test_lease_is_published_when_the_test_protocol_is_interrupted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pytest reports phase failures itself, but Exit/KeyboardInterrupt and a failing hook pass through the protocol:
    a killer interrupted that way must still leave its dependencies."""
    armed, _restarts = _armed(monkeypatch)
    session = _daemon_worker(monkeypatch, tmp_path)
    deps_path = tmp_path / "mutant.json.deps"
    _lease(monkeypatch, tmp_path, "lease-a", deps_path)
    item = cast("pytest.Item", FakeItem(session))
    plugin._apply_or_switch(item)
    wrapper = plugin.pytest_runtest_protocol(item)
    next(wrapper)
    armed[0].record(str(tmp_path / "src/x.py"), "f")
    with pytest.raises(RuntimeError):
        wrapper.throw(RuntimeError("teardown hook failed"))
    assert kill_deps.kill_deps_read(deps_path) == (("src/x.py", "f"),)


def test_original_lease_publishes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A plain original run (calibration) follows the mutant in the same worker: what it enters is not a kill
    dependency."""
    armed, _restarts = _armed(monkeypatch)
    session = _daemon_worker(monkeypatch, tmp_path)
    deps_path = tmp_path / "mutant.json.deps"
    _lease(monkeypatch, tmp_path, "lease-a", deps_path)
    item = cast("pytest.Item", FakeItem(session))
    plugin._apply_or_switch(item)
    _protocol(item)
    (tmp_path / "mutant.json").write_text("", encoding="utf-8")
    plugin._apply_or_switch(item)
    _protocol(item, lambda: armed[0].record(str(tmp_path / "src/x.py"), "f"))
    assert not deps_path.exists()


def test_paired_original_lease_adds_its_functions_to_the_kill_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A kill is proven by the green original as well: the functions its run went through are kill dependencies
    too. No mutation is applied in that lease and it is not counted as unsupported."""
    armed, _restarts = _armed(monkeypatch)
    session = _daemon_worker(monkeypatch, tmp_path)
    deps_path = tmp_path / "mutant.json.deps"
    item = cast("pytest.Item", FakeItem(session))
    _lease(monkeypatch, tmp_path, "lease-a", deps_path)
    plugin._apply_or_switch(item)
    _protocol(item, lambda: armed[0].record(str(tmp_path / "src/x.py"), "f"))
    unsupported = tmp_path / f"mutant.json{plugin.UNSUPPORTED_MARKER_SUFFIX}"
    unsupported.unlink(missing_ok=True)
    original = {"original_deps_path": str(deps_path), "request_token": "original-a"}
    (tmp_path / "mutant.json").write_text(json.dumps(original), encoding="utf-8")
    plugin._apply_or_switch(item)
    _protocol(item, lambda: armed[0].record(str(tmp_path / "src/helper.py"), "only_on_the_original"))
    assert kill_deps.kill_deps_read(deps_path) == (("src/helper.py", "only_on_the_original"), ("src/x.py", "f"))
    assert not unsupported.exists()


def test_session_fixture_code_joins_every_later_lease(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    armed, _restarts = _armed(monkeypatch)
    session = _daemon_worker(monkeypatch, tmp_path)
    item = cast("pytest.Item", FakeItem(session))
    _lease(monkeypatch, tmp_path, "lease-a", tmp_path / "first.deps")
    plugin._apply_or_switch(item)
    [recorder] = armed
    setup = plugin.pytest_fixture_setup(
        cast("pytest.FixtureDef[str]", SimpleNamespace(scope="session")),
        cast("pytest.FixtureRequest", SimpleNamespace(session=session)),
    )
    next(setup)
    recorder.record(str(tmp_path / "tests/conftest.py"), "app")
    with pytest.raises(StopIteration):
        setup.send("app-instance")
    second = tmp_path / "second.deps"
    _lease(monkeypatch, tmp_path, "lease-b", second)
    plugin._apply_or_switch(item)
    _protocol(item)
    assert kill_deps.kill_deps_read(second) == (("tests/conftest.py", "app"),)


def test_fixture_setup_without_a_recorder_yields_directly() -> None:
    # A worker without an armed recorder never enters shared_scope: the fixture body is not a kill dependency.
    session = FakeSession()
    setup = plugin.pytest_fixture_setup(
        cast("pytest.FixtureDef[str]", SimpleNamespace(scope="session")),
        cast("pytest.FixtureRequest", SimpleNamespace(session=session)),
    )
    next(setup)
    with pytest.raises(StopIteration) as exc:
        setup.send("fixture-value")
    assert exc.value.value == "fixture-value"


def test_original_lease_deps_rejects_an_empty_path() -> None:
    # An empty original_deps_path means "no paired original": it must not be treated as a path (a wrong `or`
    # would return the empty string and arm a lease against "").
    assert plugin.original_lease_deps('{"original_deps_path": ""}') is None
