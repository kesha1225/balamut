"""``balamut estimate``: how long a campaign over some paths will take, before running it.

The mutants are enumerated deterministically and every covered one is costed from the substrate: its covering
tests and their measured durations. The arithmetic follows the engine:

    wall ~ startup + sum over covered mutants of (overhead + tiered covering work) / daemons

A daemon runs one mutant at a time, so mutant parallelism is the daemon count. Covering tests run killer-first
by tier: the cheap tier (< 0.5 s) always, the expensive tier only while no cheap test killed (about half the time);
a cross-kill run pays both tiers. The per-mutant overhead (fork, patch, the worker's connections) is a constant
unless ``--probe`` measures it with a short real campaign. Expect about 10% accuracy.
"""

from __future__ import annotations

import argparse
import operator
import re
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from pytest_fast import resolve_workers

from balamut.config import Layout, ProfileError, load_profile
from balamut.core.mutants import Mutant
from balamut.coverage import substrate
from balamut.coverage.costs import CostMap, read_costs
from balamut.coverage.coverage_map import CoverageMap, load_coverage_map
from balamut.operators.src import enumerate_mutants
from balamut.paths import substrate_path

type ByFile = dict[str, list[tuple[str, ...]]]  # file -> covering sets of its covered mutants
type FileRow = tuple[float, str, int, float]  # (run seconds, file, mutants, average seconds per mutant)

_OVERHEAD_FALLBACK_S = 0.35
_EXPENSIVE_S = 0.5
_KILLER_FIRST_EXPENSIVE_SHARE = 0.5
_STARTUP_FALLBACK_S = 3.0
_REQUEST_RE = re.compile(r"request \(fork, patch, tests, bus\)\s+(\d+)ms")


def very_fast_floor(rows: list[FileRow]) -> float:
    """The very-fast bound: 1.5x the cheapest file's average (about the bare fork overhead)."""
    return min(row[3] for row in rows) * 1.5 if rows else 0.0


def select_files(rows: list[FileRow], *, top: int) -> list[FileRow]:
    """Files by cost per mutant, most expensive first. ``top > 0`` keeps exactly that many; otherwise every file
    above the very-fast bound is shown in full, plus two at the bound to show where the tail begins."""
    if not rows:
        return []
    ranked = sorted(rows, key=operator.itemgetter(3), reverse=True)
    if top > 0:
        return ranked[:top]
    floor = very_fast_floor(rows)
    keep = sum(1 for row in ranked if row[3] >= floor)
    return ranked[: keep + 2]


def work_of(
    covering: tuple[str, ...], costs: CostMap, median: float, workers: int, *, expensive_survive: float = 1.0
) -> float:
    """The covering work of a mutant by killer-first tier; each tier runs in parallel over min(workers, size)."""
    durations = [costs.get(test, median) for test in covering]
    cheap = [seconds for seconds in durations if seconds < _EXPENSIVE_S]
    expensive = [seconds for seconds in durations if seconds >= _EXPENSIVE_S]
    cheap_work = sum(cheap) / (min(workers, len(cheap)) or 1) if cheap else 0.0
    expensive_work = sum(expensive) / (min(workers, len(expensive)) or 1) if expensive else 0.0
    return cheap_work + expensive_survive * expensive_work


def estimate_run(
    by_file: ByFile,
    costs: CostMap,
    median: float,
    overhead: float,
    *,
    daemons: int,
    workers: int,
    expensive_survive: float = 1.0,
) -> tuple[float, list[FileRow]]:
    """-> (run wall, per-file rows): each file contributes the sum of its per-mutant cost over the daemons."""
    rows: list[FileRow] = []
    for file, coverings in by_file.items():
        file_cost = sum(
            overhead + work_of(c, costs, median, workers, expensive_survive=expensive_survive) for c in coverings
        )
        rows.append((file_cost / daemons, file, len(coverings), file_cost / len(coverings)))
    rows.sort(reverse=True)
    return sum(row[0] for row in rows), rows


def _walk_mutants(paths: list[str], layout: Layout) -> Iterator[Mutant]:
    for raw in paths:
        candidate = layout.root / raw
        files = [candidate] if candidate.is_file() else sorted(candidate.rglob("*.py"))
        for file in files:
            rel = file.relative_to(layout.root).as_posix()
            if layout.is_source_path(rel):
                yield from enumerate_mutants(rel, file.read_text(encoding="utf-8"))


def _covered(paths: list[str], layout: Layout, cov: CoverageMap) -> tuple[int, ByFile, set[str]]:
    total = 0
    by_file: ByFile = {}
    union: set[str] = set()
    for mutant in _walk_mutants(paths, layout):
        total += 1
        tests = cov.tests_covering(mutant.path, mutant.lineno)
        if tests:
            by_file.setdefault(mutant.path, []).append(tuple(sorted(tests)))
            union |= tests
    return total, by_file, union


