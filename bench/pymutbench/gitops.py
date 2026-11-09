"""Git plumbing of the corpus: classifying a fix commit's files and materializing the judged trees.

For a fix commit ``C`` with parent ``P`` and source files ``S`` changed by the fix, four trees are built:

* ``fixed``  — ``C`` as committed: the fixed source and the fix's tests (where the triggering tests run);
* ``buggy``  — ``C`` with ``S`` reverted to ``P``: the fix's tests against the buggy source;
* ``old``    — ``P`` with ``S`` taken from ``C``: the historical suite against the fixed source (the mutated tree);
* ``parent`` — ``P`` as committed: the historical suite against the buggy source.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_NON_SOURCE_TOP = frozenset(
    {"docs", "doc", "examples", "example", "benchmarks", "benchmark", "bench", "scripts", "tools", "tasks", ".github",
     "ci", "ci_tools", "misc", "typing_tests", "changelog.d", "tests", "test", "testing"}
)
_NON_SOURCE_NAMES = frozenset({"setup.py", "noxfile.py", "conftest.py", "fabfile.py", "tasks.py", "conf.py"})


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout  # noqa: S607


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if not name.endswith(".py"):
        return False
    if any(part in {"tests", "test", "testing"} for part in parts[:-1]):
        return True
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def is_source_path(path: str, packages: tuple[str, ...]) -> bool:
    """A Python file of one of the subject's packages (``src/pkg/...`` or ``pkg/...`` or a top-level module)."""
    if not path.endswith(".py") or is_test_path(path):
        return False
    parts = path.split("/")
    if parts[0] in _NON_SOURCE_TOP or parts[-1] in _NON_SOURCE_NAMES:
        return False
    rel = parts[1:] if parts[0] == "src" else parts
    head = rel[0].removesuffix(".py")
    return head in packages


@dataclass(frozen=True, slots=True)
class FileChange:
    status: str  # A, M or D
    path: str
    added: int
    removed: int


def changed_files(repo: Path, parent: str, commit: str) -> list[FileChange]:
    status = {}
    for line in git(repo, "diff", "--no-renames", "--name-status", parent, commit).splitlines():
        code, _, path = line.partition("\t")
        status[path] = code[0]
    changes = []
    for line in git(repo, "diff", "--no-renames", "--numstat", parent, commit).splitlines():
        added, removed, path = line.split("\t", 2)
        if added == "-":
            continue  # binary
        changes.append(FileChange(status.get(path, "M"), path, int(added), int(removed)))
    return changes


def add_worktree(repo: Path, path: Path, commit: str) -> None:
    git(repo, "worktree", "add", "--detach", "--force", "--quiet", str(path), commit)


def remove_worktree(repo: Path, path: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)], capture_output=True, check=False)  # noqa: S607


def take_files(tree: Path, commit: str, changes: list[FileChange], *, from_side: str) -> None:
    """Overwrite ``changes`` in ``tree`` with their state at ``commit``.

    ``from_side`` says which side of the diff ``commit`` is: "parent" (files added by the fix do not exist
    there and are removed) or "fix" (files deleted by the fix do not exist there and are removed).
    """
    absent = "A" if from_side == "parent" else "D"
    present = [change.path for change in changes if change.status != absent]
    for change in changes:
        if change.status == absent:
            (tree / change.path).unlink(missing_ok=True)
    if present:
        git(tree, "checkout", commit, "--", *present)


def diff_text(repo: Path, parent: str, commit: str, paths: list[str]) -> str:
    return git(repo, "diff", "--no-renames", "-U3", parent, commit, "--", *paths)
