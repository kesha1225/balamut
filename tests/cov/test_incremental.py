"""Generation-aware preparation of incremental coverage (balamut.coverage.incremental)."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

import pytest
from coverage import CoverageData

from balamut.config import Profile
from balamut.coverage import costs as pytest_costs
from balamut.coverage import incremental, substrate
from balamut.coverage import refresh as coverage_refresh
from balamut.coverage.coverage_map import CoverageMap
from balamut.coverage.refresh import MeasureContext
from balamut.coverage.store import Store
from balamut.diff.scope import DiffScope
from balamut.isolation import NoIsolation
from bm_support import make_layout
from bm_support.substrates import expected_substrate_metadata, write_valid_coverage


def _context(root: Path) -> MeasureContext:
    return MeasureContext(
        profile=Profile(layout=make_layout(root), state_dir=root / ".balamut"), isolation=NoIsolation()
    )


def _prepare_diff(base: str, **kwargs: Any) -> list[str]:
    """``prepare_diff`` on the current directory's tree (every test chdirs into its tmp tree)."""
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


def test_prepare_diff_rejects_missing_substrate_metadata_in_scoped_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(substrate.SubstrateCompatibilityError):
            _prepare_diff(
                "origin/main",
                cov_path=coverage,
                store=store,
                scope=_scope([], tmp_path),
            )
        assert not store.has_coverage()
    finally:
        store.close()


def test_prepare_diff_accepts_legacy_coverage_for_direct_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    monkeypatch.chdir(tmp_path)

    def no_changed(_base: str, _root: Path) -> list[str]:
        return []

    monkeypatch.setattr(incremental, "_git_changed", no_changed)
    store = Store(tmp_path / "cache.sqlite")
    try:
        assert _prepare_diff("origin/main", cov_path=coverage, store=store) == []
        assert store.coverage_generation() == "legacy"
        assert store.tests_covering("src/x.py", 1) == {"tests/x_test.py::test_x"}
    finally:
        store.close()


def test_prepare_diff_replaces_coverage_only_for_new_substrate_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    metadata = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    store = Store(tmp_path / "cache.sqlite")
    try:
        store.coverage_replace(
            [("tests/old_test.py::test_old", "src/old.py", 7)],
            generation="old-generation",
            substrate_fingerprint="substrate-old",
        )
        store.put_file_hash("src/old.py", "hash", "src")

        _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([], tmp_path),
        )

        assert store.coverage_generation() == metadata.generation
        assert store.tests_covering("src/old.py", 7) == set()
        assert store.tests_covering("src/x.py", 1) == {"tests/x_test.py::test_x"}
        assert store.get_file_hashes("src") == {}

        store.put_file_hash("src/current.py", "hash", "src")
        _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([], tmp_path),
        )
        assert store.get_file_hashes("src") == {"src/current.py": "hash"}
    finally:
        store.close()


def _support_tree(tmp_path: Path) -> Path:
    """A helper, a test file importing it and a test file indifferent to it."""
    helper = tmp_path / "tests/support/helper.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/x_test.py").write_text(
        "from tests.support.helper import VALUE\n\ndef test_x():\n    assert VALUE\n", encoding="utf-8"
    )
    (tmp_path / "tests/y_test.py").write_text("def test_y():\n    assert True\n", encoding="utf-8")
    return helper


@pytest.mark.parametrize("in_scope", [True, False], ids=["inside-the-diff", "outside-the-diff"])
def test_support_change_refreshes_only_the_tests_that_import_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_scope: bool
) -> None:
    """A support edit re-measures the test files importing it instead of rebuilding the substrate.

    The substrate holds no coverage of support code, so the files executing it are re-measured and an indifferent
    test file stays as it was. A repeat with the same helper content no longer refreshes: the store remembers it."""
    helper = _support_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(
        coverage,
        expected_substrate_metadata(),
        build=write_valid_coverage,
        tree=lambda: substrate.tree_snapshot(make_layout(tmp_path)),
    )
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    calls: list[tuple[list[str], list[str]]] = []
    # Re-measuring x_test yields the substrate's coverage: the store neither empties nor gets re-ingested.
    remeasured = {"tests/x_test.py::test_x": [("src/x.py", 1)]}
    monkeypatch.setattr(incremental, "measure_diff_refresh", _recording_refresh(calls, measured=remeasured))
    scope = _scope([helper] if in_scope else [], tmp_path)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=scope)
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=scope)

        assert calls == [([], ["tests/x_test.py"])]
        assert store.get_file_hashes("support") == {
            "tests/support/helper.py": hashlib.sha256(helper.read_bytes()).hexdigest()
        }
    finally:
        store.close()


