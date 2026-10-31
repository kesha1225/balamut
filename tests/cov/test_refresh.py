"""Incremental coverage refresh (balamut.coverage.refresh).

A refresh re-measures only what changed, through the same measurement as the substrate build. Its restoration
step keeps a module that an isolated re-measurement under-records (imported before the tracer starts) from
falsely becoming uncovered.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from coverage import CoverageData

from balamut.config import Profile
from balamut.coverage import failures, fast_plugin, measure, refresh
from balamut.coverage.costs import write_costs
from balamut.coverage.refresh import MeasureContext, restore_wiped_src, snapshot_src_coverage
from balamut.coverage.store import Store
from balamut.isolation import NoIsolation
from bm_support import make_layout

_F = "src/app/bootstrap.py"  # a module imported before the tracer starts
_OTHER = "src/app/other.py"  # a traceable file whose coverage a refresh updates legitimately
_T = "tests/app/bootstrap_test.py::test_metamorphic"


@dataclass
class ScriptedLauncher:
    """Stands in for one pytest-fast slot run: writes the artifacts the real workers would, returns ``returncode``."""

    returncode: int = 0
    contexts: dict[str, dict[str, set[int]]] = field(default_factory=dict[str, dict[str, set[int]]])
    failed: tuple[str, ...] = ()
    costs: dict[str, float] = field(default_factory=dict[str, float])
    write_shards: bool = True
    log_text: str = ""
    calls: list[tuple[list[str], dict[str, str]]] = field(default_factory=list[tuple[list[str], dict[str, str]]])

    def __call__(self, argv: list[str], env: Mapping[str, str], _cwd: Path, log: Path) -> measure.Launched:
        self.calls.append((argv, dict(env)))
        log.write_text(self.log_text, encoding="utf-8")
        if not self.write_shards:
            return _FinishedLauncher(self.returncode)
        slot = env[fast_plugin.SLOT_ENV]
        for index in range(int(env["PYTEST_FAST_WORKERS"])):
            key = failures.worker_key(slot, f"gw{index}")
            data = CoverageData(basename=env[fast_plugin.COVERAGE_FILE_ENV], suffix=f"{key}.{10 + index}")
            data.add_lines({"src/x.py": set()})  # an empty run still publishes a coverage file
            if index == 0:
                for context, lines in self.contexts.items():
                    data.set_context(f"{context}|run")
                    data.add_lines(lines)
            data.write()
            write_costs(
                Path(f"{env[fast_plugin.COST_FILE_ENV]}.{key}.{10 + index}.json"), self.costs if index == 0 else {}
            )
            failures.worker_artifact_write(
                Path(env[failures.FAILURE_FILE_ENV]),
                worker_key=key,
                process_id=10 + index,
                failed_nodeids=set(self.failed) if index == 0 else set(),
            )
        return _FinishedLauncher(self.returncode)


@dataclass
class _FinishedLauncher:
    """A completed launcher handle for the scripted fake."""

    returncode: int

    def wait(self) -> int:
        return self.returncode


def _context(tmp_path: Path, launcher: ScriptedLauncher) -> MeasureContext:
    profile = Profile(layout=make_layout(tmp_path), state_dir=tmp_path / ".balamut")
    return MeasureContext(profile=profile, isolation=NoIsolation(), launcher=launcher)


def _scratch(tmp_path: Path) -> list[Path]:
    scratch = tmp_path / ".balamut" / "tmp"
    return sorted(scratch.iterdir()) if scratch.exists() else []


def test_measure_returns_per_test_lines_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 6)
    launcher = ScriptedLauncher(contexts={"tests/x_test.py::test_x": {"src/x.py": {1, 2}}})
    measured = refresh.measure_test_coverage(
        ["tests/x_test.py::test_x", "tests/a_test.py"], context=_context(tmp_path, launcher)
    )
    assert measured == {"tests/x_test.py::test_x": [("src/x.py", 1), ("src/x.py", 2)]}
    [(_argv, env)] = launcher.calls
    assert env["PYTEST_FAST_WORKERS"] == "1"  # few targets: one worker
    argfile = next(token[1:] for token in env["PYTEST_ADDOPTS"].split() if token.startswith("@"))
    assert Path(argfile).name.endswith(".args")
    assert _scratch(tmp_path) == []  # every artifact of the run is removed


def test_measure_strips_the_mutation_plugin_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refresh must measure the original code: an inherited mutant plugin or mutant file would measure a mutant.
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 6)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q -p balamut.runtime.plugin")
    monkeypatch.setenv("BALAMUT_MUTANT_FILE", "/tmp/stale-mutant.json")
    launcher = ScriptedLauncher()
    refresh.measure_test_coverage(["tests/x_test.py"], context=_context(tmp_path, launcher))
    env = launcher.calls[0][1]
    assert "balamut.runtime.plugin" not in env["PYTEST_ADDOPTS"]
    assert env["PYTEST_ADDOPTS"].startswith("-q ")
    assert "BALAMUT_MUTANT_FILE" not in env


def test_measure_reports_failing_refreshed_tests_as_baseline_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 6)
    failed = ("tests/x_test.py::test_x", "tests/x_test.py::test_y[param]")
    launcher = ScriptedLauncher(returncode=1, failed=failed)
    with pytest.raises(refresh.RefreshBaselineError) as error:
        refresh.measure_test_coverage(["tests/x_test.py"], context=_context(tmp_path, launcher))
    assert error.value.nodeids == failed
    assert isinstance(error.value, RuntimeError)
    assert "tests/x_test.py::test_x" in str(error.value)


def test_infrastructure_failure_carries_the_run_output_and_keeps_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 6)
    targets = [f"tests/domain/example_test.py::test_case[{index}]" for index in range(3_786)]
    launcher = ScriptedLauncher(returncode=3, write_shards=False, log_text="INTERNALERROR> configuration failed\n")
    with pytest.raises(RuntimeError) as error:
        refresh.measure_test_coverage(targets, context=_context(tmp_path, launcher))
    message = str(error.value)
    assert not isinstance(error.value, refresh.RefreshBaselineError)
    assert "configuration failed" in message
    assert "3 786 targets" in message
    assert len(message) < 8_192
    artifacts = [path for path in _scratch(tmp_path) if path.name.endswith(".targets.json")]
    assert len(artifacts) == 1
    assert str(artifacts[0]) in message
    saved = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert saved == {"target_count": len(targets), "targets": sorted(targets)}


def test_measure_hands_back_the_costs_it_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A re-run test's duration is fresher than the substrate sidecar's; without it a test that became slower would
    # get a budget from its old cost and a false timeout.
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 6)
    launcher = ScriptedLauncher(costs={"tests/x_test.py::test_x": 1.5})
    measured_costs: dict[str, float] = {}
    assert (
        refresh.measure_test_coverage(["tests/x_test.py"], context=_context(tmp_path, launcher), costs=measured_costs)
        == {}
    )
    assert measured_costs == {"tests/x_test.py::test_x": 1.5}


def test_measure_uses_the_adaptive_worker_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 10)
    launcher = ScriptedLauncher()
    refresh.measure_test_coverage(
        [f"tests/x_test.py::test_x[{index}]" for index in range(300)], context=_context(tmp_path, launcher)
    )
    assert launcher.calls[0][1]["PYTEST_FAST_WORKERS"] == "6"


def test_measure_sorts_targets_so_one_directory_is_collected_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 1)
    targets = [
        "tests/team/invites_test.py",
        "tests/root_test.py",
        "tests/team/members_test.py::test_member[b]",
        "tests/team/members_test.py::test_member[a]",
    ]
    launcher = ScriptedLauncher()
    refresh.measure_test_coverage(targets, context=_context(tmp_path, launcher), workers=1)
    argfile = next(token[1:] for token in launcher.calls[0][1]["PYTEST_ADDOPTS"].split() if token.startswith("@"))
    assert argfile  # the file itself is cleaned up; the order is checked through the recorded call
    assert sorted(targets) == refresh.ordered_targets(targets)


def test_empty_target_list_measures_nothing(tmp_path: Path) -> None:
    launcher = ScriptedLauncher()
    assert refresh.measure_test_coverage([], context=_context(tmp_path, launcher)) == {}
    assert launcher.calls == []


def test_restoration_rows_recovers_only_wiped_bootstrap_coverage() -> None:
    measurement = refresh.RefreshMeasurement(
        src_nodeids=(_T,), replaced_nodeids=(_T,), replace_test_files=(), measured={_T: [(_OTHER, 10)]}
    )
    rows, restored = refresh.restoration_rows({_F: [(_T, 44), (_T, 45)]}, measurement, excluded_test_files=set())
    assert rows == [(_T, _F, 44), (_T, _F, 45)]
    assert restored == [_F]


def test_measure_diff_refresh_does_not_mutate_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([(_T, _F, 44)])

        def measure(_targets: list[str], **_kwargs: object) -> refresh.PerTestLines:
            return {_T: [(_OTHER, 10)]}

        monkeypatch.setattr(refresh, "measure_test_coverage", measure)
        measurement = refresh.measure_diff_refresh(
            [_F], [], store=store, context=_context(tmp_path, ScriptedLauncher())
        )
        assert measurement.src_nodeids == (_T,)
        assert measurement.replaced_nodeids == (_T,)
        assert measurement.measured == {_T: [(_OTHER, 10)]}
        assert store.tests_covering(_F, 44) == {_T}
        assert store.tests_covering(_OTHER, 10) == set()
    finally:
        store.close()


_U = "tests/app/bootstrap_other_test.py::test_other"
_REACHED = "src/app/dispatch.py"  # reached by _T only through _F


def test_file_changed_again_remeasures_tests_that_covered_an_earlier_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An edit of _F moved _T away while _U still covered it: the store no longer lists _T. When _F reverts, _T must be
    # re-measured too, or its coverage (of files reached through _F as well) would describe an edit that is gone.
    calls: list[list[str]] = []

    def measure(targets: list[str], **_kwargs: object) -> refresh.PerTestLines:
        calls.append(sorted(targets))
        return {_U: [(_F, 44)]} if _U in targets else {}

    monkeypatch.setattr(refresh, "measure_test_coverage", measure)
    store = Store(tmp_path / "c.sqlite")
    context = _context(tmp_path, ScriptedLauncher())
    try:
        store.ingest_coverage([(_T, _F, 44), (_T, _REACHED, 5), (_U, _F, 44)])
        first = refresh.measure_diff_refresh([_F], [], store=store, context=context)
        store.apply_test_coverage_refresh(
            measured=first.measured,
            measured_nodeids=first.replaced_nodeids,
            replace_test_files=(),
            restored_rows=(),
            file_hashes={},
        )
        assert store.tests_covering_file(_F) == {_U}
        second = refresh.measure_diff_refresh([_F], [], store=store, context=context)
    finally:
        store.close()
    assert calls == [sorted([_T, _U]), sorted([_T, _U])]
    assert second.replaced_nodeids == tuple(sorted([_T, _U]))


def test_changed_test_files_are_measured_by_file_not_by_node_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def measure(targets: list[str], **_kwargs: object) -> refresh.PerTestLines:
        calls.append(sorted(targets))
        return {}

    monkeypatch.setattr(refresh, "measure_test_coverage", measure)
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([("tests/a_test.py::test_a", _F, 1), ("tests/gone_test.py::test_g", _F, 2)])
        measurement = refresh.measure_diff_refresh(
            [_F],
            ["tests/a_test.py"],
            store=store,
            context=_context(tmp_path, ScriptedLauncher()),
            deleted_tests=["tests/gone_test.py"],
        )
    finally:
        store.close()
    assert calls == [["tests/a_test.py"]]
    assert measurement.replace_test_files == ("tests/a_test.py",)


def test_restore_recovers_bootstrap_file_wiped_by_refresh(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([(_T, _F, 44), (_T, _F, 45), (_T, _OTHER, 10)])
        snapshot = snapshot_src_coverage([_F], store=store)
        store.replace_test_coverage(_T, [(_OTHER, 10)])
        assert store.tests_covering_file(_F) == set()
        restored = restore_wiped_src(snapshot, set(), store=store)
        assert restored == [_F]
        assert store.tests_covering(_F, 44) == {_T}
        assert store.tests_covering(_F, 45) == {_T}
        assert store.tests_covering(_OTHER, 10) == {_T}
    finally:
        store.close()


def test_restore_skips_file_still_covered_after_refresh(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    other_test = "tests/app/producer_test.py::test_p"
    try:
        store.ingest_coverage([(_T, _F, 44), (other_test, _F, 45)])
        snapshot = snapshot_src_coverage([_F], store=store)
        store.replace_test_coverage(_T, [])
        assert restore_wiped_src(snapshot, set(), store=store) == []
        assert store.tests_covering(_F, 44) == set()
        assert store.tests_covering(_F, 45) == {other_test}
    finally:
        store.close()


def test_restore_skips_new_file_without_baseline_coverage(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([(_T, _OTHER, 10)])
        snapshot = snapshot_src_coverage([_F], store=store)
        assert snapshot == {_F: []}
        assert restore_wiped_src(snapshot, set(), store=store) == []
        assert store.tests_covering_file(_F) == set()
    finally:
        store.close()


def test_restore_excludes_deleted_and_remeasured_test_nodeids(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    changed_file = "tests/app/changed_test.py"
    changed_nodeid = f"{changed_file}::test_changed"
    untouched_nodeid = "tests/app/untouched_test.py::test_u"
    try:
        store.ingest_coverage([(changed_nodeid, _F, 44), (untouched_nodeid, _F, 44)])
        snapshot = snapshot_src_coverage([_F], store=store)
        store.replace_test_coverage(changed_nodeid, [])
        store.replace_test_coverage(untouched_nodeid, [])
        assert restore_wiped_src(snapshot, {changed_file}, store=store) == [_F]
        assert store.tests_covering(_F, 44) == {untouched_nodeid}
    finally:
        store.close()


def test_restore_all_coverage_from_excluded_tests_stays_uncovered(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    deleted_file = "tests/app/gone_test.py"
    try:
        store.ingest_coverage([(f"{deleted_file}::test_gone", _F, 44)])
        snapshot = snapshot_src_coverage([_F], store=store)
        store.replace_test_coverage(f"{deleted_file}::test_gone", [])
        assert restore_wiped_src(snapshot, {deleted_file}, store=store) == []
        assert store.tests_covering_file(_F) == set()
    finally:
        store.close()


def test_refresh_tests_replaces_whole_test_files_after_measuring_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confirm loop: edited test files are measured first, then their old tests leave the store at once."""
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage(
            [
                ("tests/a_test.py::test_gone", "src/x.py", 1),
                ("tests/a_test.py::test_kept", "src/x.py", 2),
                ("tests/b_test.py::test_b", "src/x.py", 3),
            ]
        )
        requested: list[list[str]] = []

        def measure(targets: list[str], **_kwargs: object) -> refresh.PerTestLines:
            requested.append(targets)
            return {"tests/a_test.py::test_kept": [("src/x.py", 5)], "tests/a_test.py::test_new": [("src/x.py", 6)]}

        monkeypatch.setattr(refresh, "measure_test_coverage", measure)

        measured = refresh.refresh_tests(
            ["tests/a_test.py"], store=store, context=_context(tmp_path, ScriptedLauncher())
        )

        assert measured == 2
        assert requested == [["tests/a_test.py"]]
        assert store.tests_covering_file("src/x.py") == {
            "tests/a_test.py::test_kept",
            "tests/a_test.py::test_new",
            "tests/b_test.py::test_b",
        }
        assert store.tests_covering("src/x.py", 2) == set()
    finally:
        store.close()


