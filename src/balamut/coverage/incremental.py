"""Incremental mode: changed files -> coverage refresh -> the files to mutate.

The diff is only the scope (which files to touch). Staleness is decided by content hashes (see ``core.hashing``
and the store): a mutant's cached verdict lives on its key plus the live covering hash, and this module only keeps
the coverage of the touched files fresh (the source and test tiers of ``refresh``). All paths are relative to the
project root.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

from balamut.config import Layout
from balamut.core.hashing import file_hash, sha
from balamut.coverage import costs as pytest_costs
from balamut.coverage import substrate, support_deps
from balamut.coverage.coverage_map import load_coverage_map
from balamut.coverage.discovery import ReexportMap, reexport_map
from balamut.coverage.refresh import (
    MeasureContext,
    RefreshMeasurement,
    SrcCoverageSnapshot,
    measure_diff_refresh,
    restoration_rows,
    snapshot_src_coverage,
)
from balamut.coverage.store import Store, StrMap
from balamut.diff.scope import DiffScope
from balamut.gitutil import git_output

type PhaseTimings = MutableMapping[str, float]
# scope file -> its text (as Path.read_text reads it) when the content still matches the pinned hash
type VouchedTexts = dict[str, str]
# path -> (content hash, kind): what a refresh records in the store about a re-measured file
type MeasuredHashes = dict[str, tuple[str, str]]

_VANISHED = "vanished"  # the hash of a removed file: its tests are already re-measured, no need to repeat


def _git_changed(base: str, root: Path) -> list[str]:
    """Files changed against ``base`` (committed and uncommitted) plus untracked new ones, relative to ``root``."""
    tracked = git_output("diff", "--name-only", "--relative", base, cwd=root).splitlines()
    untracked = git_output("ls-files", "--others", "--exclude-standard", cwd=root).splitlines()
    return [path for path in {*tracked, *untracked} if path]


def _phase_add(phase_timings: PhaseTimings | None, phase: str, started: float) -> None:
    if phase_timings is not None:
        phase_timings[phase] = phase_timings.get(phase, 0.0) + (time.perf_counter() - started)


def _coverage_snapshot(cov_path: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        dir=cov_path.parent,
        prefix=f".{cov_path.name}.",
        suffix=".ingest.tmp",
        delete=False,
    ) as target:
        with cov_path.open("rb") as source:
            shutil.copyfileobj(source, target)
        return Path(target.name)


def ingest_baseline(
    cov_path: Path,
    store: Store,
    *,
    metadata: substrate.SubstrateMetadata | None = None,
    root: Path | None = None,
) -> None:
    """Load a whole coverage file into the store (once): the base of ``StoreCoverage``.

    With ``metadata`` the file is copied first and checked against the published fingerprint, so a concurrent
    substrate replacement cannot hand the store a coverage file of another generation."""
    snapshot = _coverage_snapshot(cov_path) if metadata is not None else None
    load_path = snapshot or cov_path
    try:
        if metadata is not None and substrate.file_sha256(load_path) != metadata.coverage_sha256:
            raise substrate.SubstrateCompatibilityError("the coverage substrate changed before ingest; run again")
        cov = load_coverage_map(load_path, root=root)
        rows: list[tuple[str, str, int]] = [
            (nodeid, relpath, line)
            for relpath in cov.files()
            for line in cov.covered_lines(relpath)
            for nodeid in cov.tests_covering(relpath, line)
        ]
        if metadata is None:
            store.ingest_coverage(rows)
        else:
            store.coverage_replace(rows, generation=metadata.generation, substrate_fingerprint=metadata.coverage_sha256)
        sys.stderr.write(f"baseline coverage -> store: {len(rows)} (test, line) rows\n")
    finally:
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)


def _vouched_texts(scope: DiffScope, files: list[str], root: Path) -> VouchedTexts:
    """Text of the scope files whose content did not change since the scope was pinned.

    A substrate built after the scope was pinned has measured these files. The bytes are read once: the check
    against the pinned hash and the hash for the store come from the same content, with no window between them."""
    vouched: VouchedTexts = {}
    for file in files:
        pinned = scope.file_hashes.get(file)
        if pinned is None:
            continue
        try:
            raw = (root / file).read_bytes()
        except FileNotFoundError:
            continue
        if hashlib.sha256(raw).hexdigest() != pinned:
            continue
        # Path.read_text turns \r\n and \r into \n, and file_hash hashes exactly that text.
        vouched[file] = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return vouched


def _seed_scope_freshness(
    store: Store,
    scope: DiffScope,
    root: Path,
    *,
    changed_src: list[str],
    changed_tests: list[str],
    changed_support: list[str],
) -> None:
    """Mark the scope files the generation just ingested has already measured as refreshed.

    Ingesting a new generation erases the hashes of the previous one; without this mark a refresh would re-measure
    the whole diff on the very tree the substrate was just measured from."""
    vouched = {
        "src": _vouched_texts(scope, changed_src, root),
        "test": _vouched_texts(scope, changed_tests, root),
        "support": _vouched_texts(scope, changed_support, root),
    }
    if not any(vouched.values()):
        return
    store.apply_test_coverage_refresh(
        measured={},
        measured_nodeids=(),
        replace_test_files=(),
        restored_rows=[],
        file_hashes={file: (sha(text), kind) for kind, texts in vouched.items() for file, text in texts.items()},
    )
    sys.stderr.write(
        f"coverage refresh: this run's substrate already measured {len(vouched['src'])} source, "
        f"{len(vouched['test'])} test and {len(vouched['support'])} support files of the scope; not re-measured\n"
    )


@dataclass(frozen=True, slots=True)
class _OutsideDrift:
    """Files outside the scope whose content diverged from what the store's coverage describes."""

    src: list[str] = field(default_factory=list[str])  # the tests that covered them are re-measured
    vanished_src: list[str] = field(default_factory=list[str])  # removed: their tests now cover the code's new place
    # collectable files; a defining file is replaced by its wrapper, test support by the test files executing it
    tests: list[str] = field(default_factory=list[str])
    deleted_tests: list[str] = field(default_factory=list[str])  # their tests leave the store
    hashes: MeasuredHashes = field(default_factory=dict[str, tuple[str, str]])


