"""Coverage-run artifacts: the cost sidecar, the baseline-failure sidecar and per-worker coverage shards.

A measurement runs one pytest-fast process per isolation slot; each forked worker writes its own shards, keyed by
(slot, worker id, pid), and the driver merges them fail-closed: one shard per expected worker, no more, no less.
"""

from __future__ import annotations

import importlib
import types
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from _pytest.config import Config
from _pytest.nodes import Item
from _pytest.reports import CollectReport, TestReport
from coverage import CoverageData

from balamut.coverage import costs, failures, fast_plugin
from balamut.coverage.costs import read_costs, write_costs

# costs


def test_cost_sidecar_round_trips_sorted(tmp_path: Path) -> None:
    path = tmp_path / "coverage.costs.json"
    write_costs(path, {"b": 2.0, "a": 0.5})
    assert read_costs(path) == {"a": 0.5, "b": 2.0}
    assert not list(tmp_path.glob(".*.tmp"))


def test_read_costs_rejects_nonfinite_or_negative_values(tmp_path: Path) -> None:
    path = tmp_path / "coverage.costs.json"
    path.write_text('{"schema_version":1,"costs":{"negative":-1,"nan":"NaN"}}', encoding="utf-8")
    assert read_costs(path) == {}
    assert not costs.costs_valid(path)


def test_empty_cost_sidecar_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "coverage.costs.json"
    write_costs(path, {})
    assert read_costs(path) == {}
    assert costs.costs_valid(path)


def test_missing_cost_sidecar_is_invalid(tmp_path: Path) -> None:
    assert not costs.costs_valid(tmp_path / "absent.json")


# baseline failures


