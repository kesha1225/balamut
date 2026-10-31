"""Refresh keeps the substrate correct outside the diff too (balamut.coverage.incremental).

A substrate describes the tree it was measured from. After a rebase or a seed from the shared pool, files outside the
diff diverge from it, and a full rebuild costs a whole suite run under coverage. Refresh re-measures only the
divergence, like an edit inside the diff: tests that covered a changed or removed source file, and changed, new or
removed test files. Only the diff is mutated.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest
from coverage import CoverageData

from balamut.config import Profile
from balamut.coverage import incremental, substrate
from balamut.coverage import refresh as coverage_refresh
from balamut.coverage.refresh import MeasureContext
from balamut.coverage.store import Store
from balamut.diff.scope import MISSING_FILE_HASH, DiffScope
from balamut.isolation import NoIsolation
from bm_support import make_layout, write_file
from bm_support.substrates import expected_substrate_metadata, write_valid_coverage


def _context(root: Path) -> MeasureContext:
    return MeasureContext(
        profile=Profile(layout=make_layout(root), state_dir=root / ".balamut"), isolation=NoIsolation()
    )


def _prepare_diff(base: str, **kwargs: Any) -> list[str]:
    root = Path.cwd()
    return incremental.prepare_diff(base, context=_context(root), **kwargs)


def _scope(paths: list[Path], repo_root: Path) -> DiffScope:
    file_hashes = {
        path.relative_to(repo_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    }
    return DiffScope(
        base_ref="origin/main",
        base_sha="base-sha",
        diff_base_sha="diff-base-sha",
        head_sha="head-sha",
        changed_lines=dict.fromkeys(file_hashes, ()),
        file_hashes=file_hashes,
    )


_X = "tests/x_test.py::test_x"
_Y = "tests/y_test.py::test_y"

type _Calls = list[tuple[list[str], list[str]]]


def _build(path: Path) -> int:
    """The build substrate: src/x.py covered by test_x, src/y.py by test_y."""
    data = CoverageData(basename=str(path))
    data.set_context(f"{_X}|run")
    data.add_lines({"src/x.py": {1}})
    data.set_context(f"{_Y}|run")
    data.add_lines({"src/y.py": {1}})
    data.write()
    return 0


def _tree(root: Path) -> None:
    write_file(root / "src/x.py", "X = 1\n")
    write_file(root / "src/y.py", "Y = 1\n")
    write_file(root / "tests/x_test.py", "def test_x():\n    assert True\n")
    write_file(root / "tests/y_test.py", "def test_y():\n    assert True\n")


def _substrate(root: Path, *, tree_recorded: bool = True) -> Path:
    coverage = root / ".coverage"
    metadata = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=_build)
    if tree_recorded:
        substrate.tree_write(coverage, metadata.generation, substrate.tree_snapshot(make_layout(root)))
    return coverage


def _recording_refresh(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """A re-measured test file returns coverage like a real one: an empty store would re-ingest the substrate."""
    calls: _Calls = []

    def fake_refresh(
        src_files: list[str],
        test_files: list[str],
        *,
        store: Store,
        context: MeasureContext,
        deleted_tests: Collection[str] = (),
    ) -> coverage_refresh.RefreshMeasurement:
        _ = (store, context)
        calls.append((sorted(src_files), sorted(test_files)))
        return coverage_refresh.RefreshMeasurement(
            src_nodeids=(),
            replaced_nodeids=(),
            replace_test_files=tuple(test_files),
            measured={f"{test_file}::test_refreshed": [("src/x.py", 1)] for test_file in test_files},
        )

    monkeypatch.setattr(incremental, "measure_diff_refresh", fake_refresh)
    return calls


def _prepare(root: Path, coverage: Path, store: Store, *scope_files: str) -> list[str]:
    scope = _scope([root / path for path in scope_files], root)
    return _prepare_diff("origin/main", cov_path=coverage, store=store, scope=scope)


def test_src_changed_outside_the_diff_is_refreshed_once_and_not_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "src/y.py", "Y = 2\n")
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        assert _prepare(tmp_path, coverage, store, "src/x.py") == ["src/x.py"]
        assert _prepare(tmp_path, coverage, store, "src/x.py") == ["src/x.py"]
    finally:
        store.close()

    assert calls == [(["src/x.py", "src/y.py"], [])]


def test_test_files_changed_added_or_removed_outside_the_diff_are_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 == 1\n")
    write_file(tmp_path / "tests/z_test.py", "def test_z():\n    assert True\n")
    (tmp_path / "tests/y_test.py").unlink()
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare(tmp_path, coverage, store)
        _prepare(tmp_path, coverage, store)
        removed_test_coverage = store.tests_covering("src/y.py", 1)
    finally:
        store.close()

    assert calls == [([], ["tests/x_test.py", "tests/z_test.py"])]
    assert removed_test_coverage == set()


def test_file_reverted_after_its_refresh_is_refreshed_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store remembers the version it re-measured, not the build version: a revert to the build tree diverges
    too."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        write_file(tmp_path / "src/y.py", "Y = 2\n")
        _prepare(tmp_path, coverage, store)
        write_file(tmp_path / "src/y.py", "Y = 1\n")
        _prepare(tmp_path, coverage, store)
    finally:
        store.close()

    assert calls == [(["src/y.py"], []), (["src/y.py"], [])]


def test_file_that_left_the_diff_is_measured_as_it_is_now(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit measured while the file was in the diff, then reverted: the store describes an edit that is gone."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        write_file(tmp_path / "src/x.py", "X = 2\n")
        _prepare(tmp_path, coverage, store, "src/x.py")
        write_file(tmp_path / "src/x.py", "X = 1\n")
        _prepare(tmp_path, coverage, store)
    finally:
        store.close()

    assert calls == [(["src/x.py"], []), (["src/x.py"], [])]


def test_vanished_source_refreshes_the_tests_that_covered_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code of a removed file may have moved elsewhere: the tests that covered it now cover the new place."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    (tmp_path / "src/y.py").unlink()
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare(tmp_path, coverage, store)
        _prepare(tmp_path, coverage, store)
    finally:
        store.close()

    assert calls == [(["src/y.py"], [])]


@pytest.mark.parametrize("in_diff", [False, True], ids=["outside-the-diff", "inside-the-diff"])
def test_source_removed_with_its_test_file_does_not_ask_for_the_removed_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_diff: bool
) -> None:
    """Both a source file and the test file covering it are gone: asking for that node id would fail the run. The
    store drops the pairs of the removed file in the same publication."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    (tmp_path / "src/y.py").unlink()
    (tmp_path / "tests/y_test.py").unlink()
    requested: list[list[str]] = []

    def measure(targets: list[str], **_kwargs: object) -> coverage_refresh.PerTestLines:
        requested.append(sorted(targets))
        return {}

    monkeypatch.setattr(coverage_refresh, "measure_test_coverage", measure)
    removed: dict[str, str] = dict.fromkeys(("src/y.py", "tests/y_test.py") if in_diff else (), MISSING_FILE_HASH)
    lines: dict[str, tuple[int, ...]] = dict.fromkeys(removed, ())
    scope = DiffScope("origin/main", "base-sha", "diff-base-sha", "head-sha", lines, removed)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=scope)
        remaining = store.tests_seen_covering_file("src/y.py")
    finally:
        store.close()

    assert requested == []
    assert remaining == set()


