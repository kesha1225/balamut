"""pytest plugin: applies an in-memory mutant inside a pytest-fast worker before the tests run.

Loaded into daemons with ``-p balamut.runtime.plugin``. The daemon collects the original once (an empty mutant
file means no mutation). For every request the driver writes a spec carrying a unique ``request_token`` before
sending it. A persistent worker reads it at the first ``pytest_runtest_setup`` of the request, restores the
previous ``__code__``/``__defaults__``, clears the safe Python caches and applies the new mutant; the following
items of the same request see the same patch without another reset. ``sys.modules`` is never changed:
re-importing a C extension is unsafe for process-global state, and partially unloading a Python graph breaks the
identity of singletons and registries.

State lives in the worker session's ``stash``. The driver keeps the worker pool across requests, so
session-scoped fixtures and the imported graph are paid for once per campaign.
"""

from __future__ import annotations

import ast
import json
import multiprocessing
import os
import sys
from collections.abc import Callable, Generator
from contextlib import suppress
from enum import Enum
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import NamedTuple, NotRequired, TypedDict, cast

import pytest

from balamut.concurrency import fuzz_globals
from balamut.core.mutants import ApplyMechanism, BcEdit, BcEditKind
from balamut.operators.oracle import oracle_globals
from balamut.runtime import kill_deps
from balamut.runtime.bytecode import mutate_code
from balamut.runtime.fault import fault_globals
from balamut.runtime.inmemory import compile_function_code, patch_code, resolve_with_cache_clear
from balamut.runtime.worker_reset import WorkerBaseline, capture_baseline, reset_to_baseline

# None of these is fingerprinted by pytest-fast (only PYTEST_* keys and the project's own prefixes are): daemons
# with different mutant files must not restart each other as stale.
MUTANT_FILE_ENV = "BALAMUT_MUTANT_FILE"
# Set only for campaign daemons: the kill-dependency recorder is armed in their workers, not in any process that
# merely imports this module (tests, tools).
KILL_DEPS_ENV = "BALAMUT_KILL_DEPS"
TEST_ROOTS_ENV = "BALAMUT_TEST_ROOTS"
# Kill switch for the bytecode fast path (a safety fallback and an A/B lever).
FORCE_COMPILE_ENV = "BALAMUT_FORCE_COMPILE"

UNSUPPORTED_MARKER_SUFFIX = ".unsupported"
TOUCHED_MARKER_SUFFIX = ".touched"
_MONITORING_TOOL_IDS = (4, 3)  # 0/1/2/5 are reserved by CPython for debugger/coverage/profiler/optimizer

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class _AppliedMutant(NamedTuple):
    """The mutant applied in this (possibly persistent) worker: the key is the raw spec text (unique per
    request), ``restore`` reverts the edit."""

    key: str
    restore: Callable[[], None]


_STATE = pytest.StashKey["_AppliedMutant | None"]()
# Baseline of the persistent worker: the module set gates lazily imported targets; the baseline `@lru_cache`
# wrappers are cleared at every mutant boundary. The import graph stays whole.
_BASELINE = pytest.StashKey[WorkerBaseline]()
# The recorder of functions a mutant lease entered (see kill_deps): one per worker, armed before collection.
_DEPS = pytest.StashKey[kill_deps.DepsRecorder]()


class MutantSpec(TypedDict):
    module: str
    qualname: str
    mutated_source: str
    kind: str
    self_covered: NotRequired[bool]  # the mutant mutates its own covering test: patch the collected module
    mechanism: NotRequired[str]  # ApplyMechanism value; absent means CODE
    default_index: NotRequired[int]  # DEFAULTS: the slot in __defaults__
    default_literal: NotRequired[str]  # DEFAULTS: text of the new value (ast.literal_eval)
    # Bytecode edit (const/binop/compare): the site position and the structured edit. Absent: compile right away.
    bc_kind: NotRequired[str]
    bc_source: NotRequired[str]
    bc_target: NotRequired[str]
    bc_lineno: NotRequired[int]
    bc_col: NotRequired[int]
    bc_end_col: NotRequired[int]
    bc_mutated: NotRequired[str]
    request_token: NotRequired[str]  # the unique boundary of a mutant lease
    target_lineno: NotRequired[int]  # site line: selects the exact nested code object for the touch marker
    target_col: NotRequired[int]  # UTF-8 byte column of the site start
    touch_path: NotRequired[str]  # marker of an actual entry into the mutated code object
    deps_path: NotRequired[str]  # where the lease publishes the repository functions it entered


