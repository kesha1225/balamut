"""The exact-fingerprint cache of confirmed baseline failures (balamut.coverage.baseline_failure)."""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import types
from pathlib import Path

import pytest

from balamut.coverage import baseline_failure
from balamut.coverage.substrate import SubstrateMetadata
from bm_support.substrates import expected_substrate_metadata

_NODEID = "tests/x_test.py::test_x"


def _fingerprint(**overrides: str) -> str:
    shas = {"base_sha": "base", "diff_base_sha": "diff-base", "head_sha": "head", **overrides}
    return baseline_failure.failure_fingerprint(
        expected_substrate_metadata(),
        base_sha=shas["base_sha"],
        diff_base_sha=shas["diff_base_sha"],
        head_sha=shas["head_sha"],
        environment={"DATABASE_URL": "postgresql://u:secret@db/app"},
    )


def test_record_round_trips_with_sorted_unique_nodeids(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    fingerprint = _fingerprint()
    record = baseline_failure.BaselineFailureRecord(fingerprint, (_NODEID, "tests/a_test.py::test_a", _NODEID), "gen")

    baseline_failure.failure_write(coverage, record)

    assert baseline_failure.failure_read(coverage, expected_fingerprint=fingerprint) == (
        baseline_failure.BaselineFailureRecord(fingerprint, ("tests/a_test.py::test_a", _NODEID), "gen")
    )
    assert baseline_failure.failure_path(coverage).name == "coverage.substrate.baseline-failure.json"


def test_record_of_another_fingerprint_is_ignored(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord(_fingerprint(), (_NODEID,)))

    assert baseline_failure.failure_read(coverage, expected_fingerprint=_fingerprint(head_sha="other")) is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"schema_version": 2, "fingerprint": "F", "failed_nodeids": [], "generation": ""}',
        '{"schema_version": 2, "fingerprint": "F", "failed_nodeids": [1], "generation": ""}',
        '{"schema_version": 2, "fingerprint": "F", "failed_nodeids": ["tests/x_test.py::test_x"], "generation": 3}',
        '{"schema_version": 1, "fingerprint": "F", "failed_nodeids": ["tests/x_test.py::test_x"], "generation": ""}',
        (
            '{"schema_version": 2, "fingerprint": "F", "failed_nodeids": ["tests/x_test.py::test_x"], "generation": "",'
            ' "extra": 1}'
        ),
    ],
    ids=["garbage", "list", "empty", "non-string", "bad-generation", "old-schema", "extra-key"],
)
def test_malformed_record_is_ignored(tmp_path: Path, payload: str) -> None:
    coverage = tmp_path / "coverage.substrate"
    baseline_failure.failure_path(coverage).write_text(payload.replace('"F"', json.dumps("F")), encoding="utf-8")

    assert baseline_failure.failure_read(coverage, expected_fingerprint="F") is None


def test_empty_record_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="failed node ids"):
        baseline_failure.failure_write(tmp_path / "c", baseline_failure.BaselineFailureRecord("F", ()))


def test_remove_is_idempotent(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord("F", (_NODEID,)))
    baseline_failure.failure_remove(coverage)
    baseline_failure.failure_remove(coverage)

    assert not baseline_failure.failure_path(coverage).exists()


def test_fingerprint_ignores_what_a_build_produces_but_not_its_inputs() -> None:
    """Generation, completeness and output digests come out of a build; the inputs and the environment go in."""
    metadata = expected_substrate_metadata()
    produced = dataclasses.replace(metadata, complete=True, generation="g", coverage_sha256="c", costs_sha256="k")

    def of(meta: SubstrateMetadata = metadata, environment: dict[str, str] | None = None) -> str:
        return baseline_failure.failure_fingerprint(
            meta, base_sha="b", diff_base_sha="d", head_sha="h", environment=environment or {"A": "1"}
        )

    assert of(produced) == of()
    assert of(expected_substrate_metadata(config="config-v2")) != of()
    assert of(environment={"A": "2"}) != of()


