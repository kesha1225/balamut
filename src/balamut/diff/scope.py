"""The immutable git scope of one diff run: refs, the diff base, changed lines and content hashes.

The verdict of a diff run is about content, not about where refs point: a ``git fetch`` in any worktree moves the
shared base ref, and committing the edits under check moves HEAD, while the diff against the pinned base stays
the same. Only a change of that diff or of a scope file's content invalidates the run.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TypedDict

from balamut import gitutil


class BaseMode(StrEnum):
    """How the commit the diff is computed against is chosen."""

    MERGE_BASE = "merge-base"
    DIRECT = "direct"


type ChangedLines = dict[str, tuple[int, ...]]  # an empty tuple means the whole file (untracked)
type _ChangedPaths = tuple[str, ...]
type _ChangedLineSets = dict[str, set[int]]
type FileHashes = dict[str, str]
type _SerializedChangedLines = dict[str, list[int]]
type GitCall = Callable[..., str]

MISSING_FILE_HASH = "missing"


@dataclass(frozen=True, slots=True)
class DiffScope:
    """The pinned revisions, changed lines and file hashes of one run."""

    base_ref: str
    base_sha: str
    diff_base_sha: str
    head_sha: str
    changed_lines: ChangedLines
    file_hashes: FileHashes


class _ScopeRecord(TypedDict):
    base_ref: str
    base_sha: str
    diff_base_sha: str
    head_sha: str
    changed_lines: _SerializedChangedLines
    file_hashes: FileHashes


@dataclass(frozen=True, slots=True)
class Git:
    """git calls bound to one repository: ``output`` strips the result, ``raw`` keeps separators intact."""

    output: GitCall
    raw: GitCall


def git_at(root: Path) -> Git:
    def output(*args: str) -> str:
        return gitutil.git_output(*args, cwd=root)

    def raw(*args: str) -> str:
        return gitutil.git_output_raw(*args, cwd=root)

    return Git(output=output, raw=raw)


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF_DST_PREFIXES = ("b/", "w/", "i/", "c/", "o/", "a/")
_C_ESCAPE_BYTES = {"a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A, "v": 0x0B, "f": 0x0C, "r": 0x0D, '"': 0x22, "\\": 0x5C}
_OCTAL_DIGITS = frozenset(string.octdigits)


def _git_unquote(target: str) -> str:
    """Decode a path from git's C-quoted form."""
    if not (len(target) >= 2 and target.startswith('"') and target.endswith('"')):
        return target
    inner = target[1:-1]
    result = bytearray()
    index = 0
    while index < len(inner):
        char = inner[index]
        if char != "\\":
            result.extend(char.encode("utf-8"))
            index += 1
            continue
        if index + 1 >= len(inner):
            result.append(0x5C)
            index += 1
            continue
        escaped = inner[index + 1]
        if escaped in _OCTAL_DIGITS:
            end = index + 1
            while end < len(inner) and end < index + 4 and inner[end] in _OCTAL_DIGITS:
                end += 1
            result.append(int(inner[index + 1 : end], 8) & 0xFF)
            index = end
            continue
        escaped_byte = _C_ESCAPE_BYTES.get(escaped)
        if escaped_byte is not None:
            result.append(escaped_byte)
            index += 2
            continue
        result.extend(inner[index : index + 2].encode("utf-8"))
        index += 2
    return result.decode("utf-8", "replace")


def _strip_diff_prefix(target: str) -> str:
    """Decode a path and remove one git diff prefix."""
    unquoted = _git_unquote(target)
    for prefix in _DIFF_DST_PREFIXES:
        if unquoted.startswith(prefix):
            return unquoted[len(prefix) :]
    return unquoted


def changed_lines_from_diff(diff_text: str) -> ChangedLines:
    """``git diff --unified=0`` -> the right-hand-side lines per file."""
    changed_sets: _ChangedLineSets = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = None if target == "/dev/null" else _strip_diff_prefix(target)
            continue
        if current is None:
            continue
        match = _HUNK.match(line)
        if match is None:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        if count > 0:
            changed_sets.setdefault(current, set()).update(range(start, start + count))
    return {path: tuple(sorted(lines)) for path, lines in changed_sets.items()}


