"""Validity of a proven kill, by the functions the killing run really executed (balamut.runtime.kill_deps).

A kill is an observation: the killer failed on the mutant and passed on the original. It stays true until the
code that run executed changes. A coarser rule (whole files: the mutant's file, every file the covering tests
execute anywhere, every covering test file) would reset the cache on almost any edit.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import io
import os
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import CodeType, GenericAlias, ModuleType, SimpleNamespace, UnionType
from typing import cast

import pytest

from balamut.runtime import kill_deps
from bm_support import make_layout, write_file

_SUPPORT = "support-hash"


def _noop(*_args: object) -> None:
    return None


def _validity(root: Path, deps: kill_deps.KillDeps, *, support: str = _SUPPORT) -> str:
    return kill_deps.DependencyHasher(make_layout(root)).validity(deps, support_hash=support)


def _write_all(root: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        write_file(root / relative, source)


_SOURCE = """\
LIMIT = 3


def valid(timeout: int) -> bool:
    return timeout <= LIMIT


def helper(value: int) -> int:
    return value + 1
"""
_TESTS = """\
from src.x import valid


def test_rejects_long_timeout() -> None:
    assert not valid(5)


def test_accepts_short_timeout() -> None:
    assert valid(1)
"""
_SHARED = (
    "LIMIT = 3\n\n\ndef _cases() -> list[int]:\n    return [1, 2]\n\n\nCASES = _cases()\n\n\n"
    "def test_shared() -> None:\n    assert LIMIT\n"
)
# The probe tree: source modules, a package loaded by collection, and test modules importing each other.
_TREE = {
    "src/x.py": _SOURCE,
    "src/n.py": (
        "def outer() -> int:\n    def inner() -> int:\n        return 1\n    return inner()\n\n\n"
        "def other() -> int:\n    return 0\n"
    ),
    "src/c.py": (
        "class C:\n    @property\n    def x(self) -> int:\n        return self._x\n\n"
        "    @x.setter\n    def x(self, value: int) -> None:\n        self._x = value\n"
    ),
    "src/j.py": (
        "try:\n    import fastjson\nexcept ImportError:\n    def dumps(value: int) -> str:\n        return str(value)\n"
    ),
    "src/pkg/a.py": "from pkg.b import VALUE\n\n\ndef f() -> int:\n    return VALUE\n",
    "src/pkg/b.py": "VALUE = 3\n\n\ndef g() -> int:\n    return 0\n",
    "src/other/c.py": "OTHER = 1\n",
    "tests/x_test.py": _TESTS,
    "tests/shared_test.py": _SHARED,
    "tests/middle_test.py": "from tests.shared_test import LIMIT\n\nMIDDLE = LIMIT\n",
    "tests/a_test.py": (
        "from tests.shared_test import CASES, LIMIT\n\n\ndef test_a() -> None:\n    assert len(CASES) < LIMIT\n"
    ),
    "tests/other_test.py": "OTHER = 1\n\n\ndef test_other() -> None:\n    assert OTHER\n",
}
_X: kill_deps.KillDeps = (("src/x.py", "valid"),)
_KILLER: kill_deps.KillDeps = (("tests/x_test.py", "test_rejects_long_timeout"),)
_INNER: kill_deps.KillDeps = (("src/n.py", "outer.<locals>.inner"),)
_LOADED: kill_deps.KillDeps = (("src/pkg/a.py", "f"), ("src/pkg", "<dir>"))
_IMPORTING: kill_deps.KillDeps = (("tests/a_test.py", "test_a"),)
_LIMIT = "tests/shared_test.py"


def _replaced(path: str, old: str, new: str) -> dict[str, str]:
    """An edit of one file of the probe tree."""
    return {path: _TREE[path].replace(old, new)}


def _killer_importing(statement: str) -> dict[str, str]:
    """A killing test file that reaches the probe test module through the given import form."""
    return {"tests/a_test.py": f"{statement}\n\n\ndef test_a() -> None:\n    pass\n"}


@pytest.mark.parametrize(
    ("deps", "edits", "tree", "read"),
    [
        pytest.param(_X, _replaced("src/x.py", "timeout <= LIMIT", "timeout < LIMIT"), {}, True, id="entered-function"),
        pytest.param(_X, _replaced("src/x.py", "value + 1", "value + 2"), {}, False, id="function-never-entered"),
        pytest.param(
            _X,
            _replaced(
                "src/x.py", "    return timeout <= LIMIT", "    # inclusive bound\n    return (timeout <= LIMIT)"
            ),
            {},
            False,
            id="comments-and-formatting",
        ),
        # Constants, imports and function headers run at import time, not inside the run: no function entry sees them,
        # so the skeleton of a dependency file is part of the validity as a whole.
        pytest.param(_X, _replaced("src/x.py", "LIMIT = 3", "LIMIT = 4"), {}, True, id="module-scope-of-a-dependency"),
        # A new definition can override a name or a base-class method the run went through.
        pytest.param(
            _X, {"src/x.py": _SOURCE + "\n\ndef extra() -> int:\n    return 0\n"}, {}, True, id="new-definition"
        ),
        pytest.param(_X, {"src/x.py": _SOURCE + "\ndef broken(:\n"}, {}, True, id="unparseable-dependency"),
        # Adding a test that kills a survivor is the author's main action; neighbouring kills of that file stay valid,
        # another test takes no part in the killer's run. A helper beside it is part of the skeleton.
        pytest.param(
            _KILLER,
            {"tests/x_test.py": _TESTS + "\n\ndef test_boundary() -> None:\n    assert valid(3)\n"},
            {},
            False,
            id="new-test-beside-the-killer",
        ),
        pytest.param(
            _KILLER,
            _replaced("tests/x_test.py", "assert valid(1)", "assert valid(2)"),
            {},
            False,
            id="other-test-of-the-killer-file",
        ),
        pytest.param(
            _KILLER,
            {"tests/x_test.py": _TESTS + "\n\ndef _seed() -> int:\n    return 1\n"},
            {},
            True,
            id="new-helper-beside-the-killer",
        ),
        # A nested function lives in the body of the outer one; same-named definitions (getter/setter, overload)
        # count together.
        pytest.param(_INNER, _replaced("src/n.py", "return 1", "return 2"), {}, True, id="nested-function"),
        pytest.param(
            _INNER, _replaced("src/n.py", "return 0", "return 5"), {}, False, id="beside-the-enclosing-function"
        ),
        pytest.param(
            (("src/c.py", "C.x"),), _replaced("src/c.py", "= value", "= value + 1"), {}, True, id="same-qualname"
        ),
        pytest.param(
            (("src/j.py", "dumps"),),
            _replaced("src/j.py", "str(value)", "repr(value)"),
            {},
            True,
            id="conditional-definition",
        ),
        # `f` reads a constant of a module whose functions the run never entered: collection loaded the module, its body
        # ran before the lease, and its directory is a kill dependency. A module appearing beside it may join a
        # registry.
        pytest.param(
            _LOADED, _replaced("src/pkg/b.py", "VALUE = 3", "VALUE = 4"), {}, True, id="value-of-a-loaded-module"
        ),
        pytest.param(
            _LOADED, _replaced("src/pkg/b.py", "return 0", "return 5"), {}, False, id="uncalled-loaded-function"
        ),
        pytest.param(_LOADED, {"src/pkg/c.py": "HANDLERS = {}\n"}, {}, True, id="module-appearing-beside-loaded-ones"),
        pytest.param(_LOADED, {"src/other/c.py": "OTHER = 2\n"}, {}, False, id="directory-never-loaded"),
        # A test reads constants and parameters of another test module, built by a helper during import, before the
        # lease: test modules imported by the killing test file count in full, except their tests. A module the
        # killing file does not import never resets that kill.
        pytest.param(_IMPORTING, _replaced(_LIMIT, "LIMIT = 3", "LIMIT = 4"), {}, True, id="imported-test-constant"),
        pytest.param(_IMPORTING, _replaced(_LIMIT, "return [1, 2]", "return [1]"), {}, True, id="helper-run-by-import"),
        pytest.param(
            _IMPORTING,
            {_LIMIT: _SHARED.replace("assert LIMIT", "assert LIMIT > 1") + "\n\ndef test_more() -> None:\n    pass\n"},
            {},
            False,
            id="tests-of-an-imported-test-module",
        ),
        pytest.param(_IMPORTING, {"tests/other_test.py": "OTHER = 2\n"}, {}, False, id="test-module-never-imported"),
        *(
            pytest.param(
                _IMPORTING, _replaced(_LIMIT, "LIMIT = 3", "LIMIT = 4"), _killer_importing(statement), True, id=name
            )
            for name, statement in (
                ("import", "import tests.shared_test"),
                ("from-package", "from tests import shared_test"),
                ("relative", "from .shared_test import LIMIT"),
                ("relative-package", "from . import shared_test"),
                ("transitive", "from tests.middle_test import MIDDLE"),
                ("inside-a-function", "def load() -> None:\n    from tests.shared_test import LIMIT"),
            )
        ),
    ],
)
def test_validity_changes_exactly_when_the_kill_read_the_edit(
    tmp_path: Path, deps: kill_deps.KillDeps, edits: dict[str, str], tree: dict[str, str], read: bool
) -> None:
    _write_all(tmp_path, {**_TREE, **tree})
    before = _validity(tmp_path, deps)

    _write_all(tmp_path, edits)

    assert (_validity(tmp_path, deps) != before) is read


def test_removed_file_or_function_invalidates(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", _SOURCE)
    before = _validity(tmp_path, _X)

    write_file(tmp_path / "src/x.py", _SOURCE.replace("def valid(", "def is_valid("))
    assert _validity(tmp_path, _X) != before

    (tmp_path / "src/x.py").unlink()
    assert _validity(tmp_path, _X) != before


@pytest.mark.parametrize(
    ("deps", "touched"),
    [
        pytest.param(_X, "src/x.py", id="entered-function"),
        # Directories and imported test modules are read for the hash just like functions.
        pytest.param(_LOADED, "src/pkg/b.py", id="loaded-directory"),
        pytest.param(_IMPORTING, _LIMIT, id="imported-test-module"),
    ],
)
def test_state_changed_after_the_campaign_started_gives_no_validity(
    tmp_path: Path, deps: kill_deps.KillDeps, touched: str
) -> None:
    """The daemon executed the version imported at campaign start while the hash would read the current one: a kill
    observed after an edit of what it read falls back to the file rule. Cache checks need no daemon and are
    unaffected."""
    _write_all(tmp_path, _TREE)
    for path in tmp_path.rglob("*.py"):
        os.utime(path, (1_000, 1_000))
    validity = kill_deps.KillValidity(make_layout(tmp_path), support_hash=_SUPPORT, since=2_000)
    assert validity.of(deps) == _validity(tmp_path, deps)

    os.utime(tmp_path / touched, (3_000, 3_000))
    assert not validity.of(deps)
    assert validity.current(deps) == _validity(tmp_path, deps)

    (tmp_path / touched).unlink()
    assert not validity.of(deps)


def test_support_corpus_is_part_of_validity(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", _SOURCE)

    assert _validity(tmp_path, _X, support="a") != _validity(tmp_path, _X, support="b")


def test_encoded_dependencies_round_trip() -> None:
    deps: kill_deps.KillDeps = (("src/x.py", "valid"), ("tests/x_test.py", "test_rejects_long_timeout"))

    assert kill_deps.kill_deps_decode(kill_deps.kill_deps_encode(deps)) == deps


@pytest.mark.parametrize("raw", ["", "{}", "[1]", '[["src/x.py"]]', "not json", '[["src/x.py", 3]]'])
def test_malformed_encoding_decodes_to_nothing(raw: str) -> None:
    assert kill_deps.kill_deps_decode(raw) is None


def test_appended_dependencies_are_read_back_as_one_sorted_set(tmp_path: Path) -> None:
    path = tmp_path / "mutant.json.deps"
    kill_deps.kill_deps_append(path, [("tests/x_test.py", "test_a"), ("src/x.py", "valid")])
    kill_deps.kill_deps_append(path, [("src/x.py", "valid"), ("src/y.py", "other")])

    assert kill_deps.kill_deps_read(path) == (
        ("src/x.py", "valid"),
        ("src/y.py", "other"),
        ("tests/x_test.py", "test_a"),
    )
    assert kill_deps.kill_deps_read(tmp_path / "missing.deps") is None


def test_file_with_a_damaged_line_proves_nothing(tmp_path: Path) -> None:
    """A truncated line means the lease dependencies are incomplete: they cannot prove a kill."""
    path = tmp_path / "mutant.json.deps"
    kill_deps.kill_deps_append(path, [("src/x.py", "valid")])
    with path.open("a", encoding="utf-8") as stream:
        stream.write('[["src/y.py", "oth')

    assert kill_deps.kill_deps_read(path) is None


def test_each_flush_appends_only_what_the_lease_has_not_published(tmp_path: Path) -> None:
    """The file is written after every test of a lease: rewriting it whole would cost O(tests x dependencies)."""
    recorder = kill_deps.DepsRecorder(tmp_path)
    target = tmp_path / "lease.deps"
    recorder.lease_start(target)
    recorder.record(str(tmp_path / "src/x.py"), "valid")
    recorder.flush()
    recorder.flush()
    recorder.record(str(tmp_path / "src/x.py"), "valid")
    recorder.record(str(tmp_path / "src/y.py"), "other")
    recorder.flush()

    assert target.read_text(encoding="utf-8").splitlines() == ['[["src/x.py","valid"]]', '[["src/y.py","other"]]']


def test_recorder_keeps_repository_functions_only(tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path)
    recorder.lease_start(tmp_path / "lease.deps")

    recorder.record(str(tmp_path / "src/x.py"), "valid")
    recorder.record(str(tmp_path / ".venv/lib/python3.14/site-packages/sqlalchemy/orm.py"), "Session.execute")
    recorder.record("/usr/lib/python3.14/json/__init__.py", "dumps")
    recorder.record("<frozen importlib._bootstrap>", "_find_and_load")
    recorder.flush()

    assert kill_deps.kill_deps_read(tmp_path / "lease.deps") == (("src/x.py", "valid"),)


def test_recorder_starts_each_lease_empty_but_keeps_session_level_code(tmp_path: Path) -> None:
    """A session fixture is built once per worker and every later lease uses its result: without carrying it over its
    code would drop out of the dependencies of every kill but the first."""
    recorder = kill_deps.DepsRecorder(tmp_path)
    recorder.lease_start(tmp_path / "first.deps")
    recorder.record(str(tmp_path / "src/x.py"), "valid")
    with recorder.shared_scope():
        recorder.record(str(tmp_path / "tests/conftest.py"), "app")
    recorder.flush()

    recorder.lease_start(tmp_path / "second.deps")
    recorder.record(str(tmp_path / "src/y.py"), "other")
    recorder.flush()

    assert kill_deps.kill_deps_read(tmp_path / "second.deps") == (("src/y.py", "other"), ("tests/conftest.py", "app"))


def test_code_run_before_the_first_lease_joins_every_lease(tmp_path: Path) -> None:
    """Before the first lease come the application import and collection: a call filling a module constant builds
    state every lease of the process reads."""
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    recorder.record(str(tmp_path / "src/config.py"), "load_settings")
    for name in ("first", "second"):
        recorder.lease_start(tmp_path / f"{name}.deps")
        recorder.flush()

    assert kill_deps.kill_deps_read(tmp_path / "first.deps") == (("src/config.py", "load_settings"),)
    assert kill_deps.kill_deps_read(tmp_path / "second.deps") == (("src/config.py", "load_settings"),)


def test_session_part_leaves_out_module_bodies_and_test_modules(tmp_path: Path) -> None:
    """Module-body state is its skeleton, carried by its directory; test modules are held by the import closure of
    the kill, otherwise the parameters of every collected file would join every kill of the campaign."""
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    recorder.record(str(tmp_path / "src/config.py"), "<module>")
    recorder.record(str(tmp_path / "tests/x_test.py"), "_cases")
    recorder.lease_start(tmp_path / "lease.deps")
    recorder.record(str(tmp_path / "src/x.py"), "valid")
    recorder.flush()

    assert kill_deps.kill_deps_read(tmp_path / "lease.deps") == (("src/x.py", "valid"),)


def _loaded(origin: str | None) -> ModuleType:
    """A loaded module; ``None`` means no file (a builtin module, a namespace package)."""
    module = ModuleType("loaded")
    spec = importlib.machinery.ModuleSpec("loaded", None, origin=origin)
    spec.has_location = origin is not None
    module.__spec__ = spec
    return module


def test_directories_of_loaded_repository_modules_join_every_lease(tmp_path: Path) -> None:
    """A module loaded by the process holds constants and registries the run reads without entering its functions;
    one loaded later (a lazy import of a previous lease) stays in the following leases too."""
    modules = {
        "pkg": _loaded(str(tmp_path / "src/pkg/__init__.py")),
        "pkg.settings": _loaded(str(tmp_path / "src/pkg/settings.py")),
        "top": _loaded(str(tmp_path / "top.py")),
        "tests.x_test": _loaded(str(tmp_path / "tests/x_test.py")),
        "sqlalchemy": _loaded(str(tmp_path / ".venv/lib/python3.14/site-packages/sqlalchemy/__init__.py")),
        "json": _loaded("/usr/lib/python3.14/json/__init__.py"),
        "builtins": _loaded(None),
        "shim": cast("ModuleType", SimpleNamespace()),
    }
    recorder = kill_deps.DepsRecorder(tmp_path, modules=modules)
    recorder.lease_start(tmp_path / "first.deps")
    recorder.flush()
    modules["tool"] = _loaded(str(tmp_path / "tools/cli.py"))
    recorder.lease_start(tmp_path / "second.deps")
    recorder.flush()

    assert kill_deps.kill_deps_read(tmp_path / "first.deps") == ((".", "<dir>"), ("src/pkg", "<dir>"))
    assert kill_deps.kill_deps_read(tmp_path / "second.deps") == (
        (".", "<dir>"),
        ("src/pkg", "<dir>"),
        ("tools", "<dir>"),
    )


def test_recorder_writes_nothing_without_a_lease_target(tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path)
    recorder.lease_start(None)
    recorder.record(str(tmp_path / "src/x.py"), "valid")
    recorder.flush()

    assert list(tmp_path.iterdir()) == []


def _file_module(path: Path, name: str) -> ModuleType:
    """Execute a file as module ``name``: an import, as the recorder sees it."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _probe_module(root: Path) -> ModuleType:
    """A module under the probe "repository" root: the recorder sees it and not the tool itself."""
    return _file_module(write_file(root / "src/probe.py", "def probe() -> int:\n    return 1\n"), "kill_deps_probe")


