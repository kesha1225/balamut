"""The exact JSON snapshot of the running campaign: what ``balamut live`` reads.

The snapshot is replaced atomically on every terminal verdict. Its counters are consistent by construction (the
reader rejects any snapshot that is not), and the work weights give an honest ETA: the share of covering-test
cost done, not the share of mutants.
"""

from __future__ import annotations

import json
import math
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from balamut.engine.results import STATUS_VALUES, MutantStatus, StatusCounts

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type CampaignTimings = dict[str, float]
type TimingView = Mapping[str, float]
type CostMap = dict[str, float]

STATS_SCHEMA_VERSION = 1
CAMPAIGN_TIMING_KEYS = (
    "baseline_ingest",
    "staleness_selection",
    "coverage_refresh",
    "discovery_reexport",
    "mutation_enumeration",
    "pytest_collection",
    "cache_partition",
    "daemon_startup",
    "mutant_execution",
    "persistence",
)


def empty_campaign_timings() -> CampaignTimings:
    return dict.fromkeys(CAMPAIGN_TIMING_KEYS, 0.0)


@dataclass(frozen=True, slots=True)
class CampaignStats:
    """One exact snapshot of the current campaign."""

    schema_version: int
    complete: bool
    enumerated: int
    cache_resolved: int
    executed: int
    terminal: int
    status_counts: StatusCounts
    # The work weight is the sum of covering-test durations of a mutant: total over every runnable mutant, done
    # over the terminal ones. An honest ETA is elapsed * (total - done) / done.
    total_cost: float = 0.0
    done_cost: float = 0.0
    timings: CampaignTimings = field(default_factory=empty_campaign_timings)


def empty_stats(*, complete: bool, timings: TimingView | None = None) -> CampaignStats:
    """The snapshot of a campaign with nothing to mutate."""
    return CampaignStats(
        schema_version=STATS_SCHEMA_VERSION,
        complete=complete,
        enumerated=0,
        cache_resolved=0,
        executed=0,
        terminal=0,
        status_counts=dict.fromkeys(STATUS_VALUES, 0),
        timings={**empty_campaign_timings(), **(timings or {})},
    )


def _nonnegative_number(value: JsonValue) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _exact_int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_campaign_stats(path: Path) -> CampaignStats | None:
    """Read and strictly validate the atomic snapshot; None for a missing, malformed or inconsistent one."""
    try:
        payload: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    schema_version = _exact_int(payload.get("schema_version"))
    enumerated = _exact_int(payload.get("enumerated"))
    cache_resolved = _exact_int(payload.get("cache_resolved"))
    executed = _exact_int(payload.get("executed"))
    terminal = _exact_int(payload.get("terminal"))
    complete = payload.get("complete")
    raw_counts = payload.get("status_counts")
    if schema_version is None or enumerated is None or cache_resolved is None or executed is None or terminal is None:
        return None
    if not isinstance(complete, bool) or not isinstance(raw_counts, dict):
        return None
    if schema_version != STATS_SCHEMA_VERSION or set(raw_counts) != set(STATUS_VALUES):
        return None
    counts: StatusCounts = {}
    for status in STATUS_VALUES:
        value = _exact_int(raw_counts[status])
        if value is None or value < 0:
            return None
        counts[status] = value
    if any(value < 0 for value in (enumerated, cache_resolved, executed, terminal)):
        return None
    if executed > enumerated or terminal > executed or cache_resolved > enumerated - executed:
        return None
    resolved = enumerated - executed + terminal
    if sum(counts.values()) != resolved:
        return None
    if complete and (terminal != executed or resolved != enumerated):
        return None
    total_cost = _nonnegative_number(payload.get("total_cost", 0.0))
    done_cost = _nonnegative_number(payload.get("done_cost", 0.0))
    if total_cost is None or done_cost is None:
        return None
    raw_timings = payload.get("timings", {})
    if not isinstance(raw_timings, dict) or not set(raw_timings).issubset(CAMPAIGN_TIMING_KEYS):
        return None
    timings = empty_campaign_timings()
    for name, seconds in raw_timings.items():
        normalized = _nonnegative_number(seconds)
        if normalized is None:
            return None
        timings[name] = normalized
    return CampaignStats(
        schema_version=schema_version,
        complete=complete,
        enumerated=enumerated,
        cache_resolved=cache_resolved,
        executed=executed,
        terminal=terminal,
        status_counts=counts,
        total_cost=total_cost,
        done_cost=done_cost,
        timings=timings,
    )