def test_noop_rerun_after_substrate_build_hash_skips_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src/x.py"
    test_file = tmp_path / "tests/x_test.py"
    source.parent.mkdir()
    test_file.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_file.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    scope = _scope([source, test_file], tmp_path)
    refresh_calls: list[tuple[list[str], list[str]]] = []

    def fake_refresh(
        src_files: list[str],
        test_files: list[str],
        *,
        store: Store,
        context: MeasureContext,
        deleted_tests: Collection[str] = (),
    ) -> coverage_refresh.RefreshMeasurement:
        _ = (store, context)
        refresh_calls.append((src_files, test_files))
        nodeid = "tests/x_test.py::test_x"
        return coverage_refresh.RefreshMeasurement(
            src_nodeids=(nodeid,),
            replaced_nodeids=(),
            replace_test_files=tuple(test_files),
            measured={nodeid: [("src/x.py", 1)]},
        )

    monkeypatch.setattr(incremental, "measure_diff_refresh", fake_refresh)
    store = Store(tmp_path / "cache.sqlite")
    try:
        assert _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=scope,
        ) == ["src/x.py"]
        assert _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=scope,
        ) == ["src/x.py"]

        assert refresh_calls == [(["src/x.py"], ["tests/x_test.py"])]
        assert store.get_file_hashes("src") == {"src/x.py": hashlib.sha256(source.read_bytes()).hexdigest()}
        assert store.get_file_hashes("test") == {"tests/x_test.py": hashlib.sha256(test_file.read_bytes()).hexdigest()}
    finally:
        store.close()


def _source_and_test(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "src/x.py"
    test_file = tmp_path / "tests/x_test.py"
    source.parent.mkdir()
    test_file.parent.mkdir()
    source.write_text("def f(value: int) -> int:\n    return value + 1\n", encoding="utf-8")
    test_file.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    return source, test_file


def _recording_refresh(
    calls: list[tuple[list[str], list[str]]],
    *,
    measured: coverage_refresh.PerTestLines | None = None,
) -> Callable[..., coverage_refresh.RefreshMeasurement]:
    """Record what was re-measured. ``measured`` is what a real run would return: without it a re-measured test file
    would leave the store empty and the next call would re-ingest the substrate."""

    def fake_refresh(
        src_files: list[str],
        test_files: list[str],
        *,
        store: Store,
        context: MeasureContext,
        deleted_tests: Collection[str] = (),
    ) -> coverage_refresh.RefreshMeasurement:
        _ = (store, context)
        calls.append((src_files, test_files))
        return coverage_refresh.RefreshMeasurement(
            src_nodeids=(),
            replaced_nodeids=(),
            replace_test_files=tuple(test_files),
            measured=dict(measured or {}),
        )

    return fake_refresh


def test_fresh_generation_vouches_for_unchanged_scope_files_without_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingesting a new generation erases hashes; without vouching, refresh would re-measure the whole diff on the very
    tree the substrate was just measured from."""
    source, test_file = _source_and_test(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    metadata = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    calls: list[tuple[list[str], list[str]]] = []
    monkeypatch.setattr(incremental, "measure_diff_refresh", _recording_refresh(calls))
    store = Store(tmp_path / "cache.sqlite")
    try:
        paths = _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([source, test_file], tmp_path),
            fresh_generation=metadata.generation,
        )

        assert paths == ["src/x.py"]
        assert calls == []
        assert store.coverage_generation() == metadata.generation
        assert store.get_file_hashes("src") == {"src/x.py": hashlib.sha256(source.read_bytes()).hexdigest()}
        assert store.get_file_hashes("test") == {"tests/x_test.py": hashlib.sha256(test_file.read_bytes()).hexdigest()}
    finally:
        store.close()


def test_fresh_generation_does_not_vouch_for_file_edited_after_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file edited after the scope was pinned may have been measured in either version: it is re-measured."""
    source, test_file = _source_and_test(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    metadata = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    scope = _scope([source, test_file], tmp_path)
    source.write_text("def f(value: int) -> int:\n    return value + 2\n", encoding="utf-8")
    calls: list[tuple[list[str], list[str]]] = []
    monkeypatch.setattr(incremental, "measure_diff_refresh", _recording_refresh(calls))
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=scope,
            fresh_generation=metadata.generation,
        )

        assert calls == [(["src/x.py"], [])]
    finally:
        store.close()


def test_fresh_generation_of_another_substrate_vouches_for_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, test_file = _source_and_test(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    calls: list[tuple[list[str], list[str]]] = []
    monkeypatch.setattr(incremental, "measure_diff_refresh", _recording_refresh(calls))
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([source, test_file], tmp_path),
            fresh_generation="generation-built-elsewhere",
        )

        assert calls == [(["src/x.py"], ["tests/x_test.py"])]
    finally:
        store.close()


def test_fresh_generation_already_ingested_is_not_reseeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag concerns this run's ingest: a generation already in the store knows its own hashes."""
    source, test_file = _source_and_test(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    metadata = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    calls: list[tuple[list[str], list[str]]] = []
    monkeypatch.setattr(incremental, "measure_diff_refresh", _recording_refresh(calls))
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=_scope([], tmp_path))
        _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([source, test_file], tmp_path),
            fresh_generation=metadata.generation,
        )

        assert calls == [(["src/x.py"], ["tests/x_test.py"])]
    finally:
        store.close()


