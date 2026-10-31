"""Measure per-test line coverage of a project: the substrate the mutation engine selects tests from.

One pytest-fast run (``--runs 1``) per isolation slot. With a shared environment that is a single run with all
workers; with isolation every slot gets its own overlay (its own database, say) and one worker, and the test
files are sharded across slots, so the project needs no per-worker wiring of its own. Each forked worker
records exact per-test contexts with the C tracer (see ``fast_plugin``); the shards are merged fail-closed.

Exit codes: 0, every test passed; 1, some tests failed while every worker finished normally (the artifacts are
complete and the failures are listed); 2, the run itself failed and nothing was merged.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol

from pytest_fast import resolve_workers

from balamut.config import Profile
from balamut.coverage import failures, fast_plugin
from balamut.isolation import Environ, EnvMap, Isolation, IsolationError, slot_prefix

type Shards = list[list[str]]
type _TargetsByFile = dict[str, list[str]]

EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_INFRA = 2
_PLUGINS = "-p balamut.coverage.fast_plugin -p balamut.coverage.failures -p no:cacheprovider"


@dataclass(frozen=True, slots=True)
class MeasureRequest:
    profile: Profile
    coverage_file: Path
    targets: tuple[str, ...] = ()  # node ids or paths; empty measures the whole suite
    workers: int | None = None  # None: pytest-fast's inferred count, capped by coverage.workers-cap


@dataclass(frozen=True, slots=True)
class MeasureResult:
    returncode: int
    coverage_file: Path
    cost_file: Path
    failure_file: Path
    failed_nodeids: tuple[str, ...] = ()
    detail: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple[str, ...])


def cost_file_for(coverage_file: Path) -> Path:
    return coverage_file.with_name(f"{coverage_file.name}.costs.json")


def failure_file_for(coverage_file: Path) -> Path:
    return coverage_file.with_name(f"{coverage_file.name}.baseline-failures.json")


def coverage_config(profile: Profile) -> str:
    """The coverage.py configuration of a measurement.

    ``source`` must be in the file (not only on a command line) for ``relative_files`` to take effect; line
    mode only, because the engine reads line coverage and branch mode would record arcs instead."""
    sources = "".join(f"    {root}\n" for root in profile.layout.source)
    concurrency = ",".join(profile.coverage.concurrency)
    return f"[run]\nsource =\n{sources}relative_files = True\nbranch = False\nconcurrency = {concurrency}\n"


class Launched(Protocol):
    """A measurement process that was already spawned; ``wait`` returns its exit code."""

    def wait(self) -> int: ...


type Launcher = Callable[[list[str], Environ, Path, Path], Launched]  # (argv, env, cwd, log) -> running process


class _PopenHandle:
    def __init__(self, proc: subprocess.Popen[bytes], stream: BinaryIO) -> None:
        self._proc = proc
        self._stream = stream

    def wait(self) -> int:
        try:
            return self._proc.wait()
        finally:
            self._stream.close()


def default_launcher(argv: list[str], env: Environ, cwd: Path, log: Path) -> Launched:
    """Spawn the slot process and return the running handle.

    The spawn must happen in the caller's thread (the measure calls this from its main thread): macOS aborts a
    child forked from a multi-threaded process (``crashed on child side of fork pre-exec``), and the measure's
    own executor threads must never fork."""
    log.parent.mkdir(parents=True, exist_ok=True)
    stream = log.open("wb")
    proc = subprocess.Popen(argv, env=dict(env), cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
    return _PopenHandle(proc, stream)


def _test_files(profile: Profile) -> list[str]:
    layout = profile.layout
    found: set[str] = set()
    for test_root in layout.tests:
        base = layout.root / test_root
        if base.is_dir():
            found.update(
                path.relative_to(layout.root).as_posix()
                for path in base.rglob("*.py")
                if layout.is_test_file(path.name)
            )
    return sorted(found)


def _groups(profile: Profile, targets: Sequence[str]) -> list[tuple[int, list[str]]]:
    """(weight, arguments) groups that must stay in one slot: the node ids of one file, or one test file."""
    if not targets:
        return [(max(1, (profile.root / path).stat().st_size), [path]) for path in _test_files(profile)]
    by_file: _TargetsByFile = {}
    for target in targets:
        by_file.setdefault(target.partition("::")[0], []).append(target)
    return [(len(items), items) for _file, items in sorted(by_file.items())]


def _shards(groups: list[tuple[int, list[str]]], slots: int) -> Shards:
    """Greedy balance: the heaviest group goes to the lightest slot; empty slots are dropped."""
    loads = [0] * slots
    shards: Shards = [[] for _ in range(slots)]
    for weight, items in sorted(groups, key=lambda group: (-group[0], group[1])):
        lightest = loads.index(min(loads))
        loads[lightest] += weight
        shards[lightest].extend(items)
    return [shard for shard in shards if shard]


_MUTATION_PLUGIN_FLAG = "-p balamut.runtime.plugin"
_MUTATION_ENV = ("BALAMUT_MUTANT_FILE", "BALAMUT_KILL_DEPS")


def original_environment() -> EnvMap:
    """The caller's environment without anything that would measure a mutant instead of the original code."""
    env = dict(os.environ)
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID", *_MUTATION_ENV):
        env.pop(name, None)
    env["PYTEST_ADDOPTS"] = " ".join(env.get("PYTEST_ADDOPTS", "").replace(_MUTATION_PLUGIN_FLAG, "").split())
    return env


