"""``balamut live``: run a campaign with a live dashboard (elapsed, done/total, throughput, ETA, verdicts).

The engine gets a unique ``--stats-json`` path and its atomic snapshots drive every counter. The verdict cache is
read only for an advisory list of files gaining survivors and never feeds the exact progress.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from balamut.engine.stats import CampaignStats, read_campaign_stats

_CACHE_RE = re.compile(r"cache:\s*(\S+)")
_TOTAL_RE = re.compile(r"mutants:\s*(\d+)")
_DISPATCH_RE = re.compile(r"(\d+)/(\d+)\s+(\S+\.py:\d+)")
_HANG_RE = re.compile(r"(\S+\.py:\d+) stalled for")
_STARTUP_TIMEOUT_S = 120

type VerdictCounts = dict[str, int]
type FileRows = list[tuple[str, int]]


def _fmt(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def survived_by_file(cache: str) -> VerdictCounts:
    """Advisory survivors per file from the cache, opened read-only."""
    connection = sqlite3.connect(f"file:{cache}?mode=ro", uri=True, timeout=5)
    try:
        rows = connection.execute("SELECT file, count(*) FROM mutant WHERE verdict='survived' GROUP BY file").fetchall()
    finally:
        connection.close()
    return {str(file): int(count) for file, count in rows}


def survivor_delta(current: VerdictCounts, baseline: VerdictCounts) -> FileRows:
    """Files that gained survivors since the campaign started (advisory, never part of the counts)."""
    gained = [(file, count - baseline.get(file, 0)) for file, count in current.items() if count > baseline.get(file, 0)]
    return sorted(gained, key=lambda row: -row[1])[:6]


def latest_dispatch(log: str) -> str:
    matches = _DISPATCH_RE.findall(log)
    return str(matches[-1][2]) if matches else "-"


def hangs(log: str) -> list[str]:
    return [str(site) for site in _HANG_RE.findall(log)]


def render(
    *, elapsed: float, stats: CampaignStats, top: FileRows, last: str, hanging: str, work_start: float | None
) -> str:
    counts = stats.status_counts
    killed, survived = counts.get("killed", 0), counts.get("survived", 0)
    total = stats.enumerated
    done = (stats.enumerated - stats.executed) + stats.terminal
    pct = done / total * 100 if total else 0.0
    # An honest ETA: the share of work COST done (covering-test durations), not of mutants. done_cost of total_cost
    # took work_elapsed, so the rest takes work_elapsed * (total - done) / done.
    work_elapsed = elapsed - work_start if work_start is not None else 0.0
    remaining = max(0.0, stats.total_cost - stats.done_cost)
    eta = _fmt(work_elapsed * remaining / stats.done_cost) if stats.done_cost > 0 and work_elapsed > 0 else "-"
    rate = stats.terminal / work_elapsed * 60 if work_elapsed > 0 else 0.0
    scored = killed + survived
    score = killed / scored * 100 if scored else 0.0
    lines = [
        (
            f"=== balamut live . elapsed {_fmt(elapsed)} . {done}/{total} ({pct:.0f}%)"
            f" . {rate:.0f} mutants/min . ETA {eta} ==="
        ),
        (
            f"  killed {killed} . survived {survived} . timeout {counts.get('timeout', 0)}"
            f" . unsupported {counts.get('unsupported', 0)} . no_cov {counts.get('no_coverage', 0)}"
            f" . score {score:.0f}%"
        ),
        f"  last: {last}",
    ]
    if hanging != "-":
        lines.append(f"  hanging now: {hanging}")
    if top:
        lines.append("  files gaining survivors (advisory): " + " . ".join(f"{n}x {Path(f).name}" for f, n in top[:3]))
    return "\n".join(lines) + "\n"


def render_final(stats: CampaignStats | None, *, elapsed: float, rc: int) -> str:
    """The exact final for a complete snapshot; anything else is explicitly partial."""
    if stats is None:
        return (
            f"\n=== PARTIAL/UNKNOWN . {_fmt(elapsed)} . no campaign stats . the exact result is unknown . rc {rc} ===\n"
        )
    counts = stats.status_counts
    observed = (stats.enumerated - stats.executed) + stats.terminal
    tail = (
        f"killed {counts['killed']} . survived {counts['survived']} . timeout {counts['timeout']}"
        f" . unsupported {counts['unsupported']} . no_cov {counts['no_coverage']}"
    )
    if not stats.complete:
        return (
            f"\n=== PARTIAL/UNKNOWN . {_fmt(elapsed)} . {observed}/{stats.enumerated} observed . snapshot incomplete"
            f" . {tail} . rc {rc} ===\n"
        )
    scored = counts["killed"] + counts["survived"]
    score = counts["killed"] / scored * 100 if scored else 0.0
    done = f"{stats.enumerated}/{stats.enumerated}"
    return f"\n=== DONE . {_fmt(elapsed)} . {done} . {tail} . score {score:.0f}% . rc {rc} ===\n"


def _await_startup(log: Path, deadline: float, proc: subprocess.Popen[bytes]) -> tuple[str | None, int | None]:
    """Wait for the engine's ``cache:`` and ``mutants:`` lines, or for its exit."""
    cache: str | None = None
    total: int | None = None
    while True:
        text = _read_log(log)  # read before the exit check: the engine's final flush is parsed too
        if (match := _CACHE_RE.search(text)) is not None:
            cache = match.group(1)
        if (match := _TOTAL_RE.search(text)) is not None:
            total = int(match.group(1))
        if (cache is not None and total is not None) or proc.poll() is not None or time.time() >= deadline:
            return cache, total
        time.sleep(1)