def _content_hash(root: Path, path: str) -> str:
    return file_hash(root / path) if (root / path).is_file() else _VANISHED


def _wrappers(definition: str, remap: ReexportMap, recorded: ReexportMap, root: Path) -> list[str]:
    """Wrappers whose node ids collect a defining file's tests: the current one and the one at build time.

    A defining file that was deleted or lost its last test is unknown to the current re-export map, while the build
    wrapper's node ids are still in the coverage: it is re-measured too. A missing wrapper is a removed test file."""
    candidates = {wrapper for wrapper in (remap.get(definition), recorded.get(definition)) if wrapper is not None}
    return sorted(wrapper for wrapper in candidates if (root / wrapper).is_file())


def _outside_drift(
    recorded: substrate.TreeRecord,
    current: substrate.TreeRecord,
    *,
    layout: Layout,
    scope_files: frozenset[str],
    measured: StrMap,
    remap: ReexportMap,
) -> _OutsideDrift:
    """Divergence outside the scope: from the build tree, and for files refreshed before, from the refreshed version.

    The store remembers the version a refresh measured, not the build version: a file reverted to the build tree or
    gone from the scope after its refresh diverged from the coverage too. A new source file cannot be refreshed,
    in the scope or not (coverage does not know the tests reaching it through a registry or autodiscovery): the
    orchestrator rebuilds such a substrate and a direct call refuses. Diverged test support is re-measured through
    the test files executing it: the substrate holds no coverage of its own for it."""
    new_src = substrate.unknown_src(recorded, current, layout)
    if new_src:
        raise substrate.SubstrateCompatibilityError(
            f"source files the build tree does not know: {', '.join(new_src)}; the substrate needs a rebuild"
        )
    root = layout.root
    drift = _OutsideDrift()
    reexported = {*remap, *recorded.reexports}
    support: list[str] = []
    for path in sorted((set(recorded.hashes) | set(current.hashes) | set(measured)) - scope_files):
        stored = measured.get(path)
        if stored is None and recorded.hashes.get(path) == current.hashes.get(path):
            continue
        now = _content_hash(root, path)
        if now == stored:
            continue
        exists = now != _VANISHED
        if layout.is_source_path(path):
            (drift.src if exists else drift.vanished_src).append(path)
            drift.hashes[path] = (now, "src")
        elif layout.is_test_file(Path(path).name):
            (drift.tests if exists else drift.deleted_tests).append(path)
            drift.hashes[path] = (now, "test")
        elif wrappers := _wrappers(path, remap, recorded.reexports, root):
            drift.tests.extend(wrappers)
            drift.hashes[path] = (now, "test")
        elif substrate.is_support(path, reexported, layout):
            support.append(path)
            drift.hashes[path] = (now, "support")
    if support:
        drift.tests.extend(support_deps.support_dependents(layout, support))
    return drift


