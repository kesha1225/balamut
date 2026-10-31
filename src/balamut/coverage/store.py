"""SQLite store of the incremental engine: verdict cache, test costs, file hashes, coverage and metadata.

One database file per (worktree, branch): ``<state-dir>/cache/<base>_<branch>_<hash>.sqlite``. Mutant keys are
content-addressed (see ``core.hashing``); branches are separated at the file level, not in the key. WAL mode:
one driver process aggregates K daemon threads, and every access to the shared connection (reads included) is
serialized by a lock, because a bare ``sqlite3.Connection`` is not safe under concurrent execute and fetch.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import TypedDict, cast

from balamut.gitutil import git_output_in

type CostMap = dict[str, float]
type StrMap = dict[str, str]
type NumericIdMap = dict[str, int]
type MeasuredCoverage = Mapping[str, list[tuple[str, int]]]
type FileHashUpdates = Mapping[str, tuple[str, str]]  # path -> (content hash, kind)
type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
# (key, file, lineno, operator, verdict, bucket, original, mutated, covering_json, semantic, kind): a report row.
type MutantRow = tuple[str, str, int, str, str, str, str, str, str, str, str]

_SCHEMA_VERSION = "1"  # bump -> stale data tables are dropped; the cache is disposable and regenerates
_LEGACY_GENERATION = "legacy"
_VERDICT_BATCH_SIZE = 8
_VERDICT_FLUSH_INTERVAL_SECONDS = 0.25
_SQLITE_CHUNK = 900  # below SQLite's default limit of 999 bound variables
_COVERAGE_META_KEYS = ("coverage_generation", "coverage_substrate_fingerprint")
_DATA_TABLES = (
    "mutant",
    "test_cost",
    "file_meta",
    "test_collection",
    "coverage",
    "coverage_test",
    "coverage_file",
    "coverage_seen",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS mutant(
  key TEXT PRIMARY KEY, file TEXT, lineno INTEGER, operator TEXT,
  original TEXT, mutated TEXT, qualname TEXT, func_hash TEXT,
  verdict TEXT, bucket TEXT, covering_json TEXT, covering_hash TEXT, ts INTEGER,
  kind TEXT DEFAULT 'src', semantic TEXT DEFAULT '[]', killed_by TEXT DEFAULT '[]',
  kill_deps TEXT DEFAULT '', kill_validity TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS test_cost(nodeid TEXT PRIMARY KEY, seconds REAL, ts INTEGER);
CREATE TABLE IF NOT EXISTS file_meta(
  path TEXT PRIMARY KEY, content_hash TEXT, kind TEXT, generation TEXT NOT NULL, ts INTEGER);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS test_collection(
  cache_key TEXT PRIMARY KEY, nodeids_json TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS coverage_test(
  id INTEGER PRIMARY KEY, nodeid TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS coverage_file(
  id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS coverage(
  test_id INTEGER NOT NULL, file_id INTEGER NOT NULL, line INTEGER NOT NULL,
  PRIMARY KEY(test_id, file_id, line)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS coverage_by_line ON coverage(file_id, line, test_id);
CREATE TABLE IF NOT EXISTS coverage_seen(
  file_id INTEGER NOT NULL, test_id INTEGER NOT NULL,
  PRIMARY KEY(file_id, test_id)) WITHOUT ROWID;
"""


class CachedVerdict(TypedDict):
    verdict: str
    bucket: str
    covering_hash: str
    covering_json: str
    killed_by: list[str]
    kill_deps: str  # functions the killing lease executed (kill_deps_encode); "" when not recorded
    kill_validity: str  # their hash at kill time; "" means the verdict is held by the covering_hash file rule


def _sanitize(text: str, limit: int) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in text.lower())
    return cleaned.strip("_")[:limit] or "x"


