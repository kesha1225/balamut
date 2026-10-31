"""Minimal stand-ins for pytest sessions and items, for driving the plugin hooks without a daemon."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest


@dataclass
class FakeConfig:
    stash: pytest.Stash = field(default_factory=pytest.Stash)


@dataclass
class FakeSession:
    stash: pytest.Stash = field(default_factory=pytest.Stash)
    config: FakeConfig = field(default_factory=FakeConfig)


@dataclass
class FakeItem:
    session: FakeSession
