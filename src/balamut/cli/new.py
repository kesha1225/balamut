"""``balamut new``: scaffold the project config in place.

Writes ``[tool.balamut] config = "balamut_conf.py"`` into ``pyproject.toml`` (created with a minimal
``[project]`` when missing) and the config file itself, with the layout the directory tree implies and the
repository's live integration branch as the diff base:

- ``src/`` exists -> ``source=("src",)``, ``import_roots=("src",)``;
- else a directory named after the project -> ``source=("<name>",)``, ``import_roots=(".",)``: the directory
  is the package itself, imported from the project root;
- else a flat layout -> ``source=(".",)``, ``import_roots=(".",)``.

The diff base is the live ``main``/``master`` branch when the repository has one, else ``origin/main``.
An existing config (either half) is never overwritten: the command refuses and exits 1.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

_PROJECT_TEMPLATE = '[project]\nname = "{name}"\nversion = "0.1.0"\n'
_CONFIG_HEADER = '[tool.balamut]\nconfig = "balamut_conf.py"\n'


def _error(message: str) -> int:
    sys.stderr.write(f"error: {message}\n")
    return 1


def _project_name(root: Path) -> str:
    """The ``[project].name`` of the pyproject, else the directory name."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
            return root.name
        project = document.get("project")
        if isinstance(project, dict):
            # A TOML document is dict[str, Any]; after the isinstance guard the shape is fixed.
            table = cast("dict[str, object]", project)
            name = table.get("name")
            if isinstance(name, str) and name:
                return name
    return root.name


def _source_layout(root: Path, name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(source, import_roots) implied by the directory tree.

    ``src/`` holds the packages, so it is their import root. A directory named after the project is the package
    itself: its modules import as ``<name>.mod`` from the project root, and taking the directory as the root would
    name them ``mod``, which no test imports."""
    if (root / "src").is_dir():
        return ("src",), ("src",)
    if (root / name).is_dir():
        return (name,), (".",)
    return (".",), (".",)


def _live_base(root: Path) -> str:
    """The live integration branch: ``main``/``master`` when it exists, else ``origin/main``.

    A freshly initialized repository has no commit, so its branch exists only as the HEAD
    symbolic target; both shapes are probed."""
    for branch in ("main", "master"):
        probe = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return branch
    head = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if head.returncode == 0:
        branch = head.stdout.decode("utf-8").strip()
        if branch in {"main", "master"}:
            return branch
    return "origin/main"


def _write_pyproject(root: Path) -> int:
    """Add the ``[tool.balamut]`` table (or create a minimal pyproject around it)."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        if "[tool.balamut]" in text or "[tool.balamut." in text:
            return _error(f"{pyproject}: already declares [tool.balamut]")
        pyproject.write_text(text.rstrip() + "\n\n" + _CONFIG_HEADER, encoding="utf-8")
        return 0
    pyproject.write_text(
        _PROJECT_TEMPLATE.format(name=_project_name(root)) + "\n" + _CONFIG_HEADER,
        encoding="utf-8",
    )
    return 0


def _tupled(items: tuple[str, ...]) -> str:
    """``("a",)`` / ``("a", "b")`` — the config file's double-quoted tuple literal."""
    inner = ", ".join(f'"{item}"' for item in items)
    return f"({inner},)" if len(items) == 1 else f"({inner})"


def main(argv: list[str]) -> int:
    if argv and argv != ["--force"]:
        return _error(f"unknown argument(s): {' '.join(argv)}")
    force = bool(argv)
    root = Path.cwd().resolve()
    if (root / "balamut_conf.py").is_file() and not force:
        return _error("balamut_conf.py already exists (--force overwrites it)")
    if _write_pyproject(root) != 0:
        return 1
    name = _project_name(root)
    source, import_roots = _source_layout(root, name)
    base = _live_base(root)
    fields = [f"    source={_tupled(source)},", f"    import_roots={_tupled(import_roots)},"]
    if base != "origin/main":
        fields.append(f'    base="{base}",')
    body = "\n".join(["from balamut.config import init", "", "init(", *fields, ")", ""])
    (root / "balamut_conf.py").write_text(body, encoding="utf-8")
    sys.stderr.write(
        f"wrote {root / 'pyproject.toml'} and {root / 'balamut_conf.py'} (source={source}, base={base!r})\n"
    )
    return 0
