"""A real campaign: coverage measured by pytest-fast, mutants judged by live daemons (``balamut mutate``)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from balamut.config import load_profile
from balamut.coverage.measure import MeasureRequest, measure_coverage
from balamut.engine import stats
from balamut.isolation import NoIsolation
from bm_support import make_project

pytestmark = pytest.mark.daemon

type Row = dict[str, str | int | list[str]]


def _measure(root: Path) -> Path:
    profile = load_profile(root)
    previous = Path.cwd()
    os.chdir(root)
    try:
        result = measure_coverage(
            MeasureRequest(profile=profile, coverage_file=root / ".balamut/cov/coverage", workers=2),
            isolation=NoIsolation(),
        )
    finally:
        os.chdir(previous)
    assert result.returncode == 0, result.detail
    return result.coverage_file


def _mutate(root: Path, coverage: Path | None, *extra: str) -> subprocess.CompletedProcess[str]:
    """``balamut mutate`` over the sample module; without ``coverage`` it uses (and maintains) the default substrate."""
    argv = [sys.executable, "-m", "balamut", "mutate", "src/pkg/calc.py", "--kind", "src"]
    if coverage is not None:
        argv += ["--coverage", str(coverage)]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "BALAMUT_"))}
    return subprocess.run(
        [*argv, "--json", "--progress", "none", *extra],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _rows(result: subprocess.CompletedProcess[str]) -> list[Row]:
    assert result.returncode == 0, result.stderr
    rows: list[Row] = json.loads(result.stdout)
    return rows


def _one(rows: list[Row], *, line: int, original: str, mutated: str) -> Row:
    matches = [
        row for row in rows if row["lineno"] == line and row["original"] == original and row["mutated"] == mutated
    ]
    assert len(matches) == 1, [(row["lineno"], row["original"], row["mutated"]) for row in rows]
    return matches[0]


def test_a_real_campaign_kills_survives_and_reuses_its_cache(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    coverage = _measure(root)
    stats_path = tmp_path / "stats.json"

    first = _rows(_mutate(root, coverage, "--stats-json", str(stats_path)))

    statuses = {str(row["status"]) for row in first}
    assert {"killed", "survived", "no_coverage"} <= statuses
    # `return low` -> `return None` breaks test_clamp_low; no test reaches the `value > high` branch's return.
    killed = _one(first, line=3, original="return low", mutated="return None")
    assert killed["status"] == "killed"
    assert killed["killed_by"] == ["tests/test_calc.py::test_clamp_low"]
    assert _one(first, line=5, original="return high", mutated="return None")["status"] == "no_coverage"
    # `age >= 18` -> `age > 18`: the only test uses 30, so the boundary survives.
    assert _one(first, line=10, original="age >= 18", mutated="age > 18")["status"] == "survived"
    snapshot = stats.read_campaign_stats(stats_path)
    assert snapshot is not None
    assert snapshot.complete
    assert snapshot.enumerated == len(first)

    second = _rows(_mutate(root, coverage, "--stats-json", str(stats_path)))

    assert {row["key"]: row["status"] for row in second} == {row["key"]: row["status"] for row in first}
    again = stats.read_campaign_stats(stats_path)
    assert again is not None
    assert again.executed == 0, "every terminal verdict comes from the cache on an unchanged tree"


def test_a_test_written_for_a_survivor_kills_it_on_the_next_campaign(tmp_path: Path) -> None:
    """No `balamut substrate` step: the first campaign builds the substrate, the new test reaches the coverage through
    a refresh of its file, not a rebuild."""
    root = make_project(tmp_path / "proj")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    def boundary(result: subprocess.CompletedProcess[str]) -> Row:
        return _one(_rows(result), line=10, original="age >= 18", mutated="age > 18")

    first = _mutate(root, None)

    assert boundary(first)["status"] == "survived"
    assert "measuring the suite under coverage" in first.stderr
    tests = root / "tests/test_calc.py"
    killing = "\n\ndef test_adult_from_18() -> None:\n    assert is_adult(18)\n"
    tests.write_text(tests.read_text(encoding="utf-8") + killing, encoding="utf-8")

    second = _mutate(root, None)

    killed = boundary(second)
    assert killed["status"] == "killed"
    assert killed["killed_by"] == ["tests/test_calc.py::test_adult_from_18"]
    assert "measuring the suite under coverage" not in second.stderr
    assert "coverage refresh: 1 files diverged from the substrate" in second.stderr
