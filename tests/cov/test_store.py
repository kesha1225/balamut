"""The SQLite store of the incremental engine (balamut.coverage.store)."""

from __future__ import annotations

import importlib.util
import inspect
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Self, cast, get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from balamut.coverage import store as store_module
from balamut.coverage.store import Store, StoreCoverage, branch_wt_slug, default_cache_path


def _fresh_module_probe(name: str) -> ModuleType:
    """Re-execute a module's import-time lines under a probe name, without touching the live module."""
    live = sys.modules[name]
    spec = importlib.util.spec_from_file_location(f"_probe_{name}", live.__file__)
    assert spec is not None
    assert spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    sys.modules[probe.__name__] = probe
    try:
        spec.loader.exec_module(probe)
    finally:
        del sys.modules[probe.__name__]
    return probe


class _CountingLock:
    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> Self:
        self.entered += 1
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_verdict_roundtrip(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        assert store.get_verdict("k1") is None
        store.put_verdict(
            key="k1",
            file="src/x.py",
            lineno=10,
            operator="compare:Lt->LtE",
            original="a < b",
            mutated="a <= b",
            qualname="f",
            func_hash="fh",
            verdict="survived",
            bucket="high",
            covering=["t.py::test_a", "t.py::test_b"],
            covering_hash="ch",
            killed_by=["t.py::test_a"],
        )
        got = store.get_verdict("k1")
        assert got is not None
        assert got["verdict"] == "survived"
        assert got["bucket"] == "high"
        assert got["covering_hash"] == "ch"
        assert got["killed_by"] == ["t.py::test_a"]
    finally:
        store.close()


def test_verdict_with_non_string_killer_is_rejected(tmp_path: Path) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    try:
        _put_sample(store, "killed")
        store.flush()
        with sqlite3.connect(cache) as connection:
            connection.execute("UPDATE mutant SET killed_by = ? WHERE key = ?", ('["test.py::test_a", 7]', "k"))

        assert store.get_verdict("k") is None
    finally:
        store.close()


def _put_sample(store: Store, verdict: str, *, key: str = "k") -> None:
    store.put_verdict(
        key=key,
        file="f",
        lineno=1,
        operator="o",
        original="x",
        mutated="y",
        qualname="q",
        func_hash="fh",
        verdict=verdict,
        bucket="review",
        covering=[],
        covering_hash="ch",
    )


def test_verdict_batch_flushes_at_eight_and_explicit_flush_publishes_tail(tmp_path: Path) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    observer = sqlite3.connect(cache)
    try:
        for index in range(7):
            _put_sample(store, "killed", key=f"k{index}")
        assert observer.execute("SELECT count(*) FROM mutant").fetchone() == (0,)

        _put_sample(store, "killed", key="k7")
        assert observer.execute("SELECT count(*) FROM mutant").fetchone() == (8,)

        _put_sample(store, "killed", key="k8")
        assert observer.execute("SELECT count(*) FROM mutant").fetchone() == (8,)
        store.flush()
        assert observer.execute("SELECT count(*) FROM mutant").fetchone() == (9,)
    finally:
        observer.close()
        store.close()


def test_delete_tests_in_file_treats_underscore_literally(tmp_path: Path) -> None:
    # LIKE would treat `_` in `foo_test.py` as a wildcard and also delete `fooXtest.py`; GLOB is literal here.
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage(
            [
                ("tests/foo_test.py::test_1", "src/x.py", 10),
                ("tests/fooXtest.py::test_2", "src/y.py", 20),  # differs from foo_test exactly at the `_`
            ]
        )
        store.delete_tests_in_file("tests/foo_test.py")
        assert store.tests_covering("src/x.py", 10) == set()
        assert store.tests_covering("src/y.py", 20) == {"tests/fooXtest.py::test_2"}
    finally:
        store.close()


def test_files_covered_by_returns_src_dependencies(tmp_path: Path) -> None:
    # The inverse of tests_covering: source files a test executes are its dependencies (cache invalidation).
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage(
            [
                ("tests/x_test.py::test_a", "src/caller.py", 10),
                ("tests/x_test.py::test_a", "src/helper.py", 5),
                ("tests/y_test.py::test_b", "src/other.py", 1),
            ]
        )
        assert store.files_covered_by({"tests/x_test.py::test_a"}) == {"src/caller.py", "src/helper.py"}
        assert store.files_covered_by(set()) == set()  # no crash on an empty IN
    finally:
        store.close()


def test_coverer_history_outlives_a_refresh_and_leaves_with_its_test_file(tmp_path: Path) -> None:
    """Refresh targets the tests that covered a file in any version this generation saw: a test an edit moved away
    from the file stays in the history. A fully re-measured or deleted test file takes its pairs along; a new
    generation and a coverage reset start the history afresh."""
    kept, dropped, gone = "tests/a_test.py::test_kept", "tests/b_test.py::test_dropped", "tests/c_test.py::test_gone"
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([(kept, "src/f.py", 1), (dropped, "src/f.py", 1), (gone, "src/f.py", 2)])
        store.apply_test_coverage_refresh(
            measured={kept: [("src/f.py", 1)], dropped: [("src/g.py", 3)]},
            measured_nodeids=(kept, dropped),
            replace_test_files=(),
            restored_rows=(),
            file_hashes={},
        )
        assert store.tests_covering_file("src/f.py") == {kept, gone}
        assert store.tests_seen_covering_file("src/f.py") == {kept, dropped, gone}

        store.apply_test_coverage_refresh(
            measured={}, measured_nodeids=(), replace_test_files=("tests/b_test.py",), restored_rows=(), file_hashes={}
        )
        store.delete_tests_in_file("tests/c_test.py")
        assert store.tests_seen_covering_file("src/f.py") == {kept}

        store.coverage_replace([(kept, "src/g.py", 1)], generation="next", substrate_fingerprint="substrate")
        assert store.tests_seen_covering_file("src/f.py") == set()
        assert store.tests_seen_covering_file("src/g.py") == {kept}

        store.clear_coverage()
        assert store.tests_seen_covering_file("src/g.py") == set()
    finally:
        store.close()


def test_clear_coverage_removes_all_rows_keeps_verdicts(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.coverage_replace(
            [("t.py::a", "src/x.py", 1), ("t.py::b", "src/y.py", 2)],
            generation="g1",
            substrate_fingerprint="substrate-v1",
        )
        store.put_file_hash("tests/x_test.py", "hash", "test")
        _put_sample(store, "survived")
        assert store.has_coverage()
        store.clear_coverage()
        assert not store.has_coverage()
        assert store.tests_covering("src/x.py", 1) == set()
        assert store.get_file_hashes("test") == {}
        assert store.coverage_generation() is None
        assert store.get_verdict("k") is not None
    finally:
        store.close()


def test_verdict_replace(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        _put_sample(store, "survived")
        _put_sample(store, "killed")  # the same key replaces
        got = store.get_verdict("k")
        assert got is not None
        assert got["verdict"] == "killed"
    finally:
        store.close()


def test_delete_mutants_removes_only_reported_keys(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        _put_sample(store, "survived", key="k1")
        _put_sample(store, "killed", key="k2")

        assert store.delete_mutants({"k1"}) == 1
        assert store.get_verdict("k1") is None
        assert store.get_verdict("k2") is not None
        assert store.delete_mutants({"missing"}) == 0
    finally:
        store.close()


def test_costs_and_file_hashes(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.put_costs({"t.py::a": 0.05, "t.py::b": 3.2})
        costs = store.get_costs(["t.py::a", "t.py::b", "t.py::missing"])
        assert costs == {"t.py::a": 0.05, "t.py::b": 3.2}

        store.put_file_hash("src/x.py", "h1", "src")
        store.put_file_hash("tests/x_test.py", "h2", "test")
        assert store.get_file_hashes("src") == {"src/x.py": "h1"}
        assert store.get_file_hashes("test") == {"tests/x_test.py": "h2"}
        assert store.coverage_generation() == "legacy"
    finally:
        store.close()


def test_coverage_replace_clears_file_hashes_from_previous_generation(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.coverage_replace(
            [("tests/a_test.py::test_a", "src/a.py", 1)],
            generation="g1",
            substrate_fingerprint="substrate-v1",
        )
        store.put_file_hash("tests/x_test.py", "hash", "test")

        store.coverage_replace(
            [("tests/b_test.py::test_b", "src/b.py", 2)],
            generation="g2",
            substrate_fingerprint="substrate-v2",
        )

        assert store.tests_covering("src/a.py", 1) == set()
        assert store.tests_covering("src/b.py", 2) == {"tests/b_test.py::test_b"}
        assert store.get_file_hashes("test") == {}
        assert store.coverage_generation() == "g2"
    finally:
        store.close()


def test_coverage_replace_rolls_back_generation_and_rows_on_insert_error(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.coverage_replace(
            [("tests/a_test.py::test_a", "src/a.py", 1)],
            generation="g1",
            substrate_fingerprint="substrate-v1",
        )

        # SQLite rejects an arbitrary Python object; the cast keeps that runtime value to exercise the rollback.
        unsupported_line = cast("int", object())
        with pytest.raises(sqlite3.ProgrammingError):
            store.coverage_replace(
                [("tests/b_test.py::test_b", "src/b.py", unsupported_line)],
                generation="g2",
                substrate_fingerprint="substrate-v2",
            )

        assert store.coverage_generation() == "g1"
        assert store.tests_covering("src/a.py", 1) == {"tests/a_test.py::test_a"}
        assert store.tests_covering("src/b.py", 2) == set()
    finally:
        store.close()


def test_diff_refresh_rolls_back_coverage_hashes_and_generation_on_insert_error(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.coverage_replace(
            [
                ("tests/a_test.py::test_a", "src/a.py", 1),
                ("tests/b_test.py::test_b", "src/b.py", 2),
            ],
            generation="g1",
            substrate_fingerprint="substrate-v1",
        )
        store.put_file_hash("src/a.py", "old-src-hash", "src")
        store.put_file_hash("tests/a_test.py", "old-test-hash", "test")
        unsupported_line = cast("int", object())

        with pytest.raises(sqlite3.ProgrammingError):
            store.apply_test_coverage_refresh(
                measured={"tests/a_test.py::test_a": [("src/a.py", unsupported_line)]},
                measured_nodeids={"tests/a_test.py::test_a"},
                replace_test_files={"tests/a_test.py"},
                restored_rows=[],
                file_hashes={
                    "src/a.py": ("new-src-hash", "src"),
                    "tests/a_test.py": ("new-test-hash", "test"),
                },
            )

        assert store.coverage_generation() == "g1"
        assert store.tests_covering("src/a.py", 1) == {"tests/a_test.py::test_a"}
        assert store.tests_covering("src/b.py", 2) == {"tests/b_test.py::test_b"}
        assert store.get_file_hashes("src") == {"src/a.py": "old-src-hash"}
        assert store.get_file_hashes("test") == {"tests/a_test.py": "old-test-hash"}
    finally:
        store.close()


def test_refresh_hashes_are_published_with_coverage_and_cleared_with_generation(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.coverage_replace(
            [("tests/a_test.py::test_a", "src/a.py", 1)],
            generation="g1",
            substrate_fingerprint="substrate-v1",
        )

        store.apply_test_coverage_refresh(
            measured={"tests/a_test.py::test_a": [("src/a.py", 2)]},
            measured_nodeids={"tests/a_test.py::test_a"},
            replace_test_files=set(),
            restored_rows=[],
            file_hashes={"src/a.py": ("new-hash", "src")},
        )

        assert store.get_file_hashes("src") == {"src/a.py": "new-hash"}
        assert store.tests_covering("src/a.py", 2) == {"tests/a_test.py::test_a"}
        store.clear_coverage()
        assert store.get_file_hashes("src") == {}
    finally:
        store.close()


def test_meta_and_clear(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        assert store.get_meta("env_fp") is None
        store.set_meta("env_fp", "abc")
        assert store.get_meta("env_fp") == "abc"
        store.put_costs({"t::a": 1.0})
        store.clear_all()
        assert store.get_costs(["t::a"]) == {}
        assert store.get_meta("env_fp") == "abc"  # meta survives clear_all
    finally:
        store.close()


def test_coverage_schema_uses_integer_dimensions_without_rowid(tmp_path: Path) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    try:
        store.ingest_coverage(
            [
                ("tests/a_test.py::test_a", "src/x.py", 10),
                ("tests/a_test.py::test_a", "src/x.py", 10),
                ("tests/b_test.py::test_b", "src/x.py", 11),
            ]
        )
        assert store.tests_covering("src/x.py", 10) == {"tests/a_test.py::test_a"}
        assert store.tests_covering_file("src/x.py") == {
            "tests/a_test.py::test_a",
            "tests/b_test.py::test_b",
        }
    finally:
        store.close()

    database = sqlite3.connect(cache)
    try:
        columns = {str(row[1]): str(row[2]) for row in database.execute("PRAGMA table_info(coverage)")}
        schema = str(
            database.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='coverage'").fetchone()[0]
        )
        assert columns == {"test_id": "INTEGER", "file_id": "INTEGER", "line": "INTEGER"}
        assert "WITHOUT ROWID" in schema.upper()
        assert database.execute("SELECT count(*) FROM coverage_test").fetchone() == (2,)
        assert database.execute("SELECT count(*) FROM coverage_file").fetchone() == (1,)
    finally:
        database.close()


def test_stale_schema_cache_is_discarded_as_disposable_data(tmp_path: Path) -> None:
    cache = tmp_path / "v3.sqlite"
    database = sqlite3.connect(cache)
    try:
        database.executescript(
            """
            CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
            INSERT INTO meta(k, v) VALUES('schema_version', '3');
            CREATE TABLE coverage(
              test_id TEXT, file TEXT, line INTEGER,
              PRIMARY KEY(test_id, file, line)
            );
            INSERT INTO coverage(test_id, file, line)
            VALUES('tests/old_test.py::test_old', 'src/old.py', 1);
            """
        )
        database.commit()
    finally:
        database.close()

    store = Store(cache)
    try:
        assert not store.has_coverage()
        assert store.get_meta("schema_version") == "1"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("version", "extra_columns"),
    [
        pytest.param("4", "", id="stale-without-killed-by"),
        pytest.param("5", ", killed_by TEXT DEFAULT '[]'", id="stale-without-kill-dependencies"),
        pytest.param(
            "6",
            ", killed_by TEXT DEFAULT '[]', kill_deps TEXT DEFAULT '', kill_validity TEXT DEFAULT ''",
            id="stale-without-coverer-history",
        ),
    ],
)
def test_older_mutant_cache_is_discarded(tmp_path: Path, version: str, extra_columns: str) -> None:
    """The cache is disposable: a table of an older schema lacks columns new queries need, so it is dropped."""
    cache = tmp_path / f"v{version}.sqlite"
    with sqlite3.connect(cache) as database:
        database.executescript(
            f"""
            CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
            INSERT INTO meta(k, v) VALUES('schema_version', '{version}');
            CREATE TABLE mutant(
              key TEXT PRIMARY KEY, verdict TEXT, bucket TEXT,
              covering_hash TEXT, covering_json TEXT{extra_columns}
            );
            INSERT INTO mutant(key, verdict, bucket, covering_hash, covering_json)
            VALUES('old', 'killed', 'review', 'hash', '[]');
            """
        )

    store = Store(cache)
    try:
        assert store.get_meta("schema_version") == "1"
        assert store.get_verdict("old") is None
    finally:
        store.close()


@given(
    rows=st.lists(
        st.tuples(
            st.sampled_from(("tests/a_test.py::test_a", "tests/b_test.py::test_b", "tests/c_test.py::test_c")),
            st.sampled_from(("src/a.py", "src/b.py", "src/c.py")),
            st.integers(min_value=1, max_value=5),
        ),
        max_size=30,
    )
)
@settings(max_examples=30, deadline=None)
def test_coverage_queries_match_set_model(rows: list[tuple[str, str, int]]) -> None:
    model = set(rows)
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "coverage.sqlite")
        try:
            store.ingest_coverage(rows)
            for file in ("src/a.py", "src/b.py", "src/c.py"):
                assert store.tests_covering_file(file) == {
                    test_id for test_id, covered_file, _line in model if covered_file == file
                }
                assert set(store.coverage_rows_for_file(file)) == {
                    (test_id, line) for test_id, covered_file, line in model if covered_file == file
                }
                for line in range(1, 6):
                    assert store.tests_covering(file, line) == {
                        test_id
                        for test_id, covered_file, covered_line in model
                        if covered_file == file and covered_line == line
                    }

            requested = {"tests/a_test.py::test_a", "tests/c_test.py::test_c"}
            assert store.files_covered_by(requested) == {file for test_id, file, _line in model if test_id in requested}
        finally:
            store.close()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_branch_wt_slug_sanitized(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "feature/x.y")
    slug = branch_wt_slug(tmp_path)
    assert slug
    assert all(char.isalnum() or char == "_" for char in slug)
    assert slug.count("_") >= 2  # base_branch_hash
    assert "feature_x_y" in slug


def test_branch_wt_slug_outside_git_is_stable(tmp_path: Path) -> None:
    assert branch_wt_slug(tmp_path) == branch_wt_slug(tmp_path)
    assert "nogit" in branch_wt_slug(tmp_path)


def test_default_cache_path_lives_in_the_state_dir(tmp_path: Path) -> None:
    path = default_cache_path(tmp_path, tmp_path / ".balamut")
    assert path.parent == tmp_path / ".balamut" / "cache"
    assert path.suffix == ".sqlite"


# --- import-time constants and type alias ---


def test_store_import_time_constants_and_type_alias() -> None:
    probe = _fresh_module_probe("balamut.coverage.store")
    assert probe._VERDICT_BATCH_SIZE == 8
    assert probe._VERDICT_FLUSH_INTERVAL_SECONDS == 0.25
    assert probe._SQLITE_CHUNK == 900
    members = get_args(probe.JsonValue.__value__)
    assert bool in members
    assert int in members
    assert str in members
    assert type(None) in members


# --- branch_wt_slug ---


def test_branch_wt_slug_truncates_and_hashes(tmp_path: Path) -> None:
    repo = tmp_path / ("x" * 30)
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "b" * 40)
    slug = branch_wt_slug(repo)
    base, branch, digest = slug.split("_")
    assert base == "x" * 20
    assert branch == "b" * 24
    assert len(digest) == 6


def test_branch_wt_slug_outside_git_truncates_and_hashes(tmp_path: Path) -> None:
    d = tmp_path / ("y" * 30)
    d.mkdir()
    slug = branch_wt_slug(d)
    base, nogit, digest = slug.split("_")
    assert base == "y" * 20
    assert nogit == "nogit"
    assert len(digest) == 6


def test_branch_wt_slug_unborn_detached_head_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(tmp_path, "init", "-q")
    responses: dict[tuple[str, ...], str | None] = {
        ("rev-parse", "--show-toplevel"): str(tmp_path),
        ("symbolic-ref", "--quiet", "--short", "HEAD"): "",
        ("rev-parse", "--short=8", "HEAD"): None,
    }

    def fake_git(_repo: Path, *args: str) -> str | None:
        return responses.get(args)

    monkeypatch.setattr(store_module, "git_output_in", fake_git)
    slug = branch_wt_slug(tmp_path)
    assert "detached_unborn" in slug


# --- Store.__init__ pragmas ---


def test_store_connection_busy_timeout(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        store.close()


def test_store_uses_wal_journal(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        assert store._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        store.close()


def test_store_uses_normal_synchronous(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        store.close()


def test_store_schema_is_committed(tmp_path: Path) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    try:
        with sqlite3.connect(cache) as observer:
            tables = {row[0] for row in observer.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "mutant" in tables
    finally:
        store.close()


# --- _migrate_if_stale ---


def test_stale_schema_clears_coverage_meta(tmp_path: Path) -> None:
    cache = tmp_path / "stale.sqlite"
    with sqlite3.connect(cache) as db:
        db.executescript(
            """
            CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
            INSERT INTO meta(k, v) VALUES('schema_version', '3');
            INSERT INTO meta(k, v) VALUES('coverage_generation', 'old-gen');
            INSERT INTO meta(k, v) VALUES('coverage_substrate_fingerprint', 'old-fp');
            """
        )
    store = Store(cache)
    try:
        assert store.get_meta("coverage_generation") is None
        assert store.get_meta("coverage_substrate_fingerprint") is None
    finally:
        store.close()


def test_stale_schema_migration_is_committed(tmp_path: Path) -> None:
    cache = tmp_path / "stale.sqlite"
    with sqlite3.connect(cache) as db:
        db.executescript(
            """
            CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
            INSERT INTO meta(k, v) VALUES('schema_version', '3');
            INSERT INTO meta(k, v) VALUES('coverage_generation', 'old');
            """
        )
    store = Store(cache)
    try:
        with sqlite3.connect(cache) as observer:
            row = observer.execute("SELECT v FROM meta WHERE k='coverage_generation'").fetchone()
        assert row is None
    finally:
        store.close()


# --- every store method serializes through the lock ---


def test_store_methods_serialize_through_the_lock(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    lock = _CountingLock()
    store._lock = cast("threading.RLock", lock)
    _put_sample(store, "survived")
    store.put_costs({"t.py::a": 0.1})
    store.put_file_hash("src/x.py", "h", "src")
    store.put_collection("ck", {"t.py::a"})
    store.ingest_coverage([("t.py::a", "src/x.py", 1)])

    calls = (
        lambda: store.get_meta("k"),
        lambda: store.set_meta("k2", "v"),
        store.clear_all,
        lambda: store.get_collection("ck"),
        lambda: store.put_collection("ck2", {"t.py::b"}),
        lambda: store.get_verdict("k"),
        store.killed_with_killers,
        lambda: store.mutant_rows_for_files(["f"]),
        store.all_mutant_rows,
        lambda: store.put_verdict(
            key="k9",
            file="f",
            lineno=1,
            operator="o",
            original="x",
            mutated="y",
            qualname="q",
            func_hash="fh",
            verdict="survived",
            bucket="b",
            covering=[],
            covering_hash="ch",
        ),
        lambda: store.update_mutant_bucket("k", "high"),
        store.count_mutants,
        lambda: store.delete_mutants({"missing"}),
        lambda: store.get_costs(["t.py::a"]),
        lambda: store.put_costs({"t.py::c": 0.2}),
        lambda: store.get_file_hashes("src"),
        lambda: store.put_file_hash("src/y.py", "h2", "src"),
        store.has_coverage,
        store.clear_coverage,
        lambda: store.coverage_replace([("t.py::a", "src/x.py", 1)], generation="g", substrate_fingerprint="s"),
        lambda: store.tests_covering("src/x.py", 1),
        lambda: store.tests_covering_file("src/x.py"),
        lambda: store.tests_seen_covering_file("src/x.py"),
        lambda: store.coverage_rows_for_file("src/x.py"),
        lambda: store.files_covered_by({"t.py::a"}),
        lambda: store.ingest_coverage([("t.py::b", "src/y.py", 2)]),
        lambda: store.apply_test_coverage_refresh(
            measured={}, measured_nodeids=(), replace_test_files=(), restored_rows=(), file_hashes={}
        ),
        lambda: store.replace_test_coverage("t.py::a", [("src/x.py", 1)]),
        lambda: store.delete_tests_in_file("tests/x_test.py"),
        store.flush,
    )
    for call in calls:
        before = lock.entered
        call()
        assert lock.entered > before

    before = lock.entered
    store.close()
    assert lock.entered > before


# --- committed writes are visible to a second connection ---


def test_set_meta_is_committed(tmp_path: Path) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    observer = sqlite3.connect(cache)
    try:
        store.set_meta("k", "v")
        assert observer.execute("SELECT v FROM meta WHERE k='k'").fetchone() == ("v",)
    finally:
        observer.close()
        store.close()


def test_clear_all_is_committed(tmp_path: Path) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    observer = sqlite3.connect(cache)
    try:
        _put_sample(store, "survived")
        store.flush()
        assert observer.execute("SELECT count(*) FROM mutant").fetchone() == (1,)
        store.clear_all()
        assert observer.execute("SELECT count(*) FROM mutant").fetchone() == (0,)
    finally:
        observer.close()
        store.close()


def test_put_collection_is_committed(tmp_path: Path) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    observer = sqlite3.connect(cache)
    try:
        store.put_collection("ck", {"t.py::a"})
        assert observer.execute("SELECT count(*) FROM test_collection WHERE cache_key='ck'").fetchone() == (1,)
    finally:
        observer.close()
        store.close()


def test_put_costs_is_committed(tmp_path: Path) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    observer = sqlite3.connect(cache)
    try:
        store.put_costs({"t.py::a": 0.1})
        assert observer.execute("SELECT count(*) FROM test_cost").fetchone() == (1,)
    finally:
        observer.close()
        store.close()


# --- coverage id cache invalidation ---


def test_id_cache_clear_clears_both_caches(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    store._test_ids = {"a": 1}
    store._file_ids = {"b": 1}
    try:
        store._coverage_id_caches_clear()
        assert store._test_ids == {}
        assert store._file_ids == {}
    finally:
        store.close()


def test_clear_coverage_calls_id_cache_clear(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    calls: list[str] = []
    store._coverage_id_caches_clear = lambda: calls.append("clear")
    try:
        store.clear_coverage()
        assert calls == ["clear"]
    finally:
        store.close()


def test_coverage_replace_calls_id_cache_clear(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    calls: list[str] = []
    store._coverage_id_caches_clear = lambda: calls.append("clear")
    try:
        store.coverage_replace([("t.py::a", "src/x.py", 1)], generation="g", substrate_fingerprint="s")
        assert calls == ["clear"]
    finally:
        store.close()


def test_failed_ingest_clears_id_caches(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    calls: list[str] = []
    store._coverage_id_caches_clear = lambda: calls.append("clear")
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            store.ingest_coverage([("t.py::a", "src/x.py", cast("int", object()))])
        assert calls == ["clear"]
    finally:
        store.close()


def test_failed_refresh_clears_id_caches(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    calls: list[str] = []
    store._coverage_id_caches_clear = lambda: calls.append("clear")
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            store.apply_test_coverage_refresh(
                measured={"t.py::a": [("src/x.py", cast("int", object()))]},
                measured_nodeids=("t.py::a",),
                replace_test_files=(),
                restored_rows=(),
                file_hashes={},
            )
        assert calls == ["clear"]
    finally:
        store.close()


def test_failed_replace_test_coverage_clears_id_caches(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    calls: list[str] = []
    store._coverage_id_caches_clear = lambda: calls.append("clear")
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            store.replace_test_coverage("t.py::a", [("src/x.py", cast("int", object()))])
        assert calls == ["clear"]
    finally:
        store.close()


# --- coverage_replace validation ---


def test_coverage_replace_requires_a_nonempty_generation_and_fingerprint(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        with pytest.raises(ValueError):
            store.coverage_replace([("t.py::a", "src/x.py", 1)], generation="", substrate_fingerprint="s")
        with pytest.raises(ValueError):
            store.coverage_replace([("t.py::a", "src/x.py", 1)], generation="g", substrate_fingerprint="")
    finally:
        store.close()


# --- delete_tests_in_file ---


def test_delete_tests_in_file_removes_test_hash_and_nodeid(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.put_file_hash("tests/x_test.py", "h", "test")
        store.ingest_coverage([("tests/x_test.py::test_a", "src/x.py", 1)])
        store.delete_tests_in_file("tests/x_test.py")
        assert store.get_file_hashes("test") == {}
        assert store.tests_covering("src/x.py", 1) == set()
    finally:
        store.close()


def test_delete_tests_in_file_removes_coverage_test_rows(tmp_path: Path) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    try:
        store.ingest_coverage([("tests/x_test.py::test_a", "src/x.py", 1)])
        store.delete_tests_in_file("tests/x_test.py")
        with sqlite3.connect(cache) as db:
            count = db.execute("SELECT count(*) FROM coverage_test").fetchone()[0]
        assert count == 0
    finally:
        store.close()


# --- verdict column mapping and flush interval ---


def test_verdict_covering_json_maps_to_the_right_column(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.put_verdict(
            key="k",
            file="f",
            lineno=1,
            operator="o",
            original="x",
            mutated="y",
            qualname="q",
            func_hash="fh",
            verdict="killed",
            bucket="review",
            covering=["t1"],
            covering_hash="ch",
            killed_by=["t2"],
        )
        store.flush()
        got = store.get_verdict("k")
        assert got is not None
        assert got["covering_json"] == '["t1"]'
        assert got["killed_by"] == ["t2"]
    finally:
        store.close()


def test_verdict_flushes_at_the_interval_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(store_module.time, "monotonic", lambda: clock["t"])
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    observer = sqlite3.connect(cache)
    try:
        _put_sample(store, "killed", key="k1")
        assert observer.execute("SELECT count(*) FROM mutant").fetchone() == (0,)
        clock["t"] = 0.25
        _put_sample(store, "killed", key="k2")
        assert observer.execute("SELECT count(*) FROM mutant").fetchone() == (2,)
    finally:
        observer.close()
        store.close()


# --- apply_test_coverage_refresh chunking ---


def test_refresh_deletes_measured_nodeid_coverage(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([("t.py::a", "src/old.py", 1)])
        store.apply_test_coverage_refresh(
            measured={"t.py::a": [("src/new.py", 2)]},
            measured_nodeids=("t.py::a",),
            replace_test_files=(),
            restored_rows=(),
            file_hashes={},
        )
        assert store.tests_covering("src/old.py", 1) == set()
        assert store.tests_covering("src/new.py", 2) == {"t.py::a"}
    finally:
        store.close()


# --- StoreCoverage and remaining row accessors ---


def test_store_coverage_delegates(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([("t.py::a", "src/x.py", 1)])
        coverage = StoreCoverage(store)
        assert coverage.tests_covering("src/x.py", 1) == {"t.py::a"}
        assert coverage.files_covered_by({"t.py::a"}) == {"src/x.py"}
    finally:
        store.close()


def test_all_mutant_rows_returns_a_list(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        _put_sample(store, "survived")
        store.flush()
        rows = store.all_mutant_rows()
        assert isinstance(rows, list)
        assert len(rows) == 1
    finally:
        store.close()


def test_update_mutant_bucket_persists(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        _put_sample(store, "survived")
        store.flush()
        store.update_mutant_bucket("k", "high")
        got = store.get_verdict("k")
        assert got is not None
        assert got["bucket"] == "high"
    finally:
        store.close()


def test_delete_mutants_with_empty_keys_returns_zero(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        assert store.delete_mutants([]) == 0
    finally:
        store.close()


def test_close_closes_the_connection(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.get_meta("coverage_generation")


def test_coverage_substrate_fingerprint_reads_meta(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.set_meta("coverage_substrate_fingerprint", "fp-v1")
        assert store.coverage_substrate_fingerprint() == "fp-v1"
    finally:
        store.close()


class _RecordingLock:
    """Records the co_name of every function that enters the lock itself (nested calls record too)."""

    def __init__(self) -> None:
        self.acquirers: list[str] = []

    def __enter__(self) -> Self:
        frame = inspect.currentframe()
        assert frame is not None
        assert frame.f_back is not None
        self.acquirers.append(frame.f_back.f_code.co_name)
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_methods_acquire_the_lock_themselves(tmp_path: Path) -> None:
    # A nested call (get_meta/coverage_generation) also enters the lock, so a bare counting lock would see a
    # nested entry even when the outer lock is dropped; recording the caller's name tells them apart.
    store = Store(tmp_path / "c.sqlite")
    lock = _RecordingLock()
    store._lock = cast("threading.RLock", lock)
    store.get_file_hashes("src")
    store.put_file_hash("src/x.py", "h", "src")
    store.ingest_coverage([("t.py::a", "src/x.py", 1)])
    store.apply_test_coverage_refresh(
        measured={}, measured_nodeids=(), replace_test_files=(), restored_rows=(), file_hashes={}
    )
    store.delete_tests_in_file("tests/x_test.py")
    store.close()
    assert {
        "get_file_hashes",
        "put_file_hash",
        "ingest_coverage",
        "apply_test_coverage_refresh",
        "delete_tests_in_file",
    } <= set(lock.acquirers)


def test_failed_coverage_replace_clears_id_caches(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    calls: list[str] = []
    store._coverage_id_caches_clear = lambda: calls.append("clear")
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            store.coverage_replace(
                [("t.py::a", "src/x.py", cast("int", object()))], generation="g", substrate_fingerprint="s"
            )
        assert calls == ["clear", "clear"]
    finally:
        store.close()


def test_put_collection_keeps_non_ascii_unescaped(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.put_collection("ck", {"t.py::test_é"})
        raw = store._db.execute("SELECT nodeids_json FROM test_collection WHERE cache_key='ck'").fetchone()
        assert raw is not None
        assert "é" in str(raw[0])
    finally:
        store.close()


def test_flush_resets_the_pending_verdict_counter(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store._pending_verdicts = 5
        store.flush()
        assert store._pending_verdicts == 0
    finally:
        store.close()


class _CountingConnection:
    """Wraps the store's SQLite connection to observe how a query was chunked."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.executes: list[str] = []

    def execute(self, sql: str, params: Sequence[str | int] = ()) -> sqlite3.Cursor:
        self.executes.append(sql)
        return self._real.execute(sql, params)

    def __enter__(self) -> sqlite3.Connection:
        return self._real.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self._real.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def test_branch_wt_slug_passes_exact_git_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, tuple[str, ...]]] = []
    responses: dict[tuple[str, ...], str | None] = {
        ("rev-parse", "--show-toplevel"): str(tmp_path),
        ("symbolic-ref", "--quiet", "--short", "HEAD"): "",
        ("rev-parse", "--short=8", "HEAD"): "abc12345",
    }

    def fake_git(repo: Path, *args: str) -> str | None:
        calls.append((repo, args))
        return responses.get(args)

    monkeypatch.setattr(store_module, "git_output_in", fake_git)
    branch_wt_slug(tmp_path)
    assert calls == [
        (tmp_path, ("rev-parse", "--show-toplevel")),
        (tmp_path, ("symbolic-ref", "--quiet", "--short", "HEAD")),
        (tmp_path, ("rev-parse", "--short=8", "HEAD")),
    ]


def test_put_collection_writes_compact_json(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.put_collection("ck", {"t.py::test_b", "t.py::test_a"})
        with sqlite3.connect(tmp_path / "c.sqlite") as db:
            raw = db.execute("SELECT nodeids_json FROM test_collection WHERE cache_key=?", ("ck",)).fetchone()[0]
        assert raw == '["t.py::test_a","t.py::test_b"]'
    finally:
        store.close()


def test_files_covered_by_chunks_large_sets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.ingest_coverage([(f"tests/t{i}_test.py::test_x", f"src/f{i}.py", 1) for i in range(1800)])
        counter = _CountingConnection(store._db)
        monkeypatch.setattr(store, "_db", counter)
        covered = store.files_covered_by({f"tests/t{i}_test.py::test_x" for i in range(1800)})
        assert covered == {f"src/f{i}.py" for i in range(1800)}
        assert sum("GROUP BY c.file_id" in sql for sql in counter.executes) == 2
    finally:
        store.close()


def test_get_costs_chunks_large_sets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        store.put_costs({f"tests/t{i}_test.py::test_x": float(i) for i in range(1800)})
        counter = _CountingConnection(store._db)
        monkeypatch.setattr(store, "_db", counter)
        costs = store.get_costs([f"tests/t{i}_test.py::test_x" for i in range(1800)])
        assert len(costs) == 1800
        assert costs["tests/t1799_test.py::test_x"] == 1799.0
        assert sum("FROM test_cost" in sql for sql in counter.executes) == 2
    finally:
        store.close()


def test_ids_ensure_chunks_large_sets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        counter = _CountingConnection(store._db)
        monkeypatch.setattr(store, "_db", counter)
        values = {f"tests/t{i}_test.py::test_x" for i in range(1800)}
        ids = store._ids_ensure("coverage_test", "nodeid", values, store._test_ids)
        assert len(ids) == 1800
        assert sum("FROM coverage_test WHERE nodeid IN" in sql for sql in counter.executes) == 2
    finally:
        store.close()


def test_apply_refresh_chunks_large_sets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "c.sqlite")
    try:
        nodeids = [f"tests/t{i}_test.py::test_x" for i in range(1800)]
        store.ingest_coverage([(nodeid, "src/f0.py", 1) for nodeid in nodeids])
        counter = _CountingConnection(store._db)
        monkeypatch.setattr(store, "_db", counter)
        store.apply_test_coverage_refresh(
            measured={nodeid: [("src/new.py", 1)] for nodeid in nodeids},
            measured_nodeids=nodeids,
            replace_test_files=(),
            restored_rows=(),
            file_hashes={},
        )
        assert store.tests_covering("src/f0.py", 1) == set()
        assert store.tests_covering("src/new.py", 1) == set(nodeids)
        assert sum("DELETE FROM coverage WHERE test_id IN" in sql for sql in counter.executes) == 2
    finally:
        store.close()
