"""The reload log of a persistent worker (balamut.runtime.reloads): what a reload bound is put back at the boundary."""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from balamut.runtime.reloads import reload_log

_SOURCE = "COUNT = 1\n\n\ndef f():\n    return 1\n"


@pytest.fixture
def module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    """A module importable from disk, so the real ``importlib.reload`` can execute it again."""
    (tmp_path / "bm_reloaded_mod.py").write_text(_SOURCE, encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    loaded = importlib.import_module("bm_reloaded_mod")
    try:
        yield loaded
    finally:
        sys.modules.pop("bm_reloaded_mod", None)


def test_undo_puts_back_the_definitions_a_reload_bound(module: types.ModuleType) -> None:
    log = reload_log(importlib.reload)
    before = module.f

    assert log.reload(module) is module
    assert module.f is not before

    log.undo()
    assert module.f is before


def test_undo_keeps_a_name_rebound_after_the_reload(module: types.ModuleType) -> None:
    # A fixture that patched the module after the reload and restored its own value at teardown owns that binding.
    log = reload_log(importlib.reload)
    log.reload(module)

    def rebound() -> int:
        return 2

    vars(module)["f"] = rebound
    log.undo()
    assert module.f is rebound


def test_undo_removes_a_name_only_the_reload_bound(module: types.ModuleType, tmp_path: Path) -> None:
    log = reload_log(importlib.reload)
    (tmp_path / "bm_reloaded_mod.py").write_text(f"{_SOURCE}\n\ndef added():\n    return 0\n", encoding="utf-8")
    importlib.invalidate_caches()

    log.reload(module)
    assert hasattr(module, "added")

    log.undo()
    assert not hasattr(module, "added")


def test_undo_reverts_reloads_newest_first(module: types.ModuleType) -> None:
    log = reload_log(importlib.reload)
    original = module.f
    log.reload(module)
    log.reload(module)

    log.undo()
    assert module.f is original


def test_undo_runs_once(module: types.ModuleType) -> None:
    log = reload_log(importlib.reload)
    log.reload(module)
    log.undo()
    after_undo = module.f

    log.undo()
    assert module.f is after_undo


def test_forget_makes_the_reloaded_state_the_reference(module: types.ModuleType) -> None:
    log = reload_log(importlib.reload)
    log.reload(module)
    reloaded = module.f

    log.forget()
    log.undo()
    assert module.f is reloaded


def test_a_reload_that_raises_is_recorded_with_what_it_bound(module: types.ModuleType, tmp_path: Path) -> None:
    log = reload_log(importlib.reload)
    original = module.f
    (tmp_path / "bm_reloaded_mod.py").write_text(f"{_SOURCE}\nraise RuntimeError('half-way')\n", encoding="utf-8")
    importlib.invalidate_caches()

    with pytest.raises(RuntimeError, match="half-way"):
        log.reload(module)
    assert module.f is not original

    log.undo()
    assert module.f is original


def test_the_follower_gets_the_reloaded_module_until_it_is_dropped(module: types.ModuleType) -> None:
    log = reload_log(importlib.reload)
    seen: list[types.ModuleType] = []
    log.follow(seen.append)

    log.reload(module)
    assert seen == [module]

    log.follow(None)
    log.reload(module)
    assert seen == [module]


def test_a_new_follower_replaces_the_previous_one(module: types.ModuleType) -> None:
    log = reload_log(importlib.reload)
    first: list[types.ModuleType] = []
    second: list[types.ModuleType] = []
    log.follow(first.append)
    log.follow(second.append)

    log.reload(module)
    assert (first, second) == ([], [module])


def test_a_non_module_reaches_the_real_reload_unchanged() -> None:
    # The real reload raises its own TypeError for it, and nothing is recorded.
    received: list[object] = []

    def reload(module: types.ModuleType) -> types.ModuleType:
        received.append(module)
        return module

    log = reload_log(reload)
    log.reload("not a module")
    log.undo()

    assert received == ["not a module"]


def test_undo_keeps_a_name_deleted_after_the_reload(module: types.ModuleType, tmp_path: Path) -> None:
    # The reload bound NONE to None; something deleted it afterwards, which undo respects like a rebinding.
    (tmp_path / "bm_reloaded_mod.py").write_text(f"{_SOURCE}\nNONE = None\n", encoding="utf-8")
    importlib.invalidate_caches()
    vars(module)["NONE"] = 1
    log = reload_log(importlib.reload)
    log.reload(module)
    assert module.NONE is None

    del vars(module)["NONE"]
    log.undo()

    assert "NONE" not in vars(module)
