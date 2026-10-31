"""The shared substrate pool: the build of one tree seeds another (balamut.coverage.pool).

The pool lives in the repository's common git directory, so every worktree sees it. A seed takes the compatible build
closest to the current tree; refresh re-measures the divergence instead of a full rebuild.
"""

from __future__ import annotations

import fcntl
import importlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from balamut.coverage import pool as substrate_pool
from balamut.coverage import substrate
from bm_support import make_layout, write_file
from bm_support.substrates import expected_substrate_metadata, write_valid_coverage


def _source_tree(root: Path, *, changed: int = 0, total: int = 4) -> None:
    """``total`` source files; the first ``changed`` differ from the originals."""
    for index in range(total):
        write_file(root / f"src/m{index}.py", f"M = {index + (1 if index < changed else 0)}\n")


def _built(
    root: Path, *, expected: substrate.SubstrateMetadata | None = None
) -> tuple[Path, substrate.SubstrateMetadata]:
    """A substrate built in the tree ``root``, with that tree recorded."""
    coverage = root / "coverage.substrate"
    metadata = substrate.substrate_ensure(
        coverage, expected or expected_substrate_metadata(), build=write_valid_coverage
    )
    substrate.tree_write(coverage, metadata.generation, substrate.tree_snapshot(make_layout(root)))
    return coverage, metadata


def _seed(root: Path, pool: Path) -> substrate.SubstrateMetadata | None:
    layout = make_layout(root)
    return substrate_pool.pool_seed(
        pool, root / "coverage.substrate", expected_substrate_metadata(), substrate.tree_snapshot(layout), layout=layout
    )


def test_build_of_one_tree_seeds_another(tmp_path: Path) -> None:
    builder, seeded, pool = tmp_path / "builder", tmp_path / "seeded", tmp_path / "pool"
    _source_tree(builder)
    _source_tree(seeded)
    coverage, published = _built(builder)
    substrate_pool.pool_publish(pool, coverage, published)

    metadata = _seed(seeded, pool)

    assert metadata == published
    assert substrate.substrate_inspect(seeded / "coverage.substrate") == published
    assert (
        substrate.tree_drift(
            seeded / "coverage.substrate",
            published.generation,
            substrate.tree_snapshot(make_layout(seeded)),
            in_diff=frozenset(),
            layout=make_layout(seeded),
        )
        is None
    )


def test_seed_takes_the_build_closest_to_the_current_tree(tmp_path: Path) -> None:
    far, near, seeded, pool = (tmp_path / name for name in ("far", "near", "seeded", "pool"))
    _source_tree(far, changed=1, total=8)
    _source_tree(near, total=8)
    _source_tree(seeded, total=8)
    for root in (far, near):
        coverage, published = _built(root)
        substrate_pool.pool_publish(pool, coverage, published)
    closest = substrate.substrate_inspect(near / "coverage.substrate")

    assert _seed(seeded, pool) == closest


def _helper_tree(root: Path, *, helper: int, changed_src: int = 0) -> None:
    """A tree where three test files execute the helper ``tests/support/helper.py``."""
    _source_tree(root, changed=changed_src)
    write_file(root / "tests/support/helper.py", f"HELPER = {helper}\n")
    for index in range(3):
        write_file(root / f"tests/h{index}_test.py", "from tests.support.helper import HELPER\n")


def test_seed_weighs_a_diverged_helper_by_the_tests_that_run_it(tmp_path: Path) -> None:
    """Closeness of a build is the refresh work, not the number of diverged files.

    A helper edit inside the diff is not outside divergence, yet re-measuring it means three test files. A build
    differing by one source file is closer; counting files, the build with the old helper would win and the seed
    would trigger a costlier refresh than the one it saved."""
    stale_helper, stale_src, seeded, pool = (
        tmp_path / name for name in ("stale_helper", "stale_src", "seeded", "pool")
    )
    _helper_tree(stale_helper, helper=1)
    _helper_tree(stale_src, helper=2, changed_src=1)
    _helper_tree(seeded, helper=2)
    for root in (stale_helper, stale_src):
        coverage, published = _built(root)
        substrate_pool.pool_publish(pool, coverage, published)

    seeded_from = substrate_pool.pool_seed(
        pool,
        seeded / "coverage.substrate",
        expected_substrate_metadata(),
        substrate.tree_snapshot(make_layout(seeded)),
        in_diff=frozenset({"tests/support/helper.py"}),
        layout=make_layout(seeded),
    )

    assert seeded_from == substrate.substrate_inspect(stale_src / "coverage.substrate")