def test_collector_publishes_deduplicated_failed_nodeids_atomically(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    collector = failures.FailureCollector(path, slot="0")
    for when, failed, nodeid in (
        ("setup", True, "tests/a_test.py::test_a"),
        ("call", True, "tests/a_test.py::test_a"),
        ("call", False, "tests/b_test.py::test_b"),
    ):
        collector.pytest_runtest_logreport(cast("TestReport", SimpleNamespace(nodeid=nodeid, when=when, failed=failed)))
    collector.pytest_sessionfinish()
    assert failures.failed_nodeids_read(path) == ("tests/a_test.py::test_a",)
    assert not list(tmp_path.glob(".*.tmp"))


def test_collector_publishes_failed_collection_path(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    collector = failures.FailureCollector(path, slot="0")
    collector.pytest_collectreport(cast("CollectReport", SimpleNamespace(nodeid="tests/broken_test.py", failed=True)))
    collector.pytest_sessionfinish()
    assert failures.failed_nodeids_read(path) == ("tests/broken_test.py",)


def test_clean_session_removes_stale_failure_artifact(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    path.write_text("stale", encoding="utf-8")
    failures.FailureCollector(path, slot="0").pytest_sessionfinish()
    assert not path.exists()
    assert failures.failed_nodeids_read(path) == ()


def test_worker_failure_shards_merge_across_slots(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    failures.worker_artifact_write(path, worker_key="s0gw0", process_id=101, failed_nodeids={"tests/a_test.py::test_a"})
    failures.worker_artifact_write(
        path, worker_key="s1gw0", process_id=102, failed_nodeids={"tests/a_test.py::test_a", "tests/b_test.py::test_b"}
    )
    failures.combine_worker_artifacts(path, expected=frozenset({"s0gw0", "s1gw0"}))
    assert failures.failed_nodeids_read(path) == ("tests/a_test.py::test_a", "tests/b_test.py::test_b")
    assert not list(tmp_path.glob("baseline-failures.json.s*"))


def test_worker_failure_merge_rejects_missing_shard(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    failures.worker_artifact_write(path, worker_key="s0gw0", process_id=101, failed_nodeids=set())
    with pytest.raises(failures.FailureArtifactError, match="missing=s0gw1"):
        failures.combine_worker_artifacts(path, expected=frozenset({"s0gw0", "s0gw1"}))


def test_stale_cleanup_removes_duplicate_worker_shards(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    failures.worker_artifact_write(path, worker_key="s0gw0", process_id=101, failed_nodeids=set())
    failures.worker_artifact_write(path, worker_key="s0gw0", process_id=102, failed_nodeids=set())
    failures.remove_worker_artifacts(path)
    assert not list(tmp_path.glob("baseline-failures.json.s*"))


def test_worker_key_rejects_an_invalid_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="worker identity"):
        failures.worker_artifact_write(tmp_path / "f.json", worker_key="gw0", process_id=1, failed_nodeids=set())


def test_expected_worker_keys_span_every_slot() -> None:
    assert failures.expected_worker_keys((2, 1)) == frozenset({"s0gw0", "s0gw1", "s1gw0"})


def test_failure_nodeid_valid_rejects_empty_and_newlines() -> None:
    assert failures.failure_nodeid_valid("tests/x_test.py::test_x")
    assert not failures.failure_nodeid_valid("")
    assert not failures.failure_nodeid_valid("tests/x_test.py::test_x\n")
    assert not failures.failure_nodeid_valid("tests/x_test.py::test_x\r")


def test_failed_nodeids_read_rejects_extra_keys(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text('{"schema_version":1,"failed_nodeids":["tests/x_test.py::test_x"],"extra":1}', encoding="utf-8")

    assert failures.failed_nodeids_read(path) == ()


@pytest.mark.parametrize(
    "raw",
    [
        "[1]",  # a non-string node id
        '["tests/x_test.py::test_x\\n"]',  # a node id with a newline
    ],
    ids=["non-string", "newline"],
)
def test_failed_nodeids_read_rejects_invalid_nodeids(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "f.json"
    path.write_text('{"schema_version":1,"failed_nodeids":' + raw + "}", encoding="utf-8")

    assert failures.failed_nodeids_read(path) == ()


def test_worker_artifact_write_rejects_zero_process_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="worker identity"):
        failures.worker_artifact_write(tmp_path / "f.json", worker_key="s0gw0", process_id=0, failed_nodeids=set())


def test_worker_artifact_write_accepts_process_id_one(tmp_path: Path) -> None:
    failures.worker_artifact_write(tmp_path / "f.json", worker_key="s0gw0", process_id=1, failed_nodeids=set())
    assert list(tmp_path.glob("f.json.s*.json"))


def test_worker_artifact_paths_skips_names_that_do_not_parse(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    (tmp_path / "baseline-failures.json.sNOTWORKER.123.json").write_text("stray", encoding="utf-8")

    assert failures._worker_artifact_paths(path) == ()


def test_worker_artifacts_skips_a_non_matching_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "baseline-failures.json"
    good = tmp_path / "baseline-failures.json.s0gw0.1.json"
    bad = tmp_path / "baseline-failures.json.sNOTWORKER.2.json"
    good.write_text("good", encoding="utf-8")
    bad.write_text("bad", encoding="utf-8")

    def worker_artifact_paths(_path: Path) -> tuple[Path, ...]:
        return (bad, good)

    monkeypatch.setattr(failures, "_worker_artifact_paths", worker_artifact_paths)

    assert failures._worker_artifacts(path) == {"s0gw0": good}


def test_worker_failure_merge_rejects_extra_shard(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    failures.worker_artifact_write(path, worker_key="s0gw0", process_id=101, failed_nodeids=set())
    failures.worker_artifact_write(path, worker_key="s0gw1", process_id=102, failed_nodeids=set())

    with pytest.raises(failures.FailureArtifactError, match="extra=s0gw1"):
        failures.combine_worker_artifacts(path, expected=frozenset({"s0gw0"}))


def test_combine_with_no_failures_removes_the_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    path.write_text("stale", encoding="utf-8")
    failures.worker_artifact_write(path, worker_key="s0gw0", process_id=101, failed_nodeids=set())

    failures.combine_worker_artifacts(path, expected=frozenset({"s0gw0"}))

    assert not path.exists()


def test_collector_ignores_passing_collection_reports(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    collector = failures.FailureCollector(path, slot="0")
    collector.pytest_collectreport(cast("CollectReport", SimpleNamespace(nodeid="tests/ok_test.py", failed=False)))
    collector.pytest_sessionfinish()

    assert failures.failed_nodeids_read(path) == ()


def test_clean_session_without_an_artifact_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "baseline-failures.json"
    failures.FailureCollector(path, slot="0").pytest_sessionfinish()

    assert not path.exists()


def _fake_config() -> tuple[Config, list[tuple[object, str | None]]]:
    registered: list[tuple[object, str | None]] = []
    plugins: dict[str, object] = {}

    def register(plugin: object, name: str | None = None) -> None:
        registered.append((plugin, name))
        if name is not None:
            plugins[name] = plugin

    def get_plugin(name: str) -> object | None:
        return plugins.get(name)

    pluginmanager = SimpleNamespace(register=register, get_plugin=get_plugin)
    return cast("Config", SimpleNamespace(pluginmanager=pluginmanager)), registered


def test_pytest_configure_registers_the_collector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "baseline-failures.json"
    monkeypatch.setenv(failures.FAILURE_FILE_ENV, str(path))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config, registered = _fake_config()

    failures.pytest_configure(config)

    assert len(registered) == 1
    assert isinstance(registered[0][0], failures.FailureCollector)
    assert registered[0][1] == failures._COLLECTOR_NAME


def test_pytest_configure_removes_a_stale_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "baseline-failures.json"
    path.write_text("stale", encoding="utf-8")
    monkeypatch.setenv(failures.FAILURE_FILE_ENV, str(path))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config, _ = _fake_config()

    failures.pytest_configure(config)

    assert not path.exists()


def test_pytest_configure_tolerates_a_missing_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "baseline-failures.json"
    monkeypatch.setenv(failures.FAILURE_FILE_ENV, str(path))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config, registered = _fake_config()

    failures.pytest_configure(config)

    assert registered


def test_pytest_configure_skips_xdist_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(failures.FAILURE_FILE_ENV, str(tmp_path / "baseline-failures.json"))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    config, registered = _fake_config()

    failures.pytest_configure(config)

    assert registered == []


def test_pytest_configure_ignores_an_empty_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(failures.FAILURE_FILE_ENV, "")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config, registered = _fake_config()

    failures.pytest_configure(config)

    assert registered == []


def test_failure_schema_version_is_pinned() -> None:
    collector = failures.FailureCollector
    try:
        importlib.reload(failures)
        assert failures._SCHEMA_VERSION == 1
    finally:
        failures.FailureCollector = collector


def test_failure_json_value_alias_is_a_union() -> None:
    collector = failures.FailureCollector
    try:
        importlib.reload(failures)
        assert isinstance(failures._JsonValue.__value__, types.UnionType)
    finally:
        failures.FailureCollector = collector


# coverage worker hooks


class _FakeCoverage:
    def __init__(self, **kwargs: str) -> None:
        super().__init__()
        self.kwargs = kwargs
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def switch_context(self, context: str) -> None:
        self.events.append(f"context:{context}")

    def stop(self) -> None:
        self.events.append("stop")

    def save(self) -> None:
        self.events.append("save")


def _write_coverage_shard(path: Path, context: str, lines: set[int]) -> None:
    data = CoverageData(basename=str(path))
    data.set_context(context)
    data.add_lines({"src/example.py": lines})
    data.write()


def test_worker_hooks_capture_context_cost_and_flush(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    created: list[_FakeCoverage] = []

    def fake_coverage(**kwargs: str) -> _FakeCoverage:
        instance = _FakeCoverage(**kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(fast_plugin, "Coverage", fake_coverage)
    monkeypatch.setattr(fast_plugin.os, "getpid", lambda: 123)
    monkeypatch.setenv(fast_plugin.COVERAGE_FILE_ENV, str(coverage_file))
    monkeypatch.setenv(fast_plugin.COST_FILE_ENV, str(cost_file))
    monkeypatch.setenv(fast_plugin.COVERAGE_CONFIG_ENV, "coverage.ini")
    monkeypatch.setenv(fast_plugin.SLOT_ENV, "3")
    config = cast("Config", SimpleNamespace(stash={}))
    item = cast("Item", SimpleNamespace(nodeid="tests/x_test.py::test_x", config=config))

    fast_plugin.pytest_fast_configure_worker(config, "gw2")
    fast_plugin.pytest_runtest_protocol(item)
    fast_plugin.pytest_fast_annotate_result(
        item, {"nodeid": "tests/x_test.py::test_x", "outcome": "passed", "duration": 0.25}
    )
    fast_plugin.pytest_fast_worker_shutdown(config, "gw2")

    assert len(created) == 1
    assert created[0].kwargs["data_file"] == str(coverage_file)
    assert created[0].kwargs["data_suffix"] == "s3gw2.123"
    assert created[0].kwargs["config_file"] == "coverage.ini"
    assert created[0].events == ["start", "context:tests/x_test.py::test_x", "stop", "save"]
    assert read_costs(tmp_path / "coverage.costs.json.s3gw2.123.json") == {"tests/x_test.py::test_x": 0.25}


def test_combine_worker_artifacts_merges_contexts_and_costs(tmp_path: Path) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw0.100", "test_a", {1, 2})
    _write_coverage_shard(tmp_path / "coverage.substrate.s1gw0.101", "test_b", {2, 3})
    write_costs(tmp_path / "coverage.costs.json.s0gw0.100.json", {"test_a": 0.2})
    write_costs(tmp_path / "coverage.costs.json.s1gw0.101.json", {"test_b": 0.3})

    fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=frozenset({"s0gw0", "s1gw0"}))

    combined = CoverageData(basename=str(coverage_file))
    combined.read()
    assert {line: set(contexts) for line, contexts in combined.contexts_by_lineno("src/example.py").items()} == {
        1: {"test_a"},
        2: {"test_a", "test_b"},
        3: {"test_b"},
    }
    assert read_costs(cost_file) == {"test_a": 0.2, "test_b": 0.3}
    assert not list(tmp_path.glob("coverage.substrate.s*"))
    assert not list(tmp_path.glob("coverage.costs.json.s*.json"))


def test_remove_worker_artifacts_removes_only_exact_shards(tmp_path: Path) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    stale_paths = (
        tmp_path / "coverage.substrate.s0gw0.100",
        tmp_path / "coverage.substrate.s0gw0.101",
        tmp_path / "coverage.costs.json.s0gw0.100.json",
    )
    for path in stale_paths:
        path.write_text("stale", encoding="utf-8")
    unrelated = tmp_path / "coverage.substrate.s-invalid"
    unrelated.write_text("keep", encoding="utf-8")
    coverage_file.write_text("published", encoding="utf-8")
    cost_file.write_text("published", encoding="utf-8")

    fast_plugin.remove_worker_artifacts(coverage_file, cost_file)

    assert all(not path.exists() for path in stale_paths)
    assert unrelated.exists()
    assert coverage_file.exists()
    assert cost_file.exists()


def test_combine_worker_artifacts_fails_closed_on_missing_worker(tmp_path: Path) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw0.100", "test_a", {1})
    write_costs(tmp_path / "coverage.costs.json.s0gw0.100.json", {"test_a": 0.2})

    with pytest.raises(fast_plugin.CoverageArtifactError, match="missing=s0gw1"):
        fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=frozenset({"s0gw0", "s0gw1"}))

    assert not coverage_file.exists()
    assert not cost_file.exists()


def test_worker_run_result_keys_are_optional() -> None:
    key = fast_plugin._WORKER_STATE
    try:
        importlib.reload(fast_plugin)
        assert fast_plugin.WorkerRunResult.__total__ is False
    finally:
        fast_plugin._WORKER_STATE = key


def test_configure_worker_creates_missing_parent_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two missing levels: ``parents=True`` creates both, ``parents=False`` cannot.
    coverage_file = tmp_path / "nested" / "deeper" / "coverage.substrate"
    monkeypatch.setenv(fast_plugin.COVERAGE_FILE_ENV, str(coverage_file))
    monkeypatch.setenv(fast_plugin.COST_FILE_ENV, str(tmp_path / "coverage.costs.json"))
    monkeypatch.setenv(fast_plugin.COVERAGE_CONFIG_ENV, "coverage.ini")
    monkeypatch.setenv(fast_plugin.SLOT_ENV, "0")
    monkeypatch.setattr(fast_plugin, "Coverage", _FakeCoverage)
    monkeypatch.setattr(fast_plugin.os, "getpid", lambda: 1)
    config = cast("Config", SimpleNamespace(stash={}))

    fast_plugin.pytest_fast_configure_worker(config, "gw0")

    assert coverage_file.parent.is_dir()


def test_annotate_result_ignores_missing_worker_state() -> None:
    config = cast("Config", SimpleNamespace(stash={}))
    item = cast("Item", SimpleNamespace(nodeid="tests/x_test.py::test_x", config=config))
    fast_plugin.pytest_fast_annotate_result(
        item, {"nodeid": "tests/x_test.py::test_x", "outcome": "passed", "duration": 0.5}
    )


def test_annotate_result_records_zero_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    monkeypatch.setenv(fast_plugin.COVERAGE_FILE_ENV, str(coverage_file))
    monkeypatch.setenv(fast_plugin.COST_FILE_ENV, str(cost_file))
    monkeypatch.setenv(fast_plugin.COVERAGE_CONFIG_ENV, "coverage.ini")
    monkeypatch.setenv(fast_plugin.SLOT_ENV, "0")
    monkeypatch.setattr(fast_plugin, "Coverage", _FakeCoverage)
    monkeypatch.setattr(fast_plugin.os, "getpid", lambda: 1)
    config = cast("Config", SimpleNamespace(stash={}))
    item = cast("Item", SimpleNamespace(nodeid="tests/x_test.py::test_x", config=config))
    fast_plugin.pytest_fast_configure_worker(config, "gw0")
    state = config.stash[fast_plugin._WORKER_STATE]

    fast_plugin.pytest_fast_annotate_result(
        item, {"nodeid": "tests/x_test.py::test_x", "outcome": "passed", "duration": 0.0}
    )

    assert state.costs == {"tests/x_test.py::test_x": 0.0}


def test_combine_requires_every_coverage_shard(tmp_path: Path) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw0.100", "test_a", {1})
    write_costs(tmp_path / "coverage.costs.json.s0gw0.100.json", {"test_a": 0.2})
    write_costs(tmp_path / "coverage.costs.json.s0gw1.101.json", {"test_b": 0.3})

    with pytest.raises(fast_plugin.CoverageArtifactError, match="missing=s0gw1"):
        fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=frozenset({"s0gw0", "s0gw1"}))


def test_combine_requires_every_cost_shard(tmp_path: Path) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw0.100", "test_a", {1})
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw1.101", "test_b", {2})
    write_costs(tmp_path / "coverage.costs.json.s0gw0.100.json", {"test_a": 0.2})

    with pytest.raises(fast_plugin.CoverageArtifactError, match="missing=s0gw1"):
        fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=frozenset({"s0gw0", "s0gw1"}))


def test_combine_rejects_an_extra_shard(tmp_path: Path) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw0.100", "test_a", {1})
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw1.101", "test_b", {2})
    write_costs(tmp_path / "coverage.costs.json.s0gw0.100.json", {"test_a": 0.2})
    write_costs(tmp_path / "coverage.costs.json.s0gw1.101.json", {"test_b": 0.3})

    with pytest.raises(fast_plugin.CoverageArtifactError, match="extra=s0gw1"):
        fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=frozenset({"s0gw0"}))


def test_combine_removes_stale_files_before_checking_shards(tmp_path: Path) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    coverage_file.write_text("stale-coverage", encoding="utf-8")
    cost_file.write_text("stale-costs", encoding="utf-8")

    with pytest.raises(fast_plugin.CoverageArtifactError, match="missing=s0gw0"):
        fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=frozenset({"s0gw0"}))

    assert not coverage_file.exists()
    assert not cost_file.exists()


def test_scrub_worker_env_removes_every_owned_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in fast_plugin._OWNED_ENV:
        monkeypatch.setenv(key, "set")

    fast_plugin.scrub_worker_env()

    for key in fast_plugin._OWNED_ENV:
        assert key not in fast_plugin.os.environ


def test_runtest_protocol_scrubs_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fast_plugin.COVERAGE_FILE_ENV, str(tmp_path / "coverage.substrate"))
    config = cast("Config", SimpleNamespace(stash={}))
    item = cast("Item", SimpleNamespace(nodeid="tests/x_test.py::test_x", config=config))

    fast_plugin.pytest_runtest_protocol(item)

    assert fast_plugin.COVERAGE_FILE_ENV not in fast_plugin.os.environ


def test_fast_configure_worker_registers_the_collector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "baseline-failures.json"
    monkeypatch.setenv(failures.FAILURE_FILE_ENV, str(path))
    config, registered = _fake_config()

    failures.pytest_fast_configure_worker(config)

    assert len(registered) == 1
    assert isinstance(registered[0][0], failures.FailureCollector)


def test_fast_configure_worker_skips_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(failures.FAILURE_FILE_ENV, raising=False)
    config, registered = _fake_config()

    failures.pytest_fast_configure_worker(config)

    assert registered == []


def test_fast_configure_worker_skips_an_existing_collector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "baseline-failures.json"
    monkeypatch.setenv(failures.FAILURE_FILE_ENV, str(path))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config, registered = _fake_config()
    failures.pytest_configure(config)

    failures.pytest_fast_configure_worker(config)

    assert len(registered) == 1


def test_pytest_configure_defaults_the_slot_to_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "baseline-failures.json"
    monkeypatch.setenv(failures.FAILURE_FILE_ENV, str(path))
    monkeypatch.delenv(failures.SLOT_ENV, raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config, registered = _fake_config()

    failures.pytest_configure(config)

    assert isinstance(registered[0][0], failures.FailureCollector)
    assert registered[0][0].slot == "0"


def test_fast_configure_worker_defaults_slot_and_names_the_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "baseline-failures.json"
    monkeypatch.setenv(failures.FAILURE_FILE_ENV, str(path))
    monkeypatch.delenv(failures.SLOT_ENV, raising=False)
    config, registered = _fake_config()

    failures.pytest_fast_configure_worker(config)

    assert isinstance(registered[0][0], failures.FailureCollector)
    assert registered[0][0].slot == "0"
    assert registered[0][1] == failures._COLLECTOR_NAME


def test_discover_shards_skips_a_non_matching_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "coverage.substrate"
    good = tmp_path / "coverage.substrate.s0gw0.100"
    bad = tmp_path / "coverage.substrate.sNOTWORKER.200"
    good.write_text("good", encoding="utf-8")
    bad.write_text("bad", encoding="utf-8")

    def matching_shards(_base: Path, *, cost: bool = False) -> tuple[Path, ...]:
        return (bad, good)

    monkeypatch.setattr(fast_plugin, "_matching_shards", matching_shards)

    assert fast_plugin._discover_shards(base, cost=False) == {"s0gw0": good}


def test_combine_cleans_up_partial_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw0.100", "test_a", {1})
    write_costs(tmp_path / "coverage.costs.json.s0gw0.100.json", {"test_a": 0.2})

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr(fast_plugin, "write_costs", boom)
    with pytest.raises(fast_plugin.CoverageArtifactError):
        fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=frozenset({"s0gw0"}))

    assert not coverage_file.exists()
    assert not cost_file.exists()


def test_combine_cleans_up_a_cost_file_written_before_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cost sidecar already on disk when the merge fails must be removed by the except cleanup."""
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw0.100", "test_a", {1})
    write_costs(tmp_path / "coverage.costs.json.s0gw0.100.json", {"test_a": 0.2})

    def write_then_raise(path: Path, costs: dict[str, float]) -> None:
        write_costs(path, costs)
        raise OSError("boom after writing the sidecar")

    monkeypatch.setattr(fast_plugin, "write_costs", write_then_raise)
    with pytest.raises(fast_plugin.CoverageArtifactError):
        fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=frozenset({"s0gw0"}))

    assert not coverage_file.exists()
    assert not cost_file.exists()


def test_combine_cleans_up_when_the_first_shard_is_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable shard aborts before any coverage is written; the cleanup must not crash on the missing files."""
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    _write_coverage_shard(tmp_path / "coverage.substrate.s0gw0.100", "test_a", {1})
    write_costs(tmp_path / "coverage.costs.json.s0gw0.100.json", {"test_a": 0.2})

    def boom_read(self: object, *_args: object, **_kwargs: object) -> None:
        raise OSError("unreadable shard")

    monkeypatch.setattr(fast_plugin.CoverageData, "read", boom_read)
    with pytest.raises(fast_plugin.CoverageArtifactError):
        fast_plugin.combine_worker_artifacts(coverage_file, cost_file, expected=frozenset({"s0gw0"}))

    assert not coverage_file.exists()
    assert not cost_file.exists()


def test_configure_worker_tolerates_existing_parent_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage_file = tmp_path / "nested" / "coverage.substrate"
    coverage_file.parent.mkdir(parents=True)
    monkeypatch.setenv(fast_plugin.COVERAGE_FILE_ENV, str(coverage_file))
    monkeypatch.setenv(fast_plugin.COST_FILE_ENV, str(tmp_path / "coverage.costs.json"))
    monkeypatch.setenv(fast_plugin.COVERAGE_CONFIG_ENV, "coverage.ini")
    monkeypatch.setenv(fast_plugin.SLOT_ENV, "0")
    monkeypatch.setattr(fast_plugin, "Coverage", _FakeCoverage)
    monkeypatch.setattr(fast_plugin.os, "getpid", lambda: 1)
    config = cast("Config", SimpleNamespace(stash={}))

    fast_plugin.pytest_fast_configure_worker(config, "gw0")

    assert coverage_file.parent.is_dir()


def test_configure_worker_defaults_the_slot_and_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    cost_file = tmp_path / "coverage.costs.json"
    created: list[_FakeCoverage] = []

    def fake_coverage(**kwargs: str) -> _FakeCoverage:
        instance = _FakeCoverage(**kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(fast_plugin, "Coverage", fake_coverage)
    monkeypatch.setattr(fast_plugin.os, "getpid", lambda: 7)
    monkeypatch.setenv(fast_plugin.COVERAGE_FILE_ENV, str(coverage_file))
    monkeypatch.setenv(fast_plugin.COST_FILE_ENV, str(cost_file))
    monkeypatch.delenv(fast_plugin.SLOT_ENV, raising=False)
    monkeypatch.delenv(fast_plugin.COVERAGE_CONFIG_ENV, raising=False)
    config = cast("Config", SimpleNamespace(stash={}))

    fast_plugin.pytest_fast_configure_worker(config, "gw0")

    assert created[0].kwargs["data_suffix"] == "s0gw0.7"
    assert created[0].kwargs["config_file"] == ""


def test_configure_worker_rejects_a_double_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage_file = tmp_path / "coverage.substrate"
    monkeypatch.setenv(fast_plugin.COVERAGE_FILE_ENV, str(coverage_file))
    monkeypatch.setenv(fast_plugin.COST_FILE_ENV, str(tmp_path / "coverage.costs.json"))
    config = cast("Config", SimpleNamespace(stash={fast_plugin._WORKER_STATE: object()}))

    with pytest.raises(RuntimeError, match="already configured"):
        fast_plugin.pytest_fast_configure_worker(config, "gw0")
