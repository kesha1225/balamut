"""An exact-fingerprint cache of confirmed baseline failures of the diff gate.

A full substrate build that found failing tests records them under a fingerprint of everything that build depended
on. The next run with the same fingerprint confirms the failure with a focused run of those test files instead of
paying for another full build; any other fingerprint ignores the record.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from balamut.coverage import failures, substrate

type FingerprintEnvironment = Mapping[str, str]
type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

_BASELINE_FAILURE_SCHEMA_VERSION = 2
_SUFFIX = ".baseline-failure.json"
_RECORD_KEYS = frozenset({"schema_version", "fingerprint", "failed_nodeids", "generation"})
_PRODUCED_FIELDS = ("complete", "generation", "coverage_sha256", "costs_sha256")


@dataclass(frozen=True, slots=True)
class BaselineFailureRecord:
    """The fingerprint of the inputs and the failed node ids of the last failed full build.

    ``generation`` is the substrate that build published with the files of the failed tests unmeasured; empty when
    the build published nothing, so a fixed baseline builds the substrate again."""

    fingerprint: str
    failed_nodeids: tuple[str, ...]
    generation: str = ""


def failure_path(coverage_path: Path) -> Path:
    """The fail-closed sidecar next to the coverage substrate."""
    return coverage_path.with_name(coverage_path.name + _SUFFIX)


def failure_fingerprint(
    metadata: substrate.SubstrateMetadata,
    *,
    base_sha: str,
    diff_base_sha: str,
    head_sha: str,
    environment: FingerprintEnvironment,
) -> str:
    """Hash every input of a confirmation, ``environment`` secrets included, without storing them.

    Fields a build produces (its generation, completeness and output digests) are not inputs and are left out."""
    metadata_payload = asdict(metadata)
    for produced in _PRODUCED_FIELDS:
        metadata_payload.pop(produced)
    payload = {
        "schema_version": _BASELINE_FAILURE_SCHEMA_VERSION,
        "metadata": metadata_payload,
        "scope": {"base_sha": base_sha, "diff_base_sha": diff_base_sha, "head_sha": head_sha},
        "environment": dict(sorted(environment.items())),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def failure_read(coverage_path: Path, *, expected_fingerprint: str) -> BaselineFailureRecord | None:
    """The record of exactly ``expected_fingerprint``; None for a missing, malformed or foreign one."""
    try:
        payload: JsonValue = json.loads(failure_path(coverage_path).read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or frozenset(payload) != _RECORD_KEYS:
        return None
    fingerprint = payload.get("fingerprint")
    raw_nodeids = payload.get("failed_nodeids")
    generation = payload.get("generation")
    if payload.get("schema_version") != _BASELINE_FAILURE_SCHEMA_VERSION or fingerprint != expected_fingerprint:
        return None
    if not isinstance(fingerprint, str) or not isinstance(raw_nodeids, list) or not isinstance(generation, str):
        return None
    nodeids: list[str] = []
    for nodeid in raw_nodeids:
        if not isinstance(nodeid, str) or not failures.failure_nodeid_valid(nodeid):
            return None
        nodeids.append(nodeid)
    if not nodeids:
        return None
    return BaselineFailureRecord(
        fingerprint=fingerprint, failed_nodeids=tuple(sorted(set(nodeids))), generation=generation
    )


def failure_write(coverage_path: Path, record: BaselineFailureRecord) -> None:
    """Atomically publish a non-empty failure record."""
    if not record.failed_nodeids:
        raise ValueError("a baseline failure record needs failed node ids")
    path = failure_path(coverage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_obj:
            json.dump(
                {
                    "schema_version": _BASELINE_FAILURE_SCHEMA_VERSION,
                    "fingerprint": record.fingerprint,
                    "failed_nodeids": sorted(set(record.failed_nodeids)),
                    "generation": record.generation,
                },
                file_obj,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def failure_remove(coverage_path: Path) -> None:
    """Drop a stale record after a passing confirmation or a green build."""
    failure_path(coverage_path).unlink(missing_ok=True)
