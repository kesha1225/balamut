"""The reference pytest plugin of PyMutBench (loaded with ``-p pmb_pytest_plugin``).

It lives alone in its directory so that putting the directory on ``PYTHONPATH`` adds exactly one module to the
subject's import path. It is stdlib-only and records, as JSON lines in ``$PMB_OUT``:

* one ``test`` record per test item: the combined outcome of its setup/call/teardown phases (a failed subtest
  fails its test's call phase), the phase that turned it red, the exception type and a short message;
* one ``collect_error`` record per collector that failed to collect;
* an ``end`` record with the exit status, the collected count and the files the packages named in
  ``$PMB_ORIGIN_PACKAGES`` were imported from (the import-origin check: a run that imported the package under
  test from anywhere but the tree being judged is invalid, never a verdict).

Before each test it writes the node id to ``$PMB_CURRENT`` and arms ``faulthandler`` with
``$PMB_TEST_TIMEOUT`` seconds, so a hung test ends the process and the runner knows which test hung.
When Hypothesis is importable, a derandomized profile without example database or deadline is loaded, so a
property test explores the same examples on the original and on every mutant.
"""

from __future__ import annotations

import faulthandler
import json
import os
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

_OUT = os.environ.get("PMB_OUT", "")
_CURRENT = os.environ.get("PMB_CURRENT", "")
_TIMEOUT = float(os.environ.get("PMB_TEST_TIMEOUT", "0") or 0)
_ORIGIN_PACKAGES = tuple(p for p in os.environ.get("PMB_ORIGIN_PACKAGES", "").split(",") if p)
# Absolute paths whose executed lines are recorded per test (the oracle's prioritization map, never a verdict).
_COVER = frozenset(p for p in os.environ.get("PMB_COVER_FILES", "").split(os.pathsep) if p)


class _LineRecorder:
    """Per-test executed lines of ``_COVER`` files through ``sys.monitoring`` LINE events.

    A location fires once per test: the callback disables it, and every test start re-enables all disabled
    locations. Lines run before the first test (imports during collection) belong to the ``<collect>`` context.
    """

    TOOL = 4  # a tool id the subject's own coverage tooling does not use (coverage.py takes 1)

    def __init__(self) -> None:
        self.context = "<collect>"
        self.lines: dict[str, dict[str, set[int]]] = {}
        mon = sys.monitoring
        mon.use_tool_id(self.TOOL, "pymutbench")
        mon.register_callback(self.TOOL, mon.events.LINE, self._line)
        mon.set_events(self.TOOL, mon.events.LINE)

    def _line(self, code: Any, line: int) -> object:
        if code.co_filename in _COVER:
            self.lines.setdefault(self.context, {}).setdefault(code.co_filename, set()).add(line)
        return sys.monitoring.DISABLE

    def start(self, nodeid: str) -> None:
        self.context = nodeid
        sys.monitoring.restart_events()

    def dump(self) -> dict[str, dict[str, list[int]]]:
        return {ctx: {f: sorted(ls) for f, ls in files.items()} for ctx, files in self.lines.items()}


_lines = _LineRecorder() if _COVER else None

_RED = frozenset({"failed", "error"})


class _Recorder:
    def __init__(self, path: str) -> None:
        self._fh = open(path, "a", encoding="utf-8") if path else None  # noqa: SIM115 - lives for the session
        self.phases: dict[str, dict[str, Any]] = {}
        self.collected = 0

    def write(self, record: dict[str, Any]) -> None:
        if self._fh is None:
            return
        self._fh.write(json.dumps(record) + "\n")  # ASCII-escaped: no line separator inside a record
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


_recorder = _Recorder(_OUT)


