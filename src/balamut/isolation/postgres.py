"""PostgreSQL isolation: one schema-only template per migration fingerprint, one clone per slot.

The template is a fresh database after the project's migration command. It is shared by every call and every
worktree with the same migration inputs (``balamut_schema_<fingerprint>``): migrations run once per schema
change, not once per call. It is built under a temporary name and appears through an atomic rename, so a
concurrent call sees it whole or not at all; a call that lost the race drops its copy and clones the winner.
Templates unused for a week and builds abandoned for an hour are evicted. The configured target database is
only a source of connection parameters and is never cloned.
"""

from __future__ import annotations

import fnmatch
import hashlib
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from balamut.config import DatabaseEnv, IsolationSettings, Profile
from balamut.gitutil import git_output_in
from balamut.isolation.base import Environ, EnvMap, EnvOverlays, IsolationError

_IDENTIFIER_LIMIT = 63  # PostgreSQL truncates identifiers longer than NAMEDATALEN - 1
_SCHEMA_PREFIX = "balamut_schema_"
_MARK = "balamut-schema"
_BUILD_MARK = f"{_MARK}-build"
_TEMPLATE_TTL_SECONDS = 7 * 24 * 3600
_BUILD_TTL_SECONDS = 3600

type Migrate = Callable[[EnvMap], None]


class PgAdmin(Protocol):
    """The catalog operations the template provider needs."""

    def exists(self, name: str, /) -> bool: ...

    def create(self, name: str, /, *, template: str, locale: str | None = None) -> None: ...

    def drop(self, name: str, /, *, force: bool) -> None: ...

    def rename(self, old: str, new: str, /) -> bool: ...

    def terminate(self, name: str, /) -> None: ...

    def comment(self, name: str, text: str, /) -> None: ...

    def list_with_comments(self, prefix: str, /) -> list[tuple[str, str]]: ...

    def probe(self, database: str, /) -> None: ...


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