def test_refresh_tests_leaves_the_store_alone_when_measuring_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([("tests/a_test.py::test_a", "src/x.py", 1)])

        def failing(_targets: list[str], **_kwargs: object) -> refresh.PerTestLines:
            raise refresh.RefreshBaselineError(("tests/a_test.py::test_a",), detail="red")

        monkeypatch.setattr(refresh, "measure_test_coverage", failing)

        with pytest.raises(refresh.RefreshBaselineError):
            refresh.refresh_tests(["tests/a_test.py"], store=store, context=_context(tmp_path, ScriptedLauncher()))
        assert store.tests_covering("src/x.py", 1) == {"tests/a_test.py::test_a"}
    finally:
        store.close()


def test_log_tail_of_a_missing_log_is_empty(tmp_path: Path) -> None:
    assert refresh._log_tail(tmp_path / "missing.log") == ""


def test_log_tail_lines_is_pinned() -> None:
    importlib.reload(refresh)
    assert refresh._LOG_TAIL_LINES == 40


def test_targets_note_reports_a_write_failure(tmp_path: Path) -> None:
    scratch = tmp_path / "missing"
    note = refresh._targets_note(scratch, "stem", ["tests/x_test.py"])
    assert note.startswith("the full target list could not be saved:")


def test_targets_note_writes_with_two_space_indent(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    refresh._targets_note(scratch, "stem", ["tests/x_test.py"])
    assert (scratch / "stem.targets.json").read_text(encoding="utf-8").startswith('{\n  "target_count": 1,')


def test_targets_note_escapes_non_ascii(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    refresh._targets_note(scratch, "stem", ["tests/té_test.py"])
    assert "tests/t\\u00e9_test.py" in (scratch / "stem.targets.json").read_text(encoding="utf-8")


def test_targets_summary_lists_small_targets_in_full() -> None:
    summary = refresh._targets_summary([f"tests/t{i}_test.py" for i in range(6)])
    assert summary.startswith("6 targets; sample: [")
    assert "..." not in summary


def test_targets_summary_samples_seven_targets() -> None:
    summary = refresh._targets_summary([f"tests/t{i}_test.py" for i in range(7)])
    assert "..." in summary


def test_targets_summary_samples_first_and_last_three() -> None:
    summary = refresh._targets_summary([f"tests/t{i}_test.py" for i in range(10)])
    assert '"tests/t2_test.py", "...", "tests/t7_test.py"' in summary


def test_targets_summary_escapes_non_ascii() -> None:
    assert refresh._targets_summary(["tests/té_test.py"]) == '1 targets; sample: ["tests/t\\u00e9_test.py"]'


def test_baseline_error_names_ten_and_counts_the_rest() -> None:
    nodeids = tuple(f"tests/t{i}_test.py::test_t" for i in range(11))
    error = refresh.RefreshBaselineError(nodeids, detail="d")
    assert str(error) == f"coverage refresh: failing tests {', '.join(nodeids[:10])} (+1 more); d"


def test_baseline_error_with_ten_nodeids_lists_all_without_suffix() -> None:
    nodeids = tuple(f"tests/t{i}_test.py::test_t" for i in range(10))
    error = refresh.RefreshBaselineError(nodeids, detail="d")
    assert str(error) == f"coverage refresh: failing tests {', '.join(nodeids)}; d"


def test_remove_run_artifacts_keeps_only_log_suffixes(tmp_path: Path) -> None:
    data_file = tmp_path / "data.coverage"
    (tmp_path / "data.coverage.slot0.log").write_text("log", encoding="utf-8")
    (tmp_path / "data.coverage.extra").write_text("extra", encoding="utf-8")

    refresh._remove_run_artifacts(data_file, keep_logs=True)

    assert (tmp_path / "data.coverage.slot0.log").exists()
    assert not (tmp_path / "data.coverage.extra").exists()


def test_remove_run_artifacts_continues_past_kept_logs(tmp_path: Path) -> None:
    data_file = tmp_path / "data.coverage"
    (tmp_path / "data.coverage.a.log").write_text("log", encoding="utf-8")
    (tmp_path / "data.coverage.b.extra").write_text("extra", encoding="utf-8")

    refresh._remove_run_artifacts(data_file, keep_logs=True)

    assert (tmp_path / "data.coverage.a.log").exists()
    assert not (tmp_path / "data.coverage.b.extra").exists()


def test_remove_run_artifacts_tolerates_a_missing_file(tmp_path: Path) -> None:
    class _Parent:
        def glob(self, _pattern: str) -> object:
            return iter([tmp_path / "data.coverage.ghost"])  # never created

    data_file = SimpleNamespace(parent=_Parent(), name="data.coverage")
    refresh._remove_run_artifacts(cast("Path", data_file), keep_logs=False)


def test_measure_tolerates_an_existing_scratch_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 1)
    context = _context(tmp_path, ScriptedLauncher())
    refresh.measure_test_coverage(["tests/x_test.py"], context=context)
    refresh.measure_test_coverage(["tests/x_test.py"], context=context)


def test_measure_treats_failed_exit_without_nodeids_as_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 6)
    launcher = ScriptedLauncher(returncode=1, failed=())
    with pytest.raises(RuntimeError) as error:
        refresh.measure_test_coverage(["tests/x_test.py"], context=_context(tmp_path, launcher))
    assert not isinstance(error.value, refresh.RefreshBaselineError)


