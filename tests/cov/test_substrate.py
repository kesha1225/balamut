"""Atomic publication of the coverage substrate and its metadata (balamut.coverage.substrate)."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Self, cast

import pytest

from balamut.coverage import costs as pytest_costs
from balamut.coverage import discovery, substrate
from bm_support import make_layout, write_file
from bm_support.substrates import expected_substrate_metadata, write_valid_coverage

_expected = expected_substrate_metadata


def test_failed_build_does_not_publish_final_substrate(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"

    with pytest.raises(substrate.SubstrateBuildError):
        substrate.substrate_ensure(coverage, _expected(), build=lambda _path: 1)

    assert not coverage.exists()
    assert not substrate.metadata_path(coverage).exists()


def test_successful_build_publishes_matching_metadata(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"

    metadata = substrate.substrate_ensure(
        coverage,
        _expected(),
        build=write_valid_coverage,
    )

    assert metadata.complete is True
    assert metadata.generation
    assert metadata.coverage_sha256 == substrate.file_sha256(coverage)
    assert metadata.costs_sha256 == substrate.file_sha256(substrate.costs_path(coverage))
    assert pytest_costs.read_costs(substrate.costs_path(coverage)) == {}
    assert substrate.substrate_inspect(coverage) == metadata


def test_failed_rebuild_keeps_last_complete_generation(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    published = substrate.substrate_ensure(
        coverage,
        _expected(),
        build=write_valid_coverage,
    )
    coverage_bytes = coverage.read_bytes()
    metadata_bytes = substrate.metadata_path(coverage).read_bytes()

    with pytest.raises(substrate.SubstrateBuildError):
        substrate.substrate_ensure(
            coverage,
            _expected(config="config-v2"),
            build=lambda _path: 1,
        )

    assert coverage.read_bytes() == coverage_bytes
    assert substrate.metadata_path(coverage).read_bytes() == metadata_bytes
    assert substrate.substrate_inspect(coverage) == published


def _measured_tree(label: str) -> substrate.TreeRecord:
    return substrate.TreeRecord(hashes={"src/a.py": label}, reexports={})


def test_build_publishes_the_tree_it_measured_as_part_of_the_publication(tmp_path: Path) -> None:
    """The tree is taken before the build (it is what the build measures) and written under the same lock before the
    metadata. Written by the caller after the lock, it could land over the tree of a generation a neighbour published
    later."""
    coverage = tmp_path / "coverage.substrate"
    events: list[str] = []

    def tree() -> substrate.TreeRecord:
        events.append("tree")
        return _measured_tree("measured")

    def build(path: Path) -> int:
        events.append("build")
        return write_valid_coverage(path)

    published = substrate.substrate_ensure(coverage, _expected(), build=build, tree=tree)

    assert events == ["tree", "build"]
    assert substrate.tree_read(coverage, published.generation) == _measured_tree("measured")


def test_reused_or_failed_build_leaves_the_tree_alone(tmp_path: Path) -> None:
    """Only a build that published its own generation writes the tree."""
    coverage = tmp_path / "coverage.substrate"
    published = substrate.substrate_ensure(
        coverage, _expected(), build=write_valid_coverage, tree=lambda: _measured_tree("first")
    )
    record = coverage.with_name(coverage.name + ".tree.json").read_bytes()

    substrate.substrate_ensure(coverage, _expected(), build=write_valid_coverage, tree=lambda: _measured_tree("reused"))
    with pytest.raises(substrate.SubstrateBuildError):
        substrate.substrate_ensure(
            coverage,
            _expected(),
            build=lambda _path: 1,
            tree=lambda: _measured_tree("failed"),
            force_rebuild=True,
        )

    assert coverage.with_name(coverage.name + ".tree.json").read_bytes() == record
    assert substrate.tree_read(coverage, published.generation) == _measured_tree("first")


def _red_build(path: Path) -> int:
    """A build that went red on some tests while the coverage of the rest merged normally."""
    write_valid_coverage(path)
    return 1


def _suite_tree(size: int) -> substrate.TreeRecord:
    return substrate.TreeRecord(hashes={f"tests/t{index}_test.py": f"h{index}" for index in range(size)}, reexports={})


def test_red_build_publishes_coverage_with_failed_test_files_marked_unmeasured(tmp_path: Path) -> None:
    """The next run's refresh re-measures the files of the failed tests: no full rebuild is needed for them."""
    coverage = tmp_path / "coverage.substrate"

    with pytest.raises(substrate.SubstrateBaselineRedError) as raised:
        substrate.substrate_ensure(
            coverage,
            _expected(),
            build=_red_build,
            tree=lambda: _suite_tree(40),
            unmeasured=lambda: frozenset({"tests/t3_test.py"}),
        )

    published = raised.value.published
    assert substrate.substrate_inspect(coverage) == published
    recorded = substrate.tree_read(coverage, published.generation)
    assert recorded is not None
    assert recorded.hashes["tests/t3_test.py"] == substrate.UNMEASURED_FILE_HASH
    assert recorded.hashes["tests/t4_test.py"] == "h4"


