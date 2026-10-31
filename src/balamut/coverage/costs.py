"""The JSON contract of per-test durations measured by a coverage run (the cost sidecar)."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

type CostMap = dict[str, float]
type _JsonValue = bool | int | float | str | list[_JsonValue] | dict[str, _JsonValue] | None

_COST_SCHEMA_VERSION = 1


def _load_costs(path: Path) -> CostMap | None:
    try:
        payload: _JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "costs"}:
        return None
    if payload.get("schema_version") != _COST_SCHEMA_VERSION:
        return None
    raw_costs = payload.get("costs")
    if not isinstance(raw_costs, dict):
        return None
    loaded: CostMap = {}
    for nodeid, seconds in raw_costs.items():
        if isinstance(seconds, bool) or not isinstance(seconds, int | float):
            return None
        value = float(seconds)
        if not math.isfinite(value) or value < 0:
            return None
        loaded[nodeid] = value
    return loaded


def read_costs(path: Path) -> CostMap:
    """A complete sidecar with finite non-negative values; anything else reads as empty."""
    return _load_costs(path) or {}


def costs_valid(path: Path) -> bool:
    """Tell a valid empty sidecar apart from a missing or damaged one."""
    return _load_costs(path) is not None


def write_costs(path: Path, costs: CostMap) -> None:
    """Publish a sorted cost sidecar atomically."""
    write_json_atomic(path, {"schema_version": _COST_SCHEMA_VERSION, "costs": dict(sorted(costs.items()))})


def write_json_atomic(path: Path, payload: _JsonValue) -> None:
    """Write JSON through a temporary file in the same directory, fsync it and replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
