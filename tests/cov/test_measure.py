"""Coverage measurement (balamut.coverage.measure): one pytest-fast run per isolation slot, merged fail-closed.

Unit tests drive the orchestration through a fake launcher that writes the shards a real worker would. The
``daemon`` test runs the real pytest-fast forkserver on a sample project and checks per-test contexts.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Self

import pytest
from coverage import CoverageData

from balamut.config import Profile, load_profile
from balamut.coverage import failures, fast_plugin, measure
from balamut.coverage.costs import read_costs, write_costs
from balamut.coverage.coverage_map import load_coverage_map
from balamut.coverage.measure import (
    MeasureRequest,
    _groups,
    cost_file_for,
    default_launcher,
    failure_file_for,
    measure_coverage,
)
from balamut.engine import daemon
from balamut.isolation import EnvOverlays, IsolationError, NoIsolation
from bm_support import make_layout, make_project, write_file


def _fresh_module_probe(name: str) -> ModuleType:
    """Re-execute a module's import-time lines under a probe name, without touching the live module."""
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


@dataclass
class FakeIsolation:
    """Hands out overlays naming each slot, and records the lifecycle."""

    events: list[str] = field(default_factory=list[str])
    fail_cleanup: bool = False

    @property
    def isolated(self) -> bool:
        return True

    def prepare(self, count: int, prefix: str, /) -> EnvOverlays:
        self.events.append(f"prepare {count}")
        return tuple({"APP_SLOT": f"{prefix}_w{index}"} for index in range(count))

    def cleanup(self, count: int, _prefix: str, /) -> None:
        self.events.append(f"cleanup {count}")
        if self.fail_cleanup:
            raise IsolationError("cleanup failed")

    def probe(self) -> None:
        return None

    def fingerprint(self) -> str:
        return "fake"


@dataclass
class _Finished:
    """A completed launcher handle for the fakes."""

    returncode: int

    def wait(self) -> int:
        return self.returncode


@dataclass
class FakeLauncher:
    """Writes the shards of every worker of a slot, as the real forkserver workers would."""

    returncode: int = 0
    failing: tuple[str, ...] = ()
    calls: list[tuple[list[str], dict[str, str]]] = field(default_factory=list[tuple[list[str], dict[str, str]]])

    def __call__(self, argv: list[str], env: Mapping[str, str], _cwd: Path, _log: Path) -> measure.Launched:
        self.calls.append((argv, dict(env)))
        slot = env[fast_plugin.SLOT_ENV]
        workers = int(env["PYTEST_FAST_WORKERS"])
        coverage_file = Path(env[fast_plugin.COVERAGE_FILE_ENV])
        cost_file = Path(env[fast_plugin.COST_FILE_ENV])
        failure_file = Path(env[failures.FAILURE_FILE_ENV])
        for index in range(workers):
            key = failures.worker_key(slot, f"gw{index}")
            data = CoverageData(basename=str(coverage_file), suffix=f"{key}.{100 + index}")
            data.set_context(f"tests/test_calc.py::test_{key}")
            data.add_lines({"src/pkg/calc.py": {1, 2}})
            data.write()
            write_costs(Path(f"{cost_file}.{key}.{100 + index}.json"), {f"tests/test_calc.py::test_{key}": 0.1})
            failures.worker_artifact_write(
                failure_file, worker_key=key, process_id=100 + index, failed_nodeids=set(self.failing)
            )
        return _Finished(self.returncode)


def _profile(root: Path) -> Profile:
    return Profile(layout=make_layout(root), state_dir=root / ".balamut")


def _seed_tests(root: Path, count: int) -> None:
    for index in range(count):
        path = root / f"tests/test_m{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def test_{index}():\n    assert True\n", encoding="utf-8")


