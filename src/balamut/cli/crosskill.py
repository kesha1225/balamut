"""The cross-kill matrix from the verdict cache: managing the suite (duplicates, fragile kills, minimization).

A post-processor of the cache: it inverts "mutant -> killers" into "test -> killed mutants" and reports

- fragile mutants (killed by EXACTLY one test: drop that test and they survive);
- a minimal suite (a greedy set cover: the fewest tests that keep EVERY kill, a smoke set);
- how many tests can go without losing kill coverage.

The default run stops at the first killer (about one killer per mutant), so fragility is nearly universal and
subsumption is blind; the full matrix needs ``balamut mutate --cross-kill`` (every covering test per mutant).
"""

from __future__ import annotations

import argparse
import operator
import sys
from dataclasses import dataclass
from pathlib import Path

from balamut.config import load_profile
from balamut.coverage.store import Store, default_cache_path

type KillRow = tuple[str, list[str]]  # (mutant key, node ids of its killers)
type TestKills = dict[str, set[str]]  # test -> keys of the mutants it kills
type TopKillers = list[tuple[str, int]]  # (test, killed count), descending


def invert_kills(rows: list[KillRow]) -> TestKills:
    """ "mutant -> killers" as "test -> {mutants it kills}"."""
    test_kills: TestKills = {}
    for key, killers in rows:
        for test in killers:
            test_kills.setdefault(test, set()).add(key)
    return test_kills


def fragile_keys(rows: list[KillRow]) -> list[str]:
    """Mutants killed by EXACTLY one test: fragile coverage, drop the test and the mutant survives."""
    return sorted(key for key, killers in rows if len(set(killers)) == 1)


def greedy_min_suite(test_kills: TestKills) -> list[str]:
    """A greedy set cover: the fewest tests that keep EVERY kill."""
    remaining = {key for killset in test_kills.values() for key in killset}
    pool = {test: set(killset) for test, killset in test_kills.items()}
    chosen: list[str] = []
    while remaining:
        best: str | None = None
        best_gain = 0
        for test, killset in pool.items():
            gain = len(killset & remaining)
            if gain > best_gain:
                best_gain, best = gain, test
        if best is None:
            break
        chosen.append(best)
        remaining -= pool[best]
        del pool[best]
    return chosen


def subsuming_mutants(rows: list[KillRow]) -> list[str]:
    """Dominators: the mutants with MINIMAL kill sets.

    M is dominated by M' when kill(M') ⊆ kill(M): M' is harder, and its killers kill M too; of equal kill sets the
    first one stays. Testing the dominators alone kills every other mutant, which is subsumed (redundant)."""
    kill_sets = {key: frozenset(killers) for key, killers in rows if killers}
    keys = list(kill_sets)
    dominated: set[str] = set()
    for outer, key_a in enumerate(keys):
        for inner, key_b in enumerate(keys):
            if key_a == key_b or key_b in dominated:
                continue
            set_a, set_b = kill_sets[key_a], kill_sets[key_b]
            if set_b < set_a or (set_b == set_a and inner < outer):  # b is harder (or equal and earlier)
                dominated.add(key_a)
                break
    return [key for key in keys if key not in dominated]


@dataclass(frozen=True, slots=True)
class CrossKillSummary:
    """Aggregates of the matrix. Test kills, fragility, dominators and the minimal suite count only kills by a
    test; a kill without a killing test (a hang kill) is reported apart, since no test maps to it."""

    test_killed: int  # mutants killed by at least one test
    timeout_killed: int  # killed with no killing test recorded
    killers: int  # distinct killing tests
    fragile: int  # killed by exactly one test
    dominators: int
    subsumed: int
    min_suite: int  # tests of the greedy set cover (they keep every test kill)
    removable: int
    degenerate: bool
    top_killers: TopKillers


def summarize(rows: list[KillRow]) -> CrossKillSummary:
    """Fold killed rows into the aggregates.

    A kill without a killer does not map to a test: none of the matrix computations would cover it, and counting it
    would inflate the denominators and make "the minimal suite keeps every kill" false. The matrix is built over
    test kills, and the others are counted apart."""
    test_killed_rows = [(key, killers) for key, killers in rows if killers]
    timeout_killed = len(rows) - len(test_killed_rows)
    test_kills = invert_kills(test_killed_rows)
    killed_keys = {key for key, _ in test_killed_rows}
    fragile = fragile_keys(test_killed_rows)
    min_suite = greedy_min_suite(test_kills)
    dominators = subsuming_mutants(test_killed_rows)
    top: TopKillers = sorted(
        ((test, len(killed)) for test, killed in test_kills.items()), key=operator.itemgetter(1), reverse=True
    )
    # MOST test kills with a single killer means first-killer data (the run stops at the first failing test and
    # never records the others): subsumption and fragility are degenerate. The full matrix needs --cross-kill.
    degenerate = bool(killed_keys) and len(fragile) / len(killed_keys) > 0.5
    return CrossKillSummary(
        test_killed=len(killed_keys),
        timeout_killed=timeout_killed,
        killers=len(test_kills),
        fragile=len(fragile),
        dominators=len(dominators),
        subsumed=len(killed_keys) - len(dominators),
        min_suite=len(min_suite),
        removable=len(test_kills) - len(min_suite),
        degenerate=degenerate,
        top_killers=top,
    )


def _report(rows: list[KillRow]) -> None:
    summary = summarize(rows)
    if summary.degenerate:
        sys.stdout.write(
            "  ⚠ FIRST-KILLER data (about one killer per mutant): subsumption and fragility are degenerate;"
            " for the full matrix run `balamut mutate --cross-kill`\n"
        )
    timeout_note = f" · killed without a killing test: {summary.timeout_killed}" if summary.timeout_killed else ""
    sys.stdout.write(
        f"\n=== CROSS-KILL{' [first-killer]' if summary.degenerate else ' [full matrix]'} ===\n"
        f"  killed by tests: {summary.test_killed} · killing tests: {summary.killers}{timeout_note}\n"
        f"  fragile (one killer): {summary.fragile}/{summary.test_killed}\n"
        f"  dominators (subsuming): {summary.dominators} -> {summary.subsumed} subsumed (redundant, killed with them)\n"
        f"  minimal suite (smoke, set cover): {summary.min_suite} tests keep every test kill"
        f" -> {summary.removable} removable without losing kill coverage\n"
        "  top killers (test -> killed):\n"
    )
    for test, killed in summary.top_killers[:10]:
        sys.stdout.write(f"    {killed:4}  {test}\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="balamut crosskill", description="Cross-kill matrix from the verdict cache.")
    parser.add_argument("--cache", default="", help="path to the SQLite cache (default: the branch and worktree one)")
    args = parser.parse_args(argv)
    if args.cache:
        cache = Path(args.cache)
    else:
        profile = load_profile(Path.cwd())
        cache = default_cache_path(profile.root, profile.state_dir)
    store = Store(cache)
    rows = store.killed_with_killers()
    store.close()
    if not rows:
        sys.stdout.write("no killed mutants in the cache (run `balamut mutate [--cross-kill]` first)\n")
        return 0
    _report(rows)
    return 0