def mark_untracked_whole(changed: ChangedLines, untracked: Iterable[str]) -> ChangedLines:
    """Mark untracked files with an empty tuple, meaning the whole file."""
    for path in untracked:
        changed.setdefault(path, ())
    return changed


def mutant_in_scope(path: str, site_lineno: int, site_end_lineno: int, changed_lines: ChangedLines) -> bool:
    """Does a mutant's full span intersect the pinned lines?"""
    lines = changed_lines.get(path)
    if lines is None:
        return False
    if not lines:
        return True
    return any(line in lines for line in range(site_lineno, site_end_lineno + 1))


def _git_diff_output(call: GitCall, diff_base_sha: str, *args: str) -> str:
    # Pin the diff format against user configuration that would change prefixes or quote paths.
    return call(
        "-c",
        "diff.mnemonicPrefix=false",
        "-c",
        "diff.noprefix=false",
        "-c",
        "core.quotepath=false",
        "diff",
        *args,
        diff_base_sha,
    )


def _nul_paths(text: str) -> _ChangedPaths:
    return tuple(path for path in text.split("\0") if path)


def _changed_state_resolve(diff_base_sha: str, git: Git) -> tuple[ChangedLines, _ChangedPaths]:
    diff = _git_diff_output(git.output, diff_base_sha, "--unified=0")
    tracked = _nul_paths(_git_diff_output(git.raw, diff_base_sha, "--name-only", "-z"))
    # Only untracked Python sources: running the suite writes bytecode caches, logs and coverage files, and such
    # artifacts would change the pinned scope while the gate runs. Nothing but Python can be mutated anyway.
    untracked = tuple(
        path for path in _nul_paths(git.raw("ls-files", "-z", "--others", "--exclude-standard")) if path.endswith(".py")
    )
    changed = changed_lines_from_diff(diff)
    changed_paths = tuple(sorted({*tracked, *untracked}))
    return mark_untracked_whole(changed, untracked), changed_paths


def _content_hash(path: Path) -> str:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return MISSING_FILE_HASH
    return hashlib.sha256(content).hexdigest()


def _changed_file_hashes(changed_paths: Iterable[str], repo_root: Path) -> FileHashes:
    return {path: _content_hash(repo_root / path) for path in changed_paths}


def resolve_scope(base_ref: str, *, repo_root: Path, base_mode: BaseMode = BaseMode.MERGE_BASE) -> DiffScope:
    """Resolve the refs and pin the current diff with the file hashes."""
    git = git_at(repo_root)
    base_sha = git.output("rev-parse", base_ref)
    head_sha = git.output("rev-parse", "HEAD")
    match base_mode:
        case BaseMode.MERGE_BASE:
            diff_base_sha = git.output("merge-base", base_sha, head_sha)
        case BaseMode.DIRECT:
            diff_base_sha = base_sha
    changed_lines, changed_paths = _changed_state_resolve(diff_base_sha, git)
    return DiffScope(
        base_ref=base_ref,
        base_sha=base_sha,
        diff_base_sha=diff_base_sha,
        head_sha=head_sha,
        changed_lines=changed_lines,
        file_hashes=_changed_file_hashes(changed_paths, repo_root),
    )