@pytest.mark.parametrize(
    "red_files",
    [
        pytest.param(frozenset[str](), id="no-failed-nodeids"),
        pytest.param(frozenset({"tests/conftest.py"}), id="file-outside-tree"),
        pytest.param(frozenset(f"tests/t{index}_test.py" for index in range(20)), id="mass-failure"),
    ],
)
def test_red_build_that_refresh_cannot_repair_keeps_the_last_generation(
    tmp_path: Path,
    red_files: frozenset[str],
) -> None:
    coverage = tmp_path / "coverage.substrate"
    last = substrate.substrate_ensure(coverage, _expected(), build=write_valid_coverage)

    with pytest.raises(substrate.SubstrateBuildError) as raised:
        substrate.substrate_ensure(
            coverage,
            _expected(),
            build=_red_build,
            force_rebuild=True,
            tree=lambda: _suite_tree(40),
            unmeasured=lambda: red_files,
        )

    assert not isinstance(raised.value, substrate.SubstrateBaselineRedError)
    assert substrate.substrate_inspect(coverage) == last


def test_red_build_without_merged_coverage_publishes_nothing(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"

    with pytest.raises(substrate.SubstrateBuildError) as raised:
        substrate.substrate_ensure(
            coverage,
            _expected(),
            build=lambda _path: 1,
            tree=lambda: _suite_tree(40),
            unmeasured=lambda: frozenset({"tests/t3_test.py"}),
        )

    assert not isinstance(raised.value, substrate.SubstrateBaselineRedError)
    assert not coverage.exists()


def test_inspect_rejects_coverage_with_wrong_checksum(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    substrate.substrate_ensure(
        coverage,
        _expected(),
        build=write_valid_coverage,
    )
    coverage.write_bytes(coverage.read_bytes() + b"tampered")

    assert substrate.substrate_inspect(coverage) is None


def test_build_publishes_costs_and_inspect_rejects_tampering(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"

    def build(path: Path) -> int:
        return_code = write_valid_coverage(path)
        pytest_costs.write_costs(substrate.costs_path(path), {"tests/x_test.py::test_x": 0.25})
        return return_code

    metadata = substrate.substrate_ensure(coverage, _expected(), build=build)

    assert pytest_costs.read_costs(substrate.costs_path(coverage)) == {"tests/x_test.py::test_x": 0.25}
    assert metadata.costs_sha256 == substrate.file_sha256(substrate.costs_path(coverage))
    substrate.costs_path(coverage).write_text("{}", encoding="utf-8")
    assert substrate.substrate_inspect(coverage) is None


def test_ensure_keeps_the_substrate_when_only_the_test_inventory_changed(tmp_path: Path) -> None:
    """A new or deleted test file diverges from the recorded tree: refresh re-measures it, no rebuild is needed."""
    coverage = tmp_path / "coverage.substrate"
    published = substrate.substrate_ensure(
        coverage,
        _expected(inventory="inventory-v1"),
        build=write_valid_coverage,
    )

    kept = substrate.substrate_ensure(
        coverage,
        _expected(inventory="inventory-v2"),
        build=write_valid_coverage,
    )

    assert kept == published


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("config_fingerprint", "config-v2"),
        ("runtime_fingerprint", "runtime-v2"),
        ("schema_fingerprint", "schema-v2"),
    ],
)
def test_ensure_rebuilds_for_incompatible_fingerprint(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    coverage = tmp_path / "coverage.substrate"
    published = substrate.substrate_ensure(
        coverage,
        _expected(),
        build=write_valid_coverage,
    )
    expected_values = {
        "config_fingerprint": "config-v1",
        "runtime_fingerprint": "runtime-v1",
        "schema_fingerprint": "schema-v1",
    }
    expected_values[field] = value
    expected = substrate.SubstrateMetadata(
        schema_version=1,
        complete=False,
        generation="",
        test_inventory_fingerprint="inventory-v1",
        coverage_sha256="",
        **expected_values,
    )

    rebuilt = substrate.substrate_ensure(coverage, expected, build=write_valid_coverage)

    assert rebuilt.generation != published.generation
    assert rebuilt.schema_version == expected.schema_version
    assert rebuilt.config_fingerprint == expected.config_fingerprint
    assert rebuilt.runtime_fingerprint == expected.runtime_fingerprint
    assert rebuilt.schema_fingerprint == expected.schema_fingerprint


def test_tree_snapshot_records_support_files_next_to_tests(tmp_path: Path) -> None:
    """A support file is part of the tree record next to the tests: its edit is a divergence re-measured through the
    test files that import it, not a corpus fingerprint that would rebuild the whole substrate."""
    write_file(tmp_path / "tests/support/helper.py", "VALUE = 1\n")
    write_file(tmp_path / "tests/domain/x_test.py", "def test_x():\n    assert True\n")

    assert set(substrate.tree_snapshot(make_layout(tmp_path)).hashes) == {
        "tests/support/helper.py",
        "tests/domain/x_test.py",
    }


def _drift_after(tmp_path: Path, relative: str, content: str) -> substrate.TreeDrift | None:
    layout = make_layout(tmp_path)
    recorded = substrate.tree_snapshot(layout)
    write_file(tmp_path / relative, content)
    discovery.reexport_cache_clear()
    return substrate.drift_between(recorded, substrate.tree_snapshot(layout), in_diff=frozenset(), layout=layout)


def test_a_reexported_test_body_drifts_as_a_test_not_as_support(tmp_path: Path) -> None:
    """A defining file whose tests are collected through a wrapper is a test: the wrapper re-measures its edit."""
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/pkg/__init__.py", "")
    write_file(tmp_path / "tests/pkg/waves.py", "def test_wave():\n    assert True\n")
    write_file(tmp_path / "tests/pkg/waves_test.py", "from tests.pkg.waves import test_wave\n")

    drift = _drift_after(tmp_path, "tests/pkg/waves.py", "def test_wave():\n    assert 1 == 1\n")

    assert drift == substrate.TreeDrift("tree_changed_outside_diff", ("tests/pkg/waves.py",))


def test_a_helper_imported_under_a_test_prefixed_name_is_support(tmp_path: Path) -> None:
    """A `TestDict` name does not make a module a test module: it has no collectable tests, it is a helper."""
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/pkg/__init__.py", "")
    write_file(tmp_path / "tests/pkg/helper.py", "type TestDict = dict[str, str]\nVALUE = 1\n")
    write_file(tmp_path / "tests/pkg/helper_test.py", "from tests.pkg.helper import TestDict\n")

    drift = _drift_after(tmp_path, "tests/pkg/helper.py", "type TestDict = dict[str, str]\nVALUE = 2\n")

    assert drift == substrate.TreeDrift(
        "tree_support_changed",
        ("tests/pkg/helper.py",),
        support=("tests/pkg/helper.py",),
        support_tests=("tests/pkg/helper_test.py",),
    )


def test_concurrent_ensure_builds_and_publishes_one_generation(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    calls = 0
    calls_lock = threading.Lock()

    def build(path: Path) -> int:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.1)
        return write_valid_coverage(path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                substrate.substrate_ensure,
                coverage,
                _expected(),
                build=build,
            )
            for _ in range(2)
        ]
        results = [future.result() for future in futures]

    assert calls == 1
    assert results[0] == results[1]
    assert substrate.substrate_inspect(coverage) == results[0]


def _metadata_raw(**overrides: substrate.MetadataJsonValue) -> dict[str, substrate.MetadataJsonValue]:
    raw: dict[str, substrate.MetadataJsonValue] = {
        "schema_version": 1,
        "complete": True,
        "generation": "gen",
        "config_fingerprint": "config",
        "test_inventory_fingerprint": "inventory",
        "runtime_fingerprint": "runtime",
        "schema_fingerprint": "schema",
        "coverage_sha256": "coverage",
        "costs_sha256": "costs",
    }
    raw.update(overrides)
    return raw


def test_metadata_decode_rejects_non_dict_raw() -> None:
    assert substrate._metadata_decode(5) is None


def test_metadata_decode_rejects_non_int_schema_version() -> None:
    assert substrate._metadata_decode(_metadata_raw(schema_version="1")) is None


def test_metadata_decode_rejects_non_bool_complete() -> None:
    assert substrate._metadata_decode(_metadata_raw(complete=1)) is None


def test_metadata_decode_rejects_non_string_values() -> None:
    assert substrate._metadata_decode(_metadata_raw(generation=5)) is None


def test_coverage_validate_rejects_an_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.substrate"
    empty.write_bytes(b"")
    with pytest.raises(substrate.SubstrateBuildError, match="non-empty"):
        substrate._coverage_validate(empty)


def test_file_fsync_flushes_the_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", synced.append)
    substrate._file_fsync(path)
    assert len(synced) == 1


def test_directory_fsync_flushes_the_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", synced.append)
    substrate._directory_fsync(tmp_path)
    assert len(synced) == 1


def test_directory_fsync_closes_the_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    real_close = os.close

    def close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "close", close)
    substrate._directory_fsync(tmp_path)
    assert len(closed) == 1