def test_shared_environment_runs_one_slot_with_all_workers(tmp_path: Path) -> None:
    _seed_tests(tmp_path, 3)
    launcher = FakeLauncher()
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "out/cov", workers=3),
        isolation=NoIsolation(),
        launcher=launcher,
    )
    assert result.returncode == 0
    assert len(launcher.calls) == 1
    _argv, env = launcher.calls[0]
    assert env["PYTEST_FAST_WORKERS"] == "3"
    assert env["COVERAGE_CORE"] == "ctrace"
    assert env["PYTEST_FAST_WORKER_IDENTITY"] == "1"
    assert "-p balamut.coverage.fast_plugin" in env["PYTEST_ADDOPTS"]
    assert "-p balamut.coverage.failures" in env["PYTEST_ADDOPTS"]
    cov = load_coverage_map(result.coverage_file)
    assert cov.tests_covering("src/pkg/calc.py", 1) == {f"tests/test_calc.py::test_s0gw{i}" for i in range(3)}
    assert set(read_costs(result.cost_file)) == {f"tests/test_calc.py::test_s0gw{i}" for i in range(3)}


def test_isolated_environment_shards_test_files_across_slots(tmp_path: Path) -> None:
    _seed_tests(tmp_path, 5)
    launcher = FakeLauncher()
    isolation = FakeIsolation()
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", workers=2),
        isolation=isolation,
        launcher=launcher,
    )
    assert result.returncode == 0
    assert isolation.events == ["prepare 2", "cleanup 2"]
    assert len(launcher.calls) == 2
    shard_files: list[set[str]] = []
    for _argv, env in launcher.calls:
        assert env["PYTEST_FAST_WORKERS"] == "1"
        assert env["APP_SLOT"].endswith(f"_w{env[fast_plugin.SLOT_ENV]}")
        argfile = next(token[1:] for token in env["PYTEST_ADDOPTS"].split() if token.startswith("@"))
        shard_files.append(set(Path(argfile).read_text(encoding="utf-8").splitlines()))
    assert shard_files[0].isdisjoint(shard_files[1])
    assert shard_files[0] | shard_files[1] == {f"tests/test_m{index}.py" for index in range(5)}


def test_targets_reach_pytest_through_an_argument_file(tmp_path: Path) -> None:
    # Thousands of node ids as text in PYTEST_ADDOPTS would exceed the per-string execve limit (128 KiB on Linux).
    nodeids = tuple(f"tests/hub_test.py::test_case_{index}[tenant a]" for index in range(3000))
    launcher = FakeLauncher()
    measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", targets=nodeids, workers=1),
        isolation=NoIsolation(),
        launcher=launcher,
    )
    addopts = launcher.calls[0][1]["PYTEST_ADDOPTS"]
    assert addopts.count("hub_test.py::") == 0
    argfile = next(token[1:] for token in addopts.split() if token.startswith("@"))
    assert Path(argfile).read_text(encoding="utf-8").splitlines() == list(nodeids)


def test_isolated_targets_keep_node_ids_of_one_file_together(tmp_path: Path) -> None:
    nodeids = ("tests/a_test.py::test_1", "tests/b_test.py::test_1", "tests/a_test.py::test_2")
    launcher = FakeLauncher()
    measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", targets=nodeids, workers=4),
        isolation=FakeIsolation(),
        launcher=launcher,
    )
    assert len(launcher.calls) == 2  # two files: never more slots than files
    groups: list[list[str]] = []
    for _argv, env in launcher.calls:
        argfile = next(token[1:] for token in env["PYTEST_ADDOPTS"].split() if token.startswith("@"))
        groups.append(Path(argfile).read_text(encoding="utf-8").splitlines())
    assert sorted(groups) == [["tests/a_test.py::test_1", "tests/a_test.py::test_2"], ["tests/b_test.py::test_1"]]


def test_failed_tests_still_merge_and_report_the_failures(tmp_path: Path) -> None:
    launcher = FakeLauncher(returncode=1, failing=("tests/test_calc.py::test_low",))
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", workers=1),
        isolation=NoIsolation(),
        launcher=launcher,
    )
    assert result.returncode == 1
    assert result.failed_nodeids == ("tests/test_calc.py::test_low",)
    assert result.coverage_file.exists()


def test_a_broken_run_merges_nothing(tmp_path: Path) -> None:
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", workers=1),
        isolation=NoIsolation(),
        launcher=FakeLauncher(returncode=3),
    )
    assert result.returncode == 2
    assert not result.coverage_file.exists()
    assert "exited with 3" in result.detail