def test_fingerprint_keeps_secrets_out_of_the_record(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord(_fingerprint(), (_NODEID,)))

    assert "secret" not in baseline_failure.failure_path(coverage).read_text(encoding="utf-8")


def test_record_is_written_with_a_trailing_newline(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord("F", (_NODEID,)))

    assert baseline_failure.failure_path(coverage).read_bytes().endswith(b"\n")


def test_write_creates_missing_parent_directories(tmp_path: Path) -> None:
    coverage = tmp_path / "nested" / "dir" / "coverage.substrate"
    baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord("F", (_NODEID,)))

    assert baseline_failure.failure_read(coverage, expected_fingerprint="F") is not None


def test_write_removes_its_temporary_file_when_the_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(baseline_failure.json, "dump", boom)
    coverage = tmp_path / "coverage.substrate"
    with pytest.raises(OSError):
        baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord("F", (_NODEID,)))

    assert not list(tmp_path.glob(".*.tmp"))


def test_write_flushes_and_fsyncs_the_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    real_fsync = os.fsync

    def checking_fsync(fd: int) -> None:
        sizes.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", checking_fsync)
    coverage = tmp_path / "coverage.substrate"
    baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord("F", (_NODEID,)))

    assert sizes  # fsync ran (kills the fsync deletion)
    assert sizes[0] > 0  # the buffer was flushed before fsync (kills the flush deletion)


def test_fingerprint_matches_the_pinned_serialization() -> None:
    # The metadata fields are not alphabetical: sort_keys pins the serialization regardless of field order.
    assert _fingerprint() == "690a878aff9c7b3a29c87bf41015813bcb69475d7fc4d336cd72646747768ac4"


def test_fingerprint_encodes_non_ascii_environment_literally() -> None:
    fingerprint = baseline_failure.failure_fingerprint(
        expected_substrate_metadata(), base_sha="b", diff_base_sha="d", head_sha="h", environment={"K": "café"}
    )
    assert fingerprint == "736198f54836950b87509354947f900862e7ba4bc46142a877d0be62dc71a676"


def test_write_encodes_non_ascii_nodeids_literally(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.substrate"
    baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord("F", ("tests/x_test.py::test_é",)))

    assert "tests/x_test.py::test_é" in baseline_failure.failure_path(coverage).read_text(encoding="utf-8")


def test_json_value_alias_is_a_union() -> None:
    importlib.reload(baseline_failure)
    assert isinstance(baseline_failure.JsonValue.__value__, types.UnionType)


def test_schema_version_is_pinned() -> None:
    importlib.reload(baseline_failure)
    assert baseline_failure._BASELINE_FAILURE_SCHEMA_VERSION == 2


def test_failure_write_stages_the_temp_next_to_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / "coverage.substrate"
    seen: dict[str, Path] = {}
    real_replace = Path.replace

    def fake_replace(self: Path, target_path: Path) -> None:
        seen["source"] = self
        real_replace(self, target_path)

    monkeypatch.setattr(Path, "replace", fake_replace)
    baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord("F", (_NODEID,)))
    assert seen["source"].parent == coverage.parent
    assert seen["source"].name.startswith(".coverage.substrate.baseline-failure.json.")
    assert seen["source"].name.endswith(".tmp")


def test_failure_write_opens_the_temp_in_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coverage = tmp_path / "coverage.substrate"
    seen: dict[str, str | None] = {}
    real = os.fdopen

    def spy(fd: int, mode: str, encoding: str | None = None) -> object:
        seen["encoding"] = encoding
        return real(fd, mode, encoding=encoding)

    monkeypatch.setattr(os, "fdopen", spy)
    baseline_failure.failure_write(coverage, baseline_failure.BaselineFailureRecord("F", (_NODEID,)))
    assert seen["encoding"] == "utf-8"