def branch_wt_slug(root: Path) -> str:
    """``<base>_<branch>_<6hash>`` of (worktree path, branch); outside git, ``<base>_nogit_<6hash>``."""
    toplevel = git_output_in(root, "rev-parse", "--show-toplevel")
    if toplevel is None:
        resolved = root.resolve()
        digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:6]
        return f"{_sanitize(resolved.name, 20)}_nogit_{digest}"
    # symbolic-ref also names the branch of a repository without commits; a detached HEAD has none.
    branch = git_output_in(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        short = git_output_in(root, "rev-parse", "--short=8", "HEAD") or "unborn"
        branch = f"detached-{short}"
    digest = hashlib.sha256(f"{toplevel}\n{branch}".encode()).hexdigest()[:6]
    return f"{_sanitize(Path(toplevel).name, 20)}_{_sanitize(branch, 24)}_{digest}"


def default_cache_path(root: Path, state_dir: Path) -> Path:
    return state_dir / "cache" / f"{branch_wt_slug(root)}.sqlite"


class Store:
    def __init__(self, path: Path) -> None:
        super().__init__()
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: K worker threads write verdicts incrementally (a crash loses nothing done).
        # Every access goes through the RLock: write methods call read helpers under the lock (reentrancy).
        self._db = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
        self._lock = threading.RLock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._migrate_if_stale()
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._test_ids: NumericIdMap = {}
        self._file_ids: NumericIdMap = {}
        self._pending_verdicts = 0
        self._last_verdict_commit = time.monotonic()

    def _migrate_if_stale(self) -> None:
        """A different schema version drops the data tables (the cache regenerates); meta stays.

        Runs in __init__ before the store is shared with other threads, so without the lock."""
        self._db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        row = self._db.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        if row is not None and str(row[0]) == _SCHEMA_VERSION:
            return
        for table in _DATA_TABLES:
            self._db.execute(f"DROP TABLE IF EXISTS {table}")
        self._db.execute("DELETE FROM meta WHERE k IN ('coverage_generation', 'coverage_substrate_fingerprint')")
        self._db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', ?)", (_SCHEMA_VERSION,))
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._verdicts_flush_locked()
            self._db.close()

    def flush(self) -> None:
        """Publish the bounded tail of pending verdict writes."""
        with self._lock:
            self._verdicts_flush_locked()

    def _verdicts_flush_locked(self) -> None:
        self._db.commit()
        self._pending_verdicts = 0
        self._last_verdict_commit = time.monotonic()

    # meta

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (key, value))
            self._db.commit()

    def clear_all(self) -> None:
        with self._lock:
            for table in ("mutant", "test_cost", "file_meta", "test_collection"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.commit()

    # exact pytest collection

    def get_collection(self, cache_key: str) -> frozenset[str] | None:
        """The exact node ids of a stored pytest collection."""
        with self._lock:
            row = self._db.execute(
                "SELECT nodeids_json FROM test_collection WHERE cache_key=?", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        nodeids: JsonValue = json.loads(str(row[0]))
        if not isinstance(nodeids, list):
            raise ValueError("the cached pytest collection holds malformed node ids")
        validated: list[str] = []
        for nodeid in nodeids:
            if not isinstance(nodeid, str):
                raise ValueError("the cached pytest collection holds malformed node ids")
            validated.append(nodeid)
        return frozenset(validated)

    def put_collection(self, cache_key: str, nodeids: Collection[str]) -> None:
        """Store a non-empty exact set of collected node ids."""
        ordered = sorted(set(nodeids))
        if not ordered:
            raise ValueError("an empty pytest collection must not be cached")
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO test_collection(cache_key, nodeids_json, ts) VALUES(?,?,?)",
                (cache_key, json.dumps(ordered, ensure_ascii=False, separators=(",", ":")), int(time.time())),
            )
            self._db.commit()

    # mutant verdicts

    def get_verdict(self, key: str) -> CachedVerdict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT verdict, bucket, covering_hash, covering_json, killed_by, kill_deps, kill_validity"
                " FROM mutant WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            raw_killed_by: JsonValue = json.loads(str(row[4]))
        except json.JSONDecodeError:
            return None
        if not isinstance(raw_killed_by, list) or any(type(nodeid) is not str for nodeid in raw_killed_by):
            return None
        # Every element was checked above; pyright does not narrow a list through any().
        killed_by = cast("list[str]", raw_killed_by)
        return {
            "verdict": str(row[0]),
            "bucket": str(row[1]),
            "covering_hash": str(row[2]),
            "covering_json": str(row[3]),
            "killed_by": killed_by,
            "kill_deps": str(row[5]),
            "kill_validity": str(row[6]),
        }

    def killed_with_killers(self) -> list[tuple[str, list[str]]]:
        """(mutant key, killing node ids) for every killed mutant."""
        with self._lock:
            rows = self._db.execute("SELECT key, killed_by FROM mutant WHERE verdict='killed'").fetchall()
        result: list[tuple[str, list[str]]] = []
        for key, killed_by in rows:
            decoded: JsonValue = json.loads(str(killed_by))
            if isinstance(decoded, list):
                result.append((str(key), [str(nodeid) for nodeid in decoded]))
        return result

    def mutant_rows_for_files(self, files: list[str]) -> list[MutantRow]:
        """Verdict rows of the given files, the source of the agent report (chunked below SQLite's limit)."""
        rows: list[MutantRow] = []
        ordered = sorted(set(files))
        for start in range(0, len(ordered), _SQLITE_CHUNK):
            chunk = ordered[start : start + _SQLITE_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            query = (
                "SELECT key, file, lineno, operator, verdict, bucket, original, mutated, covering_json, semantic, kind"
                f" FROM mutant WHERE file IN ({placeholders})"
            )
            with self._lock:
                rows.extend(self._db.execute(query, chunk).fetchall())
        return rows

    def all_mutant_rows(self) -> list[MutantRow]:
        with self._lock:
            return list(
                self._db.execute(
                    "SELECT key, file, lineno, operator, verdict, bucket, original, mutated, covering_json, semantic,"
                    " kind FROM mutant"
                ).fetchall()
            )

    def put_verdict(
        self,
        *,
        key: str,
        file: str,
        lineno: int,
        operator: str,
        original: str,
        mutated: str,
        qualname: str,
        func_hash: str,
        verdict: str,
        bucket: str,
        covering: list[str],
        covering_hash: str,
        kind: str = "src",
        semantic: list[str] | None = None,
        killed_by: list[str] | None = None,
        kill_deps: str = "",
        kill_validity: str = "",
    ) -> None:
        params = (
            key,
            file,
            lineno,
            operator,
            original,
            mutated,
            qualname,
            func_hash,
            verdict,
            bucket,
            json.dumps(covering),
            covering_hash,
            int(time.time()),
            kind,
            json.dumps(semantic or []),
            json.dumps(killed_by or []),
            kill_deps,
            kill_validity,
        )
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO mutant(key, file, lineno, operator, original, mutated, qualname,"
                " func_hash, verdict, bucket, covering_json, covering_hash, ts, kind, semantic, killed_by,"
                " kill_deps, kill_validity)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                params,
            )
            self._pending_verdicts += 1
            elapsed = time.monotonic() - self._last_verdict_commit
            if self._pending_verdicts >= _VERDICT_BATCH_SIZE or elapsed >= _VERDICT_FLUSH_INTERVAL_SECONDS:
                self._verdicts_flush_locked()

    def update_mutant_bucket(self, key: str, bucket: str) -> None:
        """Update the static triage bucket of an existing cached verdict."""
        with self._lock, self._db:
            self._db.execute("UPDATE mutant SET bucket=? WHERE key=?", (bucket, key))

    def count_mutants(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT count(*) FROM mutant").fetchone()[0])

    def delete_mutants(self, keys: Collection[str]) -> int:
        """Delete only the verdicts with the given content-addressed keys."""
        unique_keys = tuple(dict.fromkeys(keys))
        if not unique_keys:
            return 0
        with self._lock, self._db:
            changes_before = self._db.total_changes
            self._db.executemany("DELETE FROM mutant WHERE key=?", ((key,) for key in unique_keys))
            return self._db.total_changes - changes_before

    # per-test cost (ordering and timeouts)

    def get_costs(self, nodeids: list[str]) -> CostMap:
        costs: CostMap = {}
        for start in range(0, len(nodeids), _SQLITE_CHUNK):
            chunk = nodeids[start : start + _SQLITE_CHUNK]
            marks = ",".join("?" * len(chunk))
            with self._lock:
                rows = self._db.execute(
                    f"SELECT nodeid, seconds FROM test_cost WHERE nodeid IN ({marks})", chunk
                ).fetchall()
            costs.update({str(nodeid): float(seconds) for nodeid, seconds in rows})
        return costs

    def put_costs(self, costs: CostMap) -> None:
        now = int(time.time())
        rows = [(nodeid, seconds, now) for nodeid, seconds in costs.items()]
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO test_cost(nodeid, seconds, ts) VALUES(?,?,?)", rows)
            self._db.commit()

    # file content hashes (staleness)

    def get_file_hashes(self, kind: str) -> StrMap:
        with self._lock:
            generation = self.coverage_generation()
            if generation is None:
                return {}
            rows = self._db.execute(
                "SELECT path, content_hash FROM file_meta WHERE kind=? AND generation=?", (kind, generation)
            ).fetchall()
        return {str(path): str(content_hash) for path, content_hash in rows}

    def put_file_hash(self, path: str, content_hash: str, kind: str) -> None:
        with self._lock, self._db:
            generation = self._coverage_generation_ensure_legacy()
            self._db.execute(
                "INSERT OR REPLACE INTO file_meta(path, content_hash, kind, generation, ts) VALUES(?,?,?,?,?)",
                (path, content_hash, kind, generation, int(time.time())),
            )

    # per-test line coverage (a refresh replaces, it never merges)

    def has_coverage(self) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM coverage LIMIT 1").fetchone() is not None

    def clear_coverage(self) -> None:
        """Clear coverage, the active generation and its file hashes."""
        with self._lock, self._db:
            for table in ("coverage", "coverage_seen", "coverage_test", "coverage_file", "file_meta"):
                self._db.execute(f"DELETE FROM {table}")
            self._coverage_meta_delete()
            self._coverage_id_caches_clear()

    def coverage_generation(self) -> str | None:
        return self.get_meta("coverage_generation")

    def coverage_substrate_fingerprint(self) -> str | None:
        return self.get_meta("coverage_substrate_fingerprint")

    def _coverage_meta_delete(self) -> None:
        placeholders = ",".join("?" * len(_COVERAGE_META_KEYS))
        self._db.execute(f"DELETE FROM meta WHERE k IN ({placeholders})", _COVERAGE_META_KEYS)

    def _coverage_meta_set(self, *, generation: str, substrate_fingerprint: str) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
            (("coverage_generation", generation), ("coverage_substrate_fingerprint", substrate_fingerprint)),
        )

    def _coverage_generation_ensure_legacy(self) -> str:
        generation = self.coverage_generation()
        if generation is not None:
            return generation
        self._coverage_meta_set(generation=_LEGACY_GENERATION, substrate_fingerprint=_LEGACY_GENERATION)
        return _LEGACY_GENERATION

    def coverage_replace(
        self, rows: list[tuple[str, str, int]], *, generation: str, substrate_fingerprint: str
    ) -> None:
        """Replace coverage transactionally and publish its generation."""
        if not generation or not substrate_fingerprint:
            raise ValueError("coverage generation metadata must be non-empty")
        with self._lock:
            try:
                with self._db:
                    for table in ("coverage", "coverage_seen", "coverage_test", "coverage_file", "file_meta"):
                        self._db.execute(f"DELETE FROM {table}")
                    self._coverage_id_caches_clear()
                    self._coverage_rows_insert(rows)
                    self._coverage_meta_set(generation=generation, substrate_fingerprint=substrate_fingerprint)
            except sqlite3.Error:
                self._coverage_id_caches_clear()
                raise

    def tests_covering(self, file: str, lineno: int) -> set[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT t.nodeid FROM coverage c"
                " JOIN coverage_file f ON f.id=c.file_id"
                " JOIN coverage_test t ON t.id=c.test_id"
                " WHERE f.path=? AND c.line=?",
                (file, lineno),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def tests_covering_file(self, file: str) -> set[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT t.nodeid FROM coverage c"
                " JOIN coverage_file f ON f.id=c.file_id"
                " JOIN coverage_test t ON t.id=c.test_id"
                " WHERE f.path=? GROUP BY c.test_id",
                (file,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def tests_seen_covering_file(self, file: str) -> set[str]:
        """Tests whose coverage of the file this generation saw at least once (at build or by an earlier refresh).

        Refresh targets these rather than the current coverage: an edit could have moved a test away from the file,
        and reverting to the earlier version without re-measuring that test would keep coverage of an edit that no
        longer exists. A deleted or fully re-measured test file takes its pairs along."""
        with self._lock:
            rows = self._db.execute(
                "SELECT t.nodeid FROM coverage_seen s"
                " JOIN coverage_file f ON f.id=s.file_id"
                " JOIN coverage_test t ON t.id=s.test_id"
                " WHERE f.path=?",
                (file,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def coverage_rows_for_file(self, file: str) -> list[tuple[str, int]]:
        """Every (node id, line) row of one source file: a snapshot taken before a diff refresh, for the
        conservative restoration of a module the refresh under-measures (see ``refresh.restore_wiped_src``)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT t.nodeid, c.line FROM coverage c"
                " JOIN coverage_file f ON f.id=c.file_id"
                " JOIN coverage_test t ON t.id=c.test_id"
                " WHERE f.path=?",
                (file,),
            ).fetchall()
        return [(str(test_id), int(line)) for test_id, line in rows]

    def files_covered_by(self, test_ids: set[str]) -> set[str]:
        """Source files the given tests execute: the dependencies of a mutated function as its covering tests run
        it. A helper the mutant calls is here too, so its edit invalidates the cache even when the mutant's file
        did not change."""
        covered: set[str] = set()
        ids = sorted(test_ids)
        for start in range(0, len(ids), _SQLITE_CHUNK):
            chunk = ids[start : start + _SQLITE_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            query = (
                "SELECT f.path FROM coverage c"
                " JOIN coverage_test t ON t.id=c.test_id"
                " JOIN coverage_file f ON f.id=c.file_id"
                f" WHERE t.nodeid IN ({placeholders}) GROUP BY c.file_id"
            )
            with self._lock:
                rows = self._db.execute(query, chunk).fetchall()
            covered.update(str(row[0]) for row in rows)
        return covered

    def ingest_coverage(self, rows: list[tuple[str, str, int]]) -> None:
        """Bulk ingest of (node id, file, line) rows, a baseline from a coverage file."""
        with self._lock:
            try:
                with self._db:
                    self._coverage_generation_ensure_legacy()
                    self._coverage_rows_insert(rows)
            except sqlite3.Error:
                self._coverage_id_caches_clear()
                raise

    def apply_test_coverage_refresh(
        self,
        *,
        measured: MeasuredCoverage,
        measured_nodeids: Collection[str],
        replace_test_files: Collection[str],
        restored_rows: Collection[tuple[str, str, int]],
        file_hashes: FileHashUpdates,
    ) -> None:
        """Publish a diff refresh and the file hashes it measured atomically."""
        with self._lock:
            try:
                with self._db:
                    measured_nodeid_list = tuple(measured_nodeids)
                    for start in range(0, len(measured_nodeid_list), _SQLITE_CHUNK):
                        chunk = measured_nodeid_list[start : start + _SQLITE_CHUNK]
                        placeholders = ",".join("?" * len(chunk))
                        self._db.execute(
                            "DELETE FROM coverage WHERE test_id IN"
                            f" (SELECT id FROM coverage_test WHERE nodeid IN ({placeholders}))",
                            chunk,
                        )
                    for test_file in replace_test_files:
                        self._tests_in_file_delete(test_file)
                    if replace_test_files:
                        self._test_ids.clear()
                    measured_rows = [(nodeid, file, line) for nodeid, lines in measured.items() for file, line in lines]
                    self._coverage_rows_insert([*measured_rows, *restored_rows])
                    generation = self._coverage_generation_ensure_legacy()
                    self._db.executemany(
                        "DELETE FROM file_meta WHERE path=? AND kind='test' AND generation=?",
                        ((test_file, generation) for test_file in replace_test_files),
                    )
                    now = int(time.time())
                    self._db.executemany(
                        "INSERT OR REPLACE INTO file_meta(path, content_hash, kind, generation, ts) VALUES(?,?,?,?,?)",
                        (
                            (path, content_hash, kind, generation, now)
                            for path, (content_hash, kind) in file_hashes.items()
                        ),
                    )
            except sqlite3.Error:
                self._coverage_id_caches_clear()
                raise

    def replace_test_coverage(self, test_id: str, lines: list[tuple[str, int]]) -> None:
        """Replace one test's coverage whole (delete + insert): a clean refresh without merge drift."""
        with self._lock:
            try:
                with self._db:
                    numeric_test_id = self._test_ids_ensure([test_id])[test_id]
                    self._db.execute("DELETE FROM coverage WHERE test_id=?", (numeric_test_id,))
                    self._coverage_rows_insert([(test_id, file, line) for file, line in lines])
            except sqlite3.Error:
                self._coverage_id_caches_clear()
                raise

    def delete_tests_in_file(self, test_file: str) -> None:
        """Delete the coverage of a test file and its hash in the active generation.

        GLOB treats ``_`` and ``%`` in the path literally; the wildcard suffix covers exactly that file's node ids."""
        with self._lock, self._db:
            self._tests_in_file_delete(test_file)
            self._test_ids.clear()
            generation = self.coverage_generation()
            if generation is not None:
                self._db.execute(
                    "DELETE FROM file_meta WHERE path=? AND kind='test' AND generation=?", (test_file, generation)
                )

    def _tests_in_file_delete(self, test_file: str) -> None:
        """Coverage, history and node ids of a test file; the caller holds the transaction and resets id caches."""
        pattern = f"{test_file}::*"
        for table in ("coverage", "coverage_seen"):
            self._db.execute(
                f"DELETE FROM {table} WHERE test_id IN (SELECT id FROM coverage_test WHERE nodeid GLOB ?)", (pattern,)
            )
        self._db.execute("DELETE FROM coverage_test WHERE nodeid GLOB ?", (pattern,))

    def _coverage_id_caches_clear(self) -> None:
        self._test_ids.clear()
        self._file_ids.clear()

    def _ids_ensure(self, table: str, column: str, values: Collection[str], cache: NumericIdMap) -> NumericIdMap:
        missing = sorted(set(values) - cache.keys())
        if missing:
            self._db.executemany(f"INSERT OR IGNORE INTO {table}({column}) VALUES(?)", ((value,) for value in missing))
            for start in range(0, len(missing), _SQLITE_CHUNK):
                chunk = missing[start : start + _SQLITE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = self._db.execute(
                    f"SELECT {column}, id FROM {table} WHERE {column} IN ({placeholders})", chunk
                ).fetchall()
                cache.update((str(value), int(identifier)) for value, identifier in rows)
        return {value: cache[value] for value in values}

    def _test_ids_ensure(self, nodeids: Collection[str]) -> NumericIdMap:
        return self._ids_ensure("coverage_test", "nodeid", nodeids, self._test_ids)

    def _file_ids_ensure(self, paths: Collection[str]) -> NumericIdMap:
        return self._ids_ensure("coverage_file", "path", paths, self._file_ids)

    def _coverage_rows_insert(self, rows: Collection[tuple[str, str, int]]) -> None:
        if not rows:
            return
        test_ids = self._test_ids_ensure([test_id for test_id, _file, _line in rows])
        file_ids = self._file_ids_ensure([file for _test_id, file, _line in rows])
        self._db.executemany(
            "INSERT OR IGNORE INTO coverage(test_id, file_id, line) VALUES(?,?,?)",
            ((test_ids[test_id], file_ids[file], line) for test_id, file, line in rows),
        )
        self._db.executemany(
            "INSERT OR IGNORE INTO coverage_seen(file_id, test_id) VALUES(?,?)",
            {(file_ids[file], test_ids[test_id]) for test_id, file, _line in rows},
        )


class StoreCoverage:
    """``tests_covering`` / ``files_covered_by`` over the store's coverage (the diff mode's coverage source)."""

    def __init__(self, store: Store) -> None:
        super().__init__()
        self._store = store

    def tests_covering(self, path: str, lineno: int) -> set[str]:
        return self._store.tests_covering(path, lineno)

    def files_covered_by(self, test_ids: set[str]) -> set[str]:
        return self._store.files_covered_by(test_ids)