def test_prepare_diff_publishes_coverage_and_hashes_in_one_store_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src/x.py"
    test_file = tmp_path / "tests/x_test.py"
    source.parent.mkdir()
    test_file.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_file.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)

    def measure(_targets: list[str], **_kwargs: object) -> coverage_refresh.PerTestLines:
        return {"tests/x_test.py::test_x": [("src/x.py", 1)]}

    monkeypatch.setattr(coverage_refresh, "measure_test_coverage", measure)
    store = Store(tmp_path / "cache.sqlite")
    captured_hashes: list[dict[str, tuple[str, str]]] = []
    apply_refresh = store.apply_test_coverage_refresh

    def capture_refresh(
        *,
        measured: Mapping[str, list[tuple[str, int]]],
        measured_nodeids: Collection[str],
        replace_test_files: Collection[str],
        restored_rows: Collection[tuple[str, str, int]],
        file_hashes: Mapping[str, tuple[str, str]],
    ) -> None:
        captured_hashes.append(dict(file_hashes))
        apply_refresh(
            measured=measured,
            measured_nodeids=measured_nodeids,
            replace_test_files=replace_test_files,
            restored_rows=restored_rows,
            file_hashes=file_hashes,
        )

    monkeypatch.setattr(store, "apply_test_coverage_refresh", capture_refresh)
    try:
        _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([source, test_file], tmp_path),
        )

        assert captured_hashes == [
            {
                "src/x.py": (hashlib.sha256(source.read_bytes()).hexdigest(), "src"),
                "tests/x_test.py": (hashlib.sha256(test_file.read_bytes()).hexdigest(), "test"),
            }
        ]
    finally:
        store.close()


def test_prepare_diff_ingests_immutable_snapshot_during_substrate_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    metadata = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    load_coverage_map = incremental.load_coverage_map

    def replace_final_then_load(path: Path, **kwargs: Any) -> CoverageMap:
        replacement = CoverageData(basename=str(coverage))
        replacement.set_context("tests/y_test.py::test_y|run")
        replacement.add_lines({"src/y.py": {2}})
        replacement.write()
        return load_coverage_map(path, **kwargs)

    monkeypatch.setattr(incremental, "load_coverage_map", replace_final_then_load)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([], tmp_path),
        )

        assert store.coverage_generation() == metadata.generation
        assert store.tests_covering("src/x.py", 1) == {"tests/x_test.py::test_x"}
        assert store.tests_covering("src/y.py", 2) == set()
    finally:
        store.close()


