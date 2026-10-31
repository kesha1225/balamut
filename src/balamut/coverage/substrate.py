"""Atomic publication of the coverage substrate and its compatibility checks.

A substrate is a consistent triple: ``<coverage>``, ``<coverage>.costs.json`` and ``<coverage>.metadata.json``,
plus ``<coverage>.tree.json``, the hashes of the tree it was measured from. Reuse requires valid checksums and the
same way of measuring (schema version, configuration, runtime and database schema fingerprints). The test
inventory is held by the tree record instead: a new test file or a helper edit is re-measured by a refresh, not
by a rebuild.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Callable, Collection, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from coverage import CoverageData
from coverage.exceptions import CoverageException

from balamut.config import Layout
from balamut.coverage.costs import costs_valid, write_costs
from balamut.coverage.discovery import ReexportMap, reexport_map
from balamut.coverage.support_deps import support_dependents

type SubstrateBuilder = Callable[[Path], int]
type TreeSnapshotter = Callable[[], TreeRecord]  # takes the tree the build is about to measure
# Test files a red build did not measure completely; empty means there is nothing to publish.
type UnmeasuredFiles = Callable[[], frozenset[str]]
type TreeHashes = dict[str, str]  # path relative to the root -> SHA-256 of its content
type _StringMap = dict[str, str]
type MetadataJsonValue = str | int | float | bool | list[MetadataJsonValue] | dict[str, MetadataJsonValue] | None

SUBSTRATE_SCHEMA_VERSION = 1
_METADATA_SUFFIX = ".metadata.json"
_TREE_SUFFIX = ".tree.json"
_TREE_SCHEMA_VERSION = 1
# The hash of a test file in the tree record of a red build: it matches no content, so the file looks diverged and
# the next run's refresh re-measures it whole.
UNMEASURED_FILE_HASH = "unmeasured"
_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "complete",
        "generation",
        "config_fingerprint",
        "test_inventory_fingerprint",
        "runtime_fingerprint",
        "schema_fingerprint",
        "coverage_sha256",
        "costs_sha256",
    }
)


class SubstrateError(RuntimeError):
    """Base error of the substrate lifecycle."""


class SubstrateBuildError(SubstrateError):
    """Building the substrate failed; only ``SubstrateBaselineRedError`` carries a publication."""


class SubstrateBaselineRedError(SubstrateBuildError):
    """The build went red on some tests, but the coverage of the rest was published.

    The files of the failed tests are recorded as unmeasured in the tree: the next run's refresh re-measures them
    rather than the whole suite. The verdict of this run is still a baseline failure."""

    def __init__(self, message: str, *, published: SubstrateMetadata) -> None:
        super().__init__(message)
        self.published = published


class SubstrateCompatibilityError(SubstrateError):
    """The substrate does not match the current run."""


@dataclass(frozen=True, slots=True)
class SubstrateMetadata:
    """Metadata of one fully published substrate generation."""

    schema_version: int
    complete: bool
    generation: str
    config_fingerprint: str
    test_inventory_fingerprint: str
    runtime_fingerprint: str
    schema_fingerprint: str
    coverage_sha256: str
    costs_sha256: str = ""


def metadata_path(coverage_path: Path) -> Path:
    return coverage_path.with_name(coverage_path.name + _METADATA_SUFFIX)


def costs_path(coverage_path: Path) -> Path:
    return coverage_path.with_name(coverage_path.name + ".costs.json")


def tree_path(coverage_path: Path) -> Path:
    return coverage_path.with_name(coverage_path.name + _TREE_SUFFIX)


def file_sha256(path: Path) -> str:
    """SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_decode(raw: MetadataJsonValue) -> SubstrateMetadata | None:
    if not isinstance(raw, dict) or frozenset(raw) != _METADATA_KEYS:
        return None
    schema_version = raw["schema_version"]
    complete = raw["complete"]
    if type(schema_version) is not int or type(complete) is not bool:
        return None
    strings = {key: raw[key] for key in _METADATA_KEYS - {"schema_version", "complete"}}
    if not all(isinstance(value, str) for value in strings.values()):
        return None
    return SubstrateMetadata(
        schema_version=schema_version,
        complete=complete,
        generation=str(strings["generation"]),
        config_fingerprint=str(strings["config_fingerprint"]),
        test_inventory_fingerprint=str(strings["test_inventory_fingerprint"]),
        runtime_fingerprint=str(strings["runtime_fingerprint"]),
        schema_fingerprint=str(strings["schema_fingerprint"]),
        coverage_sha256=str(strings["coverage_sha256"]),
        costs_sha256=str(strings["costs_sha256"]),
    )


