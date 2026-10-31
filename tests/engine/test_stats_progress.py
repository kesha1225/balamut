"""Campaign stats snapshots, progress reporting and the timeout policy (balamut.engine.stats/progress/timeouts)."""

from __future__ import annotations

import importlib
import json
import math
import sys
from io import StringIO
from pathlib import Path
from typing import Self, override

import pytest

from balamut.engine import progress as progress_module
from balamut.engine import stats, timeouts
from balamut.engine.progress import CampaignProgress, ProgressMode, _stderr
from balamut.engine.results import MutantStatus

type StatsPayload = dict[str, stats.JsonValue]


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def _payload(path: Path) -> StatsPayload:
    payload: stats.JsonValue = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _publisher(path: Path, *, weights: dict[str, float], total_cost: float, hang_weight: float) -> stats.StatsPublisher:
    return stats.StatsPublisher(
        path=path,
        enumerated=len(weights),
        cache_resolved=0,
        executed=len(weights),
        status_counts={"killed": 0, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0},
        weights=dict(weights),
        total_cost=total_cost,
        hang_weight=hang_weight,
    )


def test_hang_wall_budget_sums_escalating_retry_timeouts() -> None:
    assert _close(timeouts.hang_wall_budget(8.0, 3.0, 1), 32.0)
    assert _close(timeouts.hang_wall_budget(8.0, 3.0, 0), 8.0)
    assert _close(timeouts.hang_wall_budget(8.0, 3.0, 2), 104.0)
    assert _close(timeouts.hang_wall_budget(5.0, 1.0, 3), 20.0)  # a multiplier of one must not divide by zero


def test_mutant_timeout_accounts_for_the_slowest_calibrated_test() -> None:
    budget = timeouts.mutant_timeout_budget(
        8.0,
        ("tests/fast_test.py::test_fast", "tests/slow_test.py::test_slow"),
        {"tests/fast_test.py::test_fast": 0.1, "tests/slow_test.py::test_slow": 12.0},
    )

    assert _close(budget, 25.0)
    assert _close(timeouts.mutant_timeout_budget(8.0, ("test_x",), {"test_x": float("inf")}), 8.0)


@pytest.mark.parametrize(
    ("base", "multiplier", "retries"),
    [
        (float("nan"), 3.0, 1),
        (float("inf"), 3.0, 1),
        (0.0, 3.0, 1),
        (-1.0, 3.0, 1),
        (8.0, float("nan"), 1),
        (8.0, float("inf"), 1),
        (8.0, 0.0, 1),
        (8.0, -1.0, 1),
        (8.0, 3.0, -1),
        (1e308, 10.0, 2),
    ],
)
def test_timeout_policy_rejects_non_finite_nonpositive_or_overflowing_values(
    base: float, multiplier: float, retries: int
) -> None:
    assert timeouts.timeout_policy_error(base, multiplier, retries) is not None


def test_timeout_policy_accepts_the_defaults() -> None:
    assert timeouts.timeout_policy_error(8.0, 3.0, 1) is None


def test_timeout_policy_accepts_zero_retries() -> None:
    """``retries < 0`` allows zero retries; ``retries < 1`` would reject them."""
    assert timeouts.timeout_policy_error(8.0, 3.0, 0) is None


def test_timeout_policy_accepts_a_fractional_base() -> None:
    """``base <= 0`` allows any positive base; ``base <= 1`` would reject a sub-second timeout."""
    assert timeouts.timeout_policy_error(0.5, 3.0, 1) is None


def test_timeout_policy_accepts_a_fractional_multiplier() -> None:
    """``multiplier <= 0`` allows any positive multiplier; ``multiplier <= 1`` would reject a sub-unity growth."""
    assert timeouts.timeout_policy_error(8.0, 0.5, 1) is None


def test_progress_report_hang_forwards_key_and_is_noop_without_observer() -> None:
    seen: list[str] = []
    progress = CampaignProgress(total=1, mode=ProgressMode.NONE, on_hang=seen.append)
    progress.report_hang("key-7")
    assert seen == ["key-7"]
    assert progress.done == 0
    silent = CampaignProgress(total=1, mode=ProgressMode.NONE)
    silent.report_hang("key-9")
    assert silent.done == 0