def _ingest_generation(
    cov_path: Path,
    store: Store,
    *,
    require_metadata: bool,
    root: Path,
    phase_timings: PhaseTimings | None,
) -> tuple[substrate.SubstrateMetadata | None, str | None]:
    """Bring the substrate into the store when its generation is not there yet -> (metadata, ingested generation)."""
    metadata = substrate.substrate_inspect(cov_path)
    has_sidecar = substrate.metadata_path(cov_path).exists()
    if metadata is None and (require_metadata or has_sidecar):
        raise substrate.SubstrateCompatibilityError("the coverage substrate has no valid metadata")
    started = time.perf_counter()
    if metadata is None:
        if not store.has_coverage():
            ingest_baseline(cov_path, store, root=root)
            _phase_add(phase_timings, "baseline_ingest", started)
        return None, None
    if store.coverage_generation() == metadata.generation and store.has_coverage():
        return metadata, None
    ingest_baseline(cov_path, store, metadata=metadata, root=root)
    # In diff mode the cost store is the only source of durations: the sidecar of a generation seeds it once, then
    # refresh and live calibration keep it fresh (the sidecar is never laid over the store again).
    sidecar_costs = pytest_costs.read_costs(substrate.costs_path(cov_path))
    if sidecar_costs:
        store.put_costs(sidecar_costs)
    _phase_add(phase_timings, "baseline_ingest", started)
    return metadata, metadata.generation


def _publish_refresh(
    src_snapshot: SrcCoverageSnapshot,
    *,
    refresh_src: list[str],
    refresh_tests: list[str],
    deleted_tests: list[str],
    file_hashes: MeasuredHashes,
    store: Store,
    context: MeasureContext,
    phase_timings: PhaseTimings | None,
) -> RefreshMeasurement:
    """Measure both tiers, then publish the coverage and the files' new hashes in one store transaction."""
    started = time.perf_counter()
    measurement = (
        measure_diff_refresh(refresh_src, refresh_tests, store=store, context=context, deleted_tests=deleted_tests)
        if refresh_src or refresh_tests
        else RefreshMeasurement(src_nodeids=(), replaced_nodeids=(), replace_test_files=(), measured={})
    )
    replaced_test_files = {*deleted_tests, *refresh_tests}
    restored_rows, restored = restoration_rows(src_snapshot, measurement, excluded_test_files=replaced_test_files)
    if file_hashes or deleted_tests:
        store.apply_test_coverage_refresh(
            measured=measurement.measured,
            measured_nodeids=measurement.replaced_nodeids,
            replace_test_files=replaced_test_files,
            restored_rows=restored_rows,
            file_hashes=file_hashes,
        )
    if measurement.costs:
        store.put_costs(measurement.costs)
    _phase_add(phase_timings, "coverage_refresh", started)
    for file in restored:
        sys.stderr.write(
            f"coverage refresh: {file} would have been left without coverage by an isolated re-measure; "
            "its baseline is restored (a module imported before the tracer started is under-measured)\n"
        )
    return measurement


