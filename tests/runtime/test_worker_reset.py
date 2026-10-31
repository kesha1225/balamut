"""Resetting a persistent worker to its baseline (balamut.runtime.worker_reset).

The reset keeps the import graph intact and clears ``@lru_cache`` wrappers. Baselines are built by hand here
(only the cache clears a test needs) so the process-wide caches stay untouched.
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path

from balamut.runtime.worker_reset import WorkerBaseline, capture_baseline, reset_to_baseline


def _load_py_module(tmp_path: Path, name: str) -> None:
    path = tmp_path / f"{name}.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


def test_capture_baseline_snapshots_modules_without_mutating() -> None:
    baseline = capture_baseline()
    assert "sys" in baseline.modules
    assert isinstance(baseline.cache_clears, tuple)
    assert "sys" in sys.modules


def test_reset_keeps_pure_python_module_imported_after_baseline(tmp_path: Path) -> None:
    baseline = WorkerBaseline(modules=frozenset(sys.modules), cache_clears=())
    name = "balamut_pure_py_reset_mod"
    _load_py_module(tmp_path, name)
    try:
        assert name in sys.modules
        reset_to_baseline(baseline)
        assert name in sys.modules
    finally:
        sys.modules.pop(name, None)


def test_reset_keeps_submodule_and_parent_attr(tmp_path: Path) -> None:
    (tmp_path / "bm_pkg").mkdir()
    (tmp_path / "bm_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "bm_pkg" / "sub.py").write_text("MARK = 1\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.import_module("bm_pkg")
        baseline = WorkerBaseline(modules=frozenset(sys.modules), cache_clears=())
        importlib.import_module("bm_pkg.sub")
        pkg = sys.modules["bm_pkg"]
        assert "sub" in pkg.__dict__
        reset_to_baseline(baseline)
        assert "bm_pkg.sub" in sys.modules
        assert "sub" in pkg.__dict__
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("bm_pkg.sub", None)
        sys.modules.pop("bm_pkg", None)


def test_reset_does_not_reimport_partial_python_graph(tmp_path: Path) -> None:
    # Re-importing part of a graph re-runs module bodies that attach singletons: identity must survive.
    package = tmp_path / "bm_identity_graph"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "router.py").write_text("parent = None\n", encoding="utf-8")
    (package / "app.py").write_text(
        "from . import router\n"
        "if router.parent is not None:\n"
        "    raise RuntimeError('router already attached')\n"
        "router.parent = object()\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.import_module("bm_identity_graph.router")
        baseline = WorkerBaseline(modules=frozenset(sys.modules), cache_clears=())
        app = importlib.import_module("bm_identity_graph.app")
        reset_to_baseline(baseline)
        assert importlib.import_module("bm_identity_graph.app") is app
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("bm_identity_graph.app", None)
        sys.modules.pop("bm_identity_graph.router", None)
        sys.modules.pop("bm_identity_graph", None)


def test_reset_clears_lru_cache() -> None:
    calls: list[int] = []

    @functools.lru_cache
    def f(x: int) -> int:
        calls.append(x)
        return x * 2

    assert f(3) == 6
    assert f(3) == 6
    assert calls == [3]
    baseline = WorkerBaseline(modules=frozenset(sys.modules), cache_clears=(f.cache_clear,))
    reset_to_baseline(baseline)
    assert f(3) == 6
    assert calls == [3, 3]