def substrate_inspect(coverage_path: Path) -> SubstrateMetadata | None:
    """Metadata of a complete substrate whose checksums match; None otherwise."""
    try:
        raw: MetadataJsonValue = json.loads(metadata_path(coverage_path).read_text(encoding="utf-8"))
        metadata = _metadata_decode(raw)
        intact = (
            metadata is not None
            and metadata.complete
            and bool(metadata.generation)
            and bool(metadata.coverage_sha256)
            and file_sha256(coverage_path) == metadata.coverage_sha256
            and bool(metadata.costs_sha256)
            and file_sha256(costs_path(coverage_path)) == metadata.costs_sha256
        )
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    return metadata if intact else None


def metadata_compatible(actual: SubstrateMetadata, expected: SubstrateMetadata) -> bool:
    """Was the substrate measured the way this run would measure it? The test inventory is not compared: the tree
    record holds it."""
    return not metadata_mismatches(actual, expected)


def metadata_mismatches(actual: SubstrateMetadata, expected: SubstrateMetadata) -> list[str]:
    pairs = (
        ("schema_version", actual.schema_version == expected.schema_version),
        ("config_fingerprint", actual.config_fingerprint == expected.config_fingerprint),
        ("runtime_fingerprint", actual.runtime_fingerprint == expected.runtime_fingerprint),
        ("schema_fingerprint", actual.schema_fingerprint == expected.schema_fingerprint),
    )
    return [name for name, same in pairs if not same]