def test_infrastructure_failure_keeps_the_run_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 6)
    launcher = ScriptedLauncher(returncode=3, write_shards=False, log_text="boom\n")
    with pytest.raises(RuntimeError):
        refresh.measure_test_coverage(["tests/x_test.py"], context=_context(tmp_path, launcher))
    assert [path for path in _scratch(tmp_path) if path.name.endswith(".log")]


def test_restoration_rows_skips_empty_files_and_continues() -> None:
    measurement = refresh.RefreshMeasurement(
        src_nodeids=(_T,), replaced_nodeids=(_T,), replace_test_files=(), measured={}
    )
    snapshot = {"empty": [], _F: [(_T, 44)]}
    rows, restored = refresh.restoration_rows(snapshot, measurement, excluded_test_files=set())
    assert rows == [(_T, _F, 44)]
    assert restored == [_F]


def test_restoration_rows_considers_a_file_retained_when_any_nodeid_survives() -> None:
    measurement = refresh.RefreshMeasurement(
        src_nodeids=(_T,), replaced_nodeids=("tests/gone_test.py::test_gone",), replace_test_files=(), measured={}
    )
    snapshot = {_F: [("tests/gone_test.py::test_gone", 1), (_T, 2)]}
    _rows, restored = refresh.restoration_rows(snapshot, measurement, excluded_test_files=set())
    assert restored == []


