"""Thin git subprocess helpers (fixed argv, never a shell)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_output_raw(*args: str, cwd: Path | None = None) -> str:
    """git stdout with its original separators and edge characters; raises on a non-zero exit."""
    command = ["git", *(("-C", str(cwd)) if cwd is not None else ()), *args]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def git_output(*args: str, cwd: Path | None = None) -> str:
    """git stdout without surrounding whitespace; raises on a non-zero exit."""
    return git_output_raw(*args, cwd=cwd).strip()


def git_output_in(repo: Path, *args: str) -> str | None:
    """git stdout run inside ``repo``; None when git refuses (for example, the directory is not a repository)."""
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    return out.stdout.strip() if out.returncode == 0 else None