@pytest.mark.parametrize("in_diff", [False, True], ids=["outside-the-diff", "inside-the-diff"])
def test_source_the_build_never_saw_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_diff: bool) -> None:
    """A new source file cannot be refreshed: coverage does not know the tests reaching it through a registry or
    autodiscovery, in the diff or not. The orchestrator rebuilds such a substrate; a direct call refuses rather than
    marking the file measured with empty coverage."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "src/z.py", "def h() -> int:\n    return 4\n")
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(substrate.SubstrateCompatibilityError, match=r"src/z\.py"):
            _prepare(tmp_path, coverage, store, *(["src/z.py"] if in_diff else []))
    finally:
        store.close()

    assert calls == []


def test_reexported_definitions_changed_outside_the_diff_refresh_their_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pytest collects a defining file's tests through its wrapper: the wrapper is re-measured, the file itself runs
    nothing."""
    _tree(tmp_path)
    write_file(tmp_path / "tests/cases/shared_cases.py", "def test_case():\n    assert True\n")
    write_file(tmp_path / "tests/cases/cases_test.py", "from tests.cases.shared_cases import *\n")
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "tests/cases/shared_cases.py", "def test_case():\n    assert 1 == 1\n")
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare(tmp_path, coverage, store)
        _prepare(tmp_path, coverage, store)
    finally:
        store.close()

    assert calls == [([], ["tests/cases/cases_test.py"])]


def test_undermeasured_drift_source_keeps_its_baseline_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh may under-measure a bootstrap-imported module to zero lines: a file outside the diff gets its baseline
    back like a scope file, or its next edit would get a false no-coverage."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "src/y.py", "Y = 2\n")

    def undermeasuring_refresh(
        src_files: list[str],
        test_files: list[str],
        *,
        store: Store,
        context: MeasureContext,
        deleted_tests: Collection[str] = (),
    ) -> coverage_refresh.RefreshMeasurement:
        _ = (src_files, test_files, store, context)
        return coverage_refresh.RefreshMeasurement(
            src_nodeids=(_Y,), replaced_nodeids=(_Y,), replace_test_files=(), measured={}
        )

    monkeypatch.setattr(incremental, "measure_diff_refresh", undermeasuring_refresh)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare(tmp_path, coverage, store)
        covering = store.tests_covering("src/y.py", 1)
    finally:
        store.close()

    assert covering == {_Y}