def test_log_tail_tolerates_invalid_utf8(tmp_path: Path) -> None:
    log = tmp_path / "slot.log"
    log.write_bytes(b"ok\nbad\xff\n")
    assert refresh._log_tail(log) == "ok\nbad\ufffd"


def test_measure_passes_ordered_targets_as_a_tuple(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 1)
    seen: dict[str, object] = {}

    def fake_measure(request: measure.MeasureRequest, *, isolation: object, launcher: object) -> measure.MeasureResult:
        seen["targets"] = request.targets
        return measure.MeasureResult(0, tmp_path / "cov", tmp_path / "cov.costs.json", tmp_path / "cov.failures.json")

    def fake_load(path: Path, root: Path | None = None) -> SimpleNamespace:
        return SimpleNamespace(files=list)

    monkeypatch.setattr(refresh, "measure_coverage", fake_measure)
    monkeypatch.setattr(refresh, "load_coverage_map", fake_load)
    context = _context(tmp_path, ScriptedLauncher())
    refresh.measure_test_coverage(["tests/b_test.py", "tests/a_test.py"], context=context)
    assert seen["targets"] == ("tests/a_test.py", "tests/b_test.py")
    assert type(seen["targets"]) is tuple


def test_measure_relativizes_against_the_profile_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh, "resolve_workers", lambda: 1)
    seen: dict[str, object] = {}

    def fake_measure(request: object, *, isolation: object, launcher: object) -> measure.MeasureResult:
        return measure.MeasureResult(0, tmp_path / "cov", tmp_path / "cov.costs.json", tmp_path / "cov.failures.json")

    def fake_load(path: Path, root: Path | None = None) -> object:
        seen["root"] = root
        return SimpleNamespace(files=list)

    monkeypatch.setattr(refresh, "measure_coverage", fake_measure)
    monkeypatch.setattr(refresh, "load_coverage_map", fake_load)
    context = _context(tmp_path, ScriptedLauncher())
    refresh.measure_test_coverage(["tests/a_test.py"], context=context)
    assert seen["root"] == context.profile.root