def test_mark_hanging_bumps_weight_to_hang_budget_and_republishes(tmp_path: Path) -> None:
    """A hanging mutant's covering weight is tiny, its real wall time is the hang budget: the remaining cost of the
    tail stays real and the ETA does not drop to zero while it hangs."""
    path = tmp_path / "campaign-stats.json"
    publisher = _publisher(path, weights={"a": 0.5, "b": 2.0}, total_cost=2.5, hang_weight=32.0)
    publisher.publish_initial()
    publisher.mark_hanging("a")
    snapshot = stats.read_campaign_stats(path)

    assert snapshot is not None
    assert _close(snapshot.total_cost, 34.0)
    assert _close(publisher.weights["a"], 32.0)
    assert _close(publisher.total_cost, 34.0)


def test_mark_hanging_is_idempotent_and_never_lowers_a_heavier_weight(tmp_path: Path) -> None:
    publisher = _publisher(
        tmp_path / "campaign-stats.json", weights={"cheap": 1.0, "heavy": 100.0}, total_cost=101.0, hang_weight=32.0
    )
    publisher.publish_initial()
    publisher.mark_hanging("cheap")
    publisher.mark_hanging("cheap")
    assert _close(publisher.weights["cheap"], 32.0)
    assert _close(publisher.total_cost, 132.0)
    publisher.mark_hanging("heavy")
    assert _close(publisher.weights["heavy"], 100.0)
    assert _close(publisher.total_cost, 132.0)


def test_terminal_results_advance_counts_and_done_cost(tmp_path: Path) -> None:
    path = tmp_path / "campaign-stats.json"
    publisher = _publisher(path, weights={"a": 1.5}, total_cost=1.5, hang_weight=8.0)
    publisher.publish_initial()
    publisher.record_terminal(MutantStatus.KILLED, "a")
    publisher.publish_complete({"mutant_execution": 2.0})

    snapshot = stats.read_campaign_stats(path)
    assert snapshot is not None
    assert snapshot.complete is True
    assert snapshot.terminal == 1
    assert snapshot.status_counts["killed"] == 1
    assert _close(snapshot.done_cost, 1.5)
    assert _close(snapshot.timings["mutant_execution"], 2.0)
    with pytest.raises(RuntimeError):
        publisher.record_terminal(MutantStatus.KILLED, "a")


def test_incomplete_stats_cannot_be_published_as_complete(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path / "campaign-stats.json", weights={"a": 1.0}, total_cost=1.0, hang_weight=8.0)

    with pytest.raises(RuntimeError):
        publisher.publish_complete()


def test_publication_is_atomic_in_the_same_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "campaign-stats.json"
    real_replace = Path.replace
    replacements: list[tuple[Path, Path, StatsPayload]] = []

    def inspect_replace(source: Path, target: Path) -> Path:
        replacements.append((source, Path(target), _payload(source)))
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", inspect_replace)
    stats.write_campaign_stats(path, stats.empty_stats(complete=False))
    stats.write_campaign_stats(path, stats.empty_stats(complete=True))

    assert [payload["complete"] for _source, _target, payload in replacements] == [False, True]
    assert all(source.parent == path.parent and target == path for source, target, _payload in replacements)
    # The hidden temp file carries the target's name (prefix) and a .tmp suffix so a leftover is recognizable.
    assert all(source.name.startswith(f".{path.name}.") for source, _target, _payload in replacements)
    assert all(source.name.endswith(".tmp") for source, _target, _payload in replacements)
    assert not [entry for entry in tmp_path.iterdir() if entry.name != path.name]


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"terminal": 5},
        {"status_counts": {"killed": 1}},
        {"complete": True, "terminal": 0},
        {"total_cost": -1.0},
        {"timings": {"unknown_phase": 1.0}},
        {"enumerated": True},
    ],
    ids=["schema", "terminal-over-executed", "partial-counts", "complete-but-not", "negative-cost", "phase", "bool"],
)
def test_reader_rejects_inconsistent_snapshots(tmp_path: Path, changes: StatsPayload) -> None:
    path = tmp_path / "campaign-stats.json"
    publisher = _publisher(path, weights={"a": 1.0}, total_cost=1.0, hang_weight=8.0)
    publisher.publish_initial()
    payload = _payload(path)
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert stats.read_campaign_stats(path) is None


