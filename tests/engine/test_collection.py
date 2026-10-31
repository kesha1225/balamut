"""Fail-closed pytest collection of oracle tests and its exact persistent cache (balamut.engine.collection)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from functools import partial
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from balamut.coverage import substrate
from balamut.coverage.store import Store
from balamut.engine import collection
from bm_support import write_file
from bm_support.scopes import scope

_scope = partial(scope, {"tests/x_test.py": (1,)}, file_hash="scope-hash")


def _metadata() -> substrate.SubstrateMetadata:
    return substrate.SubstrateMetadata(
        schema_version=1,
        complete=True,
        generation="generation",
        config_fingerprint="config",
        test_inventory_fingerprint="inventory",
        runtime_fingerprint="runtime",
        schema_fingerprint="schema",
        coverage_sha256="coverage",
    )


def _key(root: Path) -> collection.CollectionKey:
    return collection.build_collection_key(
        frozenset({"tests/x_test.py"}), scope=_scope(), metadata=_metadata(), reexport={}, root=root
    )


def test_collection_cache_reuses_exact_parametrized_nodeids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    key = _key(tmp_path)
    calls = 0

    def run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return CompletedProcess(
            args=["pytest"], returncode=0, stdout="tests/x_test.py::test_x[a]\ntests/x_test.py::test_x[b]\n", stderr=""
        )

    monkeypatch.setattr(collection.subprocess, "run", run)
    store = Store(tmp_path / "cache.sqlite")
    try:
        expected = frozenset({"tests/x_test.py::test_x[a]", "tests/x_test.py::test_x[b]"})
        for _ in range(2):
            nodeids = collection.collect_test_nodeids(
                frozenset({"tests/x_test.py"}), store=store, cache_key=key, root=tmp_path
            )
            assert nodeids == expected
        assert calls == 1
    finally:
        store.close()


def test_collection_runs_in_the_project_root_and_clears_the_marker_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    write_file(tmp_path / "tests/x_test.py", "import pytest\n\n@pytest.mark.fragile\ndef test_x():\n    assert True\n")
    write_file(
        tmp_path / "pytest.ini", '[pytest]\naddopts = -m "not fragile"\nmarkers =\n    fragile: filtered by default\n'
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    store = Store(tmp_path / "cache.sqlite")
    try:
        nodeids = collection.collect_test_nodeids(
            frozenset({"tests/x_test.py"}), store=store, cache_key=None, root=tmp_path
        )
    finally:
        store.close()

    assert nodeids == frozenset({"tests/x_test.py::test_x"})


def test_collection_ignores_an_inherited_mutation_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A collection must see the original code: a mutation plugin or mutant inherited from the caller is dropped."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p balamut.runtime.plugin")
    monkeypatch.setenv("BALAMUT_MUTANT_FILE", str(tmp_path / "missing.json"))
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    store = Store(tmp_path / "cache.sqlite")
    try:
        nodeids = collection.collect_test_nodeids(
            frozenset({"tests/x_test.py"}), store=store, cache_key=None, root=tmp_path
        )
    finally:
        store.close()

    assert nodeids == frozenset({"tests/x_test.py::test_x"})


def test_collection_failure_is_not_cached_and_contains_both_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    key = _key(tmp_path)

    def failed(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=["pytest"], returncode=1, stdout="collection stdout", stderr="collection failed")

    monkeypatch.setattr(collection.subprocess, "run", failed)
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(collection.CollectionError) as error:
            collection.collect_test_nodeids(frozenset({"tests/x_test.py"}), store=store, cache_key=key, root=tmp_path)

        assert "stdout:\ncollection stdout" in str(error.value)
        assert "stderr:\ncollection failed" in str(error.value)
        assert store.get_collection(key.value) is None
    finally:
        store.close()


def test_collection_key_changes_with_target_content(tmp_path: Path) -> None:
    test_file = write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    first = _key(tmp_path)
    test_file.write_text("def test_x():\n    assert False\n", encoding="utf-8")

    assert first != _key(tmp_path)


def test_collection_key_changes_with_the_reexport_graph(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")
    plain = _key(tmp_path)
    linked = collection.build_collection_key(
        frozenset({"tests/x_test.py"}),
        scope=_scope(),
        metadata=_metadata(),
        reexport={("tests/x_test.py", "test_x"): "tests/wrapper_test.py"},
        root=tmp_path,
    )

    assert plain != linked


def test_empty_successful_collection_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def empty(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=["pytest"], returncode=0, stdout="", stderr="warning")

    monkeypatch.setattr(collection.subprocess, "run", empty)
    store = Store(tmp_path / "cache.sqlite")
    key = collection.CollectionKey("empty")
    try:
        with pytest.raises(collection.CollectionError, match="no node ids") as error:
            collection.collect_test_nodeids(frozenset({"tests/x_test.py"}), store=store, cache_key=key, root=tmp_path)

        assert "stderr:\nwarning" in str(error.value)
        assert store.get_collection(key.value) is None
    finally:
        store.close()


def test_collection_timeout_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        raise TimeoutExpired(cmd="pytest", timeout=300)

    monkeypatch.setattr(collection.subprocess, "run", timeout)
    store = Store(tmp_path / "cache.sqlite")
    key = collection.CollectionKey("timeout")
    try:
        with pytest.raises(collection.CollectionError, match="could not run pytest --collect-only"):
            collection.collect_test_nodeids(frozenset({"tests/x_test.py"}), store=store, cache_key=key, root=tmp_path)

        assert store.get_collection(key.value) is None
    finally:
        store.close()


def test_no_paths_collect_nothing_without_running_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        raise AssertionError

    monkeypatch.setattr(collection.subprocess, "run", unexpected)
    store = Store(tmp_path / "cache.sqlite")
    try:
        assert collection.collect_test_nodeids(frozenset(), store=store, cache_key=None, root=tmp_path) == frozenset()
    finally:
        store.close()


def test_collection_key_uses_canonical_sorted_json(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")

    key = _key(tmp_path)
    payload = {
        "metadata": asdict(_metadata()),
        "reexport": [],
        "scope": asdict(_scope()),
        "targets": [("tests/x_test.py", substrate.file_sha256(tmp_path / "tests/x_test.py"))],
    }
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert key.value == expected


def test_collection_key_keeps_non_ascii_paths_unescaped(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")

    unicode_scope = scope({"tests/tést.py": (1,)}, file_hash="h")
    key = collection.build_collection_key(
        frozenset({"tests/x_test.py"}), scope=unicode_scope, metadata=_metadata(), reexport={}, root=tmp_path
    )
    payload = {
        "metadata": asdict(_metadata()),
        "reexport": [],
        "scope": asdict(unicode_scope),
        "targets": [("tests/x_test.py", substrate.file_sha256(tmp_path / "tests/x_test.py"))],
    }
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert key.value == expected


def test_collection_reports_the_nonzero_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "tests/x_test.py", "def test_x():\n    assert True\n")

    def run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        if kwargs.get("check"):
            raise collection.subprocess.CalledProcessError(1, argv)
        return CompletedProcess(args=argv, returncode=1, stdout="sout", stderr="serr")

    monkeypatch.setattr(collection.subprocess, "run", run)
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(collection.CollectionError, match="exited with 1"):
            collection.collect_test_nodeids(frozenset({"tests/x_test.py"}), store=store, cache_key=None, root=tmp_path)
    finally:
        store.close()


def test_collection_import_time_timeout_constant() -> None:
    """The import-time collect timeout constant is 300 seconds."""
    import importlib.util
    import sys

    name = "balamut.engine._collection_probe"
    spec = importlib.util.spec_from_file_location(name, collection.__file__)
    assert spec is not None
    loader = spec.loader
    assert loader is not None
    probe = importlib.util.module_from_spec(spec)
    sys.modules[name] = probe
    try:
        loader.exec_module(probe)
    finally:
        del sys.modules[name]

    assert probe._COLLECT_TIMEOUT_SECONDS == 300