def write_scope(scope: DiffScope, path: Path) -> None:
    record: _ScopeRecord = {
        "base_ref": scope.base_ref,
        "base_sha": scope.base_sha,
        "diff_base_sha": scope.diff_base_sha,
        "head_sha": scope.head_sha,
        "changed_lines": {file: list(lines) for file, lines in scope.changed_lines.items()},
        "file_hashes": scope.file_hashes,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_scope(path: Path) -> DiffScope:
    record: _ScopeRecord = json.loads(path.read_text(encoding="utf-8"))
    return DiffScope(
        base_ref=record["base_ref"],
        base_sha=record["base_sha"],
        diff_base_sha=record["diff_base_sha"],
        head_sha=record["head_sha"],
        changed_lines={file: tuple(lines) for file, lines in record["changed_lines"].items()},
        file_hashes=dict(record["file_hashes"]),
    )


@dataclass(frozen=True, slots=True)
class ScopeChanges:
    """How the tree diverged from the pinned scope by the end of a run.

    ``current``: the checked content still matches (the same changed lines against the pinned diff base and the
    same file hashes). ``paths``: files whose lines or content diverged. ``moved_refs``: refs (``base_ref``,
    ``head``) that moved during the run; on their own they never invalidate the verdict."""

    current: bool
    paths: tuple[str, ...]
    moved_refs: tuple[str, ...]


def _diverged_paths(actual_lines: ChangedLines, actual_hashes: FileHashes, scope: DiffScope) -> tuple[str, ...]:
    paths = {*actual_lines, *scope.changed_lines, *actual_hashes, *scope.file_hashes}
    return tuple(
        sorted(
            path
            for path in paths
            if actual_lines.get(path) != scope.changed_lines.get(path)
            or actual_hashes.get(path) != scope.file_hashes.get(path)
        )
    )


def scope_files_changed(scope: DiffScope, *, repo_root: Path) -> tuple[str, ...]:
    """Scope files whose content diverged from the pinned hash.

    Hashes only, no git: this catches an edit of an already changed file (the usual edit during a run) in
    milliseconds. A new file joining the diff is seen only by the full ``scope_changes``."""
    edited = _changed_file_hashes(scope.file_hashes, repo_root)
    return tuple(sorted(path for path, content in edited.items() if content != scope.file_hashes[path]))


@dataclass(slots=True)
class ScopeWatch:
    """A campaign-long watch over the scope files, checked at most once per ``interval`` seconds.

    An edit of a scope file makes every later verdict a verdict about mixed revisions, and the final check would
    reject the run anyway. Once triggered the watch keeps the changed files for every worker: each stops at its
    next mutant boundary instead of draining the queue."""

    scope: DiffScope
    repo_root: Path
    interval: float = 2.0
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _checked_at: float | None = field(default=None, init=False)
    _changed: tuple[str, ...] = field(default=(), init=False)

    def changed(self, *, force: bool = False) -> tuple[str, ...]:
        """Diverged scope files; empty while the scope is intact or the next check is not due yet.

        ``force`` checks right away, for instance when a daemon has just reported changed sources."""
        with self._lock:
            if self._changed:
                return self._changed
            now = self.clock()
            if not force and self._checked_at is not None and now - self._checked_at < self.interval:
                return ()
            self._checked_at = now
            self._changed = scope_files_changed(self.scope, repo_root=self.repo_root)
            return self._changed


def scope_changes(scope: DiffScope, *, repo_root: Path) -> ScopeChanges:
    """Compare the tree with the scope: the verdict is about content, not about ref positions."""
    drifted = scope_files_changed(scope, repo_root=repo_root)
    if drifted:
        return ScopeChanges(current=False, paths=drifted, moved_refs=())
    git = git_at(repo_root)
    try:
        moved = tuple(
            name
            for name, ref, pinned in (("base_ref", scope.base_ref, scope.base_sha), ("head", "HEAD", scope.head_sha))
            if git.output("rev-parse", ref) != pinned
        )
        changed_lines, changed_paths = _changed_state_resolve(scope.diff_base_sha, git)
    except OSError, subprocess.SubprocessError:  # git missing or refusing: fail closed
        return ScopeChanges(current=False, paths=(), moved_refs=())
    paths = _diverged_paths(changed_lines, _changed_file_hashes(changed_paths, repo_root), scope)
    return ScopeChanges(current=not paths, paths=paths, moved_refs=moved)


def scope_is_current(scope: DiffScope, *, repo_root: Path) -> bool:
    return scope_changes(scope, repo_root=repo_root).current
