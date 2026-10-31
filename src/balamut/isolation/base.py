"""Shared types of the isolation providers."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Protocol

type EnvMap = dict[str, str]
type Environ = Mapping[str, str]  # a read-only environment (os.environ or a test mapping)
type EnvOverlays = tuple[EnvMap, ...]


class IsolationError(RuntimeError):
    """The isolation provider could not prepare, refresh or remove its slots."""


class Isolation(Protocol):
    """One provider of per-slot environment overlays."""

    @property
    def isolated(self) -> bool:
        """False when every slot shares the caller's environment."""
        ...

    def prepare(self, count: int, prefix: str, /) -> EnvOverlays:
        """Create (or recreate from a clean state) ``count`` slots and return one overlay per slot."""
        ...

    def cleanup(self, count: int, prefix: str, /) -> None:
        """Remove the slots created under ``prefix``."""
        ...

    def probe(self) -> None:
        """Raise ``IsolationError`` when the configured target is unreachable."""
        ...

    def fingerprint(self) -> str:
        """A digest of everything that shapes a fresh slot (for substrate compatibility)."""
        ...


def slot_prefix() -> str:
    """A unique, identifier-safe prefix for the slots of one campaign."""
    return f"balamut_{uuid.uuid4().hex[:12]}"
