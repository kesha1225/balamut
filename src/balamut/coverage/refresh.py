"""Incremental coverage refresh: re-measure only the tests an edit can affect and replace their coverage.

Source tier: an edited source file re-measures the tests that covered it in any version this generation saw.
Test tier: an edited test file is re-measured whole (new, changed and removed tests). A refresh measures exactly
like the substrate build (``measure.measure_coverage``): forkserver workers, isolated slots, the C tracer and
per-test contexts, so the store never mixes coverage of two environments. The store is changed by the caller in
one transaction after the whole measurement succeeded.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from pytest_fast import resolve_workers

from balamut.config import Profile
from balamut.coverage.costs import CostMap, read_costs
from balamut.coverage.coverage_map import load_coverage_map
from balamut.coverage.measure import (
    EXIT_OK,
    EXIT_TESTS_FAILED,
    Launcher,
    MeasureRequest,
    default_launcher,
    measure_coverage,
)
from balamut.coverage.store import Store
from balamut.engine.planner import resolve_refresh_workers
from balamut.isolation import Isolation

type PerTestLines = dict[str, list[tuple[str, int]]]
# source file -> its (node id, line) coverage taken from the store before a refresh (for the restoration step)
type SrcCoverageSnapshot = dict[str, list[tuple[str, int]]]

_LOG_TAIL_LINES = 40


@dataclass(frozen=True, slots=True)
class MeasureContext:
    """What a refresh needs to measure like the substrate build."""

    profile: Profile
    isolation: Isolation
    launcher: Launcher = default_launcher

    @property
    def scratch(self) -> Path:
        return self.profile.state_dir / "tmp"


class RefreshBaselineError(RuntimeError):
    """A refresh found failing tests: the same baseline failure as a substrate build, with the tests named.

    It is a RuntimeError, so callers treating any refresh failure as fatal keep working, while the diff gate tells
    it apart from an infrastructure failure and reports a baseline failure with node ids."""

    def __init__(self, nodeids: tuple[str, ...], *, detail: str) -> None:
        named = ", ".join(nodeids[:10]) + (f" (+{len(nodeids) - 10} more)" if len(nodeids) > 10 else "")
        super().__init__(f"coverage refresh: failing tests {named}; {detail}")
        self.nodeids = nodeids


@dataclass(frozen=True, slots=True)
class RefreshMeasurement:
    """A fully measured diff refresh, before anything is published to the store."""

    src_nodeids: tuple[str, ...]
    replaced_nodeids: tuple[str, ...]
    replace_test_files: tuple[str, ...]
    measured: PerTestLines
    costs: CostMap = field(default_factory=dict[str, float])  # durations measured by this very run


def ordered_targets(targets: Collection[str]) -> list[str]:
    """Targets in path order, so the tests of one directory are collected together (and keep their conftest)."""
    return sorted(targets)


def _targets_summary(targets: list[str]) -> str:
    sample = targets if len(targets) <= 6 else [*targets[:3], "...", *targets[-3:]]
    count = f"{len(targets):,}".replace(",", " ")
    return f"{count} targets; sample: {json.dumps(sample, ensure_ascii=True)}"


def _targets_note(scratch: Path, stem: str, targets: list[str]) -> str:
    """Keep the full target list next to the run for diagnosis, or say why it could not be kept."""
    artifact = scratch / f"{stem}.targets.json"
    try:
        artifact.write_text(
            json.dumps({"target_count": len(targets), "targets": targets}, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return f"the full target list could not be saved: {exc}"
    return f"full target list: {artifact}"


def _log_tail(log: Path) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-_LOG_TAIL_LINES:])


def _remove_run_artifacts(data_file: Path, *, keep_logs: bool) -> None:
    for path in data_file.parent.glob(f"{data_file.name}*"):
        if keep_logs and path.name.endswith(".log"):
            continue
        path.unlink(missing_ok=True)


def measure_test_coverage(
    targets: list[str],
    *,
    context: MeasureContext,
    workers: int | None = None,
    costs: CostMap | None = None,
) -> PerTestLines:
    """Measure ``targets`` (node ids or files) like the substrate build -> ``{nodeid: [(relpath, line)]}``.

    ``costs`` receives the durations measured by this run. A file without tests yields empty coverage. Failing tests
    raise ``RefreshBaselineError``; any other failure raises RuntimeError: returning nothing silently would let the
    caller drop the old coverage and cache false no-coverage or survivor verdicts."""
    if not targets:
        return {}
    ordered = ordered_targets(targets)
    effective_workers = resolve_refresh_workers(explicit=workers, targets=len(ordered), perf_cores=resolve_workers())
    scratch = context.scratch
    scratch.mkdir(parents=True, exist_ok=True)
    stem = f"refresh-{os.getpid()}-{time.time_ns()}"
    data_file = scratch / f"{stem}.coverage"
    infrastructure_failure = False
    try:
        result = measure_coverage(
            MeasureRequest(
                profile=context.profile, coverage_file=data_file, targets=tuple(ordered), workers=effective_workers
            ),
            isolation=context.isolation,
            launcher=context.launcher,
        )
        if result.returncode == EXIT_TESTS_FAILED and result.failed_nodeids:
            raise RefreshBaselineError(result.failed_nodeids, detail=f"for {_targets_summary(ordered)}")
        if result.returncode != EXIT_OK:
            infrastructure_failure = True
            log = data_file.with_name(f"{data_file.name}.slot0.log")
            raise RuntimeError(
                f"coverage refresh failed (exit {result.returncode}) for {_targets_summary(ordered)}; "
                f"{_targets_note(scratch, stem, ordered)}; {result.detail}; the coverage is not valid, refresh "
                f"aborted.\n{_log_tail(log)}"
            )
        coverage = load_coverage_map(data_file, root=context.profile.root)
        per_test: PerTestLines = {}
        for relpath in coverage.files():
            for line in sorted(coverage.covered_lines(relpath)):
                for nodeid in coverage.tests_covering(relpath, line):
                    per_test.setdefault(nodeid, []).append((relpath, line))
        if costs is not None:
            costs.update(read_costs(result.cost_file))
        return per_test
    finally:
        _remove_run_artifacts(data_file, keep_logs=infrastructure_failure)


def refresh_tests(changed_tests: list[str], *, store: Store, context: MeasureContext) -> int:
    """The test tier on its own (the confirm loop): re-measure whole test files and replace their tests.

    The measurement finishes before the store changes, so a failing test or a broken run leaves the stored coverage
    consistent. Node ids are unique per file, so one run measures every file. Returns the measured test count."""
    measured = measure_test_coverage(changed_tests, context=context)
    for test_file in changed_tests:
        store.delete_tests_in_file(test_file)
    for nodeid, lines in measured.items():
        store.replace_test_coverage(nodeid, lines)
    return len(measured)


def measure_diff_refresh(
    changed_src: list[str],
    changed_tests: list[str],
    *,
    store: Store,
    context: MeasureContext,
    deleted_tests: Collection[str] = (),
) -> RefreshMeasurement:
    """Measure the source tier and the test tier without changing the store.

    Source tier: tests that covered an edited source file in any version this generation saw. Test tier: edited test
    files whole. Node ids of an edited test file are measured through the file, and those of a deleted file do not
    exist (the store drops their pairs in the same publication): neither is a target of its own, since a node id
    that no longer exists would fail the run."""
    not_by_nodeid = {*changed_tests, *deleted_tests}
    src_nodeids = sorted({nodeid for file in changed_src for nodeid in store.tests_seen_covering_file(file)})
    src_only = [nodeid for nodeid in src_nodeids if nodeid.partition("::")[0] not in not_by_nodeid]
    targets = [*changed_tests, *src_only]
    costs: CostMap = {}
    measured = measure_test_coverage(targets, context=context, costs=costs) if targets else {}
    return RefreshMeasurement(
        src_nodeids=tuple(src_nodeids),
        replaced_nodeids=tuple(src_only),
        replace_test_files=tuple(changed_tests),
        measured=measured,
        costs=costs,
    )


def restoration_rows(
    snapshot: SrcCoverageSnapshot,
    measurement: RefreshMeasurement,
    *,
    excluded_test_files: set[str],
) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Rows restoring a source file a refresh would leave with no coverage at all, computed before publication.

    A module imported before the tracer started (through conftest and the application bootstrap) is under-measured
    by an isolated re-run. A file covered in the baseline but with no coverage left, neither retained nor measured,
    gets its baseline rows back, except those of test files the refresh re-measured or deleted: those were judged
    again authoritatively, and a test that legitimately stopped covering the file must not be resurrected."""
    replaced_nodeids = set(measurement.replaced_nodeids)
    rows: list[tuple[str, str, int]] = []
    restored: list[str] = []
    for file, baseline_rows in snapshot.items():
        if not baseline_rows:
            continue
        retained = any(
            nodeid not in replaced_nodeids and nodeid.partition("::")[0] not in excluded_test_files
            for nodeid, _line in baseline_rows
        )
        measured = any(
            measured_file == file
            for measured_lines in measurement.measured.values()
            for measured_file, _line in measured_lines
        )
        if retained or measured:
            continue
        keep = [
            (nodeid, file, line)
            for nodeid, line in baseline_rows
            if nodeid.partition("::")[0] not in excluded_test_files
        ]
        if keep:
            rows.extend(keep)
            restored.append(file)
    return rows, restored


def snapshot_src_coverage(changed_src: list[str], *, store: Store) -> SrcCoverageSnapshot:
    """The baseline coverage of each source file from the store, taken before a refresh overwrites it."""
    return {file: store.coverage_rows_for_file(file) for file in changed_src}


def restore_wiped_src(snapshot: SrcCoverageSnapshot, excluded_tests: set[str], *, store: Store) -> list[str]:
    """A refresh may update a file's coverage but must not take it from covered to uncovered.

    For each file covered in the baseline but uncovered after the refresh, the baseline rows come back (an additive
    insert: updated coverage of the same tests elsewhere stays), except rows of re-measured or deleted test files.
    A file without baseline coverage (new code without a test) stays uncovered. Returns the restored files."""
    restored: list[str] = []
    for file, rows in snapshot.items():
        if not rows or store.tests_covering_file(file):
            continue
        keep = [(nodeid, line) for nodeid, line in rows if nodeid.partition("::")[0] not in excluded_tests]
        if not keep:
            continue
        store.ingest_coverage([(nodeid, file, line) for nodeid, line in keep])
        restored.append(file)
    return restored