def _reexported_cases(root: Path) -> None:
    write_file(root / "tests/cases/shared_cases.py", "def test_case():\n    assert True\n")
    write_file(root / "tests/cases/cases_test.py", "from tests.cases.shared_cases import *\n")


@pytest.mark.parametrize("in_diff", [False, True], ids=["outside-the-diff", "inside-the-diff"])
def test_definition_file_that_stopped_exporting_tests_refreshes_its_recorded_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_diff: bool
) -> None:
    """A defining file lost its last test: the current re-export map no longer knows it while the build's wrapper
    keeps its node ids in the store. The wrapper comes from the recorded tree, or the run would ask for missing
    tests."""
    _tree(tmp_path)
    _reexported_cases(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "tests/cases/shared_cases.py", "def helper() -> int:\n    return 1\n")
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare(tmp_path, coverage, store, *(["tests/cases/shared_cases.py"] if in_diff else []))
    finally:
        store.close()

    assert calls == [([], ["tests/cases/cases_test.py"])]


def _refresh_tree(coverage: Path, store: Store) -> int:
    return incremental.refresh_tree(cov_path=coverage, store=store, context=_context(Path.cwd()))


def test_a_direct_refresh_remeasures_every_divergence_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct campaign has no diff: every file diverged from the build tree is refreshed, a no-op rerun is free."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "src/y.py", "Y = 2\n")
    write_file(tmp_path / "tests/z_test.py", "def test_z():\n    assert True\n")
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        first = _refresh_tree(coverage, store)
        second = _refresh_tree(coverage, store)
        refreshed = store.tests_covering("src/x.py", 1)
    finally:
        store.close()

    assert calls == [(["src/y.py"], ["tests/z_test.py"])]
    assert (first, second) == (2, 0)
    assert "tests/z_test.py::test_refreshed" in refreshed


def test_a_direct_refresh_without_a_tree_record_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the build tree a divergence is unprovable: the caller rebuilds instead of trusting the substrate."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path, tree_recorded=False)
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(substrate.SubstrateCompatibilityError, match="tree record"):
            _refresh_tree(coverage, store)
    finally:
        store.close()

    assert calls == []


def test_substrate_without_a_tree_record_refreshes_only_the_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a tree record the divergence is unprovable: the orchestrator rebuilds such a substrate, and a direct
    call refreshes only the diff."""
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path, tree_recorded=False)
    write_file(tmp_path / "src/y.py", "Y = 2\n")
    calls = _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare(tmp_path, coverage, store, "src/x.py")
    finally:
        store.close()

    assert calls == [(["src/x.py"], [])]


def test_direct_refresh_without_metadata_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(substrate.SubstrateCompatibilityError, match="no valid metadata"):
            incremental.refresh_tree(cov_path=coverage, store=store, context=_context(tmp_path))
    finally:
        store.close()


def test_direct_refresh_records_timing_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "src/y.py", "Y = 2\n")
    _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    timings: dict[str, float] = {}
    try:
        incremental.refresh_tree(cov_path=coverage, store=store, context=_context(tmp_path), phase_timings=timings)
    finally:
        store.close()
    assert "staleness_selection" in timings
    assert "coverage refresh:" in capsys.readouterr().err


def test_undermeasured_drift_reports_restoration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "src/y.py", "Y = 2\n")

    def undermeasuring_refresh(
        src_files: list[str],
        test_files: list[str],
        *,
        store: Store,
        context: MeasureContext,
        deleted_tests: Collection[str] = (),
    ) -> coverage_refresh.RefreshMeasurement:
        _ = (src_files, test_files, store, context)
        return coverage_refresh.RefreshMeasurement(
            src_nodeids=(_Y,), replaced_nodeids=(_Y,), replace_test_files=(), measured={}
        )

    monkeypatch.setattr(incremental, "measure_diff_refresh", undermeasuring_refresh)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare(tmp_path, coverage, store)
    finally:
        store.close()
    assert "would have been left without coverage" in capsys.readouterr().err


def test_diff_summary_counts_vanished_and_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(tmp_path)
    write_file(tmp_path / "src/z.py", "Z = 1\n")
    monkeypatch.chdir(tmp_path)
    coverage = _substrate(tmp_path)
    write_file(tmp_path / "src/y.py", "Y = 2\n")
    (tmp_path / "src/z.py").unlink()
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert 1 == 1\n")
    (tmp_path / "tests/y_test.py").unlink()
    _recording_refresh(monkeypatch)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare(tmp_path, coverage, store)
    finally:
        store.close()
    err = capsys.readouterr().err
    assert "diverged outside the diff: source 2," in err
    assert "test 2" in err