def test_cleanup_failure_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    _seed_tests(tmp_path, 2)
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", workers=2),
        isolation=FakeIsolation(fail_cleanup=True),
        launcher=FakeLauncher(),
    )
    assert result.returncode == 0
    assert "cleanup failed" in result.warnings[0]


def test_generated_coverage_config_names_the_source_roots(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", workers=1),
        isolation=NoIsolation(),
        launcher=launcher,
    )
    config = Path(launcher.calls[0][1][fast_plugin.COVERAGE_CONFIG_ENV]).read_text(encoding="utf-8")
    assert "source =\n    src\n" in config
    assert "relative_files = True" in config
    assert "branch = False" in config
    assert "concurrency = thread" in config


@pytest.mark.daemon
def test_real_forkserver_run_records_exact_per_test_contexts(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    profile = load_profile(root)
    previous = Path.cwd()
    os.chdir(root)
    try:
        result = measure_coverage(
            MeasureRequest(profile=profile, coverage_file=root / ".balamut/cov", workers=2), isolation=NoIsolation()
        )
    finally:
        os.chdir(previous)
    assert result.returncode == 0, result.detail
    cov = load_coverage_map(result.coverage_file)
    # `value < low` (line 2) runs in both clamp tests; `return low` (line 3) only in the low one.
    assert cov.tests_covering("src/pkg/calc.py", 2) == {
        "tests/test_calc.py::test_clamp_inside",
        "tests/test_calc.py::test_clamp_low",
    }
    assert cov.tests_covering("src/pkg/calc.py", 3) == {"tests/test_calc.py::test_clamp_low"}
    assert cov.tests_covering("src/pkg/calc.py", 10) == {"tests/test_calc.py::test_adult"}
    assert set(read_costs(result.cost_file)) == {
        "tests/test_calc.py::test_clamp_inside",
        "tests/test_calc.py::test_clamp_low",
        "tests/test_calc.py::test_adult",
    }


@pytest.mark.daemon
def test_real_forkserver_run_survives_a_project_that_errors_on_warnings(tmp_path: Path) -> None:
    # Under `filterwarnings = error` the preload's and the plugins' import warnings, and the empty shard of a worker
    # left without a test (four workers, three tests), must not kill the forkserver or a worker.
    root = make_project(tmp_path / "proj", pytest_ini='filterwarnings = ["error"]')
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    profile = load_profile(root)
    previous = Path.cwd()
    os.chdir(root)
    try:
        result = measure_coverage(
            MeasureRequest(profile=profile, coverage_file=root / ".balamut/cov", workers=4), isolation=NoIsolation()
        )
    finally:
        os.chdir(previous)
    assert result.returncode == 0, result.detail
    cov = load_coverage_map(result.coverage_file)
    assert cov.tests_covering("src/pkg/calc.py", 3) == {"tests/test_calc.py::test_clamp_low"}


def test_measurement_addopts_filter_the_import_warnings_of_the_preload_and_the_plugins() -> None:
    assert daemon.PRELOAD_REWRITE_WARNING_FILTER in measure._PLUGINS
    assert daemon.rewrite_warning_filter("balamut.coverage.failures") in measure._PLUGINS


def test_generated_coverage_config_disables_the_empty_shard_warning(tmp_path: Path) -> None:
    assert "disable_warnings = no-data-collected" in measure.coverage_config(_profile(tmp_path))


# --- module-level exit-code constants ---


def test_measure_import_time_exit_codes() -> None:
    probe = _fresh_module_probe("balamut.coverage.measure")
    assert probe.EXIT_OK == 0
    assert probe.EXIT_TESTS_FAILED == 1
    assert probe.EXIT_INFRA == 2


# --- default_launcher ---


def test_default_launcher_creates_the_log_parent(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "deep" / "slot.log"
    code = default_launcher([sys.executable, "-c", "pass"], {}, tmp_path, log).wait()
    assert code == 0
    assert log.exists()


def test_default_launcher_accepts_an_existing_log_parent(tmp_path: Path) -> None:
    log = tmp_path / "slot.log"
    code = default_launcher([sys.executable, "-c", "pass"], {}, tmp_path, log).wait()
    assert code == 0
    assert log.exists()


def test_default_launcher_reports_a_nonzero_exit_code(tmp_path: Path) -> None:
    log = tmp_path / "slot.log"
    code = default_launcher([sys.executable, "-c", "import sys; sys.exit(3)"], {}, tmp_path, log).wait()
    assert code == 3


def test_popen_handle_wait_closes_the_stream(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stream = io.BytesIO()
    handle = measure._PopenHandle(proc, stream)
    assert handle.wait() == 0
    assert stream.closed


# --- _groups (weight, arguments) ---


def test_groups_weight_is_the_file_size(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/test_m.py", "def test_a():\n    assert True\n")
    [(weight, items)] = _groups(_profile(tmp_path), [])
    assert items == ["tests/test_m.py"]
    assert weight == (tmp_path / "tests" / "test_m.py").stat().st_size


def test_groups_weights_a_single_byte_file_as_one(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/test_m.py", "x")
    assert _groups(_profile(tmp_path), []) == [(1, ["tests/test_m.py"])]


# --- measure_coverage orchestration ---


def test_measure_removes_stale_coverage_shards(tmp_path: Path) -> None:
    coverage_file = tmp_path / "cov"
    coverage_file.parent.mkdir(parents=True, exist_ok=True)
    (coverage_file.with_name(f"{coverage_file.name}.s9gw9.999")).write_text("stale")
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=coverage_file, workers=1),
        isolation=NoIsolation(),
        launcher=FakeLauncher(),
    )
    assert result.returncode == 0


def test_measure_removes_stale_failure_shards(tmp_path: Path) -> None:
    coverage_file = tmp_path / "cov"
    failure_file = failure_file_for(coverage_file)
    failure_file.parent.mkdir(parents=True, exist_ok=True)
    (failure_file.with_name(f"{failure_file.name}.s9gw9.999.json")).write_text("stale")
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=coverage_file, workers=1),
        isolation=NoIsolation(),
        launcher=FakeLauncher(),
    )
    assert result.returncode == 0


def test_measure_removes_a_stale_failure_file_on_a_broken_run(tmp_path: Path) -> None:
    coverage_file = tmp_path / "cov"
    failure_file = failure_file_for(coverage_file)
    failure_file.parent.mkdir(parents=True, exist_ok=True)
    failure_file.write_text("stale")
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=coverage_file, workers=1),
        isolation=NoIsolation(),
        launcher=FakeLauncher(returncode=3),
    )
    assert result.returncode == 2
    assert not failure_file.exists()


