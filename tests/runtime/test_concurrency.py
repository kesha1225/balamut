"""Schedule-fuzz and PCT probes (balamut.concurrency).

``run_under_fuzz`` kills a lock-less check-then-act (a lost update under some seed) and keeps the invariant
of the locked variant under every seed: exactly what a probe test needs to kill a concurrency mutant. PCT
(Burckhardt et al., ASPLOS 2010) finds a bug of depth d with probability at least 1/(n * k^(d-1)) per run.
"""

from __future__ import annotations

import asyncio
import importlib
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

import pytest

from balamut import concurrency
from balamut.concurrency import (
    _FUZZ,
    _PCT,
    _PCT_TASK,
    FuzzSchedule,
    PctSchedule,
    _hashed,
    _make_pct,
    fuzz_globals,
    fuzz_point,
    fuzz_yield,
    pct_point,
    run_under_fuzz,
    run_under_pct,
)


@dataclass
class _Counter:
    value: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def _build() -> _Counter:
    return _Counter()


async def _op_unlocked(ctx: _Counter) -> None:
    seen = ctx.value
    await fuzz_point(1)  # a yield point between read and write: a lost update without the lock
    ctx.value = seen + 1


async def _op_locked(ctx: _Counter) -> None:
    async with ctx.lock:
        await fuzz_point(1)
        ctx.value += 1


def test_fuzz_schedule_deterministic() -> None:
    assert FuzzSchedule(7).should_yield(1) == FuzzSchedule(7).should_yield(1)


def test_run_under_fuzz_kills_unlocked_holds_locked() -> None:
    seeds = list(range(16))
    asyncio.run(run_under_fuzz(seeds=seeds, build=_build, op=_op_locked, tasks=4, invariant=lambda c: c.value == 4))
    with pytest.raises(AssertionError):
        asyncio.run(
            run_under_fuzz(seeds=seeds, build=_build, op=_op_unlocked, tasks=4, invariant=lambda c: c.value == 4)
        )


def test_fuzz_globals_names_match_the_injected_helpers() -> None:
    names = set(fuzz_globals())
    assert names == {"balamut_fuzz_yield", "balamut_fuzz_point", "nullcontext"}


_SEEDS = list(range(64))


async def _fresh() -> list[int]:
    return [0]


async def _rmw_non_atomic(box: list[int]) -> None:
    tmp = box[0]
    await pct_point(0)
    box[0] = tmp + 1


async def _atomic_inc(box: list[int]) -> None:
    await pct_point(0)
    box[0] += 1


def _two_incremented(box: list[int]) -> bool:
    return box[0] == 2


def test_pct_finds_depth2_atomicity_violation() -> None:
    with pytest.raises(AssertionError):
        asyncio.run(
            run_under_pct(
                seeds=_SEEDS,
                build=_fresh,
                op=_rmw_non_atomic,
                tasks=2,
                invariant=_two_incremented,
                depth=2,
                steps_hint=2,
            )
        )


def test_pct_green_on_atomic_operation() -> None:
    asyncio.run(
        run_under_pct(
            seeds=_SEEDS, build=_fresh, op=_atomic_inc, tasks=2, invariant=_two_incremented, depth=2, steps_hint=2
        )
    )


def test_fuzz_schedule_should_yield_is_exact() -> None:
    assert FuzzSchedule(0).should_yield(0) is True
    assert FuzzSchedule(1).should_yield(1) is True
    assert FuzzSchedule(7).should_yield(1) is False
    assert FuzzSchedule(7).should_yield(2) is False
    assert FuzzSchedule(5).should_yield(7) is False
    assert FuzzSchedule(2).should_yield(3) is True


def test_fuzz_point_is_a_noop_without_a_schedule() -> None:
    asyncio.run(fuzz_point(1))


def test_run_under_fuzz_restores_the_schedule_context() -> None:
    asyncio.run(run_under_fuzz(seeds=[1], build=_build, op=_op_locked, tasks=2, invariant=lambda c: c.value == 2))
    assert _FUZZ.get() is None


def test_run_under_fuzz_resets_the_schedule_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class _CountingVar:
        def __init__(self) -> None:
            self._cv: ContextVar[FuzzSchedule | None] = ContextVar("fuzz_counting", default=None)
            self.reset_count = 0

        def get(self) -> FuzzSchedule | None:
            return self._cv.get()

        def set(self, value: FuzzSchedule | None) -> Token[FuzzSchedule | None]:
            return self._cv.set(value)

        def reset(self, token: Token[FuzzSchedule | None]) -> None:
            self.reset_count += 1
            self._cv.reset(token)

    spy = _CountingVar()
    monkeypatch.setattr(concurrency, "_FUZZ", spy)
    asyncio.run(run_under_fuzz(seeds=[1], build=_build, op=_op_locked, tasks=2, invariant=lambda c: c.value == 2))
    assert spy.reset_count == 1


def test_run_under_pct_restores_the_schedule_context() -> None:
    asyncio.run(
        run_under_pct(
            seeds=[0], build=_fresh, op=_atomic_inc, tasks=2, invariant=_two_incremented, depth=2, steps_hint=2
        )
    )
    assert _PCT.get() is None