def _probe_overhead(
    file: str, by_file: ByFile, costs: CostMap, median: float, args: argparse.Namespace
) -> float | None:
    """Measure the per-mutant overhead with a short real campaign on the cheapest file: request minus work."""
    cache = Path(tempfile.gettempdir()) / f"balamut-estimate-{abs(hash(file))}.sqlite"
    cache.unlink(missing_ok=True)
    argv = [
        sys.executable,
        "-m",
        "balamut",
        "mutate",
        file,
        "--kind",
        "src",
        "--coverage",
        str(args.coverage),
        "--limit",
        str(args.probe),
        "--no-cache",
        "--cache",
        str(cache),
        "--daemons",
        str(args.daemons or resolve_workers()),
        "--workers",
        str(args.workers),
        "--progress",
        "none",
    ]
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
    cache.unlink(missing_ok=True)
    match = _REQUEST_RE.search(completed.stderr)
    if match is None:
        return None
    sample = [work_of(c, costs, median, args.workers) for c in by_file[file][: args.probe]]
    overhead = max(0.0, int(match.group(1)) / 1000 - sum(sample) / len(sample))
    sys.stderr.write(f"probe: {file}: overhead {overhead * 1000:.0f}ms per mutant\n")
    return overhead


def main(argv: list[str]) -> int:
    try:
        profile = load_profile(Path.cwd())
    except ProfileError as exc:
        sys.stderr.write(f"error: invalid [tool.balamut]: {exc}\n")
        return 2
    coverage_default = substrate_path(profile)
    parser = argparse.ArgumentParser(prog="balamut estimate", description="Estimate the duration of a campaign.")
    parser.add_argument("paths", nargs="*", help="source files or directories (default: the source roots)")
    parser.add_argument("--coverage", type=Path, default=coverage_default, help="the coverage substrate")
    parser.add_argument("--costs", type=Path, default=None, help="test durations (default: the substrate's sidecar)")
    parser.add_argument("--daemons", type=int, default=0, help="daemons (default: the performance cores)")
    parser.add_argument("--workers", type=int, default=1, help="workers per daemon, as for mutate")
    parser.add_argument("--top", type=int, default=0, help="files in the breakdown (0: the slow ones and the bound)")
    parser.add_argument(
        "--probe", type=int, default=0, help="measure the overhead on this many mutants (0: a constant)"
    )
    args = parser.parse_args(argv)
    if not args.coverage.exists():
        sys.stderr.write(f"error: no coverage substrate at {args.coverage}: run `balamut diff` first\n")
        return 2
    layout = profile.layout
    cov = load_coverage_map(args.coverage, root=layout.root)
    costs = read_costs(args.costs or substrate.costs_path(args.coverage))
    daemons = args.daemons or resolve_workers()
    paths = args.paths or list(layout.source)
    total, by_file, union = _covered(paths, layout, cov)
    covered = sum(len(coverings) for coverings in by_file.values())
    if not covered:
        sys.stdout.write(f"{total} mutants, none covered: nothing to run\n")
        return 0
    median = statistics.median(costs.values()) if costs else 0.1
    overhead, source = _OVERHEAD_FALLBACK_S, "a constant"
    if args.probe > 0:
        cheapest = min(by_file, key=lambda file: sum(work_of(c, costs, median, args.workers) for c in by_file[file]))
        probed = _probe_overhead(cheapest, by_file, costs, median, args)
        if probed is not None:
            overhead, source = probed, "probed"
    killer_first, _rows = estimate_run(
        by_file,
        costs,
        median,
        overhead,
        daemons=daemons,
        workers=args.workers,
        expensive_survive=_KILLER_FIRST_EXPENSIVE_SHARE,
    )
    cross_kill, rows = estimate_run(by_file, costs, median, overhead, daemons=daemons, workers=args.workers)
    measured = len(union & costs.keys()) / len(union) if union else 1.0
    startup = _STARTUP_FALLBACK_S
    sys.stdout.write(
        f"\n=== estimate: {' '.join(paths)} ===\n"
        f"  killer-first (balamut mutate):   ~{(startup + killer_first) / 60:.0f} min ({startup + killer_first:.0f}s)\n"
        f"  cross-kill (--cross-kill):       ~{(startup + cross_kill) / 60:.0f} min ({startup + cross_kill:.0f}s)\n"
        f"  mutants {total} (covered {covered}, no coverage {total - covered}) . {daemons} x {args.workers} daemons"
        f" . covering tests with a measured duration {measured:.0%}\n"
        f"  overhead {overhead * 1000:.0f}ms per mutant ({source}) . accuracy about 10%\n"
        "\n  by cost per mutant [cross-kill]:\n"
    )
    shown = select_files(rows, top=args.top)
    for run_s, file, count, average in shown:
        share = run_s / cross_kill if cross_kill else 0.0
        sys.stdout.write(f"   {average:5.2f}s/mutant  {run_s:6.0f}s {share:4.0%}  {file}  ({count} mutants)\n")
    if len(shown) < len(rows):
        rest = cross_kill - sum(row[0] for row in shown)
        sys.stdout.write(f"   {'':12s}  {rest:6.0f}s        +{len(rows) - len(shown)} very fast files\n")
    return 0
