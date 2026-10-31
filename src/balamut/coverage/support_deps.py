"""Which test files an edit of test-support files can reach: a static import closure plus conftest scope.

The substrate measures coverage of the source tree, so it does not know which support code a test executed.
Without this, editing any helper would force a full substrate rebuild, although a helper is typically imported
by a handful of test files. The dependency is derived statically and generously, since an extra
re-measurement is cheaper than a missed one:

- a test file depends on everything it imports: directly, transitively, relatively, or by a string holding a
  module name (``pytest_plugins``, ``importlib.import_module``);
- a bare name (``import _helpers``) is a sibling in a directory without ``__init__.py``: pytest puts such a
  directory on sys.path, and which same-named sibling wins depends on collection order, so all are taken;
- on the conftest.py of every directory on its path: fixtures and hooks reach tests without an import;
- on the ``__init__.py`` of the packages of its modules: any import executes them;
- a file that does not parse depends on every edit: its imports are unknown.
"""

from __future__ import annotations

import ast
import functools
from collections import deque
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from balamut.config import Layout

_CONFTEST = "conftest"
_INIT = "__init__"
# Parsed files kept in memory: the whole test tree and the edits made during a run.
_PARSED_CACHE_SIZE = 8192

type ImportersByModule = dict[str, set[str]]
type BareRoots = dict[str, tuple[str, ...]]  # bare name -> directories without __init__.py holding it
type _RootsByName = dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class _Module:
    path: str  # posix, relative to the repository root
    imports: frozenset[str]  # test-tree modules its import executes
    parsed: bool


type ModulesByName = dict[str, _Module]


def module_name(path: str) -> str:
    """``tests/a/b.py`` -> ``tests.a.b``; ``tests/a/__init__.py`` -> ``tests.a``."""
    parts = Path(path).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == _INIT else parts)


def _ancestors(name: str) -> Iterator[str]:
    """Packages an import of ``name`` executes: ``tests.a.b`` -> ``tests.a``, ``tests``."""
    parts = name.split(".")
    for depth in range(len(parts) - 1, 0, -1):
        yield ".".join(parts[:depth])


def imported_modules(node: ast.AST, file: str) -> Iterator[str]:
    """Modules named by the import ``node`` in ``file`` (relative to the root): the module itself and the names
    imported from it, which may be submodules. Relative imports resolve against the file's directory; a bare
    name stays bare."""
    if isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
        return
    if not isinstance(node, ast.ImportFrom):
        return
    if node.level:
        package = PurePosixPath(file).parent.parts
        if node.level - 1 > len(package):
            return
        base = [*package[: len(package) - (node.level - 1)], *(node.module.split(".") if node.module else ())]
    else:
        base = node.module.split(".") if node.module else []
    if not base:
        return
    yield ".".join(base)
    yield from (".".join((*base, alias.name)) for alias in node.names if alias.name != "*")


def _referenced(tree: ast.Module, relative: str, roots: tuple[str, ...]) -> frozenset[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _in_tests(node.value, roots):
            found.add(node.value)
        else:
            found.update(imported_modules(node, relative))
    return frozenset(found)


@functools.lru_cache(maxsize=_PARSED_CACHE_SIZE)
def file_references(relative: str, text: str, roots: tuple[str, ...] = ("tests",)) -> frozenset[str] | None:
    """Module references of ``relative`` with content ``text``; None when the file does not parse.

    Cached by content: the question is asked several times per run (tree comparison, each pool candidate,
    refresh) while parsing the whole test tree takes seconds. An edited file is a different key."""
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError, ValueError:
        return None
    return _referenced(tree, relative, roots)


def _in_tests(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == root or name.startswith(f"{root}.") for root in roots)


def _bare_roots(paths: Collection[str], names: Collection[str], roots: tuple[str, ...]) -> BareRoots:
    """Test-tree directories without ``__init__.py``, grouped by the module and package names they hold."""
    present = set(paths)
    root_parts = {tuple(root.split(".")) for root in roots}
    dirs = {
        ".".join(directory.parts)
        for path in paths
        for directory in PurePosixPath(path).parents
        if any(directory.parts[: len(parts)] == parts for parts in root_parts)
        and f"{directory.as_posix()}/{_INIT}.py" not in present
    }
    by_name: _RootsByName = {}
    for name in names:
        parent, _, last = name.rpartition(".")
        if parent in dirs:
            by_name.setdefault(last, []).append(parent)
    return {last: tuple(sorted(parents)) for last, parents in by_name.items()}


def _resolve(references: frozenset[str], bare: BareRoots, roots: tuple[str, ...]) -> set[str]:
    """Test-tree modules the references reach, with their parent packages. A string ``tests.x.y.attr`` names a
    module attribute and ``from tests.x import y`` may too: every prefix is taken, an extra one costs nothing."""
    resolved: set[str] = set()
    for reference in references:
        top = reference.partition(".")[0]
        if _in_tests(reference, roots):
            candidates: tuple[str, ...] = (reference,)
        else:
            candidates = tuple(f"{root}.{reference}" for root in bare.get(top, ()))
        for candidate in candidates:
            resolved.update(prefix for prefix in (candidate, *_ancestors(candidate)) if _in_tests(prefix, roots))
    return resolved


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _modules(layout: Layout, roots: tuple[str, ...]) -> ModulesByName:
    paths: list[Path] = []
    for test_root in layout.tests:
        base = layout.root / test_root
        if base.is_dir():
            paths.extend(sorted(base.rglob("*.py")))
    sources = {path.relative_to(layout.root).as_posix(): _read(path) for path in paths}
    bare = _bare_roots(sources, [module_name(relative) for relative in sources], roots)
    modules: ModulesByName = {}
    for relative, text in sources.items():
        name = module_name(relative)
        references = file_references(relative, text, roots) if text is not None else None
        if references is None:
            modules[name] = _Module(relative, frozenset(), parsed=False)
            continue
        imports = _resolve(references, bare, roots) | set(_ancestors(name))
        modules[name] = _Module(relative, frozenset(imports), parsed=True)
    return modules


def _importers(modules: ModulesByName) -> ImportersByModule:
    importers: ImportersByModule = {}
    for name, module in modules.items():
        for imported in module.imports:
            importers.setdefault(imported, set()).add(name)
    return importers


def _reach(start: Iterable[str], importers: ImportersByModule) -> set[str]:
    """Modules whose import executes any of ``start``, transitively, ``start`` included."""
    reached = set(start)
    queue = deque(reached)
    while queue:
        for importer in importers.get(queue.popleft(), ()):
            if importer not in reached:
                reached.add(importer)
                queue.append(importer)
    return reached


def support_dependents(layout: Layout, changed: Iterable[str]) -> tuple[str, ...]:
    """Collectable test files (relative paths) whose coverage an edit of ``changed`` may have altered.

    A deleted file counts too: its name still reaches whoever imports it, and a deleted conftest reaches the
    directory whose fixture resolution it changed."""
    start = {module_name(path) for path in changed}
    if not start:
        return ()
    roots = tuple(root.strip("/").replace("/", ".") for root in layout.tests if root not in {".", ""})
    modules = _modules(layout, roots)
    reached = _reach(start | {name for name, module in modules.items() if not module.parsed}, _importers(modules))
    scopes = tuple(
        name.rpartition(".")[0].replace(".", "/") + "/" for name in reached if name.rpartition(".")[2] == _CONFTEST
    )
    return tuple(
        sorted(
            module.path
            for name, module in modules.items()
            if layout.is_test_file(Path(module.path).name) and (name in reached or module.path.startswith(scopes))
        )
    )