def test_run_under_pct_resets_the_schedule_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class _CountingVar:
        def __init__(self) -> None:
            self._cv: ContextVar[PctSchedule | None] = ContextVar("pct_counting", default=None)
            self.reset_count = 0

        def get(self) -> PctSchedule | None:
            return self._cv.get()

        def set(self, value: PctSchedule | None) -> Token[PctSchedule | None]:
            return self._cv.set(value)

        def reset(self, token: Token[PctSchedule | None]) -> None:
            self.reset_count += 1
            self._cv.reset(token)

    spy = _CountingVar()
    monkeypatch.setattr(concurrency, "_PCT", spy)
    asyncio.run(
        run_under_pct(
            seeds=[0], build=_fresh, op=_atomic_inc, tasks=2, invariant=_two_incremented, depth=2, steps_hint=2
        )
    )
    assert spy.reset_count == 1


def test_pct_task_context_default_is_minus_one() -> None:
    assert _PCT_TASK.get() == -1


def test_module_contextvar_defaults_are_exact() -> None:
    # Module-level ContextVar defaults run only at import; a reload inside the test body re-executes
    # them so the coverage substrate attributes those lines to this test. The ContextVars are bound by
    # the async helpers, so their identity is restored afterwards.
    fuzz, pct_task, pct = concurrency._FUZZ, concurrency._PCT_TASK, concurrency._PCT
    try:
        importlib.reload(concurrency)
        assert concurrency._FUZZ.get() is None
        assert concurrency._PCT_TASK.get() == -1
        assert concurrency._PCT.get() is None
    finally:
        concurrency._FUZZ, concurrency._PCT_TASK, concurrency._PCT = fuzz, pct_task, pct


def test_pct_runner_resets_the_task_context(monkeypatch: pytest.MonkeyPatch) -> None:
    class _CountingTaskVar:
        def __init__(self) -> None:
            self._cv = ContextVar("pct_task_counting", default=-1)
            self.reset_count = 0

        def get(self) -> int:
            return self._cv.get()

        def set(self, value: int) -> Token[int]:
            return self._cv.set(value)

        def reset(self, token: Token[int]) -> None:
            self.reset_count += 1
            self._cv.reset(token)

    spy = _CountingTaskVar()
    monkeypatch.setattr(concurrency, "_PCT_TASK", spy)
    asyncio.run(
        run_under_pct(
            seeds=[0], build=_fresh, op=_atomic_inc, tasks=2, invariant=_two_incremented, depth=2, steps_hint=2
        )
    )
    assert spy.reset_count == 2


def test_pct_schedule_starts_at_step_zero() -> None:
    assert PctSchedule(priority={}, change_points={}).step == 0


def test_pct_yield_point_increments_step_by_one() -> None:
    sched = PctSchedule(priority={0: 2, 1: 3}, change_points={})

    async def go() -> None:
        await sched.yield_point(0)

    asyncio.run(go())
    assert sched.step == 1


def test_hashed_is_deterministic_and_exact() -> None:
    assert _hashed(0, "x", 10) == 4
    assert _hashed(1, "perm:1", 2) == 1


def test_make_pct_builds_the_expected_schedule() -> None:
    two = _make_pct(2, 0, 2, 2)
    assert two.priority == {0: 3, 1: 2}
    assert two.change_points == {1: 1}

    three = _make_pct(3, 0, 2, 2)
    assert three.priority == {0: 3, 1: 4, 2: 2}
    assert three.change_points == {1: 1}

    seed2 = _make_pct(2, 2, 2, 2)
    assert seed2.change_points == {2: 1}

    seed2_one_hint = _make_pct(2, 2, 2, 1)
    assert seed2_one_hint.change_points == {1: 1}


def test_run_under_pct_passes_schedule_arguments_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    real = concurrency._make_pct
    calls: list[tuple[int, int, int, int]] = []

    def spy(num_tasks: int, seed: int, depth: int, steps_hint: int) -> PctSchedule:
        calls.append((num_tasks, seed, depth, steps_hint))
        return real(num_tasks, seed, depth, steps_hint)

    monkeypatch.setattr(concurrency, "_make_pct", spy)
    asyncio.run(
        run_under_pct(
            seeds=[0], build=_fresh, op=_atomic_inc, tasks=2, invariant=_two_incremented, depth=3, steps_hint=7
        )
    )
    assert calls == [(2, 0, 3, 7)]


def test_fuzz_yield_calls_the_scheduling_point_then_awaits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def spy_point(site: int) -> None:
        calls.append(site)

    monkeypatch.setattr(concurrency, "fuzz_point", spy_point)

    async def coro() -> int:
        return 42

    assert asyncio.run(fuzz_yield(coro(), 7)) == 42
    assert calls == [7]


def test_pct_serializes_op_execution() -> None:
    state: dict[str, int] = {"inflight": 0, "overlap": 0}

    async def build() -> dict[str, int]:
        return state

    async def op(scene: dict[str, int]) -> None:
        scene["inflight"] += 1
        if scene["inflight"] > 1:
            scene["overlap"] += 1
        await asyncio.sleep(0)
        scene["inflight"] -= 1

    def invariant(scene: dict[str, int]) -> bool:
        return scene["overlap"] == 0

    asyncio.run(run_under_pct(seeds=[0], build=build, op=op, tasks=2, invariant=invariant, depth=1, steps_hint=2))
