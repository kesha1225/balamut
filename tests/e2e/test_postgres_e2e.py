"""A real gate with PostgreSQL isolation: template from the migration, one clone per slot, no clone left behind."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from bm_support import write_file
from bm_support.diff import findings

pytestmark = [pytest.mark.daemon, pytest.mark.postgres]

_KEYS = ("BALAMUT_TEST_PG_HOST", "BALAMUT_TEST_PG_PORT", "BALAMUT_TEST_PG_USER", "BALAMUT_TEST_PG_PASSWORD")

_PSQL = (
    'PGPASSWORD="$APP_DB_PASS" psql -h "$APP_DB_HOST" -p "$APP_DB_PORT" -U "$APP_DB_USER" -d "$APP_DB_NAME"'
    " -v ON_ERROR_STOP=1 -q"
)

_TEST = """\
import os
import subprocess

from pkg.limits import allowed


def _limit() -> int:
    env = {**os.environ, "PGPASSWORD": os.environ["APP_DB_PASS"]}
    argv = ["psql", "-h", os.environ["APP_DB_HOST"], "-p", os.environ["APP_DB_PORT"], "-U", os.environ["APP_DB_USER"]]
    argv += ["-d", os.environ["APP_DB_NAME"], "-tAqc", "SELECT value FROM setting WHERE name = 'limit'"]
    return int(subprocess.run(argv, env=env, capture_output=True, text=True, check=True).stdout.strip())


def test_the_limit_is_allowed() -> None:
    assert allowed(_limit())


def test_above_the_limit_is_refused() -> None:
    assert not allowed(_limit() + 1)
"""


def _environment() -> dict[str, str]:
    if not all(os.environ.get(key) for key in _KEYS):
        pytest.skip("BALAMUT_TEST_PG_* is not set")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "BALAMUT_"))}
    return {
        **env,
        "APP_DB_HOST": os.environ["BALAMUT_TEST_PG_HOST"],
        "APP_DB_PORT": os.environ["BALAMUT_TEST_PG_PORT"],
        "APP_DB_USER": os.environ["BALAMUT_TEST_PG_USER"],
        "APP_DB_PASS": os.environ["BALAMUT_TEST_PG_PASSWORD"],
        "APP_DB_NAME": os.environ.get("BALAMUT_TEST_PG_DATABASE", "postgres"),
    }


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *args], cwd=root, check=True, capture_output=True
    )


def _clones(env: dict[str, str]) -> list[str]:
    query = "SELECT datname FROM pg_database WHERE datname LIKE 'balamut\\_%\\_w%' ORDER BY 1"
    argv = [
        "psql",
        "-h",
        env["APP_DB_HOST"],
        "-p",
        env["APP_DB_PORT"],
        "-U",
        env["APP_DB_USER"],
        "-d",
        env["APP_DB_NAME"],
    ]
    completed = subprocess.run(
        [*argv, "-tAqc", query],
        env={**env, "PGPASSWORD": env["APP_DB_PASS"]},
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def test_an_isolated_gate_runs_every_slot_on_its_own_clone(tmp_path: Path) -> None:
    env = _environment()
    root = tmp_path / "proj"
    write_file(
        root / "pyproject.toml",
        '[project]\nname = "sample"\nversion = "0"\n\n'
        '[tool.pytest.ini_options]\npythonpath = ["src"]\naddopts = "-p no:cacheprovider"\n\n'
        '[tool.balamut]\nconfig = "balamut_conf.py"\n',
    )
    write_file(
        root / "balamut_conf.py",
        textwrap.dedent(
            f"""
            from balamut.config import DatabaseEnv, IsolationKind, IsolationSettings, init

            init(
                isolation=IsolationSettings(
                    kind=IsolationKind.POSTGRES_TEMPLATE,
                    env=DatabaseEnv(
                        host="APP_DB_HOST",
                        port="APP_DB_PORT",
                        database="APP_DB_NAME",
                        user="APP_DB_USER",
                        password="APP_DB_PASS",
                    ),
                    migrate=("sh", "-c", {json.dumps(_PSQL + " -f schema.sql")}),
                    schema_inputs=("schema.sql",),
                )
            )
            """
        ),
    )
    write_file(
        root / "schema.sql",
        "CREATE TABLE setting (name text PRIMARY KEY, value int);\nINSERT INTO setting VALUES ('limit', 3);\n",
    )
    write_file(root / "src/pkg/__init__.py", "")
    write_file(root / "src/pkg/limits.py", "def allowed(count: int) -> bool:\n    return count <= 3\n")
    write_file(root / "tests/test_limits.py", _TEST)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    write_file(root / "src/pkg/limits.py", "def allowed(count: int) -> bool:\n    return 0 < count <= 3\n")

    completed = subprocess.run(
        [sys.executable, "-m", "balamut", "diff", "--base", "main", "--agent-json", "--daemons", "2"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    assert completed.stdout, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] in {"CLEAN", "FINDINGS"}, completed.stderr
    counts = report["counts"]
    assert counts["killed"] >= 1, completed.stderr
    # `0 < count` is never exercised at zero: its mutants survive, the upper bound's are killed by the DB limit.
    assert {finding["line"] for finding in findings(report)} <= {2}
    assert _clones(env) == [], "every slot clone is dropped when its campaign ends"
