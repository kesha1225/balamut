"""Re-executing a module body inside a test without leaving a second set of its objects in the process."""

from __future__ import annotations

import importlib
from collections.abc import Generator
from contextlib import contextmanager
from types import ModuleType


@contextmanager
def reloaded(module: ModuleType) -> Generator[ModuleType]:
    """Reload ``module`` for the body of the ``with`` block, then put its original namespace back.

    A reload re-runs the module and class bodies, so the coverage substrate attributes those import-time lines to
    the test. It also binds a new object to every function and class name of the module, while the other test
    files keep the objects they imported. balamut applies a mutant to the objects the module holds when the mutant
    starts, and its workers outlive a test: without the restore, every later test in the worker that calls an
    imported name runs the original code and the mutant survives."""
    namespace = dict(module.__dict__)
    try:
        yield importlib.reload(module)
    finally:
        module.__dict__.update(namespace)