def test_measure_caps_workers_at_the_configured_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(measure, "resolve_workers", lambda: 100)
    launcher = FakeLauncher()
    measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov"),
        isolation=NoIsolation(),
        launcher=launcher,
    )
    assert launcher.calls[0][1]["PYTEST_FAST_WORKERS"] == "4"


def test_isolated_workers_is_at_least_one(tmp_path: Path) -> None:
    _seed_tests(tmp_path, 2)
    isolation = FakeIsolation()
    measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", workers=1),
        isolation=isolation,
        launcher=FakeLauncher(),
    )
    assert isolation.events == ["prepare 1", "cleanup 1"]


def test_measure_reports_failed_tests_even_when_workers_report_ok(tmp_path: Path) -> None:
    launcher = FakeLauncher(returncode=0, failing=("tests/test_calc.py::test_low",))
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", workers=1),
        isolation=NoIsolation(),
        launcher=launcher,
    )
    assert result.returncode == 1
    assert result.failed_nodeids == ("tests/test_calc.py::test_low",)


def test_measure_reports_an_infra_error_when_artifacts_cannot_merge(tmp_path: Path) -> None:
    class _NoopLauncher:
        def __call__(self, _argv: list[str], _env: Mapping[str, str], _cwd: Path, _log: Path) -> measure.Launched:
            return _Finished(0)

    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", workers=1),
        isolation=NoIsolation(),
        launcher=_NoopLauncher(),
    )
    assert result.returncode == 2
    assert "missing" in result.detail


class _FailingPrepareIsolation(FakeIsolation):
    def prepare(self, count: int, prefix: str, /) -> EnvOverlays:
        self.events.append(f"prepare {count}")
        raise IsolationError("prepare failed")