def refresh_tree(
    *, cov_path: Path, store: Store, context: MeasureContext, phase_timings: PhaseTimings | None = None
) -> int:
    """Bring the store's coverage up to the current tree for a direct campaign -> the number of diverged files.

    The refresh outside a diff's scope with an empty scope: a source file diverged from the build tree (or from the
    version a refresh last measured) re-measures the tests that covered it, and a changed, new or removed test file is
    re-measured whole. A substrate without a tree record or with a source file the build never saw cannot be
    refreshed (``SubstrateCompatibilityError``): those need a rebuild."""
    layout = context.profile.layout
    metadata, _ = _ingest_generation(
        cov_path, store, require_metadata=True, root=layout.root, phase_timings=phase_timings
    )
    recorded = substrate.tree_read(cov_path, metadata.generation) if metadata is not None else None
    if recorded is None:
        raise substrate.SubstrateCompatibilityError("the coverage substrate has no tree record to refresh against")
    started = time.perf_counter()
    measured = {**store.get_file_hashes("src"), **store.get_file_hashes("test"), **store.get_file_hashes("support")}
    drift = _outside_drift(
        recorded,
        substrate.tree_snapshot(layout),
        layout=layout,
        scope_files=frozenset(),
        measured=measured,
        remap=reexport_map(layout),
    )
    src_snapshot = snapshot_src_coverage(drift.src, store=store)
    _phase_add(phase_timings, "staleness_selection", started)
    if not drift.hashes:
        return 0
    measurement = _publish_refresh(
        src_snapshot,
        refresh_src=[*drift.src, *drift.vanished_src],
        refresh_tests=list(dict.fromkeys(drift.tests)),
        deleted_tests=drift.deleted_tests,
        file_hashes=drift.hashes,
        store=store,
        context=context,
        phase_timings=phase_timings,
    )
    sys.stderr.write(
        f"coverage refresh: {len(drift.hashes)} files diverged from the substrate,"
        f" {len(measurement.measured)} tests re-measured\n"
    )
    return len(drift.hashes)