def test_reader_rejects_garbage_and_missing_files(tmp_path: Path) -> None:
    path = tmp_path / "campaign-stats.json"
    path.write_text("{not-json", encoding="utf-8")

    assert stats.read_campaign_stats(path) is None
    assert stats.read_campaign_stats(tmp_path / "missing.json") is None


class _TtyBuffer(StringIO):
    def __init__(self, is_tty: bool) -> None:
        super().__init__()
        self._is_tty = is_tty

    @override
    def isatty(self) -> bool:
        return self._is_tty


def test_progress_auto_selects_tty_only_for_tty_stream() -> None:
    tty = CampaignProgress(total=1, mode=ProgressMode.AUTO, stream=_TtyBuffer(True))
    redirected = CampaignProgress(total=1, mode=ProgressMode.AUTO, stream=_TtyBuffer(False))

    assert tty.effective_mode is ProgressMode.TTY
    assert redirected.effective_mode is ProgressMode.COMPACT


def test_compact_progress_cadence_uses_injected_clock() -> None:
    times = iter((0.0, 1.0, 5.0))
    stream = StringIO()
    progress = CampaignProgress(
        total=3, mode=ProgressMode.COMPACT, stream=stream, compact_interval=5.0, clock=lambda: next(times)
    )

    progress.advance("src/x.py:1", MutantStatus.KILLED)
    progress.advance("src/x.py:2", MutantStatus.SURVIVED)
    progress.advance("src/x.py:3", MutantStatus.TIMEOUT)

    assert progress.done == 3
    assert stream.getvalue().splitlines() == ["progress: 1/3 src/x.py:1 killed", "progress: 3/3 src/x.py:3 timeout"]


def test_progress_rejects_more_results_than_planned() -> None:
    progress = CampaignProgress(total=1, mode=ProgressMode.NONE)
    progress.advance("src/x.py:1", MutantStatus.KILLED)

    with pytest.raises(RuntimeError):
        progress.advance("src/x.py:1", MutantStatus.KILLED)


@pytest.mark.parametrize(("total", "interval"), [(-1, 5.0), (1, 0.0)])
def test_progress_rejects_invalid_configuration(total: int, interval: float) -> None:
    with pytest.raises(ValueError):
        CampaignProgress(total=total, mode=ProgressMode.NONE, compact_interval=interval)


_ZERO_COUNTS = {"killed": 0, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0}


class _RecordingLock:
    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> Self:
        self.entered += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _valid_payload() -> StatsPayload:
    return {
        "schema_version": 1,
        "complete": False,
        "enumerated": 3,
        "cache_resolved": 0,
        "executed": 3,
        "terminal": 3,
        "status_counts": {"killed": 1, "survived": 1, "timeout": 1, "no_coverage": 0, "unsupported": 0},
        "total_cost": 0.0,
        "done_cost": 0.0,
        "timings": {},
    }


def _write_payload(tmp_path: Path, payload: StatsPayload) -> Path:
    path = tmp_path / "campaign-stats.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_reader_rejects_non_bool_complete_with_valid_counts(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["complete"] = "yes"

    assert stats.read_campaign_stats(_write_payload(tmp_path, payload)) is None