def test_temporary_path_closes_the_mkstemp_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    real_close = os.close

    def close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "close", close)
    substrate._temporary_path(tmp_path / "final.substrate", ".tmp")
    assert len(closed) == 1


def test_publication_lock_releases_the_advisory_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def flock(fd: int, op: int) -> None:
        calls.append((fd, op))

    monkeypatch.setattr(fcntl, "flock", flock)
    with substrate.publication_lock(tmp_path / "coverage.substrate"):
        pass
    assert [op for _fd, op in calls] == [fcntl.LOCK_EX, fcntl.LOCK_UN]


class _SpyStream:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.flushed = 0

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushed += 1

    def fileno(self) -> int:
        return 1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_metadata_write_flushes_newline_and_fsyncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _SpyStream()
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", synced.append)

    class _FakePath:
        def open(self, *args: object, **kwargs: object) -> _SpyStream:
            return stream

    substrate._metadata_write(cast("Path", _FakePath()), _expected())
    assert "".join(stream.chunks).endswith("\n")
    assert stream.flushed == 1
    assert synced == [1]


def test_metadata_write_sorts_keys(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"
    substrate._metadata_write(path, _expected())
    content = path.read_text(encoding="utf-8")
    assert content.index('"complete"') < content.index('"schema_version"')


def test_metadata_write_indents_two_spaces(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"
    substrate._metadata_write(path, _expected())
    content = path.read_text(encoding="utf-8")
    assert '\n  "' in content
    assert '\n   "' not in content


def test_metadata_write_keeps_non_ascii_unescaped(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"
    substrate._metadata_write(path, _expected(config="café"))
    assert "café" in path.read_text(encoding="utf-8")


def test_publish_fsyncs_coverage_costs_and_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(substrate, "_file_fsync", synced.append)
    monkeypatch.setattr(substrate, "_directory_fsync", synced.append)
    substrate.substrate_ensure(tmp_path / "coverage.substrate", _expected(), build=write_valid_coverage)
    assert len(synced) == 3


def test_failed_build_removes_its_temporary_files(tmp_path: Path) -> None:
    with pytest.raises(substrate.SubstrateBuildError):
        substrate.substrate_ensure(tmp_path / "coverage.substrate", _expected(), build=_red_build)
    assert [p for p in tmp_path.iterdir() if ".tmp" in p.name] == []


def _build_with_bad_costs(path: Path) -> int:
    write_valid_coverage(path)
    substrate.costs_path(path).write_text("not json", encoding="utf-8")
    return 0


def test_failed_publish_removes_temporary_coverage_and_costs(tmp_path: Path) -> None:
    with pytest.raises(substrate.SubstrateBuildError):
        substrate.substrate_ensure(tmp_path / "coverage.substrate", _expected(), build=_build_with_bad_costs)
    assert [p for p in tmp_path.iterdir() if ".tmp" in p.name] == []


def test_publish_cleans_up_its_temporary_metadata_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(substrate, "tree_write", fail)
    with pytest.raises(RuntimeError):
        substrate.substrate_ensure(
            tmp_path / "coverage.substrate",
            _expected(),
            build=write_valid_coverage,
            tree=lambda: _measured_tree("h"),
        )
    assert [p for p in tmp_path.iterdir() if ".metadata.tmp" in p.name] == []


def test_tree_write_cleans_up_its_temporary_file_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(substrate.json, "dumps", fail)
    with pytest.raises(RuntimeError):
        substrate.tree_write(tmp_path / "coverage.substrate", "gen", _measured_tree("h"))
    assert [p for p in tmp_path.iterdir() if ".tree.tmp" in p.name] == []


def test_tree_read_rejects_non_object_tree(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    substrate.tree_path(coverage).write_text("[]", encoding="utf-8")
    assert substrate.tree_read(coverage, "gen") is None


def test_tree_read_rejects_damaged_files_map(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    tree: dict[str, object] = {"schema_version": 1, "generation": "gen", "files": 5, "reexports": {}}
    substrate.tree_path(coverage).write_text(json.dumps(tree), encoding="utf-8")
    assert substrate.tree_read(coverage, "gen") is None


def test_drift_refreshable_at_the_boundary() -> None:
    assert substrate.drift_refreshable(16, 64) is True


def test_tree_unknown_is_not_refreshable() -> None:
    assert substrate.TreeDrift("tree_unknown").refreshable(100) is False


def test_drift_with_both_support_and_test_changes_is_outside_diff(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    write_file(tmp_path / "tests/support/helper.py", "VALUE = 1\n")
    write_file(tmp_path / "tests/domain/x_test.py", "def test_x():\n    assert True\n")
    recorded = substrate.tree_snapshot(layout)
    write_file(tmp_path / "tests/support/helper.py", "VALUE = 2\n")
    write_file(tmp_path / "tests/domain/x_test.py", "def test_x():\n    assert 1 == 1\n")
    discovery.reexport_cache_clear()
    drift = substrate.drift_between(recorded, substrate.tree_snapshot(layout), in_diff=frozenset(), layout=layout)
    assert drift is not None
    assert drift.reason == "tree_changed_outside_diff"


def test_file_sha256_reads_one_megabyte_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SpyFile:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self.sizes: list[int] = []

        def read(self, size: int) -> bytes:
            self.sizes.append(size)
            data, self._data = self._data, b""
            return data

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    stream = _SpyFile(b"payload")

    class _FakePath:
        def open(self, *args: object, **kwargs: object) -> _SpyFile:
            return stream

    substrate.file_sha256(cast("Path", _FakePath()))
    assert stream.sizes[0] == 1024 * 1024


def test_tree_write_keeps_non_ascii_unescaped(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    substrate.tree_write(coverage, "gen", substrate.TreeRecord(hashes={"src/é.py": "h"}, reexports={}))
    assert "é" in substrate.tree_path(coverage).read_text(encoding="utf-8")


def test_temporary_path_stages_next_to_the_target_with_the_right_shape(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "coverage.substrate"
    target.parent.mkdir(parents=True)
    path = substrate._temporary_path(target, ".tree.tmp")
    try:
        assert path.parent == target.parent
        assert path.name.startswith(".coverage.substrate.")
        assert path.name.endswith(".tree.tmp")
    finally:
        path.unlink(missing_ok=True)


def test_tree_drift_refreshable_is_directional() -> None:
    # 100 diverged paths in a 4-file tree: the refresh work far exceeds the floor/share, so it is not refreshable.
    drift = substrate.TreeDrift("tree_changed_outside_diff", paths=tuple(f"src/f{i}.py" for i in range(100)))
    assert drift.refreshable(4) is False


def test_tree_write_uses_compact_json(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    substrate.tree_write(coverage, "gen", substrate.TreeRecord(hashes={"src/a.py": "h"}, reexports={"d.py": "w.py"}))
    content = substrate.tree_path(coverage).read_text(encoding="utf-8")
    assert ',"' in content
    assert ', "' not in content
    assert '": ' not in content
