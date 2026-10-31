"""pytest plugin collecting the node ids of failed baseline tests, written atomically per worker.

The collector is registered when the forkserver configures pytest; every forked worker inherits it (including
collection failures recorded before the fork) and publishes its own shard on shutdown, before ``os._exit``. The
driver merges the shards fail-closed.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from _pytest.config import Config
from _pytest.reports import CollectReport, TestReport

from balamut.coverage.costs import write_json_atomic

FAILURE_FILE_ENV = "BALAMUT_BASELINE_FAILURE_FILE"
SLOT_ENV = "BALAMUT_COVERAGE_SLOT"
_SCHEMA_VERSION = 1
_COLLECTOR_NAME = "balamut-baseline-failures"
WORKER_KEY = re.compile(r"s[0-9]+gw[0-9]+")
_SHARD = re.compile(r"^(?P<base>.+)\.(?P<worker>s[0-9]+gw[0-9]+)\.(?P<pid>[0-9]+)\.json$")

type WorkerArtifactMap = dict[str, Path]
type _JsonValue = bool | int | float | str | list[_JsonValue] | dict[str, _JsonValue] | None


class FailureArtifactError(RuntimeError):
    """The set of failure shards is incomplete or damaged."""


def worker_key(slot: str | int, workerid: str) -> str:
    """The shard identity of one worker: its isolation slot plus its pytest-fast worker id."""
    return f"s{slot}{workerid}"


def expected_worker_keys(workers_per_slot: Iterable[int]) -> frozenset[str]:
    return frozenset(
        worker_key(slot, f"gw{index}") for slot, workers in enumerate(workers_per_slot) for index in range(workers)
    )


def failure_nodeid_valid(nodeid: str) -> bool:
    """A test or collection node id that is safe to pass to a focused pytest call."""
    return bool(nodeid) and "\n" not in nodeid and "\r" not in nodeid


def _failed_nodeids_load(path: Path) -> tuple[str, ...] | None:
    try:
        payload: _JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "failed_nodeids"}:
        return None
    if payload.get("schema_version") != _SCHEMA_VERSION:
        return None
    raw = payload.get("failed_nodeids")
    if not isinstance(raw, list):
        return None
    nodeids: list[str] = []
    for nodeid in raw:
        if not isinstance(nodeid, str) or not failure_nodeid_valid(nodeid):
            return None
        nodeids.append(nodeid)
    return tuple(sorted(set(nodeids)))


def failed_nodeids_read(path: Path) -> tuple[str, ...]:
    """A complete sidecar of non-empty node ids; anything else reads as empty."""
    return _failed_nodeids_load(path) or ()


def failed_nodeids_write(path: Path, nodeids: Collection[str]) -> None:
    payload: _JsonValue = {"schema_version": _SCHEMA_VERSION, "failed_nodeids": [*sorted(set(nodeids))]}
    write_json_atomic(path, payload)


def worker_artifact_write(path: Path, *, worker_key: str, process_id: int, failed_nodeids: set[str]) -> None:
    """Publish the shard of one worker."""
    if not WORKER_KEY.fullmatch(worker_key) or process_id <= 0:
        raise ValueError(f"invalid pytest-fast worker identity: {worker_key!r}")
    failed_nodeids_write(path.with_name(f"{path.name}.{worker_key}.{process_id}.json"), failed_nodeids)


def _worker_artifact_paths(path: Path) -> tuple[Path, ...]:
    shards: list[Path] = []
    for candidate in sorted(path.parent.glob(f"{path.name}.s*.json")):
        match = _SHARD.fullmatch(candidate.name)
        if match is not None and match.group("base") == path.name:
            shards.append(candidate)
    return tuple(shards)


def _worker_artifacts(path: Path) -> WorkerArtifactMap:
    shards: WorkerArtifactMap = {}
    for candidate in _worker_artifact_paths(path):
        match = _SHARD.fullmatch(candidate.name)
        if match is None:
            continue
        key = match.group("worker")
        if key in shards:
            raise FailureArtifactError(f"duplicate failure shard for {key}")
        shards[key] = candidate
    return shards


def remove_worker_artifacts(path: Path) -> None:
    """Remove the recognized shards of a previous run."""
    for shard in _worker_artifact_paths(path):
        shard.unlink()


def combine_worker_artifacts(path: Path, *, expected: frozenset[str]) -> None:
    """Merge exactly one failure shard per expected worker, fail-closed."""
    if not expected:
        raise ValueError("at least one worker is expected")
    shards = _worker_artifacts(path)
    actual = frozenset(shards)
    if actual != expected:
        missing = ",".join(sorted(expected - actual)) or "-"
        extra = ",".join(sorted(actual - expected)) or "-"
        raise FailureArtifactError(f"failure shards mismatch: missing={missing}; extra={extra}")
    failed: set[str] = set()
    for key in sorted(shards):
        nodeids = _failed_nodeids_load(shards[key])
        if nodeids is None:
            raise FailureArtifactError(f"invalid failure shard for {key}")
        failed.update(nodeids)
    if failed:
        failed_nodeids_write(path, failed)
    else:
        path.unlink(missing_ok=True)
    for shard in shards.values():
        shard.unlink()


@dataclass(eq=False, slots=True)
class FailureCollector:
    """Collects failed setup/call/teardown reports and failed collection reports."""

    path: Path
    slot: str
    failed_nodeids: set[str] = field(default_factory=set[str])

    def pytest_runtest_logreport(self, report: TestReport) -> None:
        if report.when in ("setup", "call", "teardown") and report.failed:
            self.failed_nodeids.add(report.nodeid)

    def pytest_collectreport(self, report: CollectReport) -> None:
        if report.failed and failure_nodeid_valid(report.nodeid):
            self.failed_nodeids.add(report.nodeid)

    def pytest_sessionfinish(self) -> None:
        """A plain (non-forkserver) session publishes the sidecar only when something failed."""
        if not self.failed_nodeids:
            self.path.unlink(missing_ok=True)
            return
        failed_nodeids_write(self.path, self.failed_nodeids)


def pytest_fast_worker_shutdown(config: Config, workerid: str) -> None:
    """Save this worker's failure shard before pytest-fast exits the process."""
    collector = config.pluginmanager.get_plugin(_COLLECTOR_NAME)
    if not isinstance(collector, FailureCollector):
        raise RuntimeError("the pytest-fast baseline failure collector is missing")
    worker_artifact_write(
        collector.path,
        worker_key=worker_key(collector.slot, workerid),
        process_id=os.getpid(),
        failed_nodeids=collector.failed_nodeids,
    )