_REQUIRED_SPEC_KEYS = ("module", "qualname", "mutated_source", "kind")
_OPTIONAL_STR_KEYS = (
    "mechanism",
    "default_literal",
    "request_token",
    "touch_path",
    "deps_path",
    "bc_kind",
    "bc_source",
    "bc_target",
    "bc_mutated",
)
_OPTIONAL_INT_KEYS = (
    "default_index",
    "target_lineno",
    "target_col",
    "bc_lineno",
    "bc_col",
    "bc_end_col",
)
_OPTIONAL_BOOL_KEYS = ("self_covered",)


def parse_mutant_spec(raw: str) -> MutantSpec | None:
    """Parse and validate the untrusted spec text; None for non-JSON, a non-object, a missing required key or any
    value of the wrong type. Wrong types would otherwise crash the worker deep inside apply (an unhashable module
    name, a non-string qualname, a bool used as an index), so the driver gets UNSUPPORTED instead."""
    try:
        parsed: JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if any(not isinstance(parsed.get(key), str) for key in _REQUIRED_SPEC_KEYS):
        return None
    for key in _OPTIONAL_STR_KEYS:
        value = parsed.get(key)
        if value is not None and not isinstance(value, str):
            return None
    for key in _OPTIONAL_INT_KEYS:
        value = parsed.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            return None
    for key in _OPTIONAL_BOOL_KEYS:
        value = parsed.get(key)
        if value is not None and not isinstance(value, bool):
            return None
    # The shape was validated key by key above; a TypedDict cannot be narrowed from a plain dict by isinstance.
    return cast("MutantSpec", parsed)


class ApplyOutcome(Enum):
    APPLIED = "applied"
    UNSUPPORTED = "unsupported"  # not applied (resolve / compile / freevars): never reported as survived


def _codes_at_site(root: CodeType, lineno: int | None, col: int | None) -> tuple[CodeType, ...]:
    """The innermost code objects whose own source extent holds the start of the edit; the root when none does.

    Only the start is compared: the positions are those of the mutated code, and the edit may have moved
    everything after its start. A nested code object that the edit merely contains (a lambda argument of an edited
    call) does not hold the start, so the marker stays on the scope that runs the edit: armed on the lambda, it
    would stay silent for a mutant whose call fails before the lambda runs."""
    if lineno is None:
        return (root,)
    matches: list[tuple[int, CodeType]] = []

    def visit(code: CodeType, depth: int) -> None:
        if _extent_holds(code, lineno, col):
            matches.append((depth, code))
        for value in code.co_consts:
            if isinstance(value, CodeType):
                visit(value, depth + 1)

    visit(root, 0)
    if not matches:
        return (root,)
    deepest = max(depth for depth, _code in matches)
    return tuple(code for depth, code in matches if depth == deepest)


def _extent_holds(code: CodeType, lineno: int, col: int | None) -> bool:
    """Does the source extent of the code object's own instructions hold the point ``(lineno, col)``?

    Zero-width positions are skipped: they mark artificial instructions (``RESUME`` sits at column 0 of the
    definition line) and cover no source text. Without a column (an older spec, or code compiled without column
    positions) the lines decide."""
    lines: list[tuple[int, int]] = []
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for start, end, start_col, end_col in code.co_positions():
        if start is None or end is None:
            continue
        if start_col is None or end_col is None:
            lines.append((start, end))
        elif (start, start_col) != (end, end_col):
            lines.append((start, end))
            spans.append(((start, start_col), (end, end_col)))
    if col is not None and spans:
        return min(first for first, _last in spans) <= (lineno, col) < max(last for _first, last in spans)
    return bool(lines) and min(first for first, _last in lines) <= lineno <= max(last for _first, last in lines)