def test_refresh_tests_forwards_the_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def measure(
        targets: list[str],
        *,
        context: MeasureContext,
        workers: int | None = None,
        costs: object = None,
    ) -> dict[str, list[tuple[str, int]]]:
        seen["context"] = context
        return {}

    monkeypatch.setattr(refresh, "measure_test_coverage", measure)
    store = Store(tmp_path / "c.sqlite")
    try:
        context = _context(tmp_path, ScriptedLauncher())
        refresh.refresh_tests(["tests/a_test.py"], store=store, context=context)
    finally:
        store.close()
    assert seen["context"] is context


def test_measure_diff_refresh_forwards_context_and_costs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([(_T, _F, 44)])
        seen: dict[str, object] = {}

        def measure(
            targets: list[str], *, context: MeasureContext, costs: object = None
        ) -> dict[str, list[tuple[str, int]]]:
            seen["context"] = context
            seen["costs"] = costs
            return {}

        monkeypatch.setattr(refresh, "measure_test_coverage", measure)
        context = _context(tmp_path, ScriptedLauncher())
        refresh.measure_diff_refresh([_F], [], store=store, context=context)
        assert seen["context"] is context
        assert seen["costs"] == {}
    finally:
        store.close()


def test_restoration_rows_restores_a_file_when_only_excluded_rows_survive() -> None:
    measurement = refresh.RefreshMeasurement(
        src_nodeids=(_T,), replaced_nodeids=(_T,), replace_test_files=(), measured={}
    )
    snapshot = {_F: [(_T, 44), ("tests/app/excluded_test.py::test_e", 45)]}
    rows, restored = refresh.restoration_rows(snapshot, measurement, excluded_test_files={"tests/app/excluded_test.py"})
    assert rows == [(_T, _F, 44)]
    assert restored == [_F]