def test_deleted_test_readded_with_same_content_refreshes_in_same_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_file = tmp_path / "tests/x_test.py"
    test_file.parent.mkdir()
    test_content = "def test_x():\n    assert True\n"
    test_file.write_text(test_content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)
    live_scope = _scope([test_file], tmp_path)
    deleted_scope = DiffScope(
        base_ref="origin/main",
        base_sha="base-sha",
        diff_base_sha="diff-base-sha",
        head_sha="head-sha",
        changed_lines={},
        file_hashes={"tests/x_test.py": "missing"},
    )
    refresh_calls: list[list[str]] = []

    def fake_refresh(
        src_files: list[str],
        test_files: list[str],
        *,
        store: Store,
        context: MeasureContext,
        deleted_tests: Collection[str] = (),
    ) -> coverage_refresh.RefreshMeasurement:
        assert src_files == []
        _ = (store, context)
        refresh_calls.append(test_files)
        nodeid = "tests/x_test.py::test_x"
        return coverage_refresh.RefreshMeasurement(
            src_nodeids=(),
            replaced_nodeids=(),
            replace_test_files=tuple(test_files),
            measured={nodeid: [("src/x.py", 1)]} if test_files else {},
        )

    monkeypatch.setattr(incremental, "measure_diff_refresh", fake_refresh)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=live_scope)
        store.put_file_hash("tests/other_test.py", "other-test-hash", "test")
        store.put_file_hash("src/other.py", "other-src-hash", "src")

        test_file.unlink()
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=deleted_scope)

        assert store.tests_covering("src/x.py", 1) == set()
        assert "tests/x_test.py" not in store.get_file_hashes("test")
        assert store.get_file_hashes("test")["tests/other_test.py"] == "other-test-hash"
        assert store.get_file_hashes("src")["src/other.py"] == "other-src-hash"

        test_file.write_text(test_content, encoding="utf-8")
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=live_scope)

        assert refresh_calls == [["tests/x_test.py"], ["tests/x_test.py"]]
        assert store.tests_covering("src/x.py", 1) == {"tests/x_test.py::test_x"}
    finally:
        store.close()


_T = "tests/x_test.py::test_x"


def _build_with_costs(seconds: float) -> Callable[[Path], int]:
    def build(path: Path) -> int:
        write_valid_coverage(path)
        pytest_costs.write_costs(substrate.costs_path(path), {_T: seconds})
        return 0

    return build


def test_refresh_publishes_the_costs_it_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A test edit goes to refresh, not to a rebuild: only refresh knows its new duration."""
    source, test_file = _source_and_test(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=_build_with_costs(0.5))

    def fake_refresh(
        src_files: list[str],
        test_files: list[str],
        *,
        store: Store,
        context: MeasureContext,
        deleted_tests: Collection[str] = (),
    ) -> coverage_refresh.RefreshMeasurement:
        _ = (src_files, store, context)
        return coverage_refresh.RefreshMeasurement(
            src_nodeids=(),
            replaced_nodeids=(),
            replace_test_files=tuple(test_files),
            measured={},
            costs={_T: 4.0},
        )

    monkeypatch.setattr(incremental, "measure_diff_refresh", fake_refresh)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=_scope([source, test_file], tmp_path))

        assert store.get_costs([_T]) == {_T: 4.0}
    finally:
        store.close()


def test_generation_seeds_the_cost_store_once_and_later_costs_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In diff mode the cost store is the only source of durations: the sidecar seeds it once, at the ingest of its
    generation; afterwards refresh and live calibration update it, and a rerun on the same generation keeps them."""
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=_build_with_costs(2.5))
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=_scope([], tmp_path))
        assert store.get_costs([_T]) == {_T: 2.5}

        store.put_costs({_T: 9.0})
        _prepare_diff("origin/main", cov_path=coverage, store=store, scope=_scope([], tmp_path))

        assert store.get_costs([_T]) == {_T: 9.0}
    finally:
        store.close()


def test_git_changed_merges_tracked_and_untracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(*args: str, **_kwargs: object) -> str:
        return "src/a.py\nsrc/b.py\n" if args[0] == "diff" else "src/b.py\nsrc/c.py\n"

    monkeypatch.setattr(incremental, "git_output", fake_git)
    assert set(incremental._git_changed("HEAD", tmp_path)) == {"src/a.py", "src/b.py", "src/c.py"}