def _advisory(cache: str) -> VerdictCounts:
    try:
        return survived_by_file(cache)
    except sqlite3.Error:
        return {}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="balamut live", description="Run `balamut mutate` with a live dashboard.")
    parser.add_argument("--interval", type=int, default=5, help="seconds between snapshots (default 5)")
    args, rest = parser.parse_known_args(argv)
    mutate_args = [argument for argument in rest if argument != "--"]
    if any(argument == "--stats-json" or argument.startswith("--stats-json=") for argument in mutate_args):
        sys.stderr.write("error: balamut live owns --stats-json; do not pass it\n")
        return 2
    tag = f"{time.time_ns()}"
    scratch = Path(tempfile.gettempdir())
    raw_log = scratch / f"balamut-live-{tag}.log"
    stats_path = scratch / f"balamut-live-{tag}.stats.json"
    start = time.time()
    with raw_log.open("w", encoding="utf-8") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "balamut", "mutate", *mutate_args, "--stats-json", str(stats_path)],
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        sys.stdout.write(f"balamut live: the engine runs as pid {proc.pid}, log {raw_log}\n")
        sys.stdout.flush()
        cache, total = _await_startup(raw_log, start + _STARTUP_TIMEOUT_S, proc)
        if cache is None or total is None:
            sys.stdout.write("balamut live: the engine printed no startup lines; running without a dashboard\n")
        else:
            sys.stdout.write(f"balamut live: cache {cache} . {total} mutants . a snapshot every {args.interval}s\n\n")
            sys.stdout.flush()
            baseline = _advisory(cache)
            seen = 0
            work_start: float | None = None
            while proc.poll() is None:
                time.sleep(args.interval)
                elapsed = time.time() - start
                stats = read_campaign_stats(stats_path)
                if stats is None or stats.enumerated != total:
                    continue
                if work_start is None and stats.terminal > 0:
                    work_start = elapsed
                log = _read_log(raw_log)
                fresh = hangs(log[seen:])
                seen = len(log)
                sys.stdout.write(
                    render(
                        elapsed=elapsed,
                        stats=stats,
                        top=survivor_delta(_advisory(cache), baseline),
                        last=latest_dispatch(log),
                        hanging=fresh[-1] if fresh else "-",
                        work_start=work_start,
                    )
                )
                sys.stdout.flush()
        rc = proc.wait()
    final = read_campaign_stats(stats_path)
    if final is not None and total is not None and final.enumerated != total:
        final = None
    sys.stdout.write(render_final(final, elapsed=time.time() - start, rc=rc))
    return rc
