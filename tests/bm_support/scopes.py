"""Building pinned diff scopes for tests that only care about some of its fields."""

from __future__ import annotations

import hashlib
from pathlib import Path

from balamut.diff.scope import MISSING_FILE_HASH, DiffScope


def scope(
    changed_lines: dict[str, tuple[int, ...]] | None = None,
    *,
    file_hashes: dict[str, str] | None = None,
    file_hash: str = "file-hash",
    base_ref: str = "origin/main",
) -> DiffScope:
    """A scope with the given changed lines and file hashes; refs and shas are placeholders.

    ``file_hash`` is one hash for every file, for tests that care about the file set rather than content; an
    explicit ``file_hashes`` overrides it."""
    lines = changed_lines if changed_lines is not None else {"src/x.py": (10,)}
    hashes = file_hashes if file_hashes is not None else dict.fromkeys(lines, file_hash)
    return DiffScope(
        base_ref=base_ref,
        base_sha="base-sha",
        diff_base_sha="diff-base-sha",
        head_sha="head-sha",
        changed_lines=lines,
        file_hashes=hashes,
    )


def pinned(root: Path, *paths: str) -> DiffScope:
    """A scope pinning the current content of ``paths`` under ``root``."""
    hashes = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest() if (root / path).exists() else MISSING_FILE_HASH
        for path in paths
    }
    return scope(dict.fromkeys(paths, (1,)), file_hashes=hashes)