def test_reader_rejects_negative_enumeration_values(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["terminal"] = -1

    assert stats.read_campaign_stats(_write_payload(tmp_path, payload)) is None


def test_reader_rejects_negative_cache_resolved(tmp_path: Path) -> None:
    """``any(value < 0)`` rejects one negative counter; ``all(value < 0)`` would accept it."""
    payload = _valid_payload()
    payload["cache_resolved"] = -1

    assert stats.read_campaign_stats(_write_payload(tmp_path, payload)) is None


def test_reader_rejects_negative_status_count(tmp_path: Path) -> None:
    """``value is None or value < 0`` rejects a negative count; ``and`` would accept it (the sum still balances)."""
    payload = _valid_payload()
    payload["status_counts"] = {"killed": -1, "survived": 3, "timeout": 1, "no_coverage": 0, "unsupported": 0}

    assert stats.read_campaign_stats(_write_payload(tmp_path, payload)) is None


def test_reader_rejects_overrun_counts(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["executed"] = 5

    assert stats.read_campaign_stats(_write_payload(tmp_path, payload)) is None


def test_reader_rejects_executed_above_enumerated_and_cache_overrun(tmp_path: Path) -> None:
    # Exactly one of the three invariant checks trips: only `cache_resolved > enumerated - executed`.
    payload: StatsPayload = {
        "schema_version": 1,
        "complete": False,
        "enumerated": 5,
        "cache_resolved": 3,
        "executed": 3,
        "terminal": 3,
        "status_counts": {"killed": 5, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0},
        "total_cost": 0.0,
        "done_cost": 0.0,
        "timings": {},
    }

    assert stats.read_campaign_stats(_write_payload(tmp_path, payload)) is None


def test_reader_defaults_missing_costs_to_zero(tmp_path: Path) -> None:
    payload = _valid_payload()
    del payload["total_cost"]
    del payload["done_cost"]

    snapshot = stats.read_campaign_stats(_write_payload(tmp_path, payload))

    assert snapshot is not None
    assert snapshot.total_cost == 0.0
    assert snapshot.done_cost == 0.0


def test_write_campaign_stats_preserves_non_ascii_without_escaping(tmp_path: Path) -> None:
    path = tmp_path / "campaign-stats.json"
    snapshot = stats.empty_stats(complete=True, timings={"ф": 1.0})

    stats.write_campaign_stats(path, snapshot)

    assert "ф" in path.read_text(encoding="utf-8")


def test_write_campaign_stats_sorts_keys_and_ends_with_a_newline(tmp_path: Path) -> None:
    path = tmp_path / "campaign-stats.json"

    stats.write_campaign_stats(path, stats.empty_stats(complete=False))

    raw = path.read_text(encoding="utf-8")
    assert raw.startswith('{"cache_resolved"')
    assert raw.endswith("\n")


def test_write_campaign_stats_cleans_up_the_temp_file_after_a_failed_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "campaign-stats.json"

    def fail_replace(_source: Path, _target: Path) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError):
        stats.write_campaign_stats(path, stats.empty_stats(complete=False))

    assert not [entry for entry in tmp_path.iterdir() if entry.name.startswith(".campaign-stats.json.")]


def test_record_terminal_writes_an_incomplete_intermediate_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "campaign-stats.json"
    publisher = _publisher(path, weights={"a": 1.0}, total_cost=1.0, hang_weight=8.0)

    publisher.record_terminal(MutantStatus.KILLED, "a")

    snapshot = stats.read_campaign_stats(path)
    assert snapshot is not None
    assert snapshot.terminal == 1
    assert snapshot.complete is False
    # The published done_cost is the accumulated weight of this terminal, not the pre-call zero.
    assert _close(snapshot.done_cost, 1.0)


def test_record_terminal_unknown_key_adds_no_weight(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path / "campaign-stats.json", weights={"a": 1.0}, total_cost=1.0, hang_weight=8.0)
    publisher.publish_initial()
    publisher.record_terminal(MutantStatus.KILLED, "unknown-key")

    assert _close(publisher.done_cost, 0.0)


def test_mark_hanging_unknown_key_counts_the_full_hang_weight(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path / "campaign-stats.json", weights={}, total_cost=0.0, hang_weight=32.0)
    publisher.publish_initial()
    publisher.mark_hanging("unknown")

    assert _close(publisher.weights["unknown"], 32.0)
    assert _close(publisher.total_cost, 32.0)


def test_mark_hanging_equal_weight_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publisher = _publisher(tmp_path / "campaign-stats.json", weights={"a": 32.0}, total_cost=32.0, hang_weight=32.0)
    publisher.publish_initial()
    writes: list[int] = []

    def record_write(_path: Path, _snapshot: stats.CampaignStats) -> None:
        writes.append(1)

    monkeypatch.setattr(stats, "write_campaign_stats", record_write)

    publisher.mark_hanging("a")

    assert writes == []


def test_publish_complete_requires_both_invariants(tmp_path: Path) -> None:
    publisher = stats.StatsPublisher(
        path=tmp_path / "campaign-stats.json",
        enumerated=2,
        cache_resolved=0,
        executed=1,
        terminal=1,
        status_counts=dict(_ZERO_COUNTS),
    )

    with pytest.raises(RuntimeError):
        publisher.publish_complete()


def test_record_terminal_holds_its_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _RecordingLock()
    publisher = stats.StatsPublisher(
        path=tmp_path / "campaign-stats.json",
        enumerated=1,
        cache_resolved=0,
        executed=1,
        status_counts=dict(_ZERO_COUNTS),
        weights={"a": 1.0},
        total_cost=1.0,
        hang_weight=8.0,
    )
    monkeypatch.setattr(publisher, "lock", lock)

    publisher.record_terminal(MutantStatus.KILLED, "a")

    assert lock.entered == 1


def test_mark_hanging_holds_its_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _RecordingLock()
    publisher = stats.StatsPublisher(
        path=tmp_path / "campaign-stats.json",
        enumerated=1,
        cache_resolved=0,
        executed=1,
        status_counts=dict(_ZERO_COUNTS),
        weights={"a": 0.5},
        total_cost=0.5,
        hang_weight=32.0,
    )
    monkeypatch.setattr(publisher, "lock", lock)

    publisher.mark_hanging("a")

    assert lock.entered == 1


def test_publish_complete_holds_its_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _RecordingLock()
    publisher = stats.StatsPublisher(
        path=tmp_path / "campaign-stats.json",
        enumerated=1,
        cache_resolved=0,
        executed=1,
        terminal=1,
        status_counts={"killed": 1, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0},
    )
    monkeypatch.setattr(publisher, "lock", lock)

    publisher.publish_complete()

    assert lock.entered == 1


def test_module_level_constants_and_defaults(tmp_path: Path) -> None:
    importlib.reload(stats)

    assert stats.STATS_SCHEMA_VERSION == 1
    assert stats.empty_stats(complete=False).total_cost == 0.0
    assert stats.empty_stats(complete=False).done_cost == 0.0
    # ``dict.fromkeys(STATUS_VALUES, 0)`` seeds every status at zero; a dropped ``0`` seeds ``None`` instead.
    assert stats.empty_stats(complete=False).status_counts == {
        "survived": 0,
        "killed": 0,
        "timeout": 0,
        "no_coverage": 0,
        "unsupported": 0,
    }
    assert len(stats.JsonValue.__value__.__args__) == 7

    publisher = stats.StatsPublisher(
        path=tmp_path / "campaign-stats.json",
        enumerated=0,
        cache_resolved=0,
        executed=0,
        status_counts={},
    )
    assert publisher.total_cost == 0.0
    assert publisher.terminal == 0
    assert publisher.done_cost == 0.0
    assert publisher.hang_weight == 0.0


def test_timeout_policy_rejects_nonpositive_base_with_a_specific_reason() -> None:
    assert timeouts.timeout_policy_error(0.0, 3.0, 1) == "the mutant timeout must be a finite positive number"
    assert timeouts.timeout_policy_error(float("nan"), 3.0, 1) == "the mutant timeout must be a finite positive number"


def test_timeout_policy_rejects_nonpositive_multiplier_with_a_specific_reason() -> None:
    assert timeouts.timeout_policy_error(8.0, 0.0, 1) == "the timeout retry multiplier must be a finite positive number"


def test_retry_timeouts_reject_a_zero_final_timeout() -> None:
    assert timeouts._retry_timeouts_are_finite(0.0, 3.0, 1) is False


def test_retry_timeouts_accept_a_small_positive_final_timeout() -> None:
    assert timeouts._retry_timeouts_are_finite(0.5, 1.0, 0) is True


def test_retry_timeouts_overflow_is_finite_false() -> None:
    assert timeouts._retry_timeouts_are_finite(2.0, 10.0, 1000) is False


def test_timeout_policy_overflow_is_sensitive_to_which_argument_is_the_base() -> None:
    """``base * multiplier**retries`` overflows with base=2, multiplier=10, retries=1000 (2 * 10**1000),
    but the swapped ``multiplier * base**retries`` (10 * 2**1000) stays finite: only the true base overflows."""
    assert (
        timeouts.timeout_policy_error(2.0, 10.0, 1000) == "the sequence of retry timeouts must stay finite and positive"
    )


def test_mutant_timeout_accounts_for_small_positive_costs() -> None:
    assert _close(timeouts.mutant_timeout_budget(0.5, ("test_x",), {"test_x": 0.5}), 2.0)


def test_mutant_timeout_defaults_slowest_to_zero_without_costs() -> None:
    assert _close(timeouts.mutant_timeout_budget(0.5, ("test_x",), {}), 1.0)


def test_timeout_calibration_constants() -> None:
    importlib.reload(timeouts)

    assert timeouts._CALIBRATED_TIMEOUT_MULTIPLIER == 2.0
    assert timeouts._CALIBRATED_TIMEOUT_HEADROOM_S == 1.0


class _RecordingStream(StringIO):
    """A stream that records writes and flushes (optionally a tty)."""

    def __init__(self, *, is_tty: bool = False) -> None:
        super().__init__()
        self._is_tty = is_tty
        self.written: list[str] = []
        self.flushed = 0

    @override
    def isatty(self) -> bool:
        return self._is_tty

    @override
    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    @override
    def flush(self) -> None:
        self.flushed += 1

    @override
    def getvalue(self) -> str:
        return "".join(self.written)


def test_stderr_returns_the_real_stream() -> None:
    assert _stderr() is sys.stderr


def test_progress_accepts_a_unit_compact_interval() -> None:
    progress = CampaignProgress(total=1, mode=ProgressMode.NONE, compact_interval=1.0)

    assert progress.effective_mode is ProgressMode.NONE


def test_tty_progress_writes_a_timeout_suffix_and_flushes() -> None:
    stream = _RecordingStream(is_tty=True)
    progress = CampaignProgress(total=2, mode=ProgressMode.TTY, stream=stream)

    progress.advance("src/x.py:1", MutantStatus.TIMEOUT)

    assert " (timeout)" in stream.getvalue()
    assert "1/2  src/x.py:1 (timeout)" in stream.getvalue()
    assert stream.flushed == 1


def test_compact_progress_uses_elapsed_since_last_emit() -> None:
    times = iter((2.0, 4.0))
    stream = StringIO()
    progress = CampaignProgress(
        total=2, mode=ProgressMode.COMPACT, stream=stream, compact_interval=5.0, clock=lambda: next(times)
    )

    progress.advance("src/x.py:1", MutantStatus.KILLED)
    progress.advance("src/x.py:2", MutantStatus.SURVIVED)

    assert stream.getvalue().splitlines() == ["progress: 1/2 src/x.py:1 killed"]


def test_compact_progress_flushes_after_each_emit() -> None:
    times = iter((0.0, 1.0))
    stream = _RecordingStream()
    progress = CampaignProgress(
        total=2, mode=ProgressMode.COMPACT, stream=stream, compact_interval=5.0, clock=lambda: next(times)
    )

    progress.advance("src/x.py:1", MutantStatus.KILLED)
    progress.advance("src/x.py:2", MutantStatus.SURVIVED)

    assert stream.flushed == 1


def test_progress_defaults_and_field_configuration() -> None:
    importlib.reload(progress_module)

    progress = progress_module.CampaignProgress(total=0, mode=progress_module.ProgressMode.NONE)

    assert progress.compact_interval == 5.0
    assert progress.done == 0


def test_progress_advance_holds_its_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _RecordingLock()
    progress = CampaignProgress(total=1, mode=ProgressMode.NONE)
    monkeypatch.setattr(progress, "lock", lock)

    progress.advance("src/x.py:1", MutantStatus.KILLED)

    assert lock.entered == 1