def pytest_fast_configure_worker(config: Config) -> None:
    """Every pytest-fast worker records its failures, forked or fresh.

    The forkserver workers inherit the collector registered at ``pytest_configure``; a fresh
    (spawned) worker builds its own config, and ``pytest_configure`` skips registration there
    because pytest-fast's identity marker ``PYTEST_XDIST_WORKER`` is indistinguishable from a
    real xdist worker. This hook fires only in pytest-fast workers, so it is safe to register
    unconditionally here."""
    raw_path = os.environ.get(FAILURE_FILE_ENV)
    if not raw_path:
        return
    if isinstance(config.pluginmanager.get_plugin(_COLLECTOR_NAME), FailureCollector):
        return
    config.pluginmanager.register(FailureCollector(Path(raw_path), slot=os.environ.get(SLOT_ENV, "0")), _COLLECTOR_NAME)


def pytest_configure(config: Config) -> None:
    raw_path = os.environ.get(FAILURE_FILE_ENV)
    if not raw_path or os.environ.get("PYTEST_XDIST_WORKER"):
        return
    path = Path(raw_path)
    path.unlink(missing_ok=True)
    config.pluginmanager.register(FailureCollector(path, slot=os.environ.get(SLOT_ENV, "0")), _COLLECTOR_NAME)
