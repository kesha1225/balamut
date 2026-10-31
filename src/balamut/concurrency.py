"""Schedule-fuzz and PCT runtime for concurrency mutants; also the public probe API for tests.

``inject_yields`` wraps the await boundaries of a target function in ``await balamut_fuzz_yield(E, site)``. The
plugin injects ``balamut_fuzz_yield``, ``balamut_fuzz_point`` and ``nullcontext`` into the mutated module's
globals. ``FuzzSchedule(seed)`` decides deterministically whether to yield at a site, forcing event-loop
switches and amplifying interleavings. ``run_under_fuzz`` runs N concurrent tasks under K seeds and checks an
invariant: it is the killer a probe test calls. Helper names carry no leading double underscore, which would
be name-mangled inside a method body and break the globals lookup.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field

type FuzzBuild[T] = Callable[[], Awaitable[T]]  # sets up a fresh scene
type FuzzOp[T] = Callable[[T], Awaitable[object]]  # one concurrent operation on the scene
type FuzzInvariant[T] = Callable[[T], bool]  # the invariant after the race (True = holds)
type FuzzGlobals = dict[str, object]
type PctPriority = dict[int, int]  # task id -> priority
type PctChangePoints = dict[int, int]  # step k_i -> priority i
type PctWaiters = dict[int, asyncio.Event]  # tasks waiting for their turn -> their event

FUZZ_YIELD_NAME = "balamut_fuzz_yield"
FUZZ_POINT_NAME = "balamut_fuzz_point"


@dataclass(frozen=True, slots=True)
class FuzzSchedule:
    """A deterministic yield schedule derived from a seed: at which sites to switch tasks."""

    seed: int

    def should_yield(self, site: int) -> bool:
        digest = hashlib.sha256(f"{self.seed}:{site}".encode()).digest()
        return digest[0] % 2 == 0  # about half of the sites, deterministic per (seed, site)


_FUZZ: ContextVar[FuzzSchedule | None] = ContextVar("balamut_fuzz_schedule", default=None)


async def fuzz_point(site: int) -> None:
    """A scheduling point: under an active ``FuzzSchedule`` it yields the event loop."""
    schedule = _FUZZ.get()
    if schedule is not None and schedule.should_yield(site):
        await asyncio.sleep(0)


async def fuzz_yield[T](coro: Awaitable[T], site: int) -> T:
    """The wrapper of an instrumented await: a scheduling point, then the original await."""
    await fuzz_point(site)
    return await coro


def fuzz_globals() -> FuzzGlobals:
    """Helpers the plugin injects into the mutated module's globals for concurrency mutants."""
    return {FUZZ_YIELD_NAME: fuzz_yield, FUZZ_POINT_NAME: fuzz_point, "nullcontext": nullcontext}


async def run_under_fuzz[T](
    *, seeds: list[int], build: FuzzBuild[T], op: FuzzOp[T], tasks: int, invariant: FuzzInvariant[T]
) -> None:
    """For every seed: a fresh ``build()``, ``tasks`` concurrent ``op()`` calls under ``FuzzSchedule(seed)``, then
    the invariant. Any seed breaking it raises ``AssertionError`` (the mutant is killed); correct (locked) code
    holds under all seeds."""
    for seed in seeds:
        ctx = await build()
        token = _FUZZ.set(FuzzSchedule(seed))
        try:
            await asyncio.gather(*[op(ctx) for _ in range(tasks)])
        finally:
            _FUZZ.reset(token)
        if not invariant(ctx):
            msg = f"concurrency invariant broken under seed={seed}"
            raise AssertionError(msg)


# PCT (Burckhardt et al., ASPLOS 2010): a controlled scheduler with a probabilistic guarantee. A coin flip per
# site finds a depth-d bug with a probability exponentially small in the run length; PCT assigns n starting
# priorities and d-1 change points, finding a depth-d bug with probability >= 1/(n * k^(d-1)), exponential only
# in d. Bug depth is the minimum number of ordering constraints (an ordering violation is 1, atomicity 2).
# Exactly one task runs between yield points; the turn goes to the highest-priority waiter; a change point
# drops a task's priority below every starting one. asyncio runs on one OS thread, so state needs no lock.

