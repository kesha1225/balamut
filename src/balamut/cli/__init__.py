"""The ``balamut`` command: find the project root, change into it and run a subcommand.

Every subcommand is a ``main(argv) -> int`` that works from the project root: ``[tool.balamut]`` paths, the git
calls and the pytest runs are all relative to it.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from balamut import __version__
from balamut.cli import analyze, confirm, crosskill, diff, estimate, live, mutate, new, substrate_cmd

type CommandMain = Callable[[list[str]], int]
type CommandTable = Mapping[str, Command]


@dataclass(frozen=True, slots=True)
class Command:
    main: CommandMain
    summary: str


COMMANDS: CommandTable = {
    "diff": Command(diff.main, "the diff mutation gate: substrate, campaign over the diff, verdict report"),
    "doctor": Command(diff.doctor_main, "check the substrate, the isolation target and the preflight (read-only)"),
    "substrate": Command(substrate_cmd.main, "build or reuse the coverage substrate of the current tree"),
    "mutate": Command(mutate.main, "run a mutation campaign over paths or a diff"),
    "confirm": Command(confirm.main, "re-run the mutants of some paths after measuring new tests"),
    "live": Command(live.main, "run a campaign with a live dashboard and an honest ETA"),
    "estimate": Command(estimate.main, "estimate the duration of a campaign before running it"),
    "analyze": Command(analyze.main, "summarize the cached verdicts of the project"),
    "crosskill": Command(crosskill.main, "the kill matrix: redundant and fragile tests"),
    "new": Command(new.main, "scaffold the project config: pyproject wiring, the config file, layout and diff base"),
}


def _declares_balamut(pyproject: Path) -> bool:
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        return False
    tool = document.get("tool")
    return isinstance(tool, dict) and "balamut" in tool


def find_project_root(start: Path) -> Path:
    """The nearest ancestor whose pyproject.toml has ``[tool.balamut]``, else the nearest with a pyproject.toml,
    else ``start`` itself."""
    resolved = start.resolve()
    candidates = [resolved, *resolved.parents]
    for directory in candidates:
        if _declares_balamut(directory / "pyproject.toml"):
            return directory
    for directory in candidates:
        if (directory / "pyproject.toml").is_file():
            return directory
    return resolved


def _usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = [
        f"balamut {__version__}: mutation testing on a warm pytest-fast pool",
        "",
        "usage: balamut [--root DIR] COMMAND [ARGS]",
        "",
    ]
    lines += [f"  {name:<{width}}  {command.summary}" for name, command in COMMANDS.items()]
    lines += ["", "Run `balamut COMMAND --help` for the options of a command."]
    return "\n".join(lines) + "\n"


def dispatch(argv: list[str]) -> int:
    args = list(argv)
    root: Path | None = None
    if len(args) >= 2 and args[0] == "--root":
        root = Path(args[1])
        args = args[2:]
    if not args:
        sys.stderr.write(_usage())
        return 2
    name, rest = args[0], args[1:]
    if name in {"-h", "--help", "help"}:
        sys.stdout.write(_usage())
        return 0
    if name == "--version":
        sys.stdout.write(f"balamut {__version__}\n")
        return 0
    command = COMMANDS.get(name)
    if command is None:
        sys.stderr.write(f"error: unknown command {name!r}\n\n{_usage()}")
        return 2
    os.chdir(root.resolve() if root is not None else find_project_root(Path.cwd()))
    return command.main(rest)


def main_cli() -> int:
    return dispatch(sys.argv[1:])