def _arm_touch_marker(codes: tuple[CodeType, ...], path: str | None) -> Callable[[], None]:
    """Mark the first actual entry into the target code objects through a local ``sys.monitoring`` event."""
    if path is None:
        return _noop_restore
    monitoring = sys.monitoring
    tool_id: int | None = None
    for candidate in _MONITORING_TOOL_IDS:
        try:
            monitoring.use_tool_id(candidate, "balamut-touch")
        except ValueError:  # the id is taken by another monitoring tool of this process
            continue
        tool_id = candidate
        break
    if tool_id is None:
        return _noop_restore  # no marker: the driver re-checks the mutant in a fresh process

    marker = Path(path)

    def mark(_code: CodeType, _instruction_offset: int) -> kill_deps.MonitoringSignal:
        with suppress(OSError):  # fail closed: a missing marker sends a survivor to a fresh re-check
            marker.write_text("1", encoding="utf-8")
        return monitoring.DISABLE

    try:
        monitoring.register_callback(tool_id, monitoring.events.PY_START, mark)
        for code in codes:
            monitoring.set_local_events(tool_id, code, monitoring.events.PY_START)
    except ValueError:
        monitoring.clear_tool_id(tool_id)
        monitoring.free_tool_id(tool_id)
        return _noop_restore

    def disarm() -> None:
        for code in codes:
            monitoring.set_local_events(tool_id, code, 0)
        monitoring.register_callback(tool_id, monitoring.events.PY_START, None)
        monitoring.free_tool_id(tool_id)

    return disarm


def _bytecode_mutant(func: FunctionType, spec: MutantSpec) -> CodeType | None:
    """A surgical edit of the live ``func.__code__`` from the bc-fields; None means fall back to compiling."""
    if os.environ.get(FORCE_COMPILE_ENV):
        return None
    kind_raw = spec.get("bc_kind")
    lineno = spec.get("bc_lineno")
    col = spec.get("bc_col")
    end_col = spec.get("bc_end_col")
    mutated = spec.get("bc_mutated")
    if kind_raw is None or lineno is None or col is None or end_col is None or mutated is None:
        return None
    try:
        edit = BcEdit(
            kind=BcEditKind(kind_raw),
            source=spec.get("bc_source", ""),
            target=spec.get("bc_target", ""),
            mutated=mutated,
        )
    except ValueError:
        return None  # an unknown kind from an untrusted spec: recompile
    return mutate_code(func.__code__, lineno=lineno, col=col, end_col=end_col, edit=edit)


def apply_mutation(module: ModuleType, spec: MutantSpec) -> ApplyOutcome:
    """``apply_mutation_reversible`` without the restore, for one-shot callers and unit tests."""
    return apply_mutation_reversible(module, spec)[0]


def apply_mutation_reversible(module: ModuleType, spec: MutantSpec) -> tuple[ApplyOutcome, Callable[[], None] | None]:
    """Apply a mutant to a live module by its mechanism and return ``(outcome, restore)``.

    ``restore`` reverts the edit (needed by persistent workers); None means nothing was applied. CODE patches
    ``__code__``; DEFAULTS replaces ``__defaults__``."""
    func, cache_clear = resolve_with_cache_clear(module, spec["qualname"])
    if func is None:
        return ApplyOutcome.UNSUPPORTED, None
    if spec.get("mechanism") == ApplyMechanism.DEFAULTS.value:
        return _apply_defaults(func, spec, cache_clear)
    # Domain mutants reference helper names (nullcontext, fuzz/fault helpers, the oracle sentinel): inject them
    # with setdefault so an imported name of the module is never clobbered.
    for name, value in {**fuzz_globals(), **fault_globals(), **oracle_globals()}.items():
        func.__globals__.setdefault(name, value)
    new_code = _bytecode_mutant(func, spec)
    if new_code is None:
        new_code = compile_function_code(spec["mutated_source"], spec["qualname"], spec["module"])
    if new_code is None:
        return ApplyOutcome.UNSUPPORTED, None
    if func.__code__.co_qualname != new_code.co_qualname:
        # The resolved object is something else (a decorator's wrapper without __wrapped__) than the definition
        # compiled under the spec's qualname: grafting that body would destroy the wrapper.
        return ApplyOutcome.UNSUPPORTED, None
    try:
        code_restore = patch_code(func, new_code)
    except ValueError:  # freevars/cellvars mismatch: the body changed its closure
        return ApplyOutcome.UNSUPPORTED, None
    touch_restore = _arm_touch_marker(
        _codes_at_site(new_code, spec.get("target_lineno"), spec.get("target_col")),
        spec.get("touch_path"),
    )

    def restore() -> None:
        touch_restore()
        code_restore()
        cache_clear()  # a wrapper cache filled by this mutant must not leak into the next one

    return ApplyOutcome.APPLIED, restore