def _short(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Node ids are relative to pytest's rootdir, which an ini file in a subdirectory moves away from the invocation
# directory; every recorded id is rewritten relative to the invocation directory, so it can be passed back.
_PREFIX = ""


def _invocable(nodeid: str) -> str:
    return f"{_PREFIX}/{nodeid}" if _PREFIX and nodeid else nodeid


def pytest_configure(config: pytest.Config) -> None:
    global _PREFIX  # noqa: PLW0603 - one value per process, fixed at configure time
    rel = os.path.relpath(config.rootpath, config.invocation_params.dir)
    _PREFIX = "" if rel == "." else rel.replace(os.sep, "/")
    try:
        from hypothesis import settings  # noqa: PLC0415
    except ImportError:
        return
    settings.register_profile("pymutbench", derandomize=True, database=None, deadline=None, print_blob=False)
    settings.load_profile("pymutbench")


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _recorder.write({"kind": "collect_error", "nodeid": _invocable(report.nodeid),
                         "msg": _short(str(report.longrepr), 600)})


# Invocable node ids (see _invocable) that must not run: matched exactly, unlike pytest's --deselect, which
# compares root-relative ids by prefix.
_DESELECT_FILE = os.environ.get("PMB_DESELECT_FILE", "")


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    if not _DESELECT_FILE:
        return
    with open(_DESELECT_FILE, encoding="utf-8") as fh:
        unwanted = {line.rstrip("\n") for line in fh if line.strip()}
    keep = [item for item in items if _invocable(item.nodeid) not in unwanted]
    dropped = [item for item in items if _invocable(item.nodeid) in unwanted]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = keep


def pytest_collection_finish(session: pytest.Session) -> None:
    _recorder.collected = len(session.items)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None]:
    nodeid = _invocable(item.nodeid)
    if _CURRENT:
        with open(_CURRENT, "w", encoding="utf-8") as fh:
            fh.write(nodeid)
    if _lines is not None:
        _lines.start(nodeid)
    if _TIMEOUT > 0:
        faulthandler.dump_traceback_later(_TIMEOUT, exit=True)
    try:
        yield
    finally:
        if _TIMEOUT > 0:
            faulthandler.cancel_dump_traceback_later()
    _emit(nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None]:
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    phase: dict[str, Any] = {"outcome": report.outcome, "duration": round(report.duration, 6)}
    if hasattr(report, "wasxfail"):
        phase["xfail"] = True
    if call.excinfo is not None and report.outcome != "passed":
        phase["exc_type"] = call.excinfo.typename
        phase["exc_msg"] = _short(str(call.excinfo.value))
    phases = _recorder.phases.setdefault(_invocable(item.nodeid), {})
    # A subtest (unittest ``subTest`` or the ``subtests`` fixture) reports through this hook as its test's call
    # phase. A failed one keeps that phase failed whatever reports follow: pytest fails a test that contains
    # failed subtests even when its own call passes.
    if phases.get(call.when, {}).get("outcome") == "failed" and report.outcome != "failed":
        return
    phases[call.when] = phase


def _emit(nodeid: str) -> None:
    phases = _recorder.phases.pop(nodeid, {})
    outcome, when, exc_type, exc_msg = "passed", "", "", ""
    # A failure in any phase wins over a skip or xfail of another (a skipped call can be followed by a failing
    # teardown); only a test with no failing phase is classified as skipped, xfailed or xpassed.
    failed = next((name for name in ("setup", "call", "teardown")
                   if phases.get(name, {}).get("outcome") == "failed"), None)
    skipped = next((name for name in ("setup", "call", "teardown")
                    if phases.get(name, {}).get("outcome") == "skipped"), None)
    if failed is not None:
        phase = phases[failed]
        outcome = "error" if failed != "call" else "failed"
        when, exc_type, exc_msg = failed, phase.get("exc_type", ""), phase.get("exc_msg", "")
    elif skipped is not None:
        outcome = "xfailed" if phases[skipped].get("xfail") else "skipped"
        when = skipped
    elif any(phase.get("xfail") for phase in phases.values()):
        outcome = "xpassed"
    duration = round(sum(phase["duration"] for phase in phases.values()), 6)
    record: dict[str, Any] = {"kind": "test", "nodeid": nodeid, "outcome": outcome, "duration": duration}
    if when:
        record["when"] = when
    if outcome in _RED:
        record["exc_type"] = exc_type
        record["exc_msg"] = exc_msg
    _recorder.write(record)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    origins: dict[str, str] = {}
    for name in _ORIGIN_PACKAGES:
        module = sys.modules.get(name)
        if module is not None:
            origins[name] = str(getattr(module, "__file__", "") or getattr(module, "__path__", ""))
    if _lines is not None:
        _recorder.write({"kind": "lines", "by_test": _lines.dump()})
    _recorder.write({"kind": "end", "exitstatus": int(exitstatus), "collected": _recorder.collected, "origins": origins})
    _recorder.close()