def test_seed_skips_builds_of_another_support_or_runtime(tmp_path: Path) -> None:
    builder, seeded, pool = tmp_path / "builder", tmp_path / "seeded", tmp_path / "pool"
    _source_tree(builder)
    _source_tree(seeded)
    coverage, published = _built(
        builder, expected=replace(expected_substrate_metadata(), runtime_fingerprint="python-3.13")
    )
    substrate_pool.pool_publish(pool, coverage, published)

    assert _seed(seeded, pool) is None
    assert not (seeded / "coverage.substrate").exists()


def test_seed_refuses_a_build_that_diverged_over_a_quarter_of_the_tree(tmp_path: Path) -> None:
    builder, seeded, pool = tmp_path / "builder", tmp_path / "seeded", tmp_path / "pool"
    _source_tree(builder, total=40)
    _source_tree(seeded, changed=20, total=40)
    coverage, published = _built(builder)
    substrate_pool.pool_publish(pool, coverage, published)

    assert _seed(seeded, pool) is None


@pytest.mark.parametrize(
    ("change", "in_diff", "seeds"),
    [
        pytest.param("added", frozenset[str](), False, id="new-source-outside-the-diff"),
        pytest.param("added", frozenset({"src/fresh.py"}), False, id="new-source-inside-the-diff"),
        pytest.param("modified", frozenset({"src/m0.py"}), True, id="modified-source-inside-the-diff"),
    ],
)
def test_seed_takes_only_a_build_that_knows_every_source_of_the_tree(
    tmp_path: Path, change: str, in_diff: frozenset[str], seeds: bool
) -> None:
    """The build's coverage cannot name tests reaching a new module through a registry or autodiscovery, and refresh
    would not find them, even with the module in the diff. A modified source file is known: refresh finds who
    reaches it through the coverage history."""
    builder, seeded, pool = tmp_path / "builder", tmp_path / "seeded", tmp_path / "pool"
    _source_tree(builder)
    _source_tree(seeded, changed=1 if change == "modified" else 0)
    if change == "added":
        write_file(seeded / "src/fresh.py", "def f() -> int:\n    return 1\n")
    coverage, published = _built(builder)
    substrate_pool.pool_publish(pool, coverage, published)

    metadata = substrate_pool.pool_seed(
        pool,
        seeded / "coverage.substrate",
        expected_substrate_metadata(),
        substrate.tree_snapshot(make_layout(seeded)),
        in_diff=in_diff,
        layout=make_layout(seeded),
    )

    assert (metadata == published) is seeds


def test_build_without_a_tree_record_is_not_published(tmp_path: Path) -> None:
    """Without a tree record the divergence from a seed cannot be proven: such a substrate is useless in the pool."""
    builder, pool = tmp_path / "builder", tmp_path / "pool"
    _source_tree(builder)
    coverage = builder / "coverage.substrate"
    published = substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage)

    substrate_pool.pool_publish(pool, coverage, published)

    assert not pool.exists() or list(pool.iterdir()) == []


def test_build_replaced_before_publication_is_not_published(tmp_path: Path) -> None:
    """A neighbouring run of the same tree replaced the build: its files belong to another generation, and a copy
    under the old name would fail the checksums while taking a pool slot."""
    builder, pool = tmp_path / "builder", tmp_path / "pool"
    _source_tree(builder)
    coverage, stale = _built(builder)
    substrate.substrate_ensure(coverage, expected_substrate_metadata(), build=write_valid_coverage, force_rebuild=True)

    substrate_pool.pool_publish(pool, coverage, stale)

    assert not (pool / stale.generation).exists()


