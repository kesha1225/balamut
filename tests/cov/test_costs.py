"""The cost sidecar JSON contract (balamut.coverage.costs)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import cast, get_args

import pytest

from balamut.coverage import costs
from balamut.coverage.costs import costs_valid, read_costs, write_costs, write_json_atomic


def _write_sidecar(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fresh_module_probe(name: str) -> ModuleType:
    """Re-execute a module's import-time lines under a probe name, without touching the live module.

    The probe is registered in ``sys.modules`` under its own name: ``@dataclass`` decorators look up
    ``sys.modules.get(cls.__module__)`` during class creation, which would fail otherwise."""
    live = sys.modules[name]
    spec = importlib.util.spec_from_file_location(f"_probe_{name}", live.__file__)
    assert spec is not None
    assert spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    sys.modules[probe.__name__] = probe
    try:
        spec.loader.exec_module(probe)
    finally:
        del sys.modules[probe.__name__]
    return probe


# --- the schema version constant and the JsonValue type alias ---


def test_costs_import_time_constants_and_type_alias_evaluate() -> None:
    probe = _fresh_module_probe("balamut.coverage.costs")
    assert probe._COST_SCHEMA_VERSION == 1
    members = get_args(probe._JsonValue.__value__)
    assert bool in members
    assert int in members
    assert str in members
    assert type(None) in members


def test_write_costs_publishes_schema_version_one(tmp_path: Path) -> None:
    path = tmp_path / "costs.json"
    write_costs(path, {"t.py::a": 0.5})
    assert read_costs(path) == {"t.py::a": 0.5}
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


# --- _load_costs validation ---


def test_extra_keys_invalidate_the_sidecar(tmp_path: Path) -> None:
    path = _write_sidecar(tmp_path / "costs.json", {"schema_version": 1, "costs": {}, "extra": 1})
    assert costs_valid(path) is False


def test_a_bool_cost_value_is_rejected(tmp_path: Path) -> None:
    path = _write_sidecar(tmp_path / "costs.json", {"schema_version": 1, "costs": {"t.py::a": True}})
    assert costs_valid(path) is False


def test_a_negative_cost_value_is_rejected(tmp_path: Path) -> None:
    path = _write_sidecar(tmp_path / "costs.json", {"schema_version": 1, "costs": {"t.py::a": -1}})
    assert costs_valid(path) is False


def test_a_zero_cost_value_is_valid(tmp_path: Path) -> None:
    path = _write_sidecar(tmp_path / "costs.json", {"schema_version": 1, "costs": {"t.py::a": 0}})
    assert costs_valid(path) is True
    assert read_costs(path) == {"t.py::a": 0.0}


# --- write_json_atomic ---


def test_write_json_atomic_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "out.json"
    write_json_atomic(target, {"a": 1})
    assert target.read_text(encoding="utf-8") == '{"a":1}\n'


def test_write_json_atomic_accepts_an_existing_parent(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    write_json_atomic(target, {"a": 1})
    assert target.read_text(encoding="utf-8") == '{"a":1}\n'


def test_write_json_atomic_keeps_unicode_literal(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    write_json_atomic(target, {"name": "café"})
    assert "café" in target.read_text(encoding="utf-8")


def test_write_json_atomic_ends_with_a_newline(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    write_json_atomic(target, {"a": 1})
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_write_json_atomic_flushes_before_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        sizes.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr(costs.os, "fsync", recording_fsync)
    write_json_atomic(tmp_path / "out.json", {"a": 1})
    assert sizes == [len('{"a":1}\n')]


def test_write_json_atomic_cleans_up_its_temporary_on_failure(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        write_json_atomic(tmp_path / "out.json", cast("costs._JsonValue", {"a": object()}))
    assert list(tmp_path.glob(".out.json.*.tmp")) == []


def test_write_json_atomic_stages_the_temp_next_to_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "nested" / "out.json"
    seen: dict[str, Path] = {}
    real_replace = Path.replace

    def fake_replace(self: Path, target_path: Path) -> None:
        seen["source"] = self
        real_replace(self, target_path)

    monkeypatch.setattr(Path, "replace", fake_replace)
    write_json_atomic(target, {"a": 1})
    assert seen["source"].parent == target.parent
    assert seen["source"].name.startswith(".out.json.")
    assert seen["source"].name.endswith(".tmp")


def test_write_json_atomic_opens_the_temp_in_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "out.json"
    seen: dict[str, str | None] = {}
    real = os.fdopen

    def spy(fd: int, mode: str, encoding: str | None = None) -> object:
        seen["encoding"] = encoding
        return real(fd, mode, encoding=encoding)

    monkeypatch.setattr(os, "fdopen", spy)
    write_json_atomic(target, {"a": 1})
    assert seen["encoding"] == "utf-8"