class PsqlAdmin:
    """``PgAdmin`` over the ``psql`` client (no Python driver dependency)."""

    def __init__(self, environ: Environ, names: DatabaseEnv) -> None:
        super().__init__()
        self._host = _required(environ, names.host)
        self._port = _required(environ, names.port)
        self._user = _required(environ, names.user)
        self._env = {**environ, "PGPASSWORD": _required(environ, names.password)}

    def _sql(self, sql: str, *, database: str = "postgres", check: bool = True) -> str | None:
        command = ["psql", "-h", self._host, "-p", self._port, "-U", self._user, "-d", database]
        command += ["-v", "ON_ERROR_STOP=1", "-tAqc", sql]
        try:
            result = subprocess.run(command, env=self._env, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise IsolationError(f"could not run psql: {exc}") from exc
        if result.returncode != 0:
            if check:
                raise IsolationError(f"psql failed ({result.returncode}) on {sql!r}:\n{result.stderr}")
            return None
        return result.stdout

    def exists(self, name: str, /) -> bool:
        return (self._sql(f"SELECT 1 FROM pg_database WHERE datname={_literal(name)}") or "").strip() == "1"

    def create(self, name: str, /, *, template: str, locale: str | None = None) -> None:
        clause = f" ENCODING 'UTF8' LC_COLLATE {_literal(locale)} LC_CTYPE {_literal(locale)}" if locale else ""
        self._sql(f"CREATE DATABASE {_quote(name)} TEMPLATE {_quote(template)}{clause}")

    def drop(self, name: str, /, *, force: bool) -> None:
        suffix = " WITH (FORCE)" if force else ""
        self._sql(f"DROP DATABASE IF EXISTS {_quote(name)}{suffix}", check=force)

    def rename(self, old: str, new: str, /) -> bool:
        return self._sql(f"ALTER DATABASE {_quote(old)} RENAME TO {_quote(new)}", check=False) is not None

    def terminate(self, name: str, /) -> None:
        self._sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname={_literal(name)}")

    def comment(self, name: str, text: str, /) -> None:
        self._sql(f"COMMENT ON DATABASE {_quote(name)} IS {_literal(text)}", check=False)

    def list_with_comments(self, prefix: str, /) -> list[tuple[str, str]]:
        like = prefix.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%") + "%"
        output = self._sql(
            "SELECT datname || chr(9) || coalesce(shobj_description(oid, 'pg_database'), '')"
            f" FROM pg_database WHERE datname LIKE {_literal(like)}",
            check=False,
        )
        rows: list[tuple[str, str]] = []
        for line in (output or "").splitlines():
            name, _, comment = line.partition("\t")
            if name:
                rows.append((name, comment))
        return rows

    def probe(self, database: str, /) -> None:
        self._sql("SELECT 1", database=database)


def _required(environ: Environ, name: str) -> str:
    value = environ.get(name)
    if not value:
        raise IsolationError(f"environment variable {name} must be set for postgres-template isolation")
    return value


def _project_files(root: Path) -> list[str]:
    """Repository files (tracked and new, not ignored); every file below the root outside git."""
    listed = git_output_in(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed is not None:
        return sorted(path for path in listed.split("\0") if path and (root / path).is_file())
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def schema_fingerprint(root: Path, settings: IsolationSettings) -> str:
    """A digest of everything that shapes the template: matching input files, the command, its env, the locale."""
    digest = hashlib.sha256()
    for part in (
        *settings.migrate,
        "\0env",
        *(f"{k}={v}" for k, v in settings.migrate_env),
        f"\0locale={settings.locale}",
    ):
        digest.update(part.encode("utf-8") + b"\0")
    for path in _project_files(root):
        if "__pycache__" in path.split("/") or not any(
            fnmatch.fnmatchcase(path, pattern) for pattern in settings.schema_inputs
        ):
            continue
        digest.update(path.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256((root / path).read_bytes()).digest())
    return digest.hexdigest()


class PostgresTemplate:
    def __init__(
        self,
        profile: Profile,
        *,
        environ: Environ,
        admin: PgAdmin | None = None,
        migrate: Migrate | None = None,
    ) -> None:
        super().__init__()
        names = profile.isolation.env
        if names is None:
            raise IsolationError("postgres-template isolation needs [tool.balamut.isolation.env]")
        self._names = names
        self._settings = profile.isolation
        self._root = profile.root
        self._environ = dict(environ)
        self._admin = admin
        self._migrate = migrate or self._run_migrations
        self._schema: str | None = None

    @property
    def isolated(self) -> bool:
        return True

    def _db(self) -> PgAdmin:
        if self._admin is None:
            self._admin = PsqlAdmin(self._environ, self._names)
        return self._admin

    def schema_name(self) -> str:
        if self._schema is None:
            self._schema = _SCHEMA_PREFIX + schema_fingerprint(self._root, self._settings)[:16]
        return self._schema

    def fingerprint(self) -> str:
        return self.schema_name()

    def probe(self) -> None:
        self._db().probe(_required(self._environ, self._names.database))

    def _run_migrations(self, env: EnvMap) -> None:
        if not self._settings.migrate:
            return
        try:
            result = subprocess.run(
                list(self._settings.migrate), env=env, cwd=self._root, capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise IsolationError(f"could not run the migration command: {exc}") from exc
        if result.returncode != 0:
            raise IsolationError(
                f"the migration command exited with {result.returncode}:\n{result.stdout}{result.stderr}"
            )

    def _clone_names(self, count: int, prefix: str) -> list[str]:
        names = [f"{prefix}_w{index}" for index in range(count)]
        if names and len(names[-1].encode("utf-8")) > _IDENTIFIER_LIMIT:
            raise IsolationError(f"database name {names[-1]!r} exceeds PostgreSQL's 63-byte identifier limit")
        return names

    def _build(self) -> None:
        admin = self._db()
        schema = self.schema_name()
        build = f"{schema}_b{int(time.time() * 1000) % 10**9}"
        admin.drop(build, force=True)
        admin.create(build, template="template0", locale=self._settings.locale)
        admin.comment(build, f"{_BUILD_MARK} {int(time.time())}")
        try:
            env = {
                **self._environ,
                **dict(self._settings.migrate_env),
                self._names.database: build,
                self._names.password: _required(self._environ, self._names.password),
            }
            self._migrate(env)
            if not admin.rename(build, schema):
                if admin.exists(schema):
                    return  # a concurrent call published first: reuse its template
                # A migration session that outlived its process can block the rename: end it and retry once.
                admin.terminate(build)
                if not admin.rename(build, schema):
                    raise IsolationError(f"could not publish the schema template {schema}")
        finally:
            if admin.exists(build):
                admin.drop(build, force=True)

    def _clone(self, names: list[str]) -> bool:
        admin = self._db()
        schema = self.schema_name()
        if not admin.exists(schema):
            return False
        # Cloning needs a template without sessions; nobody keeps one open on a shared template on purpose.
        admin.terminate(schema)
        for name in names:
            admin.drop(name, force=True)
            admin.create(name, template=schema)
        return True

    def _evict(self) -> None:
        admin = self._db()
        now = int(time.time())
        for name, comment in admin.list_with_comments(_SCHEMA_PREFIX):
            if name == self.schema_name():
                continue
            mark, _, stamp = comment.partition(" ")
            limit = {_MARK: _TEMPLATE_TTL_SECONDS, _BUILD_MARK: _BUILD_TTL_SECONDS}.get(mark)
            if limit is None:
                continue
            age = now - int(stamp) if stamp.isdigit() else limit + 1
            if age > limit:
                admin.drop(name, force=False)

    def prepare(self, count: int, prefix: str, /) -> EnvOverlays:
        names = self._clone_names(count, prefix)
        for variable in (self._names.host, self._names.port, self._names.user, self._names.password):
            _required(self._environ, variable)
        admin = self._db()
        if not admin.exists(self.schema_name()):
            self._build()
        admin.comment(self.schema_name(), f"{_MARK} {int(time.time())}")
        if not self._clone(names):
            # The template vanished between the check and the clone (evicted by a concurrent call): rebuild once.
            self._build()
            admin.comment(self.schema_name(), f"{_MARK} {int(time.time())}")
            if not self._clone(names):
                raise IsolationError(f"the schema template {self.schema_name()} disappeared while cloning")
        self._evict()
        return tuple({self._names.database: name} for name in names)

    def cleanup(self, count: int, prefix: str, /) -> None:
        admin = self._db()
        for name in self._clone_names(count, prefix):
            admin.drop(name, force=True)