def test_phase_add_accumulates_elapsed_seconds() -> None:
    timings: dict[str, float] = {}
    incremental._phase_add(timings, "phase", time.perf_counter() - 0.5)
    assert 0.0 < timings["phase"] < 2.0


def test_phase_add_starts_from_zero() -> None:
    timings: dict[str, float] = {}
    incremental._phase_add(timings, "phase", time.perf_counter())
    assert timings["phase"] < 1.0


def test_vouched_texts_skips_files_without_a_pin(tmp_path: Path) -> None:
    source = tmp_path / "src/x.py"
    source.parent.mkdir()
    source.write_text("X = 1\n", encoding="utf-8")
    scope = _scope([source], tmp_path)
    assert incremental._vouched_texts(scope, ["src/unpinned.py", "src/x.py"], tmp_path) == {"src/x.py": "X = 1\n"}


def test_vouched_texts_skips_missing_files(tmp_path: Path) -> None:
    present = tmp_path / "src/x.py"
    present.parent.mkdir()
    present.write_text("X = 1\n", encoding="utf-8")
    scope = DiffScope(
        "origin/main",
        "base-sha",
        "diff-base-sha",
        "head-sha",
        {},
        {"src/missing.py": "any", "src/x.py": hashlib.sha256(b"X = 1\n").hexdigest()},
    )
    assert incremental._vouched_texts(scope, ["src/missing.py", "src/x.py"], tmp_path) == {"src/x.py": "X = 1\n"}


def test_vouched_texts_skips_changed_files(tmp_path: Path) -> None:
    changed = tmp_path / "src/a.py"
    match = tmp_path / "src/b.py"
    changed.parent.mkdir()
    changed.write_text("A = 2\n", encoding="utf-8")
    match.write_text("B = 1\n", encoding="utf-8")
    scope = DiffScope(
        "origin/main",
        "base-sha",
        "diff-base-sha",
        "head-sha",
        {},
        {
            "src/a.py": hashlib.sha256(b"A = 1\n").hexdigest(),
            "src/b.py": hashlib.sha256(b"B = 1\n").hexdigest(),
        },
    )
    assert incremental._vouched_texts(scope, ["src/a.py", "src/b.py"], tmp_path) == {"src/b.py": "B = 1\n"}


def test_ingest_baseline_reports_the_row_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    store = Store(tmp_path / "cache.sqlite")
    try:
        incremental.ingest_baseline(coverage, store)
    finally:
        store.close()
    assert "baseline coverage -> store:" in capsys.readouterr().err


def test_ingest_baseline_removes_its_snapshot_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / ".coverage"
    metadata = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)

    def boom(*_args: object, **_kwargs: object) -> CoverageMap:
        raise OSError("boom")

    monkeypatch.setattr(incremental, "load_coverage_map", boom)
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(OSError):
            incremental.ingest_baseline(coverage, store, metadata=metadata, root=tmp_path)
    finally:
        store.close()
    assert not list(tmp_path.glob(".*.ingest.tmp"))


def test_ingest_baseline_tolerates_a_missing_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / ".coverage"
    metadata = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)

    def ghost_snapshot(_cov_path: Path) -> Path:
        return tmp_path / "ghost.ingest.tmp"

    monkeypatch.setattr(incremental, "_coverage_snapshot", ghost_snapshot)

    def boom_sha(_path: Path) -> str:
        raise ValueError("boom")

    monkeypatch.setattr(incremental.substrate, "file_sha256", boom_sha)
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(ValueError):
            incremental.ingest_baseline(coverage, store, metadata=metadata, root=tmp_path)
    finally:
        store.close()


def test_seed_scope_freshness_reports_the_vouch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source, test_file = _source_and_test(tmp_path)
    scope = _scope([source, test_file], tmp_path)
    store = Store(tmp_path / "cache.sqlite")
    try:
        incremental._seed_scope_freshness(
            store,
            scope,
            tmp_path,
            changed_src=["src/x.py"],
            changed_tests=["tests/x_test.py"],
            changed_support=[],
        )
    finally:
        store.close()
    assert "already measured" in capsys.readouterr().err