def measure_coverage(
    request: MeasureRequest, *, isolation: Isolation, launcher: Launcher = default_launcher
) -> MeasureResult:
    profile = request.profile
    coverage_file = request.coverage_file.absolute()
    cost_file = cost_file_for(coverage_file)
    failure_file = failure_file_for(coverage_file)
    coverage_file.parent.mkdir(parents=True, exist_ok=True)
    fast_plugin.remove_worker_artifacts(coverage_file, cost_file)
    failures.remove_worker_artifacts(failure_file)
    failure_file.unlink(missing_ok=True)
    config_path = coverage_file.with_name(f"{coverage_file.name}.coveragerc")
    config_path.write_text(coverage_config(profile), encoding="utf-8")

    workers = request.workers or min(resolve_workers(), profile.coverage.workers_cap)
    if isolation.isolated:
        shards = _shards(_groups(profile, request.targets), max(1, workers))
        if not shards:
            shards = [list(profile.layout.tests)]
        workers_per_slot = [1] * len(shards)
    else:
        shards = [list(request.targets) or [root for root in profile.layout.tests if (profile.root / root).exists()]]
        workers_per_slot = [max(1, workers)]

    warnings: list[str] = []
    prefix = slot_prefix()
    overlays: tuple[EnvMap, ...] = tuple({} for _ in shards)
    if isolation.isolated:
        try:
            overlays = isolation.prepare(len(shards), prefix)
        except IsolationError as exc:
            return MeasureResult(EXIT_INFRA, coverage_file, cost_file, failure_file, detail=f"isolation: {exc}")
    try:
        returncodes = _run_slots(
            profile, shards, workers_per_slot, overlays, coverage_file, cost_file, failure_file, config_path, launcher
        )
    finally:
        if isolation.isolated:
            try:
                isolation.cleanup(len(shards), prefix)
            except IsolationError as exc:
                warnings.append(f"isolation cleanup failed: {exc}")

    broken = [(slot, code) for slot, code in enumerate(returncodes) if code not in {EXIT_OK, EXIT_TESTS_FAILED}]
    if broken:
        slot, code = broken[0]
        log = _log_path(coverage_file, slot)
        return MeasureResult(
            EXIT_INFRA,
            coverage_file,
            cost_file,
            failure_file,
            detail=f"coverage slot {slot} exited with {code}; log: {log}",
            warnings=tuple(warnings),
        )
    expected = failures.expected_worker_keys(workers_per_slot)
    try:
        failures.combine_worker_artifacts(failure_file, expected=expected)
        fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=expected)
    except (failures.FailureArtifactError, fast_plugin.CoverageArtifactError, OSError) as exc:
        return MeasureResult(
            EXIT_INFRA, coverage_file, cost_file, failure_file, detail=str(exc), warnings=tuple(warnings)
        )
    failed = failures.failed_nodeids_read(failure_file)
    code = EXIT_TESTS_FAILED if failed or EXIT_TESTS_FAILED in returncodes else EXIT_OK
    return MeasureResult(code, coverage_file, cost_file, failure_file, failed, warnings=tuple(warnings))


def _log_path(coverage_file: Path, slot: int) -> Path:
    return coverage_file.with_name(f"{coverage_file.name}.slot{slot}.log")


def _run_slots(
    profile: Profile,
    shards: Shards,
    workers_per_slot: list[int],
    overlays: tuple[EnvMap, ...],
    coverage_file: Path,
    cost_file: Path,
    failure_file: Path,
    config_path: Path,
    launcher: Launcher,
) -> list[int]:
    base = original_environment()
    inherited = base.get("PYTEST_ADDOPTS", "")

    handles: list[Launched] = []
    for slot in range(len(shards)):
        argfile = coverage_file.with_name(f"{coverage_file.name}.slot{slot}.args")
        argfile.write_text("".join(f"{argument}\n" for argument in shards[slot]), encoding="utf-8")
        env = {
            **base,
            **overlays[slot],
            "PYTEST_ADDOPTS": f"{inherited} {shlex.quote('@' + str(argfile))} {_PLUGINS}".strip(),
            "PYTEST_FAST_WORKER_IDENTITY": "1",
            "PYTEST_FAST_WORKERS": str(workers_per_slot[slot]),
            "PYTEST_FAST_MARK": "",  # measure the whole suite, overriding a project-level `-m` filter
            "COVERAGE_CORE": "ctrace",
            fast_plugin.COVERAGE_FILE_ENV: str(coverage_file),
            fast_plugin.COVERAGE_CONFIG_ENV: str(config_path),
            fast_plugin.COST_FILE_ENV: str(cost_file),
            fast_plugin.SLOT_ENV: str(slot),
            failures.FAILURE_FILE_ENV: str(failure_file),
        }
        argv = [sys.executable, "-m", "pytest_fast", "--runs", "1", "--workers", str(workers_per_slot[slot])]
        # The spawn itself runs on this (main) thread: forking a child from a threaded process is the macOS
        # abort vector. Waiting is safe from any thread.
        handles.append(launcher(argv, env, profile.root, _log_path(coverage_file, slot)))

    def _wait(handle: Launched) -> int:
        return handle.wait()

    with ThreadPoolExecutor(max_workers=len(shards), thread_name_prefix="balamut-measure") as executor:
        return list(executor.map(_wait, handles))