def write_campaign_stats(path: Path, snapshot: CampaignStats) -> None:
    """Publish a snapshot by an atomic replace within the same directory."""
    payload = {
        "schema_version": snapshot.schema_version,
        "complete": snapshot.complete,
        "enumerated": snapshot.enumerated,
        "cache_resolved": snapshot.cache_resolved,
        "executed": snapshot.executed,
        "terminal": snapshot.terminal,
        "status_counts": snapshot.status_counts,
        "total_cost": snapshot.total_cost,
        "done_cost": snapshot.done_cost,
        "timings": snapshot.timings,
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
            temporary.write("\n")
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@dataclass(slots=True)
class StatsPublisher:
    """Thread-safe publisher of the campaign snapshot."""

    path: Path
    enumerated: int
    cache_resolved: int
    executed: int
    status_counts: StatusCounts
    weights: CostMap = field(default_factory=dict[str, float])  # mutant key -> work weight
    total_cost: float = 0.0
    terminal: int = 0
    done_cost: float = 0.0
    hang_weight: float = 0.0  # the weight of a hanging mutant: the hang budget of all its attempts
    timings: CampaignTimings = field(default_factory=empty_campaign_timings)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def _snapshot(
        self,
        *,
        complete: bool,
        terminal: int | None = None,
        status_counts: StatusCounts | None = None,
        done_cost: float | None = None,
    ) -> CampaignStats:
        return CampaignStats(
            schema_version=STATS_SCHEMA_VERSION,
            complete=complete,
            enumerated=self.enumerated,
            cache_resolved=self.cache_resolved,
            executed=self.executed,
            terminal=self.terminal if terminal is None else terminal,
            status_counts=dict(self.status_counts if status_counts is None else status_counts),
            total_cost=self.total_cost,
            done_cost=self.done_cost if done_cost is None else done_cost,
            timings=dict(self.timings),
        )

    def publish_initial(self) -> None:
        """An incomplete snapshot right after the cache partition."""
        write_campaign_stats(self.path, self._snapshot(complete=False))

    def record_terminal(self, status: MutantStatus, key: str) -> None:
        """One terminal result of the executed work, and its work weight into ``done_cost``."""
        with self.lock:
            if self.terminal >= self.executed:
                raise RuntimeError("the stats received more terminal results than planned")
            next_terminal = self.terminal + 1
            next_counts = dict(self.status_counts)
            next_counts[status.value] += 1
            next_done_cost = self.done_cost + self.weights.get(key, 0.0)
            write_campaign_stats(
                self.path,
                self._snapshot(
                    complete=False, terminal=next_terminal, status_counts=next_counts, done_cost=next_done_cost
                ),
            )
            self.terminal = next_terminal
            self.status_counts = next_counts
            self.done_cost = next_done_cost

    def mark_hanging(self, key: str) -> None:
        """A mutant hangs and goes to an isolated retry: its wall time is the budget of every attempt, not its
        covering cost. The weight rises to ``hang_weight`` so the remaining cost stays real and the ETA grows
        while the tail hangs instead of dropping to zero. Idempotent; a heavier weight is never lowered."""
        with self.lock:
            current = self.weights.get(key, 0.0)
            if self.hang_weight <= current:
                return
            self.total_cost += self.hang_weight - current
            self.weights[key] = self.hang_weight
            write_campaign_stats(self.path, self._snapshot(complete=False))

    def publish_complete(self, timings: TimingView | None = None) -> None:
        """The complete snapshot, only after the campaign invariants held and the final persistence ran."""
        with self.lock:
            if self.terminal != self.executed or sum(self.status_counts.values()) != self.enumerated:
                raise RuntimeError("incomplete campaign stats cannot be published as complete")
            if timings is not None:
                self.timings.update(timings)
            write_campaign_stats(self.path, self._snapshot(complete=True))