def test_measure_reports_an_isolation_prepare_failure(tmp_path: Path) -> None:
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=tmp_path / "cov", workers=1),
        isolation=_FailingPrepareIsolation(),
        launcher=FakeLauncher(),
    )
    assert result.returncode == 2
    assert "isolation" in result.detail


def _assert_result_paths(result: measure.MeasureResult, coverage_file: Path) -> None:
    """The artifact paths must land in their positional slots (returncode, coverage, cost, failure)."""
    absolute = coverage_file.absolute()
    assert result.coverage_file == absolute
    assert result.cost_file == cost_file_for(absolute)
    assert result.failure_file == failure_file_for(absolute)


def test_broken_run_reports_exact_artifact_paths(tmp_path: Path) -> None:
    coverage_file = tmp_path / "cov"
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=coverage_file, workers=1),
        isolation=NoIsolation(),
        launcher=FakeLauncher(returncode=3),
    )
    assert result.returncode == 2
    _assert_result_paths(result, coverage_file)


def test_merge_failure_reports_exact_artifact_paths(tmp_path: Path) -> None:
    class _NoopLauncher:
        def __call__(self, _argv: list[str], _env: Mapping[str, str], _cwd: Path, _log: Path) -> measure.Launched:
            return _Finished(0)

    coverage_file = tmp_path / "cov"
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=coverage_file, workers=1),
        isolation=NoIsolation(),
        launcher=_NoopLauncher(),
    )
    assert result.returncode == 2
    _assert_result_paths(result, coverage_file)


def test_isolation_failure_reports_exact_artifact_paths(tmp_path: Path) -> None:
    coverage_file = tmp_path / "cov"
    result = measure_coverage(
        MeasureRequest(profile=_profile(tmp_path), coverage_file=coverage_file, workers=1),
        isolation=_FailingPrepareIsolation(),
        launcher=FakeLauncher(),
    )
    assert result.returncode == 2
    _assert_result_paths(result, coverage_file)


def test_default_launcher_spawns_in_cwd_with_merged_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _Proc:
        def wait(self) -> int:
            return 0

    def fake_popen(
        argv: list[str],
        env: object = None,
        cwd: object = None,
        stdout: object = None,
        stderr: object = None,
    ) -> _Proc:
        seen["argv"] = argv
        seen["env"] = env
        seen["cwd"] = cwd
        seen["stdout"] = stdout
        seen["stderr"] = stderr
        return _Proc()

    monkeypatch.setattr(measure.subprocess, "Popen", fake_popen)
    log = tmp_path / "slot.log"
    default_launcher(["python", "-c", "pass"], {"A": "1"}, tmp_path, log)
    assert seen["cwd"] == tmp_path
    assert seen["stderr"] == subprocess.STDOUT
    assert seen["stdout"] is not None
    assert seen["env"] == {"A": "1"}


def test_shards_balances_heaviest_group_first() -> None:
    # Without the key, `sorted` would place the heaviest group last and the shards would balance differently.
    assert measure._shards([(3, ["a"]), (1, ["b"]), (2, ["c"])], 2) == [["a"], ["c", "b"]]


def test_run_slots_sizes_and_names_the_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _Executor:
        def __init__(self, max_workers: object = None, thread_name_prefix: object = None) -> None:
            seen["max_workers"] = max_workers
            seen["thread_name_prefix"] = thread_name_prefix

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def map(self, fn: Callable[[measure.Launched], int], handles: list[measure.Launched]) -> list[int]:
            return [fn(handle) for handle in handles]

    monkeypatch.setattr(measure, "ThreadPoolExecutor", _Executor)

    class _Launcher:
        def __call__(self, argv: list[str], env: Mapping[str, str], cwd: Path, log: Path) -> measure.Launched:
            return _Finished(0)

    measure._run_slots(
        _profile(tmp_path),
        [["tests/x_test.py"]],
        [1],
        ({},),
        tmp_path / "cov",
        tmp_path / "cov.costs.json",
        tmp_path / "cov.failures.json",
        tmp_path / "cov.coveragerc",
        _Launcher(),
    )
    assert seen["max_workers"] == 1
    assert seen["thread_name_prefix"] == "balamut-measure"