def test_restoration_rows_skips_a_file_that_keeps_any_coverage() -> None:
    measurement = refresh.RefreshMeasurement(src_nodeids=(_T,), replaced_nodeids=(), replace_test_files=(), measured={})
    snapshot = {_F: [(_T, 44)]}
    _rows, restored = refresh.restoration_rows(snapshot, measurement, excluded_test_files=set())
    assert restored == []


def test_restoration_rows_continues_past_a_retained_file() -> None:
    measurement = refresh.RefreshMeasurement(
        src_nodeids=(_T,), replaced_nodeids=(_T,), replace_test_files=(), measured={}
    )
    snapshot = {"kept": [("tests/kept_test.py::test_k", 1)], _F: [(_T, 44)]}
    _rows, restored = refresh.restoration_rows(snapshot, measurement, excluded_test_files=set())
    assert restored == [_F]


def test_restoration_rows_keeps_only_non_excluded_test_files() -> None:
    measurement = refresh.RefreshMeasurement(
        src_nodeids=(_T,), replaced_nodeids=(_T,), replace_test_files=(), measured={}
    )
    snapshot = {_F: [("tests/app/excluded_test.py::test_e", 45)]}
    _rows, restored = refresh.restoration_rows(
        snapshot, measurement, excluded_test_files={"tests/app/excluded_test.py"}
    )
    assert restored == []


def test_restore_continues_past_a_still_covered_file(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([(_T, _F, 44), (_U, _OTHER, 10)])
        store.replace_test_coverage(_T, [])
        snapshot = {_OTHER: [(_U, 10)], _F: [(_T, 44)]}
        assert restore_wiped_src(snapshot, set(), store=store) == [_F]
    finally:
        store.close()


def test_restore_continues_past_an_all_excluded_file(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    excluded = "tests/app/excluded_test.py"
    try:
        store.ingest_coverage([(_T, _F, 44), (f"{excluded}::test_e", _OTHER, 10)])
        store.replace_test_coverage(_T, [])
        store.replace_test_coverage(f"{excluded}::test_e", [])
        snapshot = {_OTHER: [(f"{excluded}::test_e", 10)], _F: [(_T, 44)]}
        assert restore_wiped_src(snapshot, {excluded}, store=store) == [_F]
    finally:
        store.close()