def test_prepare_diff_records_phase_timings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, test_file = _source_and_test(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)

    def measure(_targets: list[str], **_kwargs: object) -> coverage_refresh.PerTestLines:
        return {"tests/x_test.py::test_x": [("src/x.py", 1)]}

    monkeypatch.setattr(coverage_refresh, "measure_test_coverage", measure)
    store = Store(tmp_path / "cache.sqlite")
    timings: dict[str, float] = {}
    try:
        _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([source, test_file], tmp_path),
            phase_timings=timings,
        )
    finally:
        store.close()
    assert "baseline_ingest" in timings
    assert "staleness_selection" in timings
    assert "coverage_refresh" in timings


def test_prepare_diff_records_baseline_ingest_for_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    monkeypatch.chdir(tmp_path)

    def no_changes(_base: str, _root: Path) -> list[str]:
        return []

    monkeypatch.setattr(incremental, "_git_changed", no_changes)
    store = Store(tmp_path / "cache.sqlite")
    timings: dict[str, float] = {}
    try:
        _prepare_diff("origin/main", cov_path=coverage, store=store, phase_timings=timings)
    finally:
        store.close()
    assert "baseline_ingest" in timings


def test_prepare_diff_reports_the_diff_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source, test_file = _source_and_test(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)

    def measure(_targets: list[str], **_kwargs: object) -> coverage_refresh.PerTestLines:
        return {"tests/x_test.py::test_x": [("src/x.py", 1)]}

    monkeypatch.setattr(coverage_refresh, "measure_test_coverage", measure)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([source, test_file], tmp_path),
        )
    finally:
        store.close()
    err = capsys.readouterr().err
    assert "diff vs origin/main:" in err
    assert err.count("hash skip 0)") == 2


def test_prepare_diff_returns_mutation_tests_with_include_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, test_file = _source_and_test(tmp_path)
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)

    def measure(_targets: list[str], **_kwargs: object) -> coverage_refresh.PerTestLines:
        return {"tests/x_test.py::test_x": [("src/x.py", 1)]}

    monkeypatch.setattr(coverage_refresh, "measure_test_coverage", measure)
    store = Store(tmp_path / "cache.sqlite")
    try:
        result = _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([source, test_file], tmp_path),
            include_tests=True,
        )
    finally:
        store.close()
    assert "tests/x_test.py" in result


def test_prepare_diff_ignores_test_tree_files_without_py_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)

    def measure(_targets: list[str], **_kwargs: object) -> coverage_refresh.PerTestLines:
        return {}

    monkeypatch.setattr(coverage_refresh, "measure_test_coverage", measure)
    store = Store(tmp_path / "cache.sqlite")
    scope = DiffScope(
        "origin/main",
        "base-sha",
        "diff-base-sha",
        "head-sha",
        {},
        {"tests/readme.txt": hashlib.sha256(b"readme").hexdigest()},
    )
    try:
        assert _prepare_diff("origin/main", cov_path=coverage, store=store, scope=scope, include_tests=True) == []
    finally:
        store.close()


