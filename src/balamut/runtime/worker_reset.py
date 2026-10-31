"""Reset the safe part of a persistent worker's process state at a mutant boundary.

A persistent worker reuses one process for many mutants, so global state leaks between them where a fresh
fork would reset it. Only caches are reverted:

- ``@lru_cache`` wrappers: a cache would otherwise return a previous mutant's values (directly or through the
  mutated function), bypassing the code restore. ``cache_clear`` only empties a dict, which is safe.

``sys.modules`` is never touched. Re-importing a C extension can run ``PyInit_`` again over process-global
state and crash; partially unloading a pure-Python graph breaks identity (a re-imported module may attach the
old singleton, router or registry from a module that stayed). The remaining state is controlled by touch and
paired confirmation: a kill whose mutated code really ran is confirmed by the original passing in the same
process state, and a kill or survivor without a touch is re-run in a fresh fork on a clean database.

Residual limits (only a fresh fork closes them):

- a test rebinding a module-level name of an already loaded module (a counter, a registry, a singleton) or
  mutating such an object in place. A ``__dict__`` snapshot/restore would fix it but costs about 40% of wall
  time per mutant, for a vector that has not been observed to change a verdict;
- a test mutating process-global state of a C extension (pools, terminal) — session fixtures, in practice.
"""

from __future__ import annotations

import gc
import sys
from collections.abc import Callable
from typing import NamedTuple

from balamut.runtime.inmemory import LRU_CACHE_WRAPPER


class WorkerBaseline(NamedTuple):
    """The worker's state before its first mutant (after collection): the reference for every boundary.

    ``modules`` are the ``sys.modules`` keys at start (a target module outside them was imported lazily and
    the mutant is UNSUPPORTED). ``cache_clears`` are the ``cache_clear`` callbacks of all ``@lru_cache``
    wrappers."""

    modules: frozenset[str]
    cache_clears: tuple[Callable[[], None], ...]


def capture_baseline() -> WorkerBaseline:
    """Snapshot the module set and every ``@lru_cache`` wrapper of the process (through ``gc``, which finds
    module, class and nested wrappers at once). Called once, before the first mutant is applied."""
    cache_clears = [obj.cache_clear for obj in gc.get_objects() if isinstance(obj, LRU_CACHE_WRAPPER)]
    return WorkerBaseline(frozenset(sys.modules), tuple(cache_clears))


def reset_to_baseline(baseline: WorkerBaseline) -> None:
    """Clear the baseline ``@lru_cache`` wrappers without changing the import graph."""
    for cache_clear in baseline.cache_clears:
        cache_clear()
