"""A real gate run: ``balamut diff`` on a git repository builds the substrate, runs the campaign and gates it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bm_support import make_project, write_file
from bm_support.diff import findings

pytestmark = pytest.mark.daemon


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _diff(root: Path, *extra: str) -> tuple[int, dict[str, object]]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "BALAMUT_"))}
    completed = subprocess.run(
        [sys.executable, "-m", "balamut", "diff", "--base", "main", "--agent-json", *extra],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert completed.stdout, completed.stderr
    report: dict[str, object] = json.loads(completed.stdout)
    return completed.returncode, report


def test_a_real_diff_gate_finds_the_untested_boundary_and_reuses_its_substrate(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    write_file(root / ".gitignore", ".balamut/\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.email=a@b.c", "-c", "user.name=t", "add", ".")
    _git(root, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    calc = root / "src/pkg/calc.py"
    calc.write_text(calc.read_text(encoding="utf-8").replace("age >= 18", "age >= 21"), encoding="utf-8")

    first_code, first = _diff(root)

    assert first["status"] == "FINDINGS", first
    assert first_code == 1
    assert {finding["line"] for finding in findings(first)} == {10}
    substrate_report = first["substrate"]
    assert isinstance(substrate_report, dict)
    assert substrate_report["miss_reason"] == "missing_coverage"

    second_code, second = _diff(root)

    assert second_code == 1
    assert second["findings"] == first["findings"]
    reused = second["substrate"]
    assert isinstance(reused, dict)
    assert reused["reused"] is True
    counts = second["counts"]
    assert isinstance(counts, dict)
    assert counts["executed"] == 0, "an unchanged scope is resolved from the cache"