def _apply_defaults(
    func: FunctionType, spec: MutantSpec, cache_clear: Callable[[], None]
) -> tuple[ApplyOutcome, Callable[[], None] | None]:
    """Replace one positional default in the live ``__defaults__``. Sibling defaults are taken from the already
    evaluated tuple (never re-evaluated), so nothing runs as a side effect."""
    index = spec.get("default_index")
    literal = spec.get("default_literal")
    if index is None or literal is None:
        return ApplyOutcome.UNSUPPORTED, None
    current = list(func.__defaults__ or ())
    if not 0 <= index < len(current):
        return ApplyOutcome.UNSUPPORTED, None
    try:
        current[index] = ast.literal_eval(literal)
    except ValueError, SyntaxError:
        return ApplyOutcome.UNSUPPORTED, None
    original = func.__defaults__
    touch_restore = _arm_touch_marker((func.__code__,), spec.get("touch_path"))

    def restore() -> None:
        touch_restore()
        func.__defaults__ = original
        cache_clear()

    func.__defaults__ = tuple(current)
    return ApplyOutcome.APPLIED, restore


def _mark_unsupported(spec_path: str) -> None:
    """Tell the driver the mutation could not be applied (import / descriptor / compile / freevars)."""
    Path(f"{spec_path}{UNSUPPORTED_MARKER_SUFFIX}").write_text("1", encoding="utf-8")


def _noop_restore() -> None:
    """Nothing to restore for a mutant that was not applied; registering it still marks the lease boundary."""


_OWNED_TRANSPORT_ENV = (
    "PYTEST_FAST_WORKER_IDENTITY",
    "PYTEST_FAST_WORKERS",
    "PYTEST_FAST_MARK",
    "_PYTEST_FAST_COLLECT",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
    "PYTEST_ADDOPTS",
    "COVERAGE_CORE",
)


def scrub_transport_env() -> None:
    """Remove the transport keys before the test body.

    A test that spawns a nested pytest-fast (the coverage tests do) must never inherit this worker's identity,
    addopts or coverage core: the nested run builds its own environment, and an inherited ``PYTEST_XDIST_WORKER``
    makes its plugins skip the per-worker setup and fail closed. The balamut lease keys stay: this plugin reads
    them per item."""
    for key in _OWNED_TRANSPORT_ENV:
        os.environ.pop(key, None)


def pytest_runtest_setup(item: pytest.Item) -> None:
    scrub_transport_env()
    # pytest-fast starts its workers with ``daemon=True``, and multiprocessing refuses to start a child from a
    # daemonic process (a test's ``multiprocessing.Pool``). The flag changes only this process's own view: the
    # controller still terminates the worker as a daemon, and the worker leaves through ``os._exit``.
    multiprocessing.current_process().daemon = False
    _apply_or_switch(item)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, bool | None, bool | None]:
    """After all phases of a test, publish the lease dependencies gathered so far.

    Not a teardown hook: a failing teardown implementation stops the others, and a killer that broke a fixture
    cleanup would be left without its record. ``finally`` also covers what propagates through the protocol."""
    try:
        return (yield)
    finally:
        recorder = _deps_recorder(item.session)
        if recorder is not None:
            recorder.flush()


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup[T](fixturedef: pytest.FixtureDef[T], request: pytest.FixtureRequest) -> Generator[None, T, T]:
    """Building a fixture wider than function scope outlives the lease: its code is a dependency of later kills."""
    recorder = _deps_recorder(request.session)
    if recorder is None or fixturedef.scope == "function":
        return (yield)
    with recorder.shared_scope():
        return (yield)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """Arm the daemon's recorder before the root conftest: the application import and collection build state every
    lease reads. Workers inherit the armed recorder through the fork."""
    if not os.environ.get(KILL_DEPS_ENV) or _DEPS in early_config.stash:
        return
    roots = tuple(root for root in os.environ.get(TEST_ROOTS_ENV, "tests").split(",") if root)
    recorder = kill_deps.DepsRecorder(Path.cwd(), test_roots=roots)
    if kill_deps.monitoring_arm(recorder) is not None:  # no free tool id: kills fall back to the file rule
        early_config.stash[_DEPS] = recorder


def _deps_recorder(session: pytest.Session) -> kill_deps.DepsRecorder | None:
    return session.config.stash.get(_DEPS, None)


def original_lease_deps(raw: str) -> str | None:
    """The dependency file of a paired original: no mutation in its lease, but the green original is the second
    half of the kill proof, so the functions only it enters are dependencies too."""
    try:
        parsed: JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    deps_path = parsed.get("original_deps_path")
    return deps_path if isinstance(deps_path, str) and deps_path else None