def test_prepare_diff_does_not_treat_a_changed_source_as_a_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed source file ends in ``.py`` but is not a test path: it must appear once, as a source only."""
    source = tmp_path / "src" / "x.py"
    source.parent.mkdir(parents=True)
    source.write_text("X = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    coverage = tmp_path / ".coverage"
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)

    def measure(_targets: list[str], **_kwargs: object) -> coverage_refresh.PerTestLines:
        return {}

    monkeypatch.setattr(coverage_refresh, "measure_test_coverage", measure)
    store = Store(tmp_path / "cache.sqlite")
    try:
        result = _prepare_diff(
            "origin/main",
            cov_path=coverage,
            store=store,
            scope=_scope([source], tmp_path),
            include_tests=True,
        )
    finally:
        store.close()
    assert result == ["src/x.py"]


def test_outside_drift_continues_past_an_unchanged_file(tmp_path: Path) -> None:
    """The unchanged file sorts first: the loop must continue past it to the diverged one after it."""
    recorded = substrate.TreeRecord(hashes={"src/a.py": "same", "src/b.py": "old"}, reexports={})
    current = substrate.TreeRecord(hashes={"src/a.py": "same", "src/b.py": "new"}, reexports={})
    for path in ("src/a.py", "src/b.py"):
        file_path = tmp_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("content\n", encoding="utf-8")

    drift = incremental._outside_drift(
        recorded,
        current,
        layout=make_layout(tmp_path),
        scope_files=frozenset(),
        measured={},
        remap={},
    )

    assert drift.src == ["src/b.py"]


def test_git_changed_passes_exact_git_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], Path | None]] = []

    def fake_git(*args: str, cwd: Path | None = None) -> str:
        calls.append((args, cwd))
        return "src/a.py\n" if args[0] == "diff" else "src/b.py\n"

    monkeypatch.setattr(incremental, "git_output", fake_git)
    assert set(incremental._git_changed("HEAD", tmp_path)) == {"src/a.py", "src/b.py"}
    assert calls == [
        (("diff", "--name-only", "--relative", "HEAD"), tmp_path),
        (("ls-files", "--others", "--exclude-standard"), tmp_path),
    ]


def test_coverage_snapshot_lands_next_to_the_source(tmp_path: Path) -> None:
    cov = tmp_path / "sub" / ".coverage"
    cov.parent.mkdir(parents=True)
    cov.write_bytes(b"data")
    snapshot = incremental._coverage_snapshot(cov)
    try:
        assert snapshot.parent == cov.parent
        assert snapshot.name.endswith(".ingest.tmp")
        assert snapshot.read_bytes() == b"data"
    finally:
        snapshot.unlink(missing_ok=True)


def test_ingest_baseline_relativizes_against_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    seen: dict[str, object] = {}

    def fake_load(path: Path, root: Path | None = None) -> CoverageMap:
        seen["root"] = root
        return CoverageMap(by_file={})

    monkeypatch.setattr(incremental, "load_coverage_map", fake_load)
    store = Store(tmp_path / "c.sqlite")
    try:
        incremental.ingest_baseline(coverage, store, root=tmp_path)
    finally:
        store.close()
    assert seen["root"] == tmp_path


def test_ingest_generation_forwards_root_to_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    calls: list[dict[str, object]] = []

    def fake_ingest(cov_path: Path, store: Store, *, metadata: object = None, root: Path | None = None) -> None:
        calls.append({"metadata": metadata, "root": root})

    monkeypatch.setattr(incremental, "ingest_baseline", fake_ingest)
    store = Store(tmp_path / "c.sqlite")
    try:
        incremental._ingest_generation(coverage, store, require_metadata=False, root=tmp_path, phase_timings=None)
    finally:
        store.close()
    assert calls
    assert all(call["root"] == tmp_path for call in calls)


def test_ingest_generation_forwards_root_with_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    metadata = expected_substrate_metadata()

    def fake_inspect(path: Path) -> substrate.SubstrateMetadata | None:
        return metadata

    def fake_read_costs(path: Path) -> dict[str, float]:
        return {}

    monkeypatch.setattr(substrate, "substrate_inspect", fake_inspect)
    monkeypatch.setattr(pytest_costs, "read_costs", fake_read_costs)
    calls: list[dict[str, object]] = []

    def fake_ingest(cov_path: Path, store: Store, *, metadata: object = None, root: Path | None = None) -> None:
        calls.append({"metadata": metadata, "root": root})

    monkeypatch.setattr(incremental, "ingest_baseline", fake_ingest)
    store = Store(tmp_path / "c.sqlite")
    try:
        incremental._ingest_generation(coverage, store, require_metadata=False, root=tmp_path, phase_timings=None)
    finally:
        store.close()
    assert calls
    assert all(call["root"] == tmp_path for call in calls)


def test_prepare_diff_passes_base_before_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    monkeypatch.chdir(tmp_path)
    calls: list[tuple[str, Path]] = []

    def fake_changed(base: str, root: Path) -> list[str]:
        calls.append((base, root))
        return []

    monkeypatch.setattr(incremental, "_git_changed", fake_changed)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff("origin/main", cov_path=coverage, store=store)
    finally:
        store.close()
    assert calls == [("origin/main", tmp_path)]


def test_prepare_diff_passes_remap_before_recorded_reexports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / ".coverage"
    write_valid_coverage(coverage)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests/pkg").mkdir(parents=True)
    (tmp_path / "tests/pkg/def.py").write_text("x = 1\n", encoding="utf-8")

    def fake_changed(base: str, root: Path) -> list[str]:
        return ["tests/pkg/def.py"]

    def fake_reexport(layout: object) -> dict[str, str]:
        return {"tests/pkg/def.py": "tests/cur_wrap.py"}

    monkeypatch.setattr(incremental, "_git_changed", fake_changed)
    monkeypatch.setattr(incremental, "reexport_map", fake_reexport)
    calls: list[tuple[str, object, object, Path]] = []

    def fake_wrappers(definition: str, remap: object, recorded: object, root: Path) -> list[str]:
        calls.append((definition, remap, recorded, root))
        return []

    monkeypatch.setattr(incremental, "_wrappers", fake_wrappers)
    store = Store(tmp_path / "cache.sqlite")
    try:
        _prepare_diff("origin/main", cov_path=coverage, store=store)
    finally:
        store.close()
    assert calls
    for _definition, remap, recorded, _root in calls:
        assert remap == {"tests/pkg/def.py": "tests/cur_wrap.py"}
        assert recorded == {}


def test_outside_drift_passes_remap_before_recorded_reexports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object, object, Path]] = []

    def fake_wrappers(definition: str, remap: object, recorded: object, root: Path) -> list[str]:
        calls.append((definition, remap, recorded, root))
        return []

    monkeypatch.setattr(incremental, "_wrappers", fake_wrappers)
    recorded = substrate.TreeRecord(
        hashes={"tests/pkg/def.py": "old"}, reexports={"tests/pkg/def.py": "tests/rec_wrap.py"}
    )
    current = substrate.TreeRecord(
        hashes={"tests/pkg/def.py": "new"}, reexports={"tests/pkg/def.py": "tests/rec_wrap.py"}
    )
    (tmp_path / "tests/pkg/def.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests/pkg/def.py").write_text("x = 1\n", encoding="utf-8")
    incremental._outside_drift(
        recorded,
        current,
        layout=make_layout(tmp_path),
        scope_files=frozenset(),
        measured={},
        remap={"tests/pkg/def.py": "tests/cur_wrap.py"},
    )
    assert calls
    for _definition, remap, recorded_reexports, _root in calls:
        assert remap == {"tests/pkg/def.py": "tests/cur_wrap.py"}
        assert recorded_reexports == {"tests/pkg/def.py": "tests/rec_wrap.py"}


def test_refresh_tree_passes_recorded_before_current_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker_recorded = substrate.TreeRecord(hashes={}, reexports={})
    marker_snapshot = substrate.TreeRecord(hashes={}, reexports={})
    seen: dict[str, object] = {}
    metadata = expected_substrate_metadata()

    def fake_ingest_generation(
        *args: object, **kwargs: object
    ) -> tuple[substrate.SubstrateMetadata | None, str | None]:
        return metadata, "gen"

    def fake_tree_read(*args: object, **kwargs: object) -> substrate.TreeRecord | None:
        return marker_recorded

    def fake_tree_snapshot(layout: object) -> substrate.TreeRecord:
        return marker_snapshot

    monkeypatch.setattr(incremental, "_ingest_generation", fake_ingest_generation)
    monkeypatch.setattr(substrate, "tree_read", fake_tree_read)
    monkeypatch.setattr(substrate, "tree_snapshot", fake_tree_snapshot)

    def fake_outside_drift(
        recorded: object, current: object, *, layout: object, scope_files: object, measured: object, remap: object
    ) -> incremental._OutsideDrift:
        seen["recorded"] = recorded
        seen["current"] = current
        return incremental._OutsideDrift()

    monkeypatch.setattr(incremental, "_outside_drift", fake_outside_drift)
    store = Store(tmp_path / "cache.sqlite")
    try:
        incremental.refresh_tree(cov_path=tmp_path / ".coverage", store=store, context=_context(tmp_path))
    finally:
        store.close()
    assert seen["recorded"] is marker_recorded
    assert seen["current"] is marker_snapshot
