"""Fixtures shared by the tests of the worker runtime."""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

from balamut.runtime import kill_deps, plugin

_TOOL_IDS = (*plugin._MONITORING_TOOL_IDS, *kill_deps._TOOL_IDS)


@pytest.fixture(autouse=True)
def _monitoring_tools_given_back() -> Iterator[None]:
    """A test leaves the ``sys.monitoring`` tool ids of the touch marker and the kill recorder as it found them.

    The ids are process-wide. A test that leaves one taken (a mutant of the marker can make it raise half-way) leaves
    every later test of the process without it, and in a balamut worker what those tests observe would then depend on
    the leases that ran before them. Ids taken before the test (the worker's own marker and recorder) stay."""
    held = {tool for tool in _TOOL_IDS if sys.monitoring.get_tool(tool) is not None}
    yield
    for tool in _TOOL_IDS:
        if tool not in held and sys.monitoring.get_tool(tool) is not None:
            sys.monitoring.clear_tool_id(tool)
            sys.monitoring.free_tool_id(tool)
