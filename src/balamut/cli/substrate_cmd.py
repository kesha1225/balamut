"""``balamut substrate``: make the coverage substrate describe the current tree, without git.

``balamut diff`` maintains the substrate itself and refreshes small divergences. A direct campaign
(``balamut mutate PATHS``) does the same through ``ensure_substrate``: it rebuilds only what a refresh cannot repair
and refreshes the rest into its store. This command makes the substrate itself describe exactly the current tree: it
reuses it only when it was measured the same way and on this very tree, and builds a new generation otherwise.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from balamut.cli.diff import Measurer, expected_metadata, substrate_miss_reason
from balamut.config import Profile, ProfileError, load_profile
from balamut.coverage import substrate
from balamut.coverage.measure import MeasureRequest, measure_coverage
from balamut.isolation import Isolation, isolation_for
from balamut.paths import ensure_state_dir, substrate_path

_EXIT_BASELINE: Final = 2


@dataclass(frozen=True, slots=True)
class Ensured:
    """The substrate a caller reads: its generation and, when this call built it, why."""

    metadata: substrate.SubstrateMetadata
    built_reason: str | None  # None: the substrate was reused


class SubstrateBuildFailedError(RuntimeError):
    """The build exited non-zero and published nothing; ``nodeids`` names the failing tests (empty: infrastructure)."""

    def __init__(self, message: str, nodeids: tuple[str, ...]) -> None:
        super().__init__(message)
        self.nodeids = nodeids


def ensure_substrate(
    profile: Profile,
    isolation: Isolation,
    path: Path,
    *,
    measure: Measurer,
    rebuild: bool = False,
    refresh_drift: bool = False,
) -> Ensured:
    """Reuse the substrate at ``path`` when it was measured the same way on this tree, or build a new generation.

    With ``refresh_drift`` a divergence a refresh can repair keeps the generation: the caller refreshes it into its
    store (``incremental.refresh_tree``) instead of measuring the whole suite again. A source file the build never saw,
    a missing tree record or a large divergence are rebuilt either way."""
    layout = profile.layout
    expected = expected_metadata(profile, isolation)
    previous = substrate.substrate_inspect(path)
    reason = substrate_miss_reason(path, previous, expected, force_rebuild=rebuild)
    current = substrate.tree_snapshot(layout)
    if previous is not None and reason is None:
        drift = substrate.tree_drift(path, previous.generation, current, in_diff=frozenset(), layout=layout)
        if drift is not None and not (refresh_drift and drift.refreshable(len(current.hashes))):
            reason = drift.reason
    if previous is not None and reason is None:
        return Ensured(previous, None)
    failed: list[str] = []

    def build(target: Path) -> int:
        result = measure(MeasureRequest(profile=profile, coverage_file=target))
        failed[:] = result.failed_nodeids
        if result.detail:
            sys.stderr.write(f"balamut substrate: {result.detail}\n")
        return result.returncode

    sys.stderr.write(f"balamut substrate: measuring the suite under coverage (reason: {reason})\n")
    try:
        published = substrate.substrate_ensure(path, expected, build=build, force_rebuild=True, tree=lambda: current)
    except substrate.SubstrateBuildError as exc:
        raise SubstrateBuildFailedError(str(exc), tuple(failed)) from exc
    return Ensured(published, reason)


def main(argv: list[str], *, root: Path | None = None, measure: Measurer | None = None) -> int:
    try:
        profile = load_profile(root or Path.cwd())
    except ProfileError as exc:
        sys.stderr.write(f"error: invalid [tool.balamut]: {exc}\n")
        return 2
    parser = argparse.ArgumentParser(prog="balamut substrate", description="Build or reuse the coverage substrate.")
    parser.add_argument("--coverage", type=Path, default=substrate_path(profile), help="where the substrate lives")
    parser.add_argument("--rebuild", action="store_true", help="build a new generation even if one fits")
    args = parser.parse_args(argv)
    isolation = isolation_for(profile)
    run: Measurer = measure or partial(measure_coverage, isolation=isolation)
    ensure_state_dir(profile)
    path: Path = args.coverage
    started = time.monotonic()
    try:
        ensured = ensure_substrate(profile, isolation, path, measure=run, rebuild=args.rebuild)
    except SubstrateBuildFailedError as exc:
        sys.stderr.write(f"error: {exc}\n")
        for nodeid in exc.nodeids:
            sys.stderr.write(f"BASELINE {nodeid}\n")
        return _EXIT_BASELINE
    generation = ensured.metadata.generation[:12]
    if ensured.built_reason is None:
        sys.stdout.write(f"substrate: reused generation {generation} at {path}\n")
        return 0
    elapsed = time.monotonic() - started
    sys.stdout.write(
        f"substrate: built generation {generation} at {path} in {elapsed:.1f}s (reason: {ensured.built_reason})\n"
    )
    return 0
