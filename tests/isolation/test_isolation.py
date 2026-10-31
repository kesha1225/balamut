"""Per-slot environment isolation (balamut.isolation).

Every daemon (and every coverage slot) of a campaign gets its own environment overlay. ``postgres-template``
builds one schema-only template per migration fingerprint and clones it per slot; ``shell`` delegates to a
project command; ``none`` shares the caller's environment.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from balamut.config import DatabaseEnv, IsolationKind, IsolationSettings, Profile
from balamut.isolation import IsolationError, isolation_for, postgres, shell, slot_prefix
from balamut.isolation.postgres import (
    PostgresTemplate,
    PsqlAdmin,
    _literal,
    _project_files,
    _quote,
    _required,
    schema_fingerprint,
)
from balamut.isolation.shell import ShellIsolation
from bm_support import make_layout, write_file

ENV = DatabaseEnv(
    host="APP_DB_HOST", port="APP_DB_PORT", database="APP_DB_NAME", user="APP_DB_USER", password="APP_DB_PASS"
)
_ENVIRON = {
    "APP_DB_HOST": "127.0.0.1",
    "APP_DB_PORT": "5432",
    "APP_DB_NAME": "app_test",
    "APP_DB_USER": "app",
    "APP_DB_PASS": "secret",
}


@dataclass
class FakeAdmin:
    """An in-memory PostgreSQL catalog: database name -> comment."""

    databases: dict[str, str] = field(default_factory=dict[str, str])
    created_from: dict[str, str] = field(default_factory=dict[str, str])
    locales: dict[str, str | None] = field(default_factory=dict[str, str | None])
    terminated: list[str] = field(default_factory=list[str])
    probed: list[str] = field(default_factory=list[str])
    drops: list[tuple[str, bool]] = field(default_factory=list[tuple[str, bool]])
    comments: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    fail_rename_once: bool = False

    def exists(self, name: str) -> bool:
        return name in self.databases

    def create(self, name: str, *, template: str, locale: str | None = None) -> None:
        if name in self.databases:
            raise IsolationError(f"database {name} exists")
        if template != "template0" and template not in self.databases:
            raise IsolationError(f"template {template} is missing")
        self.databases[name] = ""
        self.created_from[name] = template
        self.locales[name] = locale

    def drop(self, name: str, *, force: bool) -> None:
        self.drops.append((name, force))
        self.databases.pop(name, None)

    def rename(self, old: str, new: str) -> bool:
        if self.fail_rename_once:
            self.fail_rename_once = False
            return False
        if new in self.databases:
            return False
        self.databases[new] = self.databases.pop(old)
        self.locales[new] = self.locales.pop(old, None)
        self.created_from[new] = self.created_from.pop(old, "")
        return True

    def terminate(self, name: str) -> None:
        self.terminated.append(name)

    def comment(self, name: str, text: str) -> None:
        self.comments.append((name, text))
        if name in self.databases:
            self.databases[name] = text

    def list_with_comments(self, prefix: str) -> list[tuple[str, str]]:
        return [(name, comment) for name, comment in self.databases.items() if name.startswith(prefix)]

    def probe(self, database: str) -> None:
        self.probed.append(database)


def _profile(tmp_path: Path, *, locale: str | None = None) -> Profile:
    settings = IsolationSettings(
        kind=IsolationKind.POSTGRES_TEMPLATE,
        env=ENV,
        migrate=("python", "-c", "pass"),
        schema_inputs=("migrations/**",),
        locale=locale,
    )
    return Profile(layout=make_layout(tmp_path), isolation=settings, state_dir=tmp_path / ".balamut")


def _template(tmp_path: Path, admin: FakeAdmin, migrations: list[dict[str, str]]) -> PostgresTemplate:
    write_file(tmp_path / "migrations/0001.py", "STEP = 1\n")

    def migrate(env: dict[str, str]) -> None:
        migrations.append(env)

    return PostgresTemplate(_profile(tmp_path), environ=_ENVIRON, admin=admin, migrate=migrate)


def test_prepare_builds_the_template_once_and_clones_one_database_per_slot(tmp_path: Path) -> None:
    admin = FakeAdmin()
    migrations: list[dict[str, str]] = []
    template = _template(tmp_path, admin, migrations)

    overlays = template.prepare(3, "balamut_abc")

    assert overlays == (
        {"APP_DB_NAME": "balamut_abc_w0"},
        {"APP_DB_NAME": "balamut_abc_w1"},
        {"APP_DB_NAME": "balamut_abc_w2"},
    )
    schema = template.schema_name()
    assert schema.startswith("balamut_schema_")
    assert {admin.created_from[f"balamut_abc_w{k}"] for k in range(3)} == {schema}
    assert len(migrations) == 1
    assert migrations[0]["APP_DB_NAME"].startswith(f"{schema}_b")  # built under a temporary name
    assert migrations[0]["APP_DB_HOST"] == "127.0.0.1"
    assert admin.databases[schema].startswith("balamut-schema ")

    template.prepare(2, "balamut_def")
    assert len(migrations) == 1  # the template is shared by every later call with the same migrations


def test_prepare_recreates_existing_clones_from_a_clean_template(tmp_path: Path) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    template.prepare(1, "balamut_abc")
    admin.databases["balamut_abc_w0"] = "dirty"
    template.prepare(1, "balamut_abc")
    assert admin.databases["balamut_abc_w0"] == ""


def test_cleanup_drops_only_the_campaign_clones(tmp_path: Path) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    template.prepare(2, "balamut_abc")
    template.cleanup(2, "balamut_abc")
    assert set(admin.databases) == {template.schema_name()}


def test_a_migration_edit_yields_a_new_template(tmp_path: Path) -> None:
    admin = FakeAdmin()
    migrations: list[dict[str, str]] = []
    template = _template(tmp_path, admin, migrations)
    first = template.schema_name()
    write_file(tmp_path / "migrations/0002.py", "STEP = 2\n")
    second = PostgresTemplate(
        _profile(tmp_path), environ=_ENVIRON, admin=admin, migrate=migrations.append
    ).schema_name()
    assert first != second


def test_lost_rename_race_reuses_the_winner(tmp_path: Path) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    schema = template.schema_name()
    admin.fail_rename_once = True  # a concurrent call publishes the template while this one migrates

    def migrate_and_publish(env: dict[str, str]) -> None:
        admin.databases[schema] = "balamut-schema 1"

    racing = PostgresTemplate(_profile(tmp_path), environ=_ENVIRON, admin=admin, migrate=migrate_and_publish)
    racing.prepare(1, "balamut_abc")
    assert admin.created_from["balamut_abc_w0"] == schema
    assert not any(name.startswith(f"{schema}_b") for name in admin.databases)


def test_stale_templates_are_evicted(tmp_path: Path) -> None:
    now = int(time.time())
    admin = FakeAdmin(
        databases={
            "balamut_schema_old": f"balamut-schema {now - 8 * 24 * 3600}",
            "balamut_schema_fresh": f"balamut-schema {now - 3600}",
            "balamut_schema_old_b1": f"balamut-schema-build {now - 2 * 3600}",
            "balamut_schema_foreign": "something else",
        }
    )
    template = _template(tmp_path, admin, [])
    template.prepare(1, "balamut_abc")
    assert "balamut_schema_old" not in admin.databases
    assert "balamut_schema_old_b1" not in admin.databases
    assert "balamut_schema_fresh" in admin.databases
    assert "balamut_schema_foreign" in admin.databases


def test_template_locale_is_applied(tmp_path: Path) -> None:
    admin = FakeAdmin()
    write_file(tmp_path / "migrations/0001.py", "STEP = 1\n")
    template = PostgresTemplate(_profile(tmp_path, locale="C.UTF-8"), environ=_ENVIRON, admin=admin, migrate=_noop)
    template.prepare(1, "balamut_abc")
    assert admin.locales[template.schema_name()] == "C.UTF-8"


def _noop(_env: dict[str, str]) -> None:
    return None


def test_probe_checks_the_configured_target(tmp_path: Path) -> None:
    admin = FakeAdmin()
    _template(tmp_path, admin, []).probe()
    assert admin.probed == ["app_test"]


def test_missing_environment_variable_is_named(tmp_path: Path) -> None:
    environ = {key: value for key, value in _ENVIRON.items() if key != "APP_DB_PASS"}
    template = PostgresTemplate(_profile(tmp_path), environ=environ, admin=FakeAdmin(), migrate=_noop)
    with pytest.raises(IsolationError, match="APP_DB_PASS"):
        template.prepare(1, "balamut_abc")


def test_clone_names_respect_the_identifier_limit(tmp_path: Path) -> None:
    template = _template(tmp_path, FakeAdmin(), [])
    with pytest.raises(IsolationError, match="63"):
        template.prepare(1, "balamut_" + "x" * 60)


def test_schema_fingerprint_covers_inputs_and_migration_command(tmp_path: Path) -> None:
    write_file(tmp_path / "migrations/0001.py", "STEP = 1\n")
    settings = IsolationSettings(
        kind=IsolationKind.POSTGRES_TEMPLATE, env=ENV, migrate=("a",), schema_inputs=("migrations/**",)
    )
    base = schema_fingerprint(tmp_path, settings)
    other_command = IsolationSettings(
        kind=IsolationKind.POSTGRES_TEMPLATE, env=ENV, migrate=("b",), schema_inputs=("migrations/**",)
    )
    assert schema_fingerprint(tmp_path, other_command) != base
    write_file(tmp_path / "migrations/__pycache__/0001.cpython-314.pyc", "bytecode")
    assert schema_fingerprint(tmp_path, settings) == base  # build artifacts are not inputs
    write_file(tmp_path / "migrations/0001.py", "STEP = 2\n")
    assert schema_fingerprint(tmp_path, settings) != base


def test_slot_prefix_is_unique_and_safe() -> None:
    first, second = slot_prefix(), slot_prefix()
    assert first != second
    assert first.startswith("balamut_")
    assert all(char.isalnum() or char == "_" for char in first)
    assert len(first) == len("balamut_") + 12  # exactly twelve hex characters from the uuid


# none / shell


def test_none_isolation_shares_the_environment(tmp_path: Path) -> None:
    isolation = isolation_for(Profile(layout=make_layout(tmp_path), state_dir=tmp_path / ".balamut"), environ={})
    assert isolation.isolated is False
    assert isolation.prepare(3, "balamut_abc") == ({}, {}, {})
    isolation.cleanup(3, "balamut_abc")
    isolation.probe()


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = write_file(tmp_path / name, f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_shell_isolation_reads_overlays_from_the_prepare_command(tmp_path: Path) -> None:
    log = tmp_path / "log.txt"
    prepare = _script(
        tmp_path,
        "prepare.sh",
        f'echo "prepare $BALAMUT_SLOTS $BALAMUT_SLOT_PREFIX" >> {log}\n'
        'printf \'[{"QUEUE": "q0"}, {"QUEUE": "q1"}]\'',
    )
    cleanup = _script(tmp_path, "cleanup.sh", f'echo "cleanup $BALAMUT_SLOTS $BALAMUT_SLOT_PREFIX" >> {log}')
    profile = Profile(
        layout=make_layout(tmp_path),
        state_dir=tmp_path / ".balamut",
        isolation=IsolationSettings(kind=IsolationKind.SHELL, prepare=(prepare,), cleanup=(cleanup,)),
    )
    isolation = isolation_for(profile, environ=dict(os.environ))
    assert isolation.isolated is True
    assert isolation.prepare(2, "balamut_abc") == ({"QUEUE": "q0"}, {"QUEUE": "q1"})
    isolation.cleanup(2, "balamut_abc")
    assert log.read_text(encoding="utf-8").splitlines() == ["prepare 2 balamut_abc", "cleanup 2 balamut_abc"]


@pytest.mark.parametrize("output", ["not json", '[{"A": "1"}]', '[{"A": 1}, {"A": "2"}]', '{"A": "1"}'])
def test_shell_isolation_rejects_malformed_overlays(tmp_path: Path, output: str) -> None:
    prepare = _script(tmp_path, "prepare.sh", f"printf '%s' '{output}'")
    profile = Profile(
        layout=make_layout(tmp_path),
        state_dir=tmp_path / ".balamut",
        isolation=IsolationSettings(kind=IsolationKind.SHELL, prepare=(prepare,)),
    )
    with pytest.raises(IsolationError):
        isolation_for(profile, environ=dict(os.environ)).prepare(2, "balamut_abc")


def test_shell_isolation_failure_carries_the_output(tmp_path: Path) -> None:
    prepare = _script(tmp_path, "prepare.sh", "echo boom >&2\nexit 3")
    profile = Profile(
        layout=make_layout(tmp_path),
        state_dir=tmp_path / ".balamut",
        isolation=IsolationSettings(kind=IsolationKind.SHELL, prepare=(prepare,)),
    )
    with pytest.raises(IsolationError, match="boom"):
        isolation_for(profile, environ=dict(os.environ)).prepare(1, "balamut_abc")


# a real PostgreSQL (BALAMUT_TEST_PG_HOST / _PORT / _USER / _PASSWORD), skipped when absent


def _pg_environ() -> dict[str, str] | None:
    keys = ("BALAMUT_TEST_PG_HOST", "BALAMUT_TEST_PG_PORT", "BALAMUT_TEST_PG_USER", "BALAMUT_TEST_PG_PASSWORD")
    if not all(os.environ.get(key) for key in keys):
        return None
    return {
        "APP_DB_HOST": os.environ["BALAMUT_TEST_PG_HOST"],
        "APP_DB_PORT": os.environ["BALAMUT_TEST_PG_PORT"],
        "APP_DB_USER": os.environ["BALAMUT_TEST_PG_USER"],
        "APP_DB_PASS": os.environ["BALAMUT_TEST_PG_PASSWORD"],
        "APP_DB_NAME": os.environ.get("BALAMUT_TEST_PG_DATABASE", "postgres"),
        "PATH": os.environ.get("PATH", ""),
    }


@pytest.mark.postgres
def test_real_postgres_template_round_trip(tmp_path: Path) -> None:
    environ = _pg_environ()
    if environ is None:
        pytest.skip("BALAMUT_TEST_PG_* is not set")
    write_file(tmp_path / "migrations/schema.sql", "CREATE TABLE item(id integer primary key, name text);\n")
    marker = uuid.uuid4().hex[:8]
    profile = Profile(
        layout=make_layout(tmp_path),
        state_dir=tmp_path / ".balamut",
        isolation=IsolationSettings(
            kind=IsolationKind.POSTGRES_TEMPLATE,
            env=ENV,
            migrate=(
                "sh",
                "-c",
                (
                    'PGPASSWORD="$APP_DB_PASS" psql -h "$APP_DB_HOST" -p "$APP_DB_PORT" -U "$APP_DB_USER"'
                    ' -d "$APP_DB_NAME" -v ON_ERROR_STOP=1 -q -f migrations/schema.sql'
                    f" -c \"COMMENT ON TABLE item IS '{marker}'\""
                ),
            ),
            schema_inputs=("migrations/**",),
        ),
    )
    admin = PsqlAdmin(environ, ENV)
    template = PostgresTemplate(profile, environ=environ, admin=admin)
    prefix = slot_prefix()
    try:
        overlays = template.prepare(2, prefix)
        for overlay in overlays:
            out = subprocess.run(
                [
                    "psql",
                    "-h",
                    environ["APP_DB_HOST"],
                    "-p",
                    environ["APP_DB_PORT"],
                    "-U",
                    environ["APP_DB_USER"],
                    "-d",
                    overlay["APP_DB_NAME"],
                    "-tAqc",
                    "SELECT obj_description('item'::regclass)",
                ],
                env={**environ, "PGPASSWORD": environ["APP_DB_PASS"]},
                capture_output=True,
                text=True,
                check=True,
            )
            assert out.stdout.strip() == marker
    finally:
        template.cleanup(2, prefix)
        admin.drop(template.schema_name(), force=True)
    assert not admin.exists(f"{prefix}_w0")


def test_dumped_overlays_are_plain_json() -> None:
    assert json.loads(json.dumps(({"A": "1"},))) == [{"A": "1"}]


# ── postgres: focused unit tests that avoid a live PostgreSQL ──────────────────────────────────


class _FakeRun:
    """A ``subprocess.run`` stand-in that records every command and its keyword arguments."""

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, command: list[str], **kwargs: object) -> SimpleNamespace:
        self.calls.append(list(command))
        self.kwargs.append(kwargs)
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def _psql_admin(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", stderr: str = "", returncode: int = 0
) -> tuple[PsqlAdmin, _FakeRun]:
    admin = PsqlAdmin(_ENVIRON, ENV)
    run = _FakeRun(stdout=stdout, stderr=stderr, returncode=returncode)
    monkeypatch.setattr("balamut.isolation.postgres.subprocess.run", run)
    return admin, run


def test_quote_escapes_double_quotes() -> None:
    assert _quote("plain") == '"plain"'
    assert _quote('a"b') == '"a""b"'


def test_literal_escapes_single_quotes() -> None:
    assert _literal("plain") == "'plain'"
    assert _literal("a'b") == "'a''b'"


def test_required_rejects_missing_and_empty_values() -> None:
    with pytest.raises(IsolationError, match="APP_DB_HOST"):
        _required({}, "APP_DB_HOST")
    with pytest.raises(IsolationError, match="APP_DB_HOST"):
        _required({"APP_DB_HOST": ""}, "APP_DB_HOST")
    assert _required({"APP_DB_HOST": "127.0.0.1"}, "APP_DB_HOST") == "127.0.0.1"


def test_postgres_module_constants_are_loaded() -> None:
    reloaded = importlib.reload(postgres)
    assert reloaded._IDENTIFIER_LIMIT == 63
    assert reloaded._TEMPLATE_TTL_SECONDS == 7 * 24 * 3600
    assert reloaded._BUILD_TTL_SECONDS == 3600


def test_project_files_uses_git_listing_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "migrations/0001.py", "STEP = 1\n")
    write_file(tmp_path / "migrations/0002.py", "STEP = 2\n")
    seen: list[tuple[Path, tuple[str, ...]]] = []

    def fake_git_output_in(repo: Path, *args: str) -> str:
        seen.append((repo, args))
        return "migrations/0001.py\0migrations/0002.py\0"

    monkeypatch.setattr("balamut.isolation.postgres.git_output_in", fake_git_output_in)
    assert _project_files(tmp_path) == ["migrations/0001.py", "migrations/0002.py"]
    assert seen == [(tmp_path, ("ls-files", "-z", "--cached", "--others", "--exclude-standard"))]


def test_project_files_skips_git_listed_paths_missing_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "migrations/0001.py", "STEP = 1\n")

    def fake_git_output_in(repo: Path, *args: str) -> str:
        return "migrations/ghost.py\0migrations/0001.py\0"

    monkeypatch.setattr("balamut.isolation.postgres.git_output_in", fake_git_output_in)
    assert _project_files(tmp_path) == ["migrations/0001.py"]


def test_schema_fingerprint_distinguishes_file_names(tmp_path: Path) -> None:
    write_file(tmp_path / "migrations/0001.py", "STEP = 1\n")
    settings = IsolationSettings(
        kind=IsolationKind.POSTGRES_TEMPLATE, env=ENV, migrate=("a",), schema_inputs=("migrations/**",)
    )
    first = schema_fingerprint(tmp_path, settings)
    (tmp_path / "migrations/0001.py").rename(tmp_path / "migrations/0002.py")
    assert schema_fingerprint(tmp_path, settings) != first


def test_schema_fingerprint_skips_non_matching_files_without_stopping(tmp_path: Path) -> None:
    write_file(tmp_path / "aaa.txt", "ignored\n")  # sorts before migrations/, matches no schema input
    write_file(tmp_path / "migrations/0001.py", "STEP = 1\n")
    settings = IsolationSettings(
        kind=IsolationKind.POSTGRES_TEMPLATE, env=ENV, migrate=("a",), schema_inputs=("migrations/**",)
    )
    base = schema_fingerprint(tmp_path, settings)
    write_file(tmp_path / "migrations/0001.py", "STEP = 2\n")
    assert schema_fingerprint(tmp_path, settings) != base


def test_schema_fingerprint_matches_any_schema_input_pattern(tmp_path: Path) -> None:
    write_file(tmp_path / "migrations/0001.py", "STEP = 1\n")
    settings = IsolationSettings(
        kind=IsolationKind.POSTGRES_TEMPLATE,
        env=ENV,
        migrate=("a",),
        schema_inputs=("migrations/**", "other/**"),
    )
    base = schema_fingerprint(tmp_path, settings)
    write_file(tmp_path / "migrations/0001.py", "STEP = 2\n")
    assert schema_fingerprint(tmp_path, settings) != base


# ── PsqlAdmin (subprocess.run is faked; no live server) ─────────────────────────────────────────


def test_sql_builds_the_psql_command(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, run = _psql_admin(monkeypatch, stdout="1\n")
    assert admin._sql("SELECT 1") == "1\n"
    command = run.calls[0]
    assert command[:7] == ["psql", "-h", "127.0.0.1", "-p", "5432", "-U", "app"]
    assert command[7:9] == ["-d", "postgres"]
    assert command[9:] == ["-v", "ON_ERROR_STOP=1", "-tAqc", "SELECT 1"]


def test_sql_passes_capture_text_and_no_check(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, run = _psql_admin(monkeypatch)
    admin._sql("SELECT 1")
    kwargs = run.kwargs[0]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["env"] == admin._env


def test_sql_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch, returncode=1, stderr="boom")
    with pytest.raises(IsolationError, match="boom"):
        admin._sql("SELECT 1")


def test_sql_returns_none_on_failure_without_check(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch, returncode=1, stderr="boom")
    assert admin._sql("SELECT 1", check=False) is None


def test_exists_detects_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch, stdout="1\n")
    assert admin.exists("app_test") is True


def test_exists_is_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch, stdout="")
    assert admin.exists("nope") is False


def test_create_builds_create_database_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, run = _psql_admin(monkeypatch)
    admin.create("my db", template="template0", locale="C.UTF-8")
    assert run.calls[0][-1] == (
        "CREATE DATABASE \"my db\" TEMPLATE \"template0\" ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'"
    )


def test_create_without_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, run = _psql_admin(monkeypatch)
    admin.create("db", template="template0")
    assert run.calls[0][-1] == 'CREATE DATABASE "db" TEMPLATE "template0"'


def test_drop_with_force(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, run = _psql_admin(monkeypatch)
    admin.drop("my db", force=True)
    assert run.calls[0][-1] == 'DROP DATABASE IF EXISTS "my db" WITH (FORCE)'


def test_drop_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, run = _psql_admin(monkeypatch)
    admin.drop("db", force=False)
    assert run.calls[0][-1] == 'DROP DATABASE IF EXISTS "db"'


def test_drop_passes_check_equal_to_force(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch)
    seen: list[bool] = []

    def fake_sql(sql: str, *, database: str = "postgres", check: bool = True) -> str | None:
        seen.append(check)
        return ""

    monkeypatch.setattr(admin, "_sql", fake_sql)
    admin.drop("db", force=False)
    admin.drop("db", force=True)
    assert seen == [False, True]


def test_rename_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch, stdout="ALTER DATABASE")
    assert admin.rename("old", "new") is True


def test_rename_returns_false_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch, returncode=1)
    assert admin.rename("old", "new") is False


def test_terminate_builds_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, run = _psql_admin(monkeypatch)
    admin.terminate("db")
    assert "pg_terminate_backend" in run.calls[0][-1]
    assert "datname='db'" in run.calls[0][-1]


def test_comment_runs_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, run = _psql_admin(monkeypatch)
    admin.comment("db", "text")
    assert run.calls[0][-1] == "COMMENT ON DATABASE \"db\" IS 'text'"


def test_comment_does_not_check(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch, returncode=1, stderr="boom")
    admin.comment("db", "text")  # no raise despite the failure: check=False


def test_list_with_comments_parses_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch, stdout="db1\tsome comment\ndb2\t\n")
    assert admin.list_with_comments("balamut_schema_") == [("db1", "some comment"), ("db2", "")]


def test_list_with_comments_handles_missing_output(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch, returncode=1)
    assert admin.list_with_comments("p") == []


def test_list_with_comments_escapes_like_wildcards_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, _ = _psql_admin(monkeypatch)
    seen: list[str] = []

    def fake_sql(sql: str, *, database: str = "postgres", check: bool = True) -> str | None:
        seen.append(sql)
        return ""

    monkeypatch.setattr(admin, "_sql", fake_sql)
    admin.list_with_comments(r"a_b\%")
    # Backslash first, then underscore, then percent: each step must escape the literal, not the
    # backslash the previous step just inserted.
    assert seen[0].endswith(r"LIKE 'a\_b\\\%%'")


def test_probe_runs_select_one(monkeypatch: pytest.MonkeyPatch) -> None:
    admin, run = _psql_admin(monkeypatch)
    admin.probe("app_test")
    assert run.calls[0][-1] == "SELECT 1"
    assert "app_test" in run.calls[0]


class _InitRecorder:
    called = False

    def __init__(self) -> None:
        _InitRecorder.called = True


class _RecordingPsqlAdmin(PsqlAdmin, _InitRecorder):
    """A PsqlAdmin with a cooperative second base: ``super().__init__`` must reach it."""


def test_psql_admin_calls_super_init() -> None:
    _InitRecorder.called = False
    _RecordingPsqlAdmin(_ENVIRON, ENV)
    assert _InitRecorder.called


# ── PostgresTemplate internals (FakeAdmin) ──────────────────────────────────────────────────────


def test_postgres_template_is_isolated(tmp_path: Path) -> None:
    assert _template(tmp_path, FakeAdmin(), []).isolated is True


def test_schema_name_uses_16_char_fingerprint(tmp_path: Path) -> None:
    template = _template(tmp_path, FakeAdmin(), [])
    assert len(template.schema_name()) == len("balamut_schema_") + 16


def test_fingerprint_matches_schema_name(tmp_path: Path) -> None:
    template = _template(tmp_path, FakeAdmin(), [])
    assert template.fingerprint() == template.schema_name()


def test_db_constructs_psql_admin_with_environ_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = PostgresTemplate(_profile(tmp_path), environ=_ENVIRON)
    calls: list[tuple[dict[str, str], DatabaseEnv]] = []

    def fake_psql_admin(environ: dict[str, str], names: DatabaseEnv) -> PsqlAdmin:
        calls.append((environ, names))
        return PsqlAdmin(environ, names)

    monkeypatch.setattr(postgres, "PsqlAdmin", fake_psql_admin)
    template._db()
    assert calls == [(template._environ, template._names)]


def test_clone_name_at_63_bytes_is_allowed(tmp_path: Path) -> None:
    template = _template(tmp_path, FakeAdmin(), [])
    prefix = "x" * 60
    assert len(f"{prefix}_w0".encode()) == 63
    assert template._clone_names(1, prefix) == [f"{prefix}_w0"]


def test_clone_name_at_64_bytes_is_rejected(tmp_path: Path) -> None:
    template = _template(tmp_path, FakeAdmin(), [])
    prefix = "x" * 61
    assert len(f"{prefix}_w0".encode()) == 64
    with pytest.raises(IsolationError, match="63"):
        template._clone_names(1, prefix)


def test_build_name_uses_millisecond_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = FakeAdmin()
    migrations: list[dict[str, str]] = []
    template = _template(tmp_path, admin, migrations)
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: 1500000.0)
    template.prepare(1, "balamut_abc")
    schema = template.schema_name()
    assert migrations[0]["APP_DB_NAME"] == f"{schema}_b500000000"


def test_build_drops_stale_build_with_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: 1500000.0)
    template.prepare(1, "balamut_abc")
    schema = template.schema_name()
    assert (f"{schema}_b500000000", True) in admin.drops


def test_build_comments_the_build_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: 1500000.0)
    template._build()
    schema = template.schema_name()
    assert (f"{schema}_b500000000", "balamut-schema-build 1500000") in admin.comments


def test_build_retries_rename_when_first_attempt_loses_race(tmp_path: Path) -> None:
    admin = FakeAdmin()
    admin.fail_rename_once = True
    template = _template(tmp_path, admin, [])
    schema = template.schema_name()
    template._build()
    assert schema in admin.databases
    assert admin.terminated  # the stuck migration session was terminated before the retry


def test_build_renames_the_build_database_to_the_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    calls: list[tuple[str, str]] = []

    def fake_rename(old: str, new: str) -> bool:
        calls.append((old, new))
        return True

    monkeypatch.setattr(admin, "rename", fake_rename)
    template._build()
    schema = template.schema_name()
    assert calls[0][0].startswith(f"{schema}_b")
    assert calls[0][1] == schema


def test_build_cleans_up_the_build_database_on_migration_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = FakeAdmin()
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: 1500000.0)

    def failing_migrate(env: dict[str, str]) -> None:
        raise RuntimeError("migration boom")

    template = PostgresTemplate(_profile(tmp_path), environ=_ENVIRON, admin=admin, migrate=failing_migrate)
    with pytest.raises(RuntimeError, match="boom"):
        template._build()
    schema = template.schema_name()
    # The finally cleanup drops the abandoned build with force: the last drop of that name.
    assert admin.drops[-1] == (f"{schema}_b500000000", True)


def test_clone_terminates_the_template_schema(tmp_path: Path) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    template.prepare(1, "balamut_abc")
    assert template.schema_name() in admin.terminated


def test_clone_drops_existing_clones_with_force(tmp_path: Path) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    template.prepare(1, "balamut_abc")
    assert ("balamut_abc_w0", True) in admin.drops


def test_clone_returns_false_when_schema_missing(tmp_path: Path) -> None:
    template = _template(tmp_path, FakeAdmin(), [])
    assert template._clone(["balamut_abc_w0"]) is False


def test_cleanup_drops_clones_with_force(tmp_path: Path) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    template.prepare(1, "balamut_abc")
    admin.drops.clear()
    template.cleanup(1, "balamut_abc")
    assert admin.drops == [("balamut_abc_w0", True)]


def test_evict_keeps_template_at_exact_ttl_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1500000
    admin = FakeAdmin(databases={"balamut_schema_edge": f"balamut-schema {now - 7 * 24 * 3600}"})
    template = _template(tmp_path, admin, [])
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: float(now))
    template.prepare(1, "balamut_abc")
    assert "balamut_schema_edge" in admin.databases


def test_evict_drops_template_just_past_the_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1500000
    admin = FakeAdmin(databases={"balamut_schema_stale": f"balamut-schema {now - (7 * 24 * 3600 + 1)}"})
    template = _template(tmp_path, admin, [])
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: float(now))
    template.prepare(1, "balamut_abc")
    assert "balamut_schema_stale" not in admin.databases


def test_evict_drops_abandoned_build_just_past_the_build_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1500000
    admin = FakeAdmin(databases={"balamut_schema_b_b1": f"balamut-schema-build {now - 3601}"})
    template = _template(tmp_path, admin, [])
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: float(now))
    template.prepare(1, "balamut_abc")
    assert "balamut_schema_b_b1" not in admin.databases


def test_evict_drops_template_with_unparseable_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1500000
    admin = FakeAdmin(databases={"balamut_schema_bad": "balamut-schema not-a-number"})
    template = _template(tmp_path, admin, [])
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: float(now))
    template.prepare(1, "balamut_abc")
    assert "balamut_schema_bad" not in admin.databases


def test_evict_ignores_entries_with_unknown_marks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1500000
    admin = FakeAdmin(
        databases={
            "balamut_schema_aaa": "unknown-mark 1",
            "balamut_schema_old": f"balamut-schema {now - 8 * 24 * 3600}",
        }
    )
    template = _template(tmp_path, admin, [])
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: float(now))
    template.prepare(1, "balamut_abc")
    assert "balamut_schema_old" not in admin.databases


def test_evict_continues_past_own_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1500000
    schema = _template(tmp_path, FakeAdmin(), []).schema_name()
    admin = FakeAdmin(
        databases={
            schema: "balamut-schema 1",
            "balamut_schema_old": f"balamut-schema {now - 8 * 24 * 3600}",
        }
    )
    template = PostgresTemplate(_profile(tmp_path), environ=_ENVIRON, admin=admin, migrate=_noop)
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: float(now))
    template.prepare(1, "balamut_abc")
    assert "balamut_schema_old" not in admin.databases


def test_evict_drops_stale_templates_without_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1500000
    admin = FakeAdmin(databases={"balamut_schema_old": f"balamut-schema {now - 8 * 24 * 3600}"})
    template = _template(tmp_path, admin, [])
    monkeypatch.setattr("balamut.isolation.postgres.time.time", lambda: float(now))
    template.prepare(1, "balamut_abc")
    assert ("balamut_schema_old", False) in admin.drops


@pytest.mark.parametrize("missing", ["APP_DB_HOST", "APP_DB_PORT", "APP_DB_USER"])
def test_prepare_requires_all_connection_variables(tmp_path: Path, missing: str) -> None:
    environ = {key: value for key, value in _ENVIRON.items() if key != missing}
    template = PostgresTemplate(_profile(tmp_path), environ=environ, admin=FakeAdmin(), migrate=_noop)
    with pytest.raises(IsolationError, match=missing):
        template.prepare(1, "balamut_abc")


def test_prepare_comments_the_schema_once(tmp_path: Path) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    template.prepare(1, "balamut_abc")
    schema = template.schema_name()
    assert len([comment for name, comment in admin.comments if name == schema]) == 1


def test_prepare_rebuilds_when_template_vanishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = FakeAdmin()
    template = _template(tmp_path, admin, [])
    schema = template.schema_name()
    admin.databases[schema] = "balamut-schema 1"  # the check sees an existing template
    real_exists = admin.exists
    seen = 0

    def vanish_exists(name: str) -> bool:
        nonlocal seen
        if name == schema:
            seen += 1
            if seen == 2:  # the template disappears between the check and the clone
                admin.databases.pop(schema, None)
                return False
        return real_exists(name)

    monkeypatch.setattr(admin, "exists", vanish_exists)
    overlays = template.prepare(1, "balamut_abc")
    assert overlays == ({"APP_DB_NAME": "balamut_abc_w0"},)
    assert schema in admin.databases
    assert admin.databases[schema].startswith("balamut-schema ")


def test_run_migrations_skips_without_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = IsolationSettings(
        kind=IsolationKind.POSTGRES_TEMPLATE, env=ENV, migrate=(), schema_inputs=("migrations/**",)
    )
    profile = Profile(layout=make_layout(tmp_path), isolation=settings, state_dir=tmp_path / ".balamut")
    template = PostgresTemplate(profile, environ=_ENVIRON, admin=FakeAdmin(), migrate=None)
    run = _FakeRun()
    monkeypatch.setattr("balamut.isolation.postgres.subprocess.run", run)
    template._run_migrations({})
    assert run.calls == []


def test_run_migrations_passes_capture_and_no_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = _template(tmp_path, FakeAdmin(), [])
    run = _FakeRun()
    monkeypatch.setattr("balamut.isolation.postgres.subprocess.run", run)
    template._run_migrations({})
    kwargs = run.kwargs[0]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["cwd"] == template._root
    assert kwargs["env"] == {}


def test_run_migrations_raises_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = _template(tmp_path, FakeAdmin(), [])
    run = _FakeRun(returncode=1, stdout="out", stderr="err")
    monkeypatch.setattr("balamut.isolation.postgres.subprocess.run", run)
    with pytest.raises(IsolationError, match="migration command exited"):
        template._run_migrations({})


def test_run_migrations_passes_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = _template(tmp_path, FakeAdmin(), [])
    run = _FakeRun(returncode=0)
    monkeypatch.setattr("balamut.isolation.postgres.subprocess.run", run)
    template._run_migrations({})  # no raise


# ── shell / none / __init__ ──────────────────────────────────────────────────────────────────────


def test_shell_isolation_fingerprint() -> None:
    isolation = ShellIsolation(("prepare", "arg"), ("cleanup",), environ={})
    assert isolation.fingerprint() == "shell:prepare\0arg\0|\0cleanup"


def test_shell_isolation_rejects_mixed_type_overlays(tmp_path: Path) -> None:
    prepare = _script(tmp_path, "prepare.sh", 'printf \'[{"A": "1", "B": 2}]\'')
    profile = Profile(
        layout=make_layout(tmp_path),
        state_dir=tmp_path / ".balamut",
        isolation=IsolationSettings(kind=IsolationKind.SHELL, prepare=(prepare,)),
    )
    with pytest.raises(IsolationError):
        isolation_for(profile, environ=dict(os.environ)).prepare(1, "balamut_abc")


def test_shell_run_reports_the_failing_command(monkeypatch: pytest.MonkeyPatch) -> None:
    isolation = ShellIsolation(("first", "second"), (), environ={})

    def boom(command: list[str], **kwargs: object) -> None:
        raise OSError("no such file")

    monkeypatch.setattr("balamut.isolation.shell.subprocess.run", boom)
    with pytest.raises(IsolationError, match="first"):
        isolation._run(("first", "second"), 1, "p")


def test_shell_run_passes_capture_and_text_and_no_check(monkeypatch: pytest.MonkeyPatch) -> None:
    isolation = ShellIsolation(("echo",), (), environ={})
    run = _FakeRun(stdout="[]")
    monkeypatch.setattr("balamut.isolation.shell.subprocess.run", run)
    isolation._run(("echo",), 1, "p")
    kwargs = run.kwargs[0]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False


def test_json_value_alias_evaluates_to_a_union() -> None:
    reloaded = importlib.reload(shell)
    value = reloaded._JsonValue.__value__
    assert get_args(value)


def test_isolation_for_postgres_template_returns_provider(tmp_path: Path) -> None:
    profile = Profile(
        layout=make_layout(tmp_path),
        isolation=IsolationSettings(
            kind=IsolationKind.POSTGRES_TEMPLATE,
            env=ENV,
            migrate=("python", "-c", "pass"),
            schema_inputs=("migrations/**",),
        ),
        state_dir=tmp_path / ".balamut",
    )
    assert isinstance(isolation_for(profile, environ=_ENVIRON), PostgresTemplate)
