"""The substrate describes the tree it was measured on (``balamut diff``): drift is refreshed, a noticeable share
or a new source file rebuilds, and a new tree starts from the shared pool."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from balamut.coverage import substrate
from bm_support import write_file
from bm_support.diff import (
    Report,
    Scenario,
    coverage_path,
    expected,
    mutation_argv,
    project_profile,
    published_metadata,
    run_main,
    section,
)


def _tree(root: Path) -> None:
    """src/x.py is in the default scenario scope; src/y.py and tests/y_test.py are outside it."""
    write_file(root / "src/x.py", "def f() -> int:\n    return 1\n")
    write_file(root / "src/y.py", "def g() -> int:\n    return 2\n")
    write_file(root / "tests/y_test.py", "def test_g() -> None:\n    assert True\n")


def _cached_with_recorded_tree(root: Path) -> substrate.SubstrateMetadata:
    Scenario(root)
    cached = published_metadata(expected(root), generation="cached-generation")
    substrate.tree_write(coverage_path(root), cached.generation, substrate.tree_snapshot(project_profile(root).layout))
    return cached


def _substrate(report: Report) -> dict[str, object]:
    return section(report, "substrate")


def test_the_build_records_the_tree_it_measured(tmp_path: Path) -> None:
    _tree(tmp_path)

    return_code, _report = run_main(Scenario(tmp_path))

    layout = project_profile(tmp_path).layout
    assert return_code == 0
    assert (
        substrate.tree_drift(
            coverage_path(tmp_path),
            "rebuilt-generation",
            substrate.tree_snapshot(layout),
            in_diff=frozenset(),
            layout=layout,
        )
        is None
    )


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        pytest.param("tests/y_test.py", "def test_g() -> None:\n    assert 1 == 1\n", id="test-changed"),
        pytest.param("tests/z_test.py", "def test_h() -> None:\n    assert True\n", id="test-added"),
        pytest.param("src/y.py", "def g() -> int:\n    return 3\n", id="src-changed"),
        pytest.param("src/y.py", None, id="src-removed"),
    ],
)
def test_drift_outside_the_diff_is_refreshed_instead_of_rebuilt(
    tmp_path: Path, relative: str, content: str | None
) -> None:
    _tree(tmp_path)
    scenario = Scenario(tmp_path, cached_metadata=_cached_with_recorded_tree(tmp_path))
    if content is None:
        (tmp_path / relative).unlink()
    else:
        write_file(tmp_path / relative, content)

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert "coverage-build" not in scenario.events
    assert scenario.ensure_force is False
    assert _substrate(report) == {
        "generation": "cached-generation",
        "reused": True,
        "miss_reason": None,
        "drift_files": 1,
        "seeded_from": None,
    }


@pytest.mark.parametrize("new_source", ["src/z.py", "src/x.py"], ids=["outside-the-diff", "inside-the-diff"])
def test_a_source_the_build_never_saw_rebuilds(tmp_path: Path, new_source: str) -> None:
    _tree(tmp_path)
    (tmp_path / "src/x.py").unlink()
    scenario = Scenario(tmp_path, cached_metadata=_cached_with_recorded_tree(tmp_path))
    write_file(tmp_path / "src/x.py", "def f() -> int:\n    return 1\n")
    write_file(tmp_path / new_source, "def h() -> int:\n    return 4\n")

    _return_code, report = run_main(scenario)

    assert "coverage-build" in scenario.events
    assert scenario.ensure_force is True
    assert _substrate(report)["miss_reason"] == "tree_new_src"


def test_drift_over_a_quarter_of_the_tree_rebuilds(tmp_path: Path) -> None:
    _tree(tmp_path)
    for index in range(20):
        write_file(tmp_path / f"src/m{index}.py", f"M = {index}\n")
    scenario = Scenario(tmp_path, cached_metadata=_cached_with_recorded_tree(tmp_path))
    for index in range(20):
        write_file(tmp_path / f"src/m{index}.py", f"M = {index + 1}\n")

    _return_code, report = run_main(scenario)

    assert "coverage-build" in scenario.events
    assert scenario.ensure_force is True
    assert _substrate(report)["miss_reason"] == "tree_changed_outside_diff"


def test_a_diff_file_changed_since_the_build_keeps_the_substrate(tmp_path: Path) -> None:
    _tree(tmp_path)
    scenario = Scenario(tmp_path, cached_metadata=_cached_with_recorded_tree(tmp_path))
    write_file(tmp_path / "src/x.py", "def f() -> int:\n    return 10\n")

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert "coverage-build" not in scenario.events
    assert _substrate(report)["miss_reason"] is None


def test_a_substrate_without_a_tree_record_rebuilds(tmp_path: Path) -> None:
    _tree(tmp_path)
    Scenario(tmp_path)
    cached = published_metadata(expected(tmp_path), generation="cached-generation")
    scenario = Scenario(tmp_path, cached_metadata=cached, tree_recorded=False)

    _return_code, report = run_main(scenario)

    assert "coverage-build" in scenario.events
    assert _substrate(report)["miss_reason"] == "tree_unknown"


def _tree_with_support(root: Path) -> None:
    _tree(root)
    write_file(root / "tests/support/helper.py", "VALUE = 1\n")
    write_file(
        root / "tests/y_test.py", "from tests.support.helper import VALUE\n\ndef test_g() -> None:\n    assert VALUE\n"
    )


def test_a_support_change_is_refreshed_through_the_tests_that_import_it(tmp_path: Path) -> None:
    _tree_with_support(tmp_path)
    scenario = Scenario(tmp_path, cached_metadata=_cached_with_recorded_tree(tmp_path))
    write_file(tmp_path / "tests/support/helper.py", "VALUE = 2\n")

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert "coverage-build" not in scenario.events
    assert _substrate(report) == {
        "generation": "cached-generation",
        "reused": True,
        "miss_reason": None,
        "drift_files": 1,
        "seeded_from": None,
    }


def test_support_the_whole_suite_depends_on_rebuilds(tmp_path: Path) -> None:
    """What the root conftest imports runs in every test: its refresh is a full run without a new publication."""
    _tree(tmp_path)
    write_file(tmp_path / "tests/conftest.py", "from tests.support.rooted import ROOTED\n")
    write_file(tmp_path / "tests/support/rooted.py", "ROOTED = 1\n")
    for index in range(20):
        write_file(tmp_path / f"tests/t{index}_test.py", "def test_t() -> None:\n    assert True\n")
    scenario = Scenario(tmp_path, cached_metadata=_cached_with_recorded_tree(tmp_path))
    write_file(tmp_path / "tests/support/rooted.py", "ROOTED = 2\n")

    _return_code, report = run_main(scenario)

    assert "coverage-build" in scenario.events
    assert _substrate(report)["miss_reason"] == "tree_support_changed"


def _pool_tree(root: Path) -> None:
    write_file(root / "src/x.py", "def f() -> int:\n    return 1\n")
    write_file(root / "tests/x_test.py", "def test_f() -> None:\n    assert True\n")


def _pooled(root: Path) -> substrate.SubstrateMetadata:
    Scenario(root)
    return published_metadata(expected(root), generation="pooled-generation")


def test_a_missing_substrate_is_seeded_from_the_pool_instead_of_built(tmp_path: Path) -> None:
    _pool_tree(tmp_path)
    scenario = Scenario(tmp_path, pool_seed=_pooled(tmp_path))

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert "coverage-build" not in scenario.events
    assert _substrate(report) == {
        "generation": "pooled-generation",
        "reused": False,
        "miss_reason": "missing_coverage",
        "drift_files": 0,
        "seeded_from": "pooled-generation",
    }
    # A seed was measured on another tree before this scope was pinned: the refresh re-measures the scope files.
    assert "--fresh-substrate-generation" not in mutation_argv(scenario)


def test_a_seeded_substrate_with_a_refreshable_drift_is_still_seeded_from(tmp_path: Path) -> None:
    """A refreshable drift of the seeded build keeps the seed attribution; only a rebuild drops it."""
    _pool_tree(tmp_path)
    pooled = _pooled(tmp_path)
    scenario = Scenario(tmp_path, pool_seed=pooled)

    def drifted_seed(
        _repo_root: Path,
        path: Path,
        _expected: substrate.SubstrateMetadata,
        current: substrate.TreeRecord,
        _in_diff: frozenset[str],
    ) -> substrate.SubstrateMetadata | None:
        scenario.events.append("substrate-seed")
        stale_hashes = dict(current.hashes)
        stale_hashes["tests/x_test.py"] = "stale-hash"
        substrate.tree_write(
            path, pooled.generation, substrate.TreeRecord(hashes=stale_hashes, reexports=current.reexports)
        )
        scenario.cached_metadata = pooled
        return pooled

    runtime = replace(scenario.runtime(), substrate_seed=drifted_seed)

    _return_code, report = run_main(scenario, runtime=runtime)

    assert _substrate(report)["seeded_from"] == "pooled-generation"


def test_a_local_substrate_of_another_config_is_replaced_by_a_pooled_one(tmp_path: Path) -> None:
    _pool_tree(tmp_path)
    pooled = _pooled(tmp_path)
    stale = replace(pooled, generation="local-generation", config_fingerprint="old-config")
    scenario = Scenario(tmp_path, cached_metadata=stale, pool_seed=pooled)

    _return_code, report = run_main(scenario)

    assert "coverage-build" not in scenario.events
    assert _substrate(report)["miss_reason"] == "config_fingerprint"
    assert _substrate(report)["seeded_from"] == "pooled-generation"


def test_a_refreshable_drift_does_not_reseed_from_the_pool(tmp_path: Path) -> None:
    """A refreshable drift reuses the tree's own substrate; it must not be mistaken for a broken one and reseeded."""
    _tree(tmp_path)
    cached = _cached_with_recorded_tree(tmp_path)
    pooled = _pooled(tmp_path)
    scenario = Scenario(tmp_path, cached_metadata=cached, pool_seed=pooled)
    write_file(tmp_path / "src/y.py", "def g() -> int:\n    return 3\n")

    _return_code, report = run_main(scenario)

    assert "coverage-build" not in scenario.events
    assert _substrate(report)["miss_reason"] is None
    assert _substrate(report)["seeded_from"] is None


def test_an_empty_pool_falls_back_to_a_build_that_is_then_shared(tmp_path: Path) -> None:
    _pool_tree(tmp_path)
    scenario = Scenario(tmp_path)

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert scenario.events.index("substrate-seed") < scenario.events.index("coverage-build")
    assert scenario.pool_shared == ["rebuilt-generation"]
    assert _substrate(report)["seeded_from"] is None


def test_a_reused_substrate_is_neither_seeded_nor_shared_again(tmp_path: Path) -> None:
    _pool_tree(tmp_path)
    scenario = Scenario(tmp_path, cached_metadata=_pooled(tmp_path))

    run_main(scenario)

    assert "substrate-seed" not in scenario.events
    assert scenario.pool_shared == []


def test_a_forced_rebuild_skips_the_pool(tmp_path: Path) -> None:
    _pool_tree(tmp_path)
    scenario = Scenario(tmp_path, pool_seed=_pooled(tmp_path))

    _return_code, report = run_main(scenario, "--rebuild-substrate")

    assert "substrate-seed" not in scenario.events
    assert "coverage-build" in scenario.events
    assert _substrate(report)["seeded_from"] is None