def _coverage_validate(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise SubstrateBuildError("the build did not produce a non-empty coverage file")
    data = CoverageData(basename=str(path))
    try:
        data.read()
    except CoverageException as exc:
        raise SubstrateBuildError("the build produced an unreadable coverage file") from exc
    if not data.measured_files():
        raise SubstrateBuildError("the coverage file has no measured files")


def _file_fsync(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _directory_fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _temporary_path(final_path: Path, suffix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(dir=final_path.parent, prefix=f".{final_path.name}.", suffix=suffix)
    os.close(descriptor)
    return Path(raw_path)


@contextmanager
def publication_lock(coverage_path: Path) -> Generator[None]:
    """The lock of one substrate: a build and the placement of a seed never overlap."""
    lock_path = coverage_path.with_name(coverage_path.name + ".lock")
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _metadata_write(path: Path, metadata: SubstrateMetadata) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(asdict(metadata), stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _publish(
    coverage_path: Path,
    expected: SubstrateMetadata,
    temporary_coverage: Path,
    temporary_costs: Path,
    tree: TreeRecord | None,
) -> SubstrateMetadata:
    """Validate the built coverage and cost sidecar and publish them atomically as a new generation."""
    _coverage_validate(temporary_coverage)
    if not temporary_costs.is_file():
        write_costs(temporary_costs, {})
    if not costs_valid(temporary_costs):
        raise SubstrateBuildError("the build produced an invalid cost sidecar")
    _file_fsync(temporary_coverage)
    _file_fsync(temporary_costs)
    published = replace(
        expected,
        complete=True,
        generation=uuid.uuid4().hex,
        coverage_sha256=file_sha256(temporary_coverage),
        costs_sha256=file_sha256(temporary_costs),
    )
    final_metadata = metadata_path(coverage_path)
    temporary_metadata = _temporary_path(final_metadata, ".metadata.tmp")
    try:
        _metadata_write(temporary_metadata, published)
        temporary_coverage.replace(coverage_path)
        temporary_costs.replace(costs_path(coverage_path))
        # The tree is part of the publication: under the same lock and before the metadata. Written later, it could
        # land over the tree of a generation a neighbouring run published afterwards.
        if tree is not None:
            tree_write(coverage_path, published.generation, tree)
        temporary_metadata.replace(final_metadata)
        _directory_fsync(coverage_path.parent)
    finally:
        temporary_metadata.unlink(missing_ok=True)
    return published


def _red_tree(measured: TreeRecord | None, red_files: frozenset[str]) -> TreeRecord | None:
    """The tree of a red build with the failed test files marked unmeasured; None when it must not be published.

    Only a divergence refresh will repair is published: without a tree record there is nothing to mark, a file
    outside the tree (a directory's collection, a conftest) cannot be refreshed, and a mass failure signals a broken
    environment whose coverage is worse than the previous generation."""
    if measured is None or not red_files or not red_files <= measured.hashes.keys():
        return None
    if not drift_refreshable(len(red_files), len(measured.hashes)):
        return None
    hashes = {path: UNMEASURED_FILE_HASH if path in red_files else content for path, content in measured.hashes.items()}
    return TreeRecord(hashes=hashes, reexports=measured.reexports)


def substrate_ensure(
    coverage_path: Path,
    expected: SubstrateMetadata,
    *,
    build: SubstrateBuilder,
    force_rebuild: bool = False,
    tree: TreeSnapshotter | None = None,
    unmeasured: UnmeasuredFiles | None = None,
) -> SubstrateMetadata:
    """Reuse a compatible substrate or atomically publish a new generation (with the tree ``tree`` when given).

    ``unmeasured`` names the test files a red build did not measure completely. When they fit a divergence refresh
    can repair, the build is published with those files as unmeasured and ``SubstrateBaselineRedError`` is raised;
    otherwise the failure publishes nothing and the previous generation stays."""
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    with publication_lock(coverage_path):
        current = substrate_inspect(coverage_path)
        if current is not None and metadata_compatible(current, expected) and not force_rebuild:
            return current
        temporary_coverage = _temporary_path(coverage_path, ".coverage.tmp")
        temporary_costs = costs_path(temporary_coverage)
        try:
            measured_tree = tree() if tree is not None else None  # taken before the build: it is what it measures
            return_code = build(temporary_coverage)
            if return_code == 0:
                return _publish(coverage_path, expected, temporary_coverage, temporary_costs, measured_tree)
            message = f"building the coverage substrate exited with {return_code}"
            red_tree = _red_tree(measured_tree, unmeasured()) if unmeasured is not None else None
            if red_tree is None or not temporary_coverage.is_file():
                raise SubstrateBuildError(message)
            published = _publish(coverage_path, expected, temporary_coverage, temporary_costs, red_tree)
            raise SubstrateBaselineRedError(message, published=published)
        finally:
            temporary_coverage.unlink(missing_ok=True)
            temporary_costs.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class TreeDrift:
    """Why the substrate cannot be reused as is: the reason for the report and the diverged files."""

    reason: str
    # Files outside the diff that diverged from the build tree: sources, tests and test support.
    paths: tuple[str, ...] = ()
    # Source files the build tree did not know (in the diff too): tests reaching them through a registry or
    # autodiscovery are not named by the build's coverage, and a refresh would not find them.
    new_src: tuple[str, ...] = ()
    # Test-support files that diverged, inside the diff or not: the substrate holds no coverage of them, so the test
    # files executing them (``support_tests``) are re-measured.
    support: tuple[str, ...] = ()
    support_tests: tuple[str, ...] = ()

    def refresh_work(self) -> frozenset[str]:
        """What a refresh re-measures instead of a rebuild: diverged sources and tests themselves, support through
        the test files importing it."""
        return frozenset({path for path in self.paths if path not in self.support} | set(self.support_tests))

    def refreshable(self, tree_size: int) -> bool:
        """Can a refresh repair this divergence instead of a rebuild?"""
        if self.reason == "tree_unknown" or self.new_src:
            return False
        return drift_refreshable(len(self.refresh_work()), tree_size)


@dataclass(frozen=True, slots=True)
class TreeRecord:
    """The tree a substrate measures: hashes of source and test files plus re-export links.

    The defining file -> wrapper link is kept after it disappears from the current map: a defining file that was
    deleted or lost its last test is gone from it, while the wrapper's node ids are still in the coverage."""

    hashes: TreeHashes
    reexports: ReexportMap


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def tree_snapshot(layout: Layout) -> TreeRecord:
    """Hashes of every Python file under the source and test roots, plus re-export links.

    The test roots are taken whole: collectable tests, defining files of re-exported tests and test support. A
    helper edit is then a divergence re-measured through its importers, not a reason to rebuild."""
    roots = dict.fromkeys(root for root in (*layout.source, *layout.tests) if root not in {".", ""})
    files = dict.fromkeys(path for root in roots for path in _python_files(layout.root / root))
    return TreeRecord(
        hashes={path.relative_to(layout.root).as_posix(): file_sha256(path) for path in files},
        reexports=reexport_map(layout),
    )


def tree_write(coverage_path: Path, generation: str, tree: TreeRecord) -> None:
    """Atomically write the tree ``generation`` was measured from."""
    path = tree_path(coverage_path)
    temporary = _temporary_path(path, ".tree.tmp")
    try:
        payload = {
            "schema_version": _TREE_SCHEMA_VERSION,
            "generation": generation,
            "files": dict(sorted(tree.hashes.items())),
            "reexports": dict(sorted(tree.reexports.items())),
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _string_map(raw: MetadataJsonValue) -> _StringMap | None:
    if not isinstance(raw, dict) or any(not isinstance(value, str) for value in raw.values()):
        return None
    return {key: value for key, value in raw.items() if isinstance(value, str)}


def tree_read(coverage_path: Path, generation: str) -> TreeRecord | None:
    """The tree ``generation`` was measured from; None when missing, damaged or of another generation."""
    try:
        raw: MetadataJsonValue = json.loads(tree_path(coverage_path).read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != _TREE_SCHEMA_VERSION:
        return None
    if raw.get("generation") != generation:
        return None
    hashes = _string_map(raw.get("files"))
    reexports = _string_map(raw.get("reexports"))
    if hashes is None or reexports is None:
        return None
    return TreeRecord(hashes=hashes, reexports=reexports)


def tree_drift(
    coverage_path: Path, generation: str, current: TreeRecord, *, in_diff: frozenset[str], layout: Layout
) -> TreeDrift | None:
    """Compare the current tree with the build tree of ``generation``; None when the substrate still describes it.

    Files of the diff are refreshed anyway. Everything else must match: after a rebase a test or source changed in
    the base would otherwise stay in the coverage map as it was at build time. A missing record proves nothing."""
    recorded = tree_read(coverage_path, generation)
    if recorded is None:
        return TreeDrift("tree_unknown")
    return drift_between(recorded, current, in_diff=in_diff, layout=layout)


def drift_between(
    recorded: TreeRecord, current: TreeRecord, *, in_diff: frozenset[str], layout: Layout
) -> TreeDrift | None:
    """The divergence of a build tree from the current one that a refresh of the diff alone would not repair.

    A source or test edit inside the diff is refreshed through the coverage history of who reaches it. A new source
    file has no such history. Test support has none at all (coverage is of the source tree), so its edits count the
    same inside and outside the diff: by the number of test files executing them."""
    diverged = tree_diverged(recorded, current)
    outside = tuple(sorted(diverged - in_diff))
    new_src = unknown_src(recorded, current, layout)
    reexported = {*recorded.reexports, *current.reexports}
    support = tuple(sorted(path for path in diverged if is_support(path, reexported, layout)))
    if not outside and not new_src and not support:
        return None
    if new_src:
        reason = "tree_new_src"
    elif any(path not in support for path in outside):
        reason = "tree_changed_outside_diff"
    else:
        reason = "tree_support_changed"
    support_tests = support_dependents(layout, support) if support else ()
    return TreeDrift(reason, outside, new_src, support=support, support_tests=support_tests)


def is_support(path: str, reexported: Collection[str], layout: Layout) -> bool:
    """Test support: a Python file of a test tree that is neither a collectable test nor the defining file of
    re-exported tests (that one is a test: its wrapper re-measures it)."""
    return (
        layout.is_test_path(path)
        and path.endswith(".py")
        and not layout.is_test_file(Path(path).name)
        and path not in reexported
    )


def unknown_src(recorded: TreeRecord, current: TreeRecord, layout: Layout) -> tuple[str, ...]:
    """Source files of the current tree the build tree did not know."""
    return tuple(sorted(path for path in current.hashes if layout.is_source_path(path) and path not in recorded.hashes))


def tree_diverged(recorded: TreeRecord, current: TreeRecord) -> set[str]:
    """Files missing from one tree or with different content in them."""
    return {
        path
        for path in set(recorded.hashes) | set(current.hashes)
        if recorded.hashes.get(path) != current.hashes.get(path)
    }


# A divergence refresh re-measures instead of a rebuild. A noticeable share of the tree is a reason for a fresh build:
# it is more authoritative than a refresh set and joins the shared pool. The floor keeps a small tree from being
# rebuilt over a couple of files.
_DRIFT_REFRESH_SHARE = 0.25
_DRIFT_REFRESH_FLOOR = 16


def drift_refreshable(diverged: int, tree_size: int) -> bool:
    return diverged <= max(_DRIFT_REFRESH_FLOOR, tree_size * _DRIFT_REFRESH_SHARE)