def _deps_lease(session: pytest.Session, target: str | None) -> None:
    """A lease boundary: the previous mutant's dependencies are dropped, events disabled by DISABLE come back."""
    recorder = _deps_recorder(session)
    if recorder is None:
        return
    recorder.lease_start(Path(target) if target else None)
    kill_deps.monitoring_restart()


def _target_module(item: pytest.Item, spec: MutantSpec, baseline: WorkerBaseline) -> ModuleType | None:
    """The live module to patch, or None (UNSUPPORTED).

    Oracle mutants mutate the test itself: the collected module (``item.module``) is used rather than guessing a
    dotted name, which is fragile for test files outside packages. A source module missing from sys.modules is
    imported lazily by a test body (after its monkeypatching); importing it here, before the test, could bind it
    under the wrong environment. A module present but outside the baseline was imported by a previous mutant's test
    in this persistent worker: in a fresh fork it would not exist, so mutating that state proves nothing."""
    if spec.get("self_covered") is True and isinstance(item, pytest.Function):
        # pytest types `Function.module` as Any; the collected module of a Python test function is a ModuleType.
        return cast("ModuleType", item.module)
    module = sys.modules.get(spec["module"])
    if module is None or spec["module"] not in baseline.modules:
        return None
    return module


def _apply_or_switch(item: pytest.Item) -> None:
    """Apply the mutant lease from the mutant file and isolate it from the previous lease.

    The driver puts a unique ``request_token`` into the raw spec. The same raw text means the next item of the same
    lease: the mutation and the process state stay active. Different text is a boundary: the previous mutation is
    restored, the safe Python state goes back to the baseline, then the new mutant is applied."""
    spec_path = os.environ.get(MUTANT_FILE_ENV)
    if not spec_path:
        return
    session = item.session
    baseline = session.stash.get(_BASELINE, None)
    if baseline is None:
        # The first mutant of this worker: snapshot the process (after collection) as the reference.
        baseline = capture_baseline()
        session.stash[_BASELINE] = baseline
    prev = session.stash.get(_STATE, None)

    def _reset_to_original() -> None:
        # Restore before any early exit, so the tests of this request run against the original.
        if prev is not None:
            prev.restore()
        reset_to_baseline(baseline)
        session.stash[_STATE] = None

    path = Path(spec_path)
    if not path.exists():
        _reset_to_original()
        _deps_lease(session, None)
        return
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:  # a directory, binary or unreadable spec: never crash the worker
        _mark_unsupported(spec_path)
        _reset_to_original()
        _deps_lease(session, None)
        return
    if prev is not None and prev.key == raw:
        return  # the next item of the same lease: resetting again would clear every cache for nothing
    if prev is not None:
        prev.restore()
        reset_to_baseline(baseline)
    spec = parse_mutant_spec(raw) if raw else None
    original_deps = original_lease_deps(raw) if raw and spec is None else None
    # The lease starts before the mutation is applied: the first entry into the mutated code is a dependency too.
    _deps_lease(session, spec.get("deps_path") if spec is not None else original_deps)
    if not raw or original_deps is not None:
        # Cost calibration and a paired original carry no mutation; the no-op state still marks their boundary, so
        # the next mutant lease resets the state those tests polluted before applying.
        session.stash[_STATE] = _AppliedMutant(raw, _noop_restore)
        return
    if spec is None:
        _mark_unsupported(spec_path)
        session.stash[_STATE] = _AppliedMutant(raw, _noop_restore)
        return
    module = _target_module(item, spec, baseline)
    if module is None:
        _mark_unsupported(spec_path)
        session.stash[_STATE] = _AppliedMutant(raw, _noop_restore)
        return
    outcome, restore = apply_mutation_reversible(module, spec)
    if outcome is ApplyOutcome.UNSUPPORTED or restore is None:
        _mark_unsupported(spec_path)
        session.stash[_STATE] = _AppliedMutant(raw, _noop_restore)
        return
    session.stash[_STATE] = _AppliedMutant(raw, restore)


__all__ = [
    "FORCE_COMPILE_ENV",
    "KILL_DEPS_ENV",
    "MUTANT_FILE_ENV",
    "TEST_ROOTS_ENV",
    "TOUCHED_MARKER_SUFFIX",
    "UNSUPPORTED_MARKER_SUFFIX",
    "ApplyOutcome",
    "MutantSpec",
    "apply_mutation",
    "apply_mutation_reversible",
    "parse_mutant_spec",
]
