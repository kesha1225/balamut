"""Re-export discovery: which wrapper file pytest collects a re-exported test under.

pytest collects a ``test_*`` function re-exported into ``x_test.py`` from a helper module under the node id of
the wrapper (``x_test.py::test_foo``), not of the defining file. The index is keyed by name, not only by file
pair: a wrapper may import only some of a helper's tests, and the others are collected nowhere.
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from balamut.config import Layout

type ReexportMap = dict[str, str]  # defining file -> wrapper file (posix, relative to the root)
type ReexportIndex = dict[tuple[str, str], str]  # (defining file, top-level name) -> wrapper file
type ReexportSnapshotMap = Mapping[str, str]
type ReexportSnapshotIndex = Mapping[tuple[str, str], str]
type DefinesTestsCache = dict[Path, bool]

# A cheap text prefilter runs before the parser: a file can contribute an index entry only when its text has a
# top-level `from <test-root>.<...> import ...` into an existing non-test module that declares a module-level
# `test_*`/`Test*`. The patterns are deliberately wider than the syntax (any whitespace, backslash
# continuations, a form feed before `def`): the prefilter may only skip files that certainly contribute
# nothing, so it can err only towards an extra parse. The parser remains the authority.
_TEST_NAME_RE = re.compile(rb"^[^\S\n]*(?:async[\s\\]+def[\s\\]+test_|def[\s\\]+test_|class[\s\\]+Test)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ReexportSnapshot:
    """The immutable re-export index of one invocation and its file-level projection."""

    index: ReexportSnapshotIndex
    by_file: ReexportSnapshotMap


def _root_pattern(module_root: str) -> bytes:
    return rb"[\s\\]*\.[\s\\]*".join(re.escape(part.encode()) for part in module_root.split("."))


def _module_test_names(def_file: Path) -> set[str]:
    """Module-level ``test_*`` functions and ``Test*`` classes: what ``import *`` re-exports."""
    try:
        tree = ast.parse(def_file.read_text(encoding="utf-8"))
    except OSError, SyntaxError, UnicodeDecodeError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() if node.name.startswith("test_"):
                names.add(node.name)
            case ast.ClassDef() if node.name.startswith("Test"):
                names.add(node.name)
            case _:
                pass
    return names


def _defines_collectable_tests(def_file: Path, cache: DefinesTestsCache) -> bool:
    """Does a defining file declare any module-level ``test_*``/``Test*``? A necessary condition only."""
    known = cache.get(def_file)
    if known is not None:
        return known
    try:
        defines = _TEST_NAME_RE.search(def_file.read_bytes()) is not None
    except OSError:
        defines = False
    cache[def_file] = defines
    return defines


@dataclass(frozen=True, slots=True)
class _Prefilter:
    any_import: re.Pattern[bytes]
    module_import: re.Pattern[bytes]


def _prefilter(module_roots: tuple[str, ...]) -> _Prefilter:
    roots = b"|".join(_root_pattern(root) for root in module_roots)
    return _Prefilter(
        any_import=re.compile(rb"from[\s\\]+(?:" + roots + rb")[\s\\]*\."),
        module_import=re.compile(rb"from[\s\\]+((?:" + roots + rb")(?:[\s\\]*\.[\s\\]*\w+)+)[\s\\]+import"),
    )


def _wrapper_may_reexport(layout: Layout, source: bytes, prefilter: _Prefilter, cache: DefinesTestsCache) -> bool:
    """Can this wrapper contribute an index entry, judging by its text alone?

    No ``from <test-root>.`` at all: certainly not. Fewer recognized imports than occurrences (a module name the
    byte ``\\w`` cannot read, such as a non-ASCII one): the decision goes to the parser. Comparing counts matters:
    "recognized at least one" would treat a wrapper with one plain import into a helper without tests and one
    unrecognized import as fully read and drop it, losing the re-exported test."""
    occurrences = len(prefilter.any_import.findall(source))
    if occurrences == 0:
        return False
    modules = prefilter.module_import.findall(source)
    if len(modules) < occurrences:
        return True
    for raw in modules:
        # Whitespace and backslashes are logical-line joins, not part of the name.
        module = "".join(raw.decode("utf-8", "replace").split()).replace("\\", "")
        def_file = Path(module.replace(".", "/") + ".py")
        if layout.is_test_file(def_file.name):
            continue
        if _defines_collectable_tests(layout.root / def_file, cache):
            return True
    return False


def _snapshot_build(layout: Layout) -> ReexportSnapshot:
    module_roots = tuple(root.strip("/").replace("/", ".") for root in layout.tests if root not in {".", ""})
    index: ReexportIndex = {}
    if not module_roots:
        return ReexportSnapshot(index=MappingProxyType(index), by_file=MappingProxyType({}))
    prefilter = _prefilter(module_roots)
    defines_cache: DefinesTestsCache = {}
    root = layout.root
    for raw in layout.tests:
        base = root / raw
        if not base.is_dir():
            continue
        for wrapper in sorted(base.rglob("*.py")):
            if not layout.is_test_file(wrapper.name):
                continue
            try:
                source = wrapper.read_bytes()
            except OSError:
                continue
            if not _wrapper_may_reexport(layout, source, prefilter, defines_cache):
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError, ValueError:
                continue
            for node in tree.body:
                if not (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and any(node.module.startswith(f"{module_root}.") for module_root in module_roots)
                ):
                    continue
                def_file = Path(node.module.replace(".", "/") + ".py")
                absolute = root / def_file
                if not (absolute.is_file() and not layout.is_test_file(def_file.name)):
                    continue
                imported = {alias.name for alias in node.names}
                defined_tests = _module_test_names(absolute)
                names = defined_tests if "*" in imported else imported & defined_tests
                for name in names:
                    index[def_file.as_posix(), name] = wrapper.relative_to(root).as_posix()
    by_file = {def_file: wrapper for (def_file, _name), wrapper in index.items()}
    return ReexportSnapshot(index=MappingProxyType(index), by_file=MappingProxyType(by_file))


@functools.cache
def reexport_snapshot(layout: Layout) -> ReexportSnapshot:
    """One re-export snapshot per layout for the life of the process (see ``reexport_cache_clear``)."""
    return _snapshot_build(layout)


def reexport_cache_clear() -> None:
    """The explicit invalidation boundary for tests and long-lived callers."""
    reexport_snapshot.cache_clear()


def reexport_index(layout: Layout) -> ReexportIndex:
    """(defining file, top-level name) -> wrapper file for every re-exported test."""
    return dict(reexport_snapshot(layout).index)


def reexport_map(layout: Layout) -> ReexportMap:
    """Defining file -> wrapper file: the file-level projection of ``reexport_index``."""
    return dict(reexport_snapshot(layout).by_file)