def prepare_diff(
    base: str,
    *,
    cov_path: Path,
    store: Store,
    context: MeasureContext,
    scope: DiffScope | None = None,
    include_tests: bool = False,
    phase_timings: PhaseTimings | None = None,
    fresh_generation: str | None = None,
) -> list[str]:
    """Refresh the coverage and return the changed files to mutate.

    ``fresh_generation`` is the generation of a substrate this run built after pinning ``scope``. When this very call
    ingests it into the store, the scope files unchanged since pinning count as refreshed."""
    layout = context.profile.layout
    root = layout.root
    changed = sorted(scope.file_hashes) if scope is not None else _git_changed(base, root)
    metadata, ingested_generation = _ingest_generation(
        cov_path, store, require_metadata=scope is not None, root=root, phase_timings=phase_timings
    )

    selection_started = time.perf_counter()
    remap = reexport_map(layout)
    # The build tree: without its record a divergence outside the scope is unprovable (the orchestrator rebuilds).
    recorded = substrate.tree_read(cov_path, metadata.generation) if metadata is not None else None
    recorded_reexports = recorded.reexports if recorded is not None else {}

    def exists(path: str) -> bool:
        return (root / path).exists()

    changed_src = [f for f in changed if layout.is_source_path(f) and exists(f)]
    changed_mutation_tests = [f for f in changed if layout.is_test_path(f) and f.endswith(".py") and exists(f)]
    # Collectable test files are taken directly. A changed defining file of re-exported tests (its name does not
    # match, but pytest collects its tests through a wrapper) refreshes the wrapper: a refresh of the defining file
    # itself would collect nothing. Other non-collectable files of a test tree (support, conftest, helpers) hold no
    # tests and go to the support tier below.
    changed_test_paths: list[str] = []
    for f in changed:
        if not (layout.is_test_path(f) and f.endswith(".py")):
            continue
        if layout.is_test_file(Path(f).name):
            changed_test_paths.append(f)
        else:
            changed_test_paths.extend(_wrappers(f, remap, recorded_reexports, root))
    changed_tests = [f for f in dict.fromkeys(changed_test_paths) if exists(f)]
    # Removed or renamed test files are not measured, but their node ids are dropped in the same transaction that
    # publishes the rest of the refresh.
    deleted_tests = [f for f in changed_test_paths if not exists(f)]
    # Test support of the scope (removed too): the substrate holds no coverage of it, so the test files executing it
    # are re-measured. A defining file is not support: its wrapper is re-measured by the test tier.
    changed_support = [f for f in changed if substrate.is_support(f, {*remap, *recorded_reexports}, layout)]
    if scope is not None and fresh_generation is not None and fresh_generation == ingested_generation:
        _seed_scope_freshness(
            store,
            scope,
            root,
            changed_src=changed_src,
            changed_tests=changed_tests,
            changed_support=changed_support,
        )

    # Hash skip: a file whose content did not change since its last refresh keeps valid coverage in the store, so a
    # no-op rerun and an edit loop over one file do not pay for a full refresh. Coverage of an unchanged file stays
    # fresh without its refresh: a changed dependency is in the diff itself and its source tier re-measures the
    # shared covering tests. Test support uses the same skip: the store remembers the version already re-measured.
    src_hashes = store.get_file_hashes("src")
    test_hashes = store.get_file_hashes("test")
    support_hashes = store.get_file_hashes("support")
    src_now = {file: file_hash(root / file) for file in changed_src}
    test_now = {file: file_hash(root / file) for file in changed_tests}
    support_now = {file: _content_hash(root, file) for file in changed_support}
    stale_src = [file for file in changed_src if src_now[file] != src_hashes.get(file)]
    stale_tests = [file for file in changed_tests if test_now[file] != test_hashes.get(file)]
    stale_support = [file for file in changed_support if support_now[file] != support_hashes.get(file)]
    support_tests = list(support_deps.support_dependents(layout, stale_support)) if stale_support else []

    # Outside the scope a refresh keeps the coverage correct the same way: after a rebase or a seed from the pool,
    # files diverged from the build tree are re-measured instead of rebuilding the substrate. Only the scope mutates.
    drift = (
        _outside_drift(
            recorded,
            substrate.tree_snapshot(layout),
            layout=layout,
            scope_files=frozenset(changed),
            measured={**src_hashes, **test_hashes, **support_hashes},
            remap=remap,
        )
        if recorded is not None
        else _OutsideDrift()
    )

    # Conservative refresh: snapshot the baseline coverage of the source files before the refresh. An isolated
    # re-measure under-measures a module imported before the tracer started (through conftest and the application
    # bootstrap) down to zero lines; overwriting the authoritative substrate coverage with that would produce a false
    # no-coverage. Both the scope's source files and the drifted ones outside it are covered: either tier may wipe
    # them, and a drifted file's hash goes to the store, so without restoration its next edit would find no baseline.
    src_snapshot = snapshot_src_coverage([*changed_src, *drift.src], store=store)
    _phase_add(phase_timings, "staleness_selection", selection_started)
    measurement = _publish_refresh(
        src_snapshot,
        refresh_src=[*stale_src, *drift.src, *drift.vanished_src],
        refresh_tests=list(dict.fromkeys([*stale_tests, *support_tests, *drift.tests])),
        deleted_tests=[*deleted_tests, *drift.deleted_tests],
        file_hashes={
            **drift.hashes,
            **{file: (src_now[file], "src") for file in stale_src},
            **{file: (test_now[file], "test") for file in stale_tests},
            **{file: (support_now[file], "support") for file in stale_support},
        },
        store=store,
        context=context,
        phase_timings=phase_timings,
    )

    sys.stderr.write(
        f"diff vs {base}: source {len(changed_src)} (refresh {len(stale_src)}, "
        f"hash skip {len(changed_src) - len(stale_src)}),"
        f" test {len(changed_tests)} (refresh {len(stale_tests)}, hash skip {len(changed_tests) - len(stale_tests)}),"
        f" support {len(changed_support)} (refresh {len(stale_support)} -> test files {len(support_tests)}),"
        f" diverged outside the diff: source {len(drift.src) + len(drift.vanished_src)},"
        f" test {len(drift.tests) + len(drift.deleted_tests)}"
        f" (refreshed {len(measurement.src_nodeids)} source-tier tests,"
        f" {len(measurement.measured)} measured tests)\n"
    )
    return [*changed_src, *changed_mutation_tests] if include_tests else changed_src
