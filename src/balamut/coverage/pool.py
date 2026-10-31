"""The shared pool of coverage substrates: the build of one tree seeds another.

A substrate lives per (worktree, branch), so a new worktree or branch would start with a full coverage run although
a neighbouring tree of the same repository has just measured almost the same thing. The pool lives in the common git
directory (``git rev-parse --git-common-dir``), shared by every worktree. A seed takes the compatible build closest
to the current tree; refresh re-measures the divergence.
"""

from __future__ import annotations

import fcntl
import shutil
import tempfile
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from balamut.config import Layout
from balamut.coverage import substrate
from balamut.gitutil import git_output_in

POOL_KEEP = 4  # enough for neighbouring branches; a build can weigh tens of megabytes
_POOL_SUBDIR = Path("balamut") / "substrates"
_ENTRY_COVERAGE = "substrate"
_LAST_USED = "last-used"  # when a build was published or taken as a seed: the pool evicts the least recently used


def pool_dir(repo_root: Path) -> Path | None:
    """The pool directory in the repository's common git directory; None outside a git repository."""
    common_dir = git_output_in(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common_dir).resolve() / _POOL_SUBDIR if common_dir else None


@contextmanager
def _pool_lock(pool: Path) -> Generator[None]:
    """Publication, eviction and seed selection share one lock: a seed never reads a build being evicted."""
    pool.mkdir(parents=True, exist_ok=True)
    with (pool / ".lock").open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _replace_with_copy(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_substrate(source: Path, target: Path, generation: str, tree: substrate.TreeRecord) -> None:
    """Copy a substrate with its sidecars; the metadata goes last, since ``substrate_inspect`` needs it."""
    _replace_with_copy(source, target)
    _replace_with_copy(substrate.costs_path(source), substrate.costs_path(target))
    substrate.tree_write(target, generation, tree)
    _replace_with_copy(substrate.metadata_path(source), substrate.metadata_path(target))


def _touch(entry: Path) -> None:
    (entry / _LAST_USED).write_text(str(time.time_ns()), encoding="utf-8")


def _last_used(entry: Path) -> int:
    try:
        return int((entry / _LAST_USED).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return 0


def _entries(pool: Path) -> list[Path]:
    return [entry for entry in pool.iterdir() if entry.is_dir() and not entry.name.startswith(".")]


def pool_publish(pool: Path, coverage_path: Path, metadata: substrate.SubstrateMetadata) -> None:
    """Put a build into the pool, evicting the least recently used ones beyond ``POOL_KEEP``.

    A build without a tree record is skipped: its divergence from any tree is unprovable. The copy is taken under
    the lock of its own tree and only while that tree still holds this very generation (a neighbouring run may
    have replaced it); the copy then moves into the pool under the pool's lock."""
    tree = substrate.tree_read(coverage_path, metadata.generation)
    if tree is None:
        return
    pool.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=pool, prefix=f".{metadata.generation}.") as raw:
        staged = Path(raw) / "entry"
        staged.mkdir()
        with substrate.publication_lock(coverage_path):
            if substrate.substrate_inspect(coverage_path) != metadata:
                return
            _copy_substrate(coverage_path, staged / _ENTRY_COVERAGE, metadata.generation, tree)
        with _pool_lock(pool):
            entry = pool / metadata.generation
            if not entry.exists():
                staged.rename(entry)
            _touch(entry)
            for stale in sorted(_entries(pool), key=_last_used, reverse=True)[POOL_KEEP:]:
                shutil.rmtree(stale, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class _Candidate:
    entry: Path
    metadata: substrate.SubstrateMetadata
    tree: substrate.TreeRecord
    # How many files a refresh re-measures on top of this build: support counts through the test files executing it.
    work: int


def _candidate(
    entry: Path,
    expected: substrate.SubstrateMetadata,
    current: substrate.TreeRecord,
    in_diff: frozenset[str],
    layout: Layout,
) -> _Candidate | None:
    """A pool build whose divergence from the tree a refresh can handle, by the same rule as the tree's own build."""
    coverage = entry / _ENTRY_COVERAGE
    metadata = substrate.substrate_inspect(coverage)
    if metadata is None or not substrate.metadata_compatible(metadata, expected):
        return None
    tree = substrate.tree_read(coverage, metadata.generation)
    if tree is None:
        return None
    drift = substrate.drift_between(tree, current, in_diff=in_diff, layout=layout)
    if drift is not None and not drift.refreshable(len(current.hashes)):
        return None
    return _Candidate(entry, metadata, tree, len(drift.refresh_work()) if drift is not None else 0)


def pool_seed(
    pool: Path,
    coverage_path: Path,
    expected: substrate.SubstrateMetadata,
    current: substrate.TreeRecord,
    *,
    in_diff: frozenset[str] = frozenset(),
    layout: Layout,
) -> substrate.SubstrateMetadata | None:
    """Place the compatible pool build closest to ``current`` at ``coverage_path``: the one a refresh has the least
    work on top of. None when no build is compatible or every one diverged beyond a refresh.

    Selection and copy happen under the pool lock, the placement under the tree's own lock: waiting for a neighbour
    of the same tree while holding the pool would stall every other tree."""
    if not pool.is_dir():
        return None
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=coverage_path.parent, prefix=f".{coverage_path.name}.seed.") as raw:
        staged = Path(raw) / _ENTRY_COVERAGE
        with _pool_lock(pool):
            candidates = [
                candidate
                for entry in _entries(pool)
                if (candidate := _candidate(entry, expected, current, in_diff, layout)) is not None
            ]
            best = min(candidates, key=lambda candidate: candidate.work, default=None)
            if best is None:
                return None
            _copy_substrate(best.entry / _ENTRY_COVERAGE, staged, best.metadata.generation, best.tree)
            _touch(best.entry)
        with substrate.publication_lock(coverage_path):
            _copy_substrate(staged, coverage_path, best.metadata.generation, best.tree)
        return best.metadata
