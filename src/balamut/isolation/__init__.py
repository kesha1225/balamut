"""Per-slot isolation of the environment a test run touches (databases, queues, directories).

A campaign runs several daemons at once; a coverage measurement runs several slots. Each gets an environment
overlay from the isolation provider, so tests of one never see the writes of another, and a mutant that broke a
rollback or cleanup cannot poison the next run.
"""

from __future__ import annotations

import os

from balamut.config import IsolationKind, Profile
from balamut.isolation.base import Environ, EnvMap, EnvOverlays, Isolation, IsolationError, slot_prefix
from balamut.isolation.postgres import PostgresTemplate
from balamut.isolation.shell import NoIsolation, ShellIsolation


def isolation_for(profile: Profile, *, environ: Environ | None = None) -> Isolation:
    """The provider configured by ``[tool.balamut.isolation]``."""
    env = dict(os.environ if environ is None else environ)
    settings = profile.isolation
    match settings.kind:
        case IsolationKind.NONE:
            return NoIsolation()
        case IsolationKind.SHELL:
            return ShellIsolation(settings.prepare, settings.cleanup, environ=env)
        case IsolationKind.POSTGRES_TEMPLATE:
            return PostgresTemplate(profile, environ=env)


__all__ = [
    "EnvMap",
    "EnvOverlays",
    "Isolation",
    "IsolationError",
    "NoIsolation",
    "PostgresTemplate",
    "ShellIsolation",
    "isolation_for",
    "slot_prefix",
]
