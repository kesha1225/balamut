"""Loading per-test coverage contexts into a queryable map (balamut.coverage.coverage_map).

A synthetic ``.coverage`` is built through the public ``coverage.CoverageData`` API, without running pytest.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from coverage import CoverageData

from balamut.coverage import coverage_map
from balamut.coverage.coverage_map import CoverageMap, load_coverage_map, normalize_context


def test_files_covered_by_reverse_index() -> None:
    cov = CoverageMap(
        by_file={
            "src/caller.py": {10: frozenset({"t.py::test_a"})},
            "src/helper.py": {5: frozenset({"t.py::test_a"})},
            "src/other.py": {1: frozenset({"t.py::test_b"})},
        }
    )
    assert cov.files_covered_by({"t.py::test_a"}) == {"src/caller.py", "src/helper.py"}
    assert cov.files_covered_by({"t.py::test_b"}) == {"src/other.py"}
    assert cov.files_covered_by(set()) == set()


def _write_coverage(tmp_path: Path) -> Path:
    path = tmp_path / ".coverage"
    data = CoverageData(basename=str(path))
    data.set_context("tests/a_test.py::test_one")
    data.add_lines({"src/x.py": [1, 2, 3], "src/y.py": [10]})
    data.set_context("tests/a_test.py::test_two|run")  # the phase suffix must normalize away
    data.add_lines({"src/x.py": [3, 4]})
    data.write()
    return path


def test_loads_line_to_tests(tmp_path: Path) -> None:
    cov = load_coverage_map(_write_coverage(tmp_path))
    assert cov.files() == ["src/x.py", "src/y.py"]
    assert cov.tests_covering("src/x.py", 1) == frozenset({"tests/a_test.py::test_one"})
    assert cov.tests_covering("src/y.py", 10) == frozenset({"tests/a_test.py::test_one"})


def test_line_covered_by_multiple_tests_unions(tmp_path: Path) -> None:
    cov = load_coverage_map(_write_coverage(tmp_path))
    assert cov.tests_covering("src/x.py", 3) == frozenset({"tests/a_test.py::test_one", "tests/a_test.py::test_two"})


def test_covered_lines_and_totals(tmp_path: Path) -> None:
    cov = load_coverage_map(_write_coverage(tmp_path))
    assert cov.covered_lines("src/x.py") == frozenset({1, 2, 3, 4})
    assert cov.covered_lines("src/missing.py") == frozenset()
    assert cov.tests_covering("src/x.py", 999) == frozenset()
    assert cov.total_lines() == 5


def test_normalize_context_strips_phase() -> None:
    assert normalize_context("tests/a_test.py::test_x|run") == "tests/a_test.py::test_x"
    assert normalize_context("tests/a_test.py::test_x|setup") == "tests/a_test.py::test_x"
    assert normalize_context("tests/a_test.py::test_x|teardown") == "tests/a_test.py::test_x"
    assert normalize_context("tests/a_test.py::test_x") == "tests/a_test.py::test_x"
    # A `|` inside a parameter id: only the final phase suffix is cut.
    assert normalize_context("tests/a_test.py::test_f[a|b]|run") == "tests/a_test.py::test_f[a|b]"
    assert normalize_context("tests/a_test.py::test_f[a|b]") == "tests/a_test.py::test_f[a|b]"


def test_absolute_paths_are_relativized(tmp_path: Path) -> None:
    path = tmp_path / ".coverage"
    data = CoverageData(basename=str(path))
    data.set_context("tests/a_test.py::test_one")
    data.add_lines({str(tmp_path / "src/x.py"): [1]})
    data.write()
    cov = load_coverage_map(path, root=tmp_path)
    assert cov.files() == ["src/x.py"]


def test_tests_covering_an_unknown_file_is_empty(tmp_path: Path) -> None:
    cov = CoverageMap(by_file={"src/x.py": {1: frozenset({"t.py::test_a"})}})
    assert cov.tests_covering("src/missing.py", 1) == frozenset()


def test_absolute_paths_without_root_are_kept(tmp_path: Path) -> None:
    path = tmp_path / ".coverage"
    data = CoverageData(basename=str(path))
    data.set_context("tests/a_test.py::test_one")
    data.add_lines({str(tmp_path / "src/x.py"): [1]})
    data.write()
    cov = load_coverage_map(path)  # root=None: no relativization, the measured path is kept as-is
    assert cov.files() == [str(tmp_path / "src/x.py")]


def test_absolute_path_outside_root_is_kept(tmp_path: Path) -> None:
    path = tmp_path / ".coverage"
    other = tmp_path / "elsewhere"
    other.mkdir()
    data = CoverageData(basename=str(path))
    data.set_context("tests/a_test.py::test_one")
    data.add_lines({str(other / "x.py"): [1]})
    data.write()
    cov = load_coverage_map(path, root=tmp_path / "src")
    assert cov.files() == [str(other / "x.py")]


def test_coverage_map_dataclass_field_is_derived_not_a_constructor_arg() -> None:
    # Re-run the import-time class body under a probe name: files_by_test is field(init=False).
    spec = importlib.util.spec_from_file_location("_probe_coverage_map", coverage_map.__file__)
    assert spec is not None
    assert spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    sys.modules[probe.__name__] = probe
    try:
        spec.loader.exec_module(probe)
    finally:
        del sys.modules[probe.__name__]
    cov = probe.CoverageMap(by_file={"src/x.py": {1: frozenset({"t.py::test_a"})}})
    assert cov.files_by_test == {"t.py::test_a": frozenset({"src/x.py"})}


def test_files_covered_by_an_unknown_test_is_empty() -> None:
    # The default of `files_by_test.get` is None; the explicit `frozenset()` keeps an unknown test from crashing.
    cov = CoverageMap(by_file={"src/x.py": {1: frozenset({"t.py::test_a"})}})
    assert cov.files_covered_by({"t.py::test_a", "t.py::missing"}) == {"src/x.py"}
    assert cov.files_covered_by({"t.py::missing"}) == set()