_PCT_TASK: ContextVar[int] = ContextVar("balamut_pct_task", default=-1)


@dataclass(slots=True)
class PctSchedule:
    """The PCT schedule of one run: task priorities and change points (step k_i -> priority i)."""

    priority: PctPriority
    change_points: PctChangePoints
    step: int = 0
    active: int | None = None
    waiting: PctWaiters = field(default_factory=dict[int, asyncio.Event])

    def grant(self) -> None:
        """Hand the turn to the highest-priority waiter when nobody is active."""
        if self.active is not None or not self.waiting:
            return
        tid = max(self.waiting, key=lambda t: self.priority[t])
        self.active = tid
        self.waiting.pop(tid).set()

    def register(self, tid: int) -> asyncio.Event:
        event = asyncio.Event()
        self.waiting[tid] = event
        return event

    async def yield_point(self, tid: int) -> None:
        self.step += 1
        new_priority = self.change_points.get(self.step)
        if new_priority is not None:
            self.priority[tid] = new_priority
        self.active = None
        event = self.register(tid)
        self.grant()
        await event.wait()

    def leave(self, _tid: int) -> None:
        self.active = None
        self.grant()


_PCT: ContextVar[PctSchedule | None] = ContextVar("balamut_pct_schedule", default=None)


def _hashed(seed: int, label: str, modulo: int) -> int:
    """A deterministic pseudo-random number derived with hashlib (no ``random`` module)."""
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % modulo


def _make_pct(num_tasks: int, seed: int, depth: int, steps_hint: int) -> PctSchedule:
    # Starting priorities {depth .. depth+n-1}, permuted by the seed (Fisher-Yates over the hash stream); they are
    # above the change-point values 1..d-1 that drop a priority.
    priority_values = list(range(depth, depth + num_tasks))
    for i in range(num_tasks - 1, 0, -1):
        j = _hashed(seed, f"perm:{i}", i + 1)
        priority_values[i], priority_values[j] = priority_values[j], priority_values[i]
    priority = {tid: priority_values[tid] for tid in range(num_tasks)}
    change_points = {1 + _hashed(seed, f"cp:{i}", max(1, steps_hint)): i for i in range(1, depth)}
    return PctSchedule(priority=priority, change_points=change_points)


async def pct_point(_site: int) -> None:
    """A scheduling point under an active PCT schedule: hands the turn over by priority (steps are counted)."""
    schedule = _PCT.get()
    if schedule is not None:
        await schedule.yield_point(_PCT_TASK.get())


async def run_under_pct[T](
    *,
    seeds: list[int],
    build: FuzzBuild[T],
    op: FuzzOp[T],
    tasks: int,
    invariant: FuzzInvariant[T],
    depth: int = 2,
    steps_hint: int = 20,
) -> None:
    """For every seed: a fresh scene, ``tasks`` concurrent ``op`` calls under a PCT schedule (``op`` calls
    ``pct_point`` at its yield points). A broken invariant raises ``AssertionError``: a bug of depth <= ``depth``.
    The first turn is granted after a barrier (everyone registered), then reactively, one relinquish at a time."""
    for seed in seeds:
        ctx = await build()
        schedule = _make_pct(tasks, seed, depth, steps_hint)
        token = _PCT.set(schedule)
        start = asyncio.Barrier(tasks)

        async def _runner(tid: int, sched: PctSchedule, barrier: asyncio.Barrier, scene: T) -> None:
            task_token = _PCT_TASK.set(tid)
            try:
                event = sched.register(tid)
                await barrier.wait()
                sched.grant()  # idempotent: a no-op once a task is active
                await event.wait()
                await op(scene)
            finally:
                sched.leave(tid)
                _PCT_TASK.reset(task_token)

        try:
            await asyncio.gather(*[_runner(tid, schedule, start, ctx) for tid in range(tasks)])
        finally:
            _PCT.reset(token)
        if not invariant(ctx):
            msg = f"PCT: invariant broken under seed={seed} (a bug of depth <= {depth})"
            raise AssertionError(msg)
