"""Machine output of a campaign: raw JSON rows, invalidation keys and the flat report (balamut.engine.output)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from balamut.core.mutants import Mutant
from balamut.engine import output
from balamut.engine.results import MutantResult, MutantStatus
from bm_support.campaign import mutant


def test_raw_json_row_exposes_mutant_key_and_site() -> None:
    result = MutantResult(
        mutant=mutant("src/x.py", 10),
        status=MutantStatus.SURVIVED,
        covering=1,
        key="k1",
        covering_tests=("tests/x_test.py::test_x",),
    )

    row = output.result_json(result)

    assert row["key"] == "k1"
    assert row["covering_tests"] == ["tests/x_test.py::test_x"]
    assert (row["start_byte"], row["end_byte"]) == (0, 1)
    assert row["status"] == "survived"


def test_result_json_preserves_confirmed_killed_by_nodeids() -> None:
    result = MutantResult(
        mutant=mutant("src/x.py", 10),
        status=MutantStatus.KILLED,
        covering=2,
        key="killed-key",
        covering_tests=("tests/a_test.py::test_a", "tests/b_test.py::test_b"),
        killed_by=("tests/a_test.py::test_a",),
    )

    assert output.result_json(result)["killed_by"] == ["tests/a_test.py::test_a"]
    assert output.result_json(result)["bucket"] == ""


def test_result_json_assigns_a_stable_key_to_unsupported_mutant() -> None:
    result = MutantResult(mutant=mutant("src/x.py", 10), status=MutantStatus.UNSUPPORTED, covering=0)

    first = output.result_json(result)["key"]

    assert first
    assert first == output.result_json(result)["key"]


def test_invalidation_keys_read_raw_rows_and_direct_report_findings(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps([{"key": "k1"}, {"key": ""}, {}]), encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"findings": [{"key": "k2"}, {"key": "k3"}]}), encoding="utf-8")

    assert output.read_invalidation_keys(raw) == {"k1"}
    assert output.read_invalidation_keys(report) == {"k2", "k3"}


@pytest.mark.parametrize(
    "payload",
    [{"nested": {"key": "k1"}}, {"findings": "not-a-list"}, {"findings": [{"key": 1}]}, [{"key": None}], [], [1]],
)
def test_invalidation_rejects_malformed_or_keyless_input(tmp_path: Path, payload: output.JsonValue) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        output.read_invalidation_keys(path)


def test_invalidation_rejects_unreadable_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not read"):
        output.read_invalidation_keys(tmp_path / "missing.json")


def test_flat_report_groups_survivors_by_bucket_and_scores(capsys: pytest.CaptureFixture[str]) -> None:
    killed = MutantResult(mutant("src/x.py", 1), MutantStatus.KILLED, 1, "a")
    survived = MutantResult(mutant("src/x.py", 2), MutantStatus.SURVIVED, 3, "b")
    uncovered = MutantResult(mutant("src/x.py", 3), MutantStatus.NO_COVERAGE, 0, "c")

    output.flat_report([killed, survived, uncovered])

    out = capsys.readouterr().out
    assert "MUTATION SCORE: 50%" in out
    assert "src/x.py:2" in out
    assert "no_coverage 1" in out


def test_invalidation_skips_keyless_rows_in_a_raw_array(tmp_path: Path) -> None:
    path = tmp_path / "raw.json"
    path.write_text(json.dumps([{"key": "k1"}, {}, {"key": "k2"}]), encoding="utf-8")

    assert output.read_invalidation_keys(path) == {"k1", "k2"}


def test_invalidation_requires_a_key_on_every_report_finding(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"findings": [{"key": "k2"}, {}]}), encoding="utf-8")

    with pytest.raises(ValueError, match="direct key"):
        output.read_invalidation_keys(path)


def test_flat_report_writes_a_bucket_header_for_each_nonempty_group(
    capsys: pytest.CaptureFixture[str],
) -> None:
    survived = MutantResult(mutant("src/x.py", 2), MutantStatus.SURVIVED, 3, "b")

    output.flat_report([survived])

    assert "=== REVIEW: ambiguous" in capsys.readouterr().out


def test_flat_report_counts_absent_statuses_as_zero(capsys: pytest.CaptureFixture[str]) -> None:
    killed = MutantResult(mutant("src/x.py", 1), MutantStatus.KILLED, 1, "a")
    survived = MutantResult(mutant("src/x.py", 2), MutantStatus.SURVIVED, 1, "b")

    output.flat_report([killed, survived])

    out = capsys.readouterr().out
    assert "timeout 0" in out
    assert "no_coverage 0" in out
    assert "unsupported 0" in out


def test_flat_report_scores_an_empty_verdict_set_as_zero(capsys: pytest.CaptureFixture[str]) -> None:
    uncovered = MutantResult(mutant("src/x.py", 1), MutantStatus.NO_COVERAGE, 0, "c")

    output.flat_report([uncovered])

    out = capsys.readouterr().out
    assert "MUTATION SCORE: 0%" in out
    assert "killed 0 / survived 0" in out


def _semantic_mutant(lineno: int, status: MutantStatus, key: str) -> MutantResult:
    return MutantResult(
        mutant=Mutant("src/x.py", lineno, 0, 1, "a", "b", "compare:Lt->LtE", semantic=("tenant",)),
        status=status,
        covering=1,
        key=key,
    )


def test_flat_report_scores_killed_and_survived_by_semantic_class(capsys: pytest.CaptureFixture[str]) -> None:
    uncovered = MutantResult(mutant("src/x.py", 1), MutantStatus.NO_COVERAGE, 0, "c")
    results = [
        uncovered,
        _semantic_mutant(2, MutantStatus.KILLED, "a"),
        _semantic_mutant(3, MutantStatus.KILLED, "b"),
        _semantic_mutant(4, MutantStatus.SURVIVED, "c"),
    ]

    output.flat_report(results)

    assert "tenant 67% (2/3)" in capsys.readouterr().out


def test_flat_report_sorts_survivors_by_covering_then_path_then_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``key=(-covering, path, lineno)`` lists the heaviest covering first, then path, then line; the default
    dataclass order would sort by mutant first."""
    results = [
        MutantResult(mutant("src/a.py", 3), MutantStatus.SURVIVED, 1, "k3"),
        MutantResult(mutant("src/b.py", 1), MutantStatus.SURVIVED, 1, "k2"),
        MutantResult(mutant("src/a.py", 1), MutantStatus.SURVIVED, 2, "k1"),
    ]

    output.flat_report(results)

    lines = [line.strip().split("  ")[0] for line in capsys.readouterr().out.splitlines() if line.startswith("  ")]
    assert lines == ["src/a.py:1", "src/a.py:3", "src/b.py:1"]


def test_flat_report_forwards_custom_semantic_patterns(capsys: pytest.CaptureFixture[str]) -> None:
    """A custom semantic table reaches ``semantic_classes``; the default table would find no class for this mutant."""
    result = MutantResult(
        Mutant("src/x.py", 2, 0, 1, "balance < 0", "balance <= 0", "compare:Lt->LtE"),
        MutantStatus.SURVIVED,
        1,
        "a",
    )
    custom = (("money", ("balance",)),)

    output.flat_report([result], semantic=custom)

    assert "money 0% (0/1)" in capsys.readouterr().out


def test_output_import_time_type_alias_is_a_recursive_union() -> None:
    """The import-time type alias is a recursive union of the scalar JSON values."""
    import importlib.util
    import types
    import typing

    spec = importlib.util.spec_from_file_location("balamut.engine._output_probe", output.__file__)
    assert spec is not None
    loader = spec.loader
    assert loader is not None
    probe = importlib.util.module_from_spec(spec)
    loader.exec_module(probe)

    assert isinstance(probe.JsonValue, typing.TypeAliasType)
    assert isinstance(probe.JsonValue.__value__, types.UnionType)
    assert bool in probe.JsonValue.__value__.__args__
    assert int in probe.JsonValue.__value__.__args__
