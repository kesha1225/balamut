"""Content fingerprints of the code that determines each stage's results.

A generation ledger carries the fingerprint of the shared generation code and of its own tool's adapter; an
oracle result carries the fingerprint of the files the oracle executes. The report refuses to aggregate a tool's
ledgers, or oracle results, whose fingerprints differ, so a resumed or partly repeated run can never mix two
versions of the code behind one measure.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent

GENERATION_SHARED = ("generate.py", "artifacts.py", "astmat.py", "scope.py", "gitops.py", "adapters/_common.py")
STAGES: dict[str, tuple[str, ...]] = {
    "oracle": ("oracle.py", "pytestrun.py", "plugin_path/pmb_pytest_plugin.py", "artifacts.py", "gitops.py",
               "validate.py", "subjects.py"),
}


def _digest(patterns: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for pattern in patterns:
        for path in sorted(ROOT.glob(pattern)):
            digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def fingerprint(stage: str) -> str:
    return _digest(STAGES[stage])


def generation_fingerprint(adapter: str) -> str:
    """The code one tool's generation ledgers depend on: the shared generation files and that tool's adapter."""
    return _digest((*GENERATION_SHARED, f"adapters/{adapter}"))
