"""Reloads a test runs in a persistent worker, undone at the next mutant boundary.

``importlib.reload`` executes a module body again into the module's own namespace: every function and class name is
bound to a new object, while each module that imported a name (``from module import f``) keeps the object it got. A
mutant is applied to the definition the module binds when its lease starts. In a worker that runs many leases the two
drift apart after a reload: a later lease edits the new object while the tests call the old one, and the verdict
depends on which tests the worker ran before.

The worker replaces ``importlib.reload`` with ``ReloadLog.reload`` before collection. Each reload is recorded with
the namespace before and after it. At the next lease boundary ``undo`` puts back every binding a reload made that
nothing rebound since, newest reload first, so each lease starts from the definitions the worker was forked with.
During a lease, a reload hands the module to the follower: the active mutant applies its edit to the definition the
reload bound, as a reload of an edited file would.

The record and its operations are closures created once, when the hook is installed. When balamut tests itself, the
worker's own modules are mutation targets: a method is looked up at call time and can run a live mutant's code, a
closure keeps the code it was created with.
"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import NamedTuple, cast

type Follower = Callable[[ModuleType], None]
type _Reload = tuple[ModuleType, dict[str, object], dict[str, object]]  # (module, namespace before, after)

_ABSENT = object()


class ReloadLog(NamedTuple):
    """The reload hook of a worker and the operations on what it recorded."""

    reload: Callable[[object], ModuleType]  # installed as ``importlib.reload``
    undo: Callable[[], None]  # put back the bindings of the recorded reloads
    forget: Callable[[], None]  # drop the record: the current state is the reference
    follow: Callable[[Follower | None], None]  # the callback a reload hands its module to; None: no callback


def reload_log(reload: Callable[[ModuleType], ModuleType]) -> ReloadLog:
    """A log around ``reload``, the real ``importlib.reload``."""
    done: list[_Reload] = []
    followers: list[Follower] = []

    def logged(module: object) -> ModuleType:
        if not isinstance(module, ModuleType):
            # The real reload rejects anything but a module with its own TypeError.
            return reload(cast("ModuleType", module))
        before = dict(module.__dict__)
        try:
            reloaded = reload(module)
        finally:
            # A body that raised part-way has still rebound the names it reached.
            done.append((module, before, dict(module.__dict__)))
        for follower in tuple(followers):
            follower(module)
        return reloaded

    def undo() -> None:
        while done:
            module, before, after = done.pop()
            namespace = module.__dict__
            for name, value in after.items():
                previous = before.get(name, _ABSENT)
                if previous is value or namespace.get(name, _ABSENT) is not value:
                    continue  # the reload kept the binding, or something rebound the name after it
                if previous is _ABSENT:
                    del namespace[name]
                else:
                    namespace[name] = previous

    def forget() -> None:
        done.clear()

    def follow(follower: Follower | None) -> None:
        followers[:] = [] if follower is None else [follower]

    return ReloadLog(logged, undo, forget, follow)
