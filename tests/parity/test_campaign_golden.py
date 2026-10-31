"""Golden campaign parity: the terminal verdicts of a real campaign over a fixed project.

Runs ``balamut mutate`` (real daemons, default substrate) on the sample project and pins, per
mutant: path, lineno, span, operator, original/mutated text, status, bucket, reason and the exact
covering/killing tests (as sorted sets). The golden is the behavioral contract of the whole engine
— enumeration, covering selection, verdicts, triage — for the capability rework. Regenerate only
with ``BALAMUT_WRITE_GOLDEN=1``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bm_support import make_project

pytestmark = pytest.mark.daemon

GOLDEN = Path(__file__).parent / "golden" / "campaign.json"

FIELDS: tuple[str, ...] = (
    "path",
    "lineno",
    "start_byte",
    "end_byte",
    "operator",
    "original",
    "mutated",
    "status",
    "bucket",
    "reason",
    "site_lineno",
    "site_end_lineno",
)

type Row = dict[str, str | int | list[str]]


def _campaign(root: Path) -> list[Row]:
    argv = [sys.executable, "-m", "balamut", "mutate", "src/pkg/calc.py", "--json", "--progress", "none"]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "BALAMUT_"))}
    result = subprocess.run(
        argv,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _pick(row: Row, field: str) -> str | int | list[str]:
    value = row[field]
    if field in {"covering", "killed_by"}:
        assert isinstance(value, list), f"{field} must be a list"
        return sorted(value)
    return value


def _sort_key(row: Row) -> tuple[str, int, int, int, str]:
    def integer(name: str) -> int:
        value = row[name]
        assert isinstance(value, int), f"{name} must be an int"
        return value

    assert isinstance(row["path"], str)
    assert isinstance(row["operator"], str)
    return (row["path"], integer("lineno"), integer("start_byte"), integer("end_byte"), row["operator"])


def _normalize(rows: list[Row]) -> list[Row]:
    normalized = [{field: _pick(row, field) for field in FIELDS} for row in rows]
    normalized.sort(key=_sort_key)
    return normalized


def test_campaign_verdicts_golden(tmp_path: Path) -> None:
    rows = _normalize(_campaign(make_project(tmp_path / "proj")))
    if os.environ.get("BALAMUT_WRITE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
        pytest.skip("golden written")
    expected: list[Row] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert rows == expected, "campaign verdicts drifted from the golden: review the diff, never regenerate blindly"