@contextmanager
def _monitored(recorder: kill_deps.DepsRecorder) -> Generator[Callable[[Path], None]]:
    """Real `sys.monitoring` for the probe; yields a lease opener, the way the plugin opens one."""
    disarm = kill_deps.monitoring_arm(recorder, tool_ids=(5, 2, 0, 1, 3, 4))
    assert disarm is not None

    def lease(target: Path) -> None:
        recorder.lease_start(target)
        kill_deps.monitoring_restart()

    try:
        yield lease
    finally:
        disarm()


def test_monitoring_records_first_entry_and_restarts_per_lease(tmp_path: Path) -> None:
    probe = _probe_module(tmp_path).probe
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    with _monitored(recorder) as lease:
        lease(tmp_path / "first.deps")
        probe()
        probe()
        recorder.flush()
        lease(tmp_path / "second.deps")
        probe()
        recorder.flush()

    assert kill_deps.kill_deps_read(tmp_path / "first.deps") == (("src/probe.py", "probe"),)
    assert kill_deps.kill_deps_read(tmp_path / "second.deps") == (("src/probe.py", "probe"),)


def test_functions_called_by_a_lazily_imported_module_join_every_later_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module first imported inside a lease builds state for later leases too: what its body called is a
    dependency of their kills, though they never enter it. What the test itself called belongs to its lease only."""
    helpers_source = "def compute() -> int:\n    return 1\n\n\ndef direct() -> int:\n    return 2\n"
    helpers = _file_module(write_file(tmp_path / "src/helpers.py", helpers_source), "kill_deps_probe_helpers")
    monkeypatch.setitem(sys.modules, "kill_deps_probe_helpers", helpers)
    lazy_source = "import kill_deps_probe_helpers\n\nVALUE = kill_deps_probe_helpers.compute()\n"
    lazy = write_file(tmp_path / "src/lazy.py", lazy_source)
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    with _monitored(recorder) as lease:
        lease(tmp_path / "during.deps")
        _file_module(lazy, "kill_deps_probe_lazy")
        helpers.direct()
        recorder.flush()
        lease(tmp_path / "after.deps")
        recorder.flush()

    called = (("src/helpers.py", "compute"), ("src/helpers.py", "direct"))
    assert kill_deps.kill_deps_read(tmp_path / "during.deps") == called
    assert kill_deps.kill_deps_read(tmp_path / "after.deps") == called[:1]


def test_class_bodies_and_deferred_annotations_are_left_to_the_skeleton(tmp_path: Path) -> None:
    """Class bodies, deferred annotations and module-level expressions run text that is already in the module
    skeleton. Recorded as functions they would bloat every kill with the application's classes, and a class-body hash
    would absorb the bodies of all its methods."""
    source = (
        "class Settings:\n    limit: int = 3\n\n    def bound(self) -> int:\n        return self.limit\n\n\n"
        "TOTAL = sum(value for value in (1, 2))\nANNOTATIONS = Settings.__annotations__\n\n\n"
        "def build() -> int:\n    return 1\n\n\nBUILT = build()\n"
    )
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    with _monitored(recorder) as lease:
        lease(tmp_path / "lease.deps")
        _file_module(write_file(tmp_path / "src/settings.py", source), "kill_deps_probe_settings")
        recorder.flush()

    assert kill_deps.kill_deps_read(tmp_path / "lease.deps") == (("src/settings.py", "build"),)


def test_json_value_type_alias_materializes() -> None:
    value = kill_deps._JsonValue.__value__
    assert isinstance(value, UnionType)


def test_parsed_files_type_alias_materializes() -> None:
    value = kill_deps._ParsedFiles.__value__
    assert isinstance(value, GenericAlias)


def test_tool_ids_are_reserved_slots() -> None:
    assert kill_deps._TOOL_IDS == (5, 2)


def test_kill_deps_encode_keeps_unicode_literal() -> None:
    encoded = kill_deps.kill_deps_encode((("src/x.py", "ƒ"),))
    assert "ƒ" in encoded
    assert "\\u0192" not in encoded


def test_recorder_starts_with_modules_seen_sentinel(tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path)
    assert recorder._modules_seen == -1


def test_relative_skips_site_packages(tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path)
    assert recorder._relative(str(tmp_path / "lib/python/site-packages/x.py")) is None


def test_relative_skips_environment(tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path)
    assert recorder._relative(str(tmp_path / ".venv/x.py")) is None


def test_shared_scope_balances_depth(tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path)
    assert recorder._shared_depth == 0
    with recorder.shared_scope():
        assert recorder._shared_depth == 1
        with recorder.shared_scope():
            assert recorder._shared_depth == 2
        assert recorder._shared_depth == 1
    assert recorder._shared_depth == 0


def test_monitoring_disarm_clears_events(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    freed: list[int] = []
    cleared_events: list[object] = []
    unregistered: list[object] = []

    def use_tool_id(_candidate: int, _name: str) -> None:
        pass

    def register_callback(_tool_id: int, _event: object, func: object) -> None:
        if func is None:
            unregistered.append(func)

    def set_events(_tool_id: int, events: object) -> None:
        cleared_events.append(events)

    monkeypatch.setattr(sys.monitoring, "use_tool_id", use_tool_id)
    monkeypatch.setattr(sys.monitoring, "register_callback", register_callback)
    monkeypatch.setattr(sys.monitoring, "set_events", set_events)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", freed.append)
    disarm = kill_deps.monitoring_arm(recorder)
    assert disarm is not None
    disarm()
    assert 0 in cleared_events
    assert unregistered == [None]
    assert freed


def test_tool_id_take_returns_false_when_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    def use_tool_id(_candidate: int, _name: str) -> None:
        raise ValueError("taken")

    monkeypatch.setattr(sys.monitoring, "use_tool_id", use_tool_id)
    assert kill_deps._tool_id_take(5) is False


def test_changed_since_is_strict(tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text("x = 1\n", encoding="utf-8")
    os.utime(path, (1_000, 1_000))
    assert not kill_deps._changed_since(path, 1_000)
    assert kill_deps._changed_since(path, 999)


def test_kill_validity_of_returns_empty_string_when_changed(tmp_path: Path) -> None:
    write_file(tmp_path / "src/x.py", _SOURCE)
    for path in tmp_path.rglob("*.py"):
        os.utime(path, (1_000, 1_000))
    validity = kill_deps.KillValidity(make_layout(tmp_path), support_hash=_SUPPORT, since=2_000)
    assert validity.of(_X) != ""

    os.utime(tmp_path / "src/x.py", (3_000, 3_000))
    assert validity.of(_X) == ""


def test_kill_validity_of_holds_the_lock(tmp_path: Path) -> None:
    _write_all(tmp_path, _TREE)
    validity = kill_deps.KillValidity(make_layout(tmp_path), support_hash=_SUPPORT)
    entered: list[bool] = []

    class _FakeLock:
        def __enter__(self) -> None:
            entered.append(True)

        def __exit__(self, *_args: object) -> None:
            return None

    validity._lock = cast("threading.Lock", _FakeLock())
    validity.of(_X)
    assert entered == [True]


def test_kill_validity_current_holds_the_lock(tmp_path: Path) -> None:
    _write_all(tmp_path, _TREE)
    validity = kill_deps.KillValidity(make_layout(tmp_path), support_hash=_SUPPORT)
    entered: list[bool] = []

    class _FakeLock:
        def __enter__(self) -> None:
            entered.append(True)

        def __exit__(self, *_args: object) -> None:
            return None

    validity._lock = cast("threading.Lock", _FakeLock())
    validity.current(_X)
    assert entered == [True]


def test_definitions_follow_nested_class_path() -> None:
    tree = ast.parse("class A:\n    class B:\n        def f(self):\n            pass\n")
    found = kill_deps._definitions(tree.body, ("A", "B", "f"))
    assert len(found) == 1


def test_skeleton_visits_class_methods() -> None:
    skeleton = kill_deps._Skeleton(drop_tests=True)
    tree = ast.parse("class A:\n    def method(self):\n        return 1\n")
    classdef = tree.body[0]
    assert isinstance(classdef, ast.ClassDef)
    result = skeleton.visit_ClassDef(classdef)
    method = result.body[0]
    assert isinstance(method, ast.FunctionDef)
    assert isinstance(method.body[0], ast.Pass)


def test_skeleton_visit_classdef_restores_class_stack() -> None:
    skeleton = kill_deps._Skeleton(drop_tests=True)
    tree = ast.parse("class A:\n    def method(self):\n        return 1\n")
    classdef = tree.body[0]
    assert isinstance(classdef, ast.ClassDef)
    result = skeleton.visit_ClassDef(classdef)
    assert result is classdef
    assert skeleton._classes == []


def test_skeleton_keeps_async_function() -> None:
    skeleton = kill_deps._Skeleton(drop_tests=True)
    tree = ast.parse("async def load():\n    return 1\n")
    result = skeleton.visit(tree)
    assert result.body
    assert isinstance(result.body[0], ast.AsyncFunctionDef)


def test_skeleton_is_test_false_for_non_test_names() -> None:
    skeleton = kill_deps._Skeleton(drop_tests=True)
    assert skeleton._is_test("helper") is False


def test_skeleton_is_test_requires_test_class() -> None:
    skeleton = kill_deps._Skeleton(drop_tests=True)
    skeleton._classes.append("TestSuite")
    assert skeleton._is_test("test_x") is True
    skeleton._classes = ["Helper"]
    assert skeleton._is_test("test_x") is False


def test_skeleton_is_test_uses_innermost_class() -> None:
    skeleton = kill_deps._Skeleton(drop_tests=True)
    skeleton._classes = ["Outer", "TestSuite"]
    assert skeleton._is_test("test_x") is True


def test_module_body_running_false_outside_module_body(tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    assert recorder._module_body_running() is False


def test_module_body_running_true_during_module_body(tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    results: list[bool] = []
    helper = ModuleType("_probe_helper")
    vars(helper)["check"] = lambda: results.append(recorder._module_body_running())
    sys.modules["_probe_helper"] = helper
    try:
        probe = write_file(tmp_path / "src/probe_body.py", "import _probe_helper\n_probe_helper.check()\n")
        _file_module(probe, "probe_body")
    finally:
        sys.modules.pop("_probe_helper", None)
    assert results == [True]


def test_decode_empty_list_is_none() -> None:
    assert kill_deps.kill_deps_decode("[]") is None


def test_monitoring_arm_tries_default_tool_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    candidates: list[int] = []

    def use_tool_id(candidate: int, _name: str) -> None:
        candidates.append(candidate)

    monkeypatch.setattr(sys.monitoring, "use_tool_id", use_tool_id)
    monkeypatch.setattr(sys.monitoring, "register_callback", _noop)
    monkeypatch.setattr(sys.monitoring, "set_events", _noop)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", _noop)
    disarm = kill_deps.monitoring_arm(recorder)
    assert disarm is not None
    assert candidates == [5]
    disarm()


def test_monitoring_on_start_records_only_optimized_functions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    recorder.lease_start(tmp_path / "lease.deps")
    captured: dict[str, Callable[[CodeType | None, int], object]] = {}

    def register_callback(_tool_id: int, _event: object, func: Callable[[CodeType | None, int], object]) -> None:
        captured["func"] = func

    monkeypatch.setattr(sys.monitoring, "use_tool_id", _noop)
    monkeypatch.setattr(sys.monitoring, "register_callback", register_callback)
    monkeypatch.setattr(sys.monitoring, "set_events", _noop)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", _noop)
    disarm = kill_deps.monitoring_arm(recorder)
    assert disarm is not None
    on_start = captured["func"]

    # A None code object (a stale event) is skipped without crashing.
    assert on_start(None, 0) is sys.monitoring.DISABLE

    # An optimized function is recorded.
    fn_code = compile("def f():\n    return 1\n", str(tmp_path / "src/m.py"), "exec")
    f_code = next(c for c in fn_code.co_consts if isinstance(c, CodeType))
    assert on_start(f_code, 0) is sys.monitoring.DISABLE

    # A class body (not CO_OPTIMIZED) is left to the skeleton.
    class_code = compile("class C:\n    pass\n", str(tmp_path / "src/c.py"), "exec")
    c_code = next(c for c in class_code.co_consts if isinstance(c, CodeType) and not (c.co_flags & 1))
    assert on_start(c_code, 0) is sys.monitoring.DISABLE

    recorder.flush()
    disarm()
    assert kill_deps.kill_deps_read(tmp_path / "lease.deps") == (("src/m.py", "f"),)


def test_relative_munges_separators_to_forward_slashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Stored dependency paths are POSIX-style. On POSIX os.sep is "/" and a swapped replace would be a no-op, so
    # pin a non-POSIX separator to prove the munging converts it to "/".
    recorder = kill_deps.DepsRecorder(tmp_path)
    monkeypatch.setattr(os, "sep", "\\")
    assert recorder._relative(str(tmp_path / "src" / "x.py")) == "src/x.py"


def test_kill_deps_append_writes_utf8(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The lease file is written UTF-8 explicitly so a unicode qualname survives on any locale; a dropped encoding
    # would fall back to the locale encoding and corrupt or reject the line.
    original_open = io.open
    encodings: list[str | None] = []

    def spy(
        file: str | bytes | int,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> object:
        encodings.append(encoding)
        return original_open(file, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(io, "open", spy)
    kill_deps.kill_deps_append(tmp_path / "x.deps", (("src/x.py", "ƒ"),))
    assert encodings == ["utf-8"]
    assert (tmp_path / "x.deps").read_bytes() == b'[["src/x.py","\xc6\x92"]]\n'


def test_monitoring_arm_returns_none_when_no_tool_id_is_free(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `next(..., None)` defaults to None when every tool id is taken: without the default, `next` raises
    # StopIteration and the worker crashes instead of falling back to the file rule.
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})

    def use_tool_id(_candidate: int, _name: str) -> None:
        raise ValueError("taken")

    monkeypatch.setattr(sys.monitoring, "use_tool_id", use_tool_id)
    assert kill_deps.monitoring_arm(recorder) is None


def test_monitoring_disarm_unregisters_with_tool_id_and_event_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `register_callback(tool_id, PY_START, None)` must keep the tool id first: the monitoring API is
    # (tool_id, event, callback), and a swapped pair would register the wrong id.
    recorder = kill_deps.DepsRecorder(tmp_path, modules={})
    calls: list[tuple[int, object, object]] = []

    def use_tool_id(_candidate: int, _name: str) -> None:
        pass

    def register_callback(tool_id: int, event: object, func: object) -> None:
        calls.append((tool_id, event, func))

    def set_events(_tool_id: int, _events: object) -> None:
        pass

    monkeypatch.setattr(sys.monitoring, "use_tool_id", use_tool_id)
    monkeypatch.setattr(sys.monitoring, "register_callback", register_callback)
    monkeypatch.setattr(sys.monitoring, "set_events", set_events)
    monkeypatch.setattr(sys.monitoring, "free_tool_id", _noop)
    disarm = kill_deps.monitoring_arm(recorder)
    assert disarm is not None
    disarm()
    assert calls[-1] == (5, sys.monitoring.events.PY_START, None)