def test_pool_keeps_only_the_newest_builds(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    generations: list[str] = []
    for index in range(substrate_pool.POOL_KEEP + 2):
        root = tmp_path / f"tree{index}"
        _source_tree(root)
        coverage, published = _built(root)
        substrate_pool.pool_publish(pool, coverage, published)
        generations.append(published.generation)

    kept = sorted(entry.name for entry in pool.iterdir() if entry.is_dir())
    assert kept == sorted(generations[-substrate_pool.POOL_KEEP :])


def git_run(cwd: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)


def test_every_worktree_of_a_repository_shares_one_pool(tmp_path: Path) -> None:
    main, linked = tmp_path / "main", tmp_path / "linked"
    write_file(main / "README.md", "repo\n")
    git_run(main, "init", "-q")
    git_run(main, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init")
    git_run(main, "worktree", "add", "-q", str(linked))

    shared = substrate_pool.pool_dir(main)

    assert shared is not None
    assert substrate_pool.pool_dir(linked) == shared
    assert shared.is_relative_to((main / ".git").resolve())


def test_directory_outside_git_has_no_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    assert substrate_pool.pool_dir(tmp_path) is None


def test_pool_lock_creates_a_missing_pool(tmp_path: Path) -> None:
    pool = tmp_path / "nested" / "pool"
    with substrate_pool._pool_lock(pool):
        pass
    assert pool.is_dir()


def test_pool_lock_flocks_exclusive_then_unlocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ops: list[int] = []

    def fake_flock(_fd: int, op: int) -> None:
        ops.append(op)

    monkeypatch.setattr(substrate_pool.fcntl, "flock", fake_flock)
    with substrate_pool._pool_lock(tmp_path / "pool"):
        pass
    assert ops == [fcntl.LOCK_EX, fcntl.LOCK_UN]


def test_replace_with_copy_cleans_up_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("x", encoding="utf-8")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(substrate_pool.shutil, "copyfile", boom)
    with pytest.raises(OSError):
        substrate_pool._replace_with_copy(source, target)
    assert not list(tmp_path.glob(".target.*.tmp"))


def test_replace_with_copy_removes_the_staged_temp_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy that succeeds but a replace that fails leaves the staged temp behind unless the ``finally`` unlinks it."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("x", encoding="utf-8")

    def boom_replace(self: Path, *_args: object, **_kwargs: object) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", boom_replace)
    with pytest.raises(OSError):
        substrate_pool._replace_with_copy(source, target)
    assert not list(tmp_path.glob(".target.*.tmp"))


def test_entries_skip_files(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    (pool / "not-a-directory").write_text("x", encoding="utf-8")

    assert substrate_pool._entries(pool) == []


def test_entries_include_a_directory_with_a_dot_suffix(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    (pool / "visible.").mkdir(parents=True)

    assert substrate_pool._entries(pool) == [pool / "visible."]


def test_last_used_defaults_to_zero(tmp_path: Path) -> None:
    entry = tmp_path / "entry"
    entry.mkdir()

    assert substrate_pool._last_used(entry) == 0


def test_last_used_reads_the_stamp(tmp_path: Path) -> None:
    entry = tmp_path / "entry"
    entry.mkdir()
    (entry / substrate_pool._LAST_USED).write_text("42", encoding="utf-8")

    assert substrate_pool._last_used(entry) == 42


def test_publish_creates_a_nested_pool(tmp_path: Path) -> None:
    builder, pool = tmp_path / "builder", tmp_path / "nested" / "pool"
    _source_tree(builder)
    coverage, published = _built(builder)

    substrate_pool.pool_publish(pool, coverage, published)

    assert (pool / published.generation).is_dir()


def test_seed_creates_missing_coverage_parent(tmp_path: Path) -> None:
    builder, seeded, pool = tmp_path / "builder", tmp_path / "seeded", tmp_path / "pool"
    _source_tree(builder)
    _source_tree(seeded)
    coverage, published = _built(builder)
    substrate_pool.pool_publish(pool, coverage, published)

    # Two missing levels: ``parents=True`` creates both, ``parents=False`` cannot.
    nested_coverage = seeded / "nested" / "deeper" / "coverage.substrate"
    layout = make_layout(seeded)
    metadata = substrate_pool.pool_seed(
        pool, nested_coverage, expected_substrate_metadata(), substrate.tree_snapshot(layout), layout=layout
    )

    assert metadata == published
    assert nested_coverage.exists()


def test_seed_touches_the_selected_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builder, seeded, pool = tmp_path / "builder", tmp_path / "seeded", tmp_path / "pool"
    _source_tree(builder)
    _source_tree(seeded)
    coverage, published = _built(builder)
    substrate_pool.pool_publish(pool, coverage, published)
    touched: list[Path] = []
    monkeypatch.setattr(substrate_pool, "_touch", touched.append)

    _seed(seeded, pool)

    assert touched


def test_eviction_ignores_an_unremovable_entry(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    pool.mkdir(parents=True)
    real = tmp_path / "real-dir"
    real.mkdir()
    (pool / "symlink-entry").symlink_to(real, target_is_directory=True)
    for index in range(substrate_pool.POOL_KEEP + 1):
        root = tmp_path / f"tree{index}"
        _source_tree(root)
        coverage, published = _built(root)
        substrate_pool.pool_publish(pool, coverage, published)

    # The unremovable symlink entry was skipped: eviction still completed.


def test_pool_keep_is_pinned() -> None:
    importlib.reload(substrate_pool)
    assert substrate_pool.POOL_KEEP == 4


def test_pool_subdir_is_pinned() -> None:
    importlib.reload(substrate_pool)
    assert Path("balamut") / "substrates" == substrate_pool._POOL_SUBDIR


def test_seed_tolerates_existing_coverage_parent(tmp_path: Path) -> None:
    builder, seeded, pool = tmp_path / "builder", tmp_path / "seeded", tmp_path / "pool"
    _source_tree(builder)
    _source_tree(seeded)
    coverage, published = _built(builder)
    substrate_pool.pool_publish(pool, coverage, published)

    nested_coverage = seeded / "coverage.substrate"
    nested_coverage.parent.mkdir(parents=True, exist_ok=True)
    layout = make_layout(seeded)
    metadata = substrate_pool.pool_seed(
        pool, nested_coverage, expected_substrate_metadata(), substrate.tree_snapshot(layout), layout=layout
    )

    assert metadata == published
    assert nested_coverage.exists()


def test_publish_stages_the_entry_inside_the_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builder, pool = tmp_path / "builder", tmp_path / "pool"
    _source_tree(builder)
    coverage, published = _built(builder)
    seen: dict[str, Path] = {}
    real_rename = Path.rename

    def fake_rename(self: Path, target: Path) -> Path:
        seen["source"] = self
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", fake_rename)
    substrate_pool.pool_publish(pool, coverage, published)
    assert seen["source"].parent.parent == pool
    assert seen["source"].parent.name.startswith(f".{published.generation}.")


def test_seed_stages_in_the_coverage_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builder, seeded, pool = tmp_path / "builder", tmp_path / "seeded", tmp_path / "pool"
    _source_tree(builder)
    _source_tree(seeded)
    coverage, published = _built(builder)
    substrate_pool.pool_publish(pool, coverage, published)
    sources: list[Path] = []
    real_replace = Path.replace

    def fake_replace(self: Path, target_path: Path) -> None:
        sources.append(self)
        real_replace(self, target_path)

    monkeypatch.setattr(Path, "replace", fake_replace)
    _seed(seeded, pool)
    cov_path = seeded / "coverage.substrate"
    staged = [source for source in sources if source.parent.name.startswith(f".{cov_path.name}.seed.")]
    assert staged
    assert all(source.parent.parent == cov_path.parent for source in staged)
