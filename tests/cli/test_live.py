"""The live campaign dashboard (balamut.cli.live): an honest ETA and exact final counts."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import IO

import pytest

from balamut.cli import live
from balamut.engine.stats import CampaignStats

type Counts = dict[str, int]


def _stats(
    *,
    complete: bool,
    enumerated: int,
    executed: int,
    terminal: int,
    counts: Counts,
    total_cost: float = 0.0,
    done_cost: float = 0.0,
) -> CampaignStats:
    return CampaignStats(
        schema_version=1,
        complete=complete,
        enumerated=enumerated,
        cache_resolved=0,
        executed=executed,
        terminal=terminal,
        status_counts=counts,
        total_cost=total_cost,
        done_cost=done_cost,
    )


def _counts(**overrides: int) -> Counts:
    return {"killed": 0, "survived": 0, "timeout": 0, "unsupported": 0, "no_coverage": 0, **overrides}


def _fail_sleep(seconds: float) -> None:
    _ = seconds
    pytest.fail("should not sleep")


def _eta_seconds(rendered: str) -> int:
    eta = rendered[rendered.index("ETA ") + 4 :].split(" ", 1)[0]
    if eta == "-":
        return -1
    minutes, seconds = eta.split(":")
    return int(minutes) * 60 + int(seconds)


def test_eta_is_the_cost_fraction_of_work_not_the_mutant_count() -> None:
    stats = _stats(
        complete=False,
        enumerated=10,
        executed=8,
        terminal=2,
        counts=_counts(killed=2, no_coverage=2),
        total_cost=100.0,
        done_cost=25.0,
    )

    rendered = live.render(elapsed=15.0, stats=stats, top=[], last="src/x.py:1", hanging="-", work_start=5.0)

    assert _eta_seconds(rendered) == 30  # (15 - 5) * (100 - 25) / 25


def test_eta_reflects_cost_not_count() -> None:
    counts = _counts(killed=5)
    cheap = _stats(
        complete=False, enumerated=10, executed=10, terminal=5, counts=counts, total_cost=100.0, done_cost=10.0
    )
    dear = _stats(
        complete=False, enumerated=10, executed=10, terminal=5, counts=counts, total_cost=100.0, done_cost=50.0
    )

    cheap_eta = _eta_seconds(live.render(elapsed=20.0, stats=cheap, top=[], last="x", hanging="-", work_start=10.0))
    dear_eta = _eta_seconds(live.render(elapsed=20.0, stats=dear, top=[], last="x", hanging="-", work_start=10.0))

    assert (cheap_eta, dear_eta) == (90, 10)


def test_eta_is_a_dash_before_any_cost_completes() -> None:
    stats = _stats(
        complete=False,
        enumerated=5,
        executed=3,
        terminal=0,
        counts=_counts(no_coverage=2),
        total_cost=50.0,
        done_cost=0.0,
    )

    assert _eta_seconds(live.render(elapsed=10.0, stats=stats, top=[], last="-", hanging="-", work_start=None)) == -1


def test_a_complete_final_uses_the_snapshot_counts() -> None:
    stats = _stats(
        complete=True,
        enumerated=7,
        executed=4,
        terminal=4,
        counts=_counts(killed=2, survived=2, timeout=1, no_coverage=1, unsupported=1),
    )

    rendered = live.render_final(stats, elapsed=65.0, rc=0)

    assert "7/7" in rendered
    assert "timeout 1" in rendered
    assert "unsupported 1" in rendered
    assert "score 50%" in rendered
    assert "PARTIAL/UNKNOWN" not in rendered


def test_timeouts_and_runtime_unsupported_count_as_completed_work() -> None:
    stats = _stats(
        complete=False,
        enumerated=6,
        executed=4,
        terminal=4,
        counts=_counts(killed=1, survived=1, timeout=1, unsupported=1, no_coverage=2),
    )

    rendered = live.render(elapsed=10.0, stats=stats, top=[], last="src/x.py:10", hanging="-", work_start=None)

    assert "6/6 (100%)" in rendered
    assert "score 50%" in rendered


def test_a_provisional_timeout_is_not_counted_and_shows_as_hanging() -> None:
    stats = _stats(complete=False, enumerated=1, executed=1, terminal=0, counts=_counts())

    rendered = live.render(
        elapsed=10.0, stats=stats, top=[], last="src/x.py:10", hanging="src/x.py:10", work_start=None
    )

    assert "0/1 (0%)" in rendered
    assert "hanging now: src/x.py:10" in rendered


def test_an_incomplete_final_is_explicitly_partial() -> None:
    stats = _stats(
        complete=False,
        enumerated=7,
        executed=4,
        terminal=2,
        counts=_counts(killed=1, survived=1, unsupported=1, no_coverage=2),
    )

    rendered = live.render_final(stats, elapsed=65.0, rc=1)

    assert "PARTIAL/UNKNOWN" in rendered
    assert "5/7 observed" in rendered
    assert "DONE" not in rendered
    assert "score" not in rendered


def test_the_last_dispatch_and_hangs_are_read_from_the_engine_log() -> None:
    log = (
        "progress: 1/3 src/a.py:4 killed\n"
        "  src/b.py:9 stalled for 6s; killed at 8s\n"
        "progress: 2/3 src/c.py:1 survived\n"
    )

    assert live.latest_dispatch(log) == "src/c.py:1"
    assert live.hangs(log) == ["src/b.py:9"]


def test_read_log_replaces_invalid_utf8_bytes(tmp_path: Path) -> None:
    """``_read_log`` replaces undecodable bytes instead of raising (a dropped ``errors="replace"`` reads strict)."""
    path = tmp_path / "engine.log"
    path.write_bytes("café ".encode() + b"\xff\n")

    text = live._read_log(path)

    assert "café" in text
    assert "\ufffd" in text


class _CompletedProcess(subprocess.Popen[bytes]):
    pid = 12345
    returncode = 0

    def __init__(self) -> None:
        self._child_created = False

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return self.returncode


def _run_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object] | None
) -> tuple[int, list[str], Path]:
    command: list[str] = []
    stats_paths: list[Path] = []

    def fake_popen(argv: list[str], *, stdout: IO[str], stderr: int) -> _CompletedProcess:
        assert stderr == subprocess.STDOUT
        command.extend(argv)
        stats_path = Path(argv[argv.index("--stats-json") + 1])
        stats_paths.append(stats_path)
        if payload is not None:
            stats_path.write_text(json.dumps(payload), encoding="utf-8")
        stdout.write(f"cache: {tmp_path / 'cache.sqlite'}\nmutants: 1, daemons: auto x 1 workers\n")
        stdout.flush()
        return _CompletedProcess()

    monkeypatch.setattr(live.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    return_code = live.main(["src/x.py"])

    assert len(stats_paths) == 1
    return return_code, command, stats_paths[0]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"schema_version": 1},
        {
            "schema_version": 1,
            "complete": False,
            "enumerated": 1,
            "cache_resolved": 0,
            "executed": 1,
            "terminal": 0,
            "status_counts": {"killed": 0, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0},
        },
    ],
)
def test_missing_invalid_or_incomplete_stats_never_fabricate_an_exact_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object] | None,
) -> None:
    return_code, _command, _stats_path = _run_live(tmp_path, monkeypatch, payload)

    output = capsys.readouterr().out
    assert return_code == 0
    assert "PARTIAL/UNKNOWN" in output
    assert "DONE" not in output


def test_live_runs_the_engine_with_a_unique_stats_path_and_renders_the_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "complete": True,
        "enumerated": 1,
        "cache_resolved": 0,
        "executed": 1,
        "terminal": 1,
        "status_counts": {"killed": 1, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0},
    }

    return_code, command, stats_path = _run_live(tmp_path, monkeypatch, payload)

    output = capsys.readouterr().out
    assert return_code == 0
    assert command[1:4] == ["-m", "balamut", "mutate"]
    assert command.count("--stats-json") == 1
    assert stats_path.name.startswith("balamut-live-")
    assert "DONE" in output
    assert "1/1" in output
    assert "killed 1" in output
    assert "the engine runs as pid" in output
    assert "a snapshot every 5s" in output


def test_a_caller_cannot_pass_its_own_stats_path(capsys: pytest.CaptureFixture[str]) -> None:
    assert live.main(["src/x.py", "--stats-json", "x.json"]) == 2
    assert "--stats-json" in capsys.readouterr().err


def test_a_caller_cannot_pass_its_own_stats_path_with_equals_form(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(argv: list[str], *, stdout: IO[str], stderr: int) -> None:
        raise AssertionError("the guard must reject --stats-json= before spawning")

    monkeypatch.setattr(live.subprocess, "Popen", boom)
    assert live.main(["src/x.py", "--stats-json=x.json"]) == 2


def test_fmt_renders_minutes_and_seconds() -> None:
    assert live._fmt(60.0) == "1:00"
    assert live._fmt(65.0) == "1:05"
    assert live._fmt(59.0) == "0:59"


def test_render_defaults_missing_counts_to_zero() -> None:
    stats = CampaignStats(
        schema_version=1, complete=False, enumerated=0, cache_resolved=0, executed=0, terminal=0, status_counts={}
    )

    rendered = live.render(elapsed=0.0, stats=stats, top=[], last="-", hanging="-", work_start=None)

    assert "killed 0 . survived 0 . timeout 0" in rendered
    assert "unsupported 0 . no_cov 0" in rendered
    assert "(0%)" in rendered
    assert "score 0%" in rendered


def test_render_rate_is_zero_before_work_starts() -> None:
    stats = _stats(complete=False, enumerated=1, executed=0, terminal=2, counts=_counts(killed=1))

    rendered = live.render(elapsed=10.0, stats=stats, top=[], last="-", hanging="-", work_start=None)

    assert "0 mutants/min" in rendered
    assert "ETA -" in rendered


def test_render_score_uses_the_exact_percentage() -> None:
    # 1/3 * 100 == 33, while 1/3 * 101 rounds to 34: the multiplier must stay exactly 100.
    stats = _stats(complete=True, enumerated=3, executed=3, terminal=3, counts=_counts(killed=1, survived=2))

    rendered = live.render(elapsed=1.0, stats=stats, top=[], last="-", hanging="-", work_start=0.0)

    assert "score 33%" in rendered


def test_render_eta_is_a_dash_without_a_work_start() -> None:
    # A missing work start reads as zero elapsed work: the ETA must stay a dash, not report a phantom value.
    stats = _stats(
        complete=False,
        enumerated=10,
        executed=8,
        terminal=2,
        counts=_counts(killed=2),
        total_cost=10.0,
        done_cost=5.0,
    )

    rendered = live.render(elapsed=5.0, stats=stats, top=[], last="-", hanging="-", work_start=None)

    assert _eta_seconds(rendered) == -1


def test_render_clamps_negative_remaining_cost_to_zero() -> None:
    stats = _stats(
        complete=False,
        enumerated=5,
        executed=2,
        terminal=2,
        counts=_counts(killed=1),
        total_cost=3.0,
        done_cost=5.0,
    )

    rendered = live.render(elapsed=10.0, stats=stats, top=[], last="-", hanging="-", work_start=5.0)

    assert _eta_seconds(rendered) == 0


def test_eta_uses_done_cost_when_exactly_one_cost_completes() -> None:
    stats = _stats(
        complete=False,
        enumerated=10,
        executed=8,
        terminal=2,
        counts=_counts(killed=2),
        total_cost=10.0,
        done_cost=1.0,
    )

    rendered = live.render(elapsed=5.0, stats=stats, top=[], last="-", hanging="-", work_start=0.0)

    assert _eta_seconds(rendered) == 45


def test_eta_is_a_dash_when_no_cost_has_completed() -> None:
    stats = _stats(
        complete=False,
        enumerated=10,
        executed=8,
        terminal=2,
        counts=_counts(killed=2),
        total_cost=10.0,
        done_cost=0.0,
    )

    rendered = live.render(elapsed=5.0, stats=stats, top=[], last="-", hanging="-", work_start=0.0)

    assert _eta_seconds(rendered) == -1


def test_eta_is_a_dash_when_no_work_elapsed() -> None:
    stats = _stats(
        complete=False,
        enumerated=10,
        executed=8,
        terminal=2,
        counts=_counts(killed=2),
        total_cost=10.0,
        done_cost=5.0,
    )

    rendered = live.render(elapsed=5.0, stats=stats, top=[], last="-", hanging="-", work_start=5.0)

    assert _eta_seconds(rendered) == -1


def test_eta_uses_work_elapsed_even_when_under_a_second() -> None:
    stats = _stats(
        complete=False,
        enumerated=10,
        executed=8,
        terminal=2,
        counts=_counts(killed=2),
        total_cost=10.0,
        done_cost=5.0,
    )

    rendered = live.render(elapsed=5.5, stats=stats, top=[], last="-", hanging="-", work_start=5.0)

    assert _eta_seconds(rendered) != -1


def test_render_rate_is_terminals_per_work_minute() -> None:
    stats = _stats(complete=False, enumerated=2, executed=0, terminal=2, counts=_counts(killed=1))

    rendered = live.render(elapsed=10.0, stats=stats, top=[], last="-", hanging="-", work_start=8.0)

    assert "60 mutants/min" in rendered


def test_render_rate_counts_sub_second_work_elapsed() -> None:
    stats = _stats(complete=False, enumerated=2, executed=0, terminal=2, counts=_counts(killed=1))

    rendered = live.render(elapsed=8.5, stats=stats, top=[], last="-", hanging="-", work_start=8.0)

    assert "240 mutants/min" in rendered


def test_render_lists_files_gaining_survivors() -> None:
    stats = _stats(complete=False, enumerated=2, executed=0, terminal=2, counts=_counts(killed=1))
    top: list[tuple[str, int]] = [("src/a.py", 2), ("src/b.py", 3), ("src/c.py", 1), ("src/d.py", 9)]

    rendered = live.render(elapsed=10.0, stats=stats, top=top, last="-", hanging="-", work_start=None)

    assert "files gaining survivors" in rendered
    assert "3x b.py" in rendered
    assert "9x d.py" not in rendered


def test_render_final_score_zero_when_nothing_scored() -> None:
    stats = _stats(complete=True, enumerated=3, executed=3, terminal=3, counts=_counts(timeout=3))

    rendered = live.render_final(stats, elapsed=60.0, rc=0)

    assert "score 0%" in rendered


def test_render_final_score_uses_exact_percentage() -> None:
    stats = _stats(complete=True, enumerated=3, executed=3, terminal=3, counts=_counts(killed=1, survived=2))

    rendered = live.render_final(stats, elapsed=60.0, rc=0)

    assert "score 33%" in rendered


def test_survived_by_file_reads_verdict_counts_and_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class _FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, sql: str) -> _FakeConnection:
            calls["sql"] = sql
            return self

        def fetchall(self) -> list[tuple[str, int]]:
            return [("src/a.py", 2), ("src/b.py", 1)]

        def close(self) -> None:
            self.closed = True

    connection = _FakeConnection()

    def fake_connect(path: str, *, uri: bool, timeout: int) -> _FakeConnection:
        calls["path"] = path
        calls["uri"] = uri
        calls["timeout"] = timeout
        return connection

    monkeypatch.setattr(live.sqlite3, "connect", fake_connect)

    assert live.survived_by_file("/tmp/cache.sqlite") == {"src/a.py": 2, "src/b.py": 1}
    assert calls["uri"] is True
    assert calls["timeout"] == 5
    assert connection.closed is True


def test_survivor_delta_returns_files_that_gained_survivors() -> None:
    assert live.survivor_delta({"src/a.py": 3, "src/b.py": 1}, {"src/a.py": 1, "src/b.py": 2}) == [("src/a.py", 2)]


def test_survivor_delta_ignores_unchanged_files() -> None:
    assert live.survivor_delta({"src/a.py": 1}, {"src/a.py": 1}) == []


def test_survivor_delta_uses_zero_baseline_for_new_files() -> None:
    assert live.survivor_delta({"src/a.py": 1}, {}) == [("src/a.py", 1)]


def test_survivor_delta_caps_at_six_files() -> None:
    current = {f"src/f{i}.py": i + 1 for i in range(7)}

    gained = live.survivor_delta(current, {})

    assert len(gained) == 6
    assert gained[0] == ("src/f6.py", 7)


def test_main_advises_survivor_delta_with_current_then_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard passes the live advisory first and the startup baseline second (gained, not lost, survivors)."""
    spy = _SpyStdout()
    calls: list[tuple[dict[str, int], dict[str, int]]] = []
    state = {"n": 0}

    def advisory_fake(_cache: str) -> dict[str, int]:
        state["n"] += 1
        return {"src/a.py": state["n"]}

    def fake_survivor_delta(current: dict[str, int], baseline: dict[str, int]) -> list[tuple[str, int]]:
        calls.append((current, baseline))
        return []

    monkeypatch.setattr(live, "survivor_delta", fake_survivor_delta)
    monkeypatch.setattr(live, "_advisory", advisory_fake)
    monkeypatch.setattr(live.sys, "stdout", spy)

    def fake_sleep(_seconds: float) -> None:
        return None

    def fake_await_startup(
        _log: Path, _deadline: float, _proc: subprocess.Popen[bytes]
    ) -> tuple[str | None, int | None]:
        return "/tmp/cache.sqlite", 1

    def fake_read_log(_path: Path) -> str:
        return ""

    def fake_hangs(_text: str) -> list[str]:
        return []

    def fake_latest_dispatch(_text: str) -> str:
        return "-"

    monkeypatch.setattr(live.time, "sleep", fake_sleep)
    monkeypatch.setattr(live.time, "time", lambda: 1000.0)
    monkeypatch.setattr(live.time, "time_ns", lambda: 0)
    monkeypatch.setattr(live.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(live, "_await_startup", fake_await_startup)

    def read_stats_fake(_path: Path) -> CampaignStats:
        return _stats(complete=False, enumerated=1, executed=0, terminal=1, counts=_counts(killed=1))

    monkeypatch.setattr(live, "read_campaign_stats", read_stats_fake)
    monkeypatch.setattr(live, "_read_log", fake_read_log)
    monkeypatch.setattr(live, "hangs", fake_hangs)
    monkeypatch.setattr(live, "latest_dispatch", fake_latest_dispatch)

    proc = _PollProc([None, None, 0])

    def fake_popen(argv: list[str], *, stdout: IO[str], stderr: int) -> _PollProc:
        assert stderr == subprocess.STDOUT
        stdout.write("cache: /tmp/cache.sqlite\nmutants: 1\n")
        stdout.flush()
        return proc

    monkeypatch.setattr(live.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    live.main(["src/x.py"])

    assert calls
    assert calls[0] == ({"src/a.py": 2}, {"src/a.py": 1})


def test_advisory_returns_survivors_by_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def survivors_fake(cache: str) -> dict[str, int]:
        return {"src/a.py": 1}

    monkeypatch.setattr(live, "survived_by_file", survivors_fake)
    assert live._advisory("c.sqlite") == {"src/a.py": 1}


def test_advisory_returns_empty_on_sqlite_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cache: str) -> dict[str, int]:
        raise sqlite3.Error("nope")

    monkeypatch.setattr(live, "survived_by_file", boom)
    assert live._advisory("c.sqlite") == {}


class _RunningProc(subprocess.Popen[bytes]):
    pid = 999

    def __init__(self) -> None:
        self._child_created = False

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return 0


class _ExitingProc(subprocess.Popen[bytes]):
    pid = 999

    def __init__(self, after: int) -> None:
        self._child_created = False
        self._after = after
        self.calls = 0

    def poll(self) -> int | None:
        self.calls += 1
        return None if self.calls < self._after else 0

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return 0


def test_await_startup_returns_cache_and_total(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "engine.log"
    log.write_text("cache: /tmp/c.sqlite\nmutants: 42\n", encoding="utf-8")
    monkeypatch.setattr(live.time, "time", lambda: 0.0)
    monkeypatch.setattr(live.time, "sleep", _fail_sleep)
    proc = _RunningProc()

    cache, total = live._await_startup(log, deadline=1e18, proc=proc)

    assert cache == "/tmp/c.sqlite"
    assert total == 42


def test_await_startup_waits_for_both_cache_and_total(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "engine.log"
    log.write_text("cache: /tmp/c.sqlite\n", encoding="utf-8")
    sleeps: list[float] = []
    monkeypatch.setattr(live.time, "time", lambda: 0.0)
    monkeypatch.setattr(live.time, "sleep", sleeps.append)
    proc = _ExitingProc(after=2)

    cache, total = live._await_startup(log, deadline=1e18, proc=proc)

    assert cache == "/tmp/c.sqlite"
    assert total is None
    assert sleeps == [1]


def test_await_startup_returns_when_process_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "engine.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(live.time, "time", lambda: 0.0)
    monkeypatch.setattr(live.time, "sleep", _fail_sleep)
    proc = _CompletedProcess()

    cache, total = live._await_startup(log, deadline=1e18, proc=proc)

    assert (cache, total) == (None, None)


def test_await_startup_returns_at_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "engine.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(live.time, "time", lambda: 5.0)
    monkeypatch.setattr(live.time, "sleep", _fail_sleep)
    proc = _RunningProc()

    cache, total = live._await_startup(log, deadline=5.0, proc=proc)

    assert (cache, total) == (None, None)


def test_await_startup_sleeps_between_polls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "engine.log"
    log.write_text("", encoding="utf-8")
    sleeps: list[float] = []
    monkeypatch.setattr(live.time, "time", lambda: 0.0)
    monkeypatch.setattr(live.time, "sleep", sleeps.append)
    proc = _ExitingProc(after=3)

    cache, total = live._await_startup(log, deadline=1e18, proc=proc)

    assert (cache, total) == (None, None)
    assert sleeps == [1, 1]


class _SpyStdout:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.flushes = 0

    def write(self, text: str) -> None:
        self.parts.append(text)

    def flush(self) -> None:
        self.flushes += 1

    def isatty(self) -> bool:
        return False

    @property
    def text(self) -> str:
        return "".join(self.parts)


class _PollProc:
    pid = 4242

    def __init__(self, polls: list[int | None], returncode: int = 0) -> None:
        self._polls = polls
        self._returncode = returncode
        self.calls = 0

    def poll(self) -> int | None:
        value = self._polls[min(self.calls, len(self._polls) - 1)]
        self.calls += 1
        return value

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return self._returncode


def _run_live_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    polls: list[int | None],
    stats: list[CampaignStats | None],
    startup: str,
    times: list[float] | None = None,
) -> tuple[_SpyStdout, list[float]]:
    spy = _SpyStdout()
    sleeps: list[float] = []
    monkeypatch.setattr(live.sys, "stdout", spy)
    monkeypatch.setattr(live.time, "sleep", sleeps.append)
    clock = iter(times) if times is not None else None
    monkeypatch.setattr(live.time, "time", lambda: next(clock, 1000.0) if clock else 1000.0)
    monkeypatch.setattr(live.time, "time_ns", lambda: 0)
    # `time_ns` is patched to a constant, so every test would share one `balamut-live-0.log` in the real temp
    # dir; forkserver workers run tests concurrently and would race on it. Pin the log into the test's own dir.
    monkeypatch.setattr(live.tempfile, "gettempdir", lambda: str(tmp_path))

    def await_fake(log: Path, deadline: float, proc: subprocess.Popen[bytes]) -> tuple[str | None, int | None]:
        return "/tmp/cache.sqlite", 1

    def advisory_fake(cache: str) -> dict[str, int]:
        return {}

    monkeypatch.setattr(live, "_await_startup", await_fake)
    monkeypatch.setattr(live, "_advisory", advisory_fake)
    stats_iter = iter(stats)

    def read_stats_fake(path: Path) -> CampaignStats | None:
        return next(stats_iter, None)

    monkeypatch.setattr(live, "read_campaign_stats", read_stats_fake)
    proc = _PollProc(polls)

    def fake_popen(argv: list[str], *, stdout: IO[str], stderr: int) -> _PollProc:
        assert stderr == subprocess.STDOUT
        stdout.write(startup)
        stdout.flush()
        return proc

    monkeypatch.setattr(live.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)
    live.main(["src/x.py"])
    return spy, sleeps


def test_live_loop_skips_missing_and_mismatched_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mismatch = _stats(complete=False, enumerated=7, executed=0, terminal=0, counts=_counts(killed=0))
    ok = _stats(complete=False, enumerated=1, executed=0, terminal=1, counts=_counts(killed=1))

    spy, sleeps = _run_live_loop(
        tmp_path,
        monkeypatch,
        polls=[None, None, None, 0],
        stats=[None, mismatch, ok],
        startup="cache: /tmp/cache.sqlite\nmutants: 1\n",
    )

    assert "the engine runs as pid" in spy.text
    assert "a snapshot every 5s" in spy.text
    assert "=== balamut live" in spy.text
    assert "elapsed 0:00" in spy.text
    assert "7/7" not in spy.text
    assert spy.flushes == 3
    assert sleeps == [5, 5, 5]


def test_live_loop_sets_work_start_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _stats(
        complete=False, enumerated=1, executed=0, terminal=1, counts=_counts(killed=1), total_cost=100.0, done_cost=10.0
    )
    second = _stats(
        complete=False, enumerated=1, executed=0, terminal=1, counts=_counts(killed=1), total_cost=100.0, done_cost=10.0
    )

    spy, _sleeps = _run_live_loop(
        tmp_path,
        monkeypatch,
        polls=[None, None, 0],
        stats=[first, second],
        startup="cache: /tmp/cache.sqlite\nmutants: 1\n",
        times=[1000.0, 1005.0, 1015.0, 1016.0],
    )

    assert "ETA 1:30" in spy.text


def test_live_loop_waits_for_first_terminal_before_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _stats(
        complete=False, enumerated=1, executed=0, terminal=0, counts=_counts(), total_cost=100.0, done_cost=10.0
    )
    after = _stats(
        complete=False, enumerated=1, executed=0, terminal=1, counts=_counts(killed=1), total_cost=100.0, done_cost=10.0
    )

    spy, _sleeps = _run_live_loop(
        tmp_path,
        monkeypatch,
        polls=[None, None, 0],
        stats=[before, after],
        startup="cache: /tmp/cache.sqlite\nmutants: 1\n",
        times=[1000.0, 1005.0, 1015.0, 1016.0],
    )

    assert "ETA 1:30" not in spy.text


def test_live_loop_renders_the_most_recent_hang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _stats(complete=False, enumerated=1, executed=0, terminal=1, counts=_counts(killed=1))

    spy, _sleeps = _run_live_loop(
        tmp_path,
        monkeypatch,
        polls=[None, None, 0],
        stats=[ok],
        startup="src/a.py:1 stalled for 6s\nsrc/b.py:2 stalled for 7s\n",
    )

    assert "hanging now: src/b.py:2" in spy.text


def test_live_loop_reads_hangs_from_the_start_of_the_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _stats(complete=False, enumerated=1, executed=0, terminal=1, counts=_counts(killed=1))

    spy, _sleeps = _run_live_loop(
        tmp_path,
        monkeypatch,
        polls=[None, None, 0],
        stats=[ok],
        startup="src/a.py:1 stalled for 6s\n",
    )

    assert "hanging now: src/a.py:1" in spy.text


def test_live_without_a_cache_line_runs_without_a_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command: list[str] = []

    def fake_popen(argv: list[str], *, stdout: IO[str], stderr: int) -> _CompletedProcess:
        assert stderr == subprocess.STDOUT
        command.extend(argv)
        stdout.write("mutants: 5\n")
        stdout.flush()
        return _CompletedProcess()

    monkeypatch.setattr(live.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    return_code = live.main(["src/x.py"])

    assert return_code == 0
    assert "no startup lines" in capsys.readouterr().out


def test_live_without_a_mutants_line_runs_without_a_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_popen(argv: list[str], *, stdout: IO[str], stderr: int) -> _CompletedProcess:
        assert stderr == subprocess.STDOUT
        stdout.write("cache: /tmp/cache.sqlite\n")
        stdout.flush()
        return _CompletedProcess()

    monkeypatch.setattr(live.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    return_code = live.main(["src/x.py"])

    assert return_code == 0
    assert "no startup lines" in capsys.readouterr().out


def test_live_discards_final_when_enumerated_differs_from_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "complete": True,
        "enumerated": 3,
        "cache_resolved": 0,
        "executed": 3,
        "terminal": 3,
        "status_counts": {"killed": 1, "survived": 0, "timeout": 0, "no_coverage": 2, "unsupported": 0},
    }

    return_code, _command, _stats_path = _run_live(tmp_path, monkeypatch, payload)

    output = capsys.readouterr().out
    assert return_code == 0
    assert "PARTIAL/UNKNOWN" in output
    assert "DONE" not in output


def test_live_reports_elapsed_from_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(live.time, "time", lambda: 1000.0)
    monkeypatch.setattr(live.time, "time_ns", lambda: 0)

    def fake_popen(argv: list[str], *, stdout: IO[str], stderr: int) -> _CompletedProcess:
        assert stderr == subprocess.STDOUT
        stdout.write("cache: /tmp/cache.sqlite\nmutants: 1\n")
        stdout.flush()
        return _CompletedProcess()

    monkeypatch.setattr(live.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    return_code = live.main(["src/x.py"])

    output = capsys.readouterr().out
    assert return_code == 0
    assert "0:00" in output


def test_main_passes_a_startup_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, float] = {}

    def capture_await_startup(log: Path, deadline: float, proc: object) -> tuple[None, None]:
        captured["deadline"] = deadline
        return None, None

    monkeypatch.setattr(live, "_await_startup", capture_await_startup)
    monkeypatch.setattr(live.time, "time", lambda: 1000.0)
    monkeypatch.setattr(live.time, "time_ns", lambda: 0)

    def fake_popen(argv: list[str], *, stdout: IO[str], stderr: int) -> _CompletedProcess:
        assert stderr == subprocess.STDOUT
        return _CompletedProcess()

    monkeypatch.setattr(live.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    live.main(["src/x.py"])

    assert captured["deadline"] == 1120.0
