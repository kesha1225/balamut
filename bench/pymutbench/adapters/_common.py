"""Shared plumbing of the generation adapters.

An adapter runs with the tool's own interpreter, from a fresh temporary working directory; its own directory
(these helpers) comes first on ``sys.path`` and ``/bench`` (the ``pymutbench`` package) last.
It writes one JSON line per native mutant: the file, the tool's identity for the mutant, its operator when the
tool names one, and either the complete mutated text or the reason no faithful text exists.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import IO, Any

BENCH = "/bench"
if BENCH not in sys.path:
    sys.path.append(BENCH)


class Scope:
    """Line ranges per file (``{"path": [[start, end], ...]}``); an empty scope admits everything.

    Adapters use it to skip the materialization of candidates outside the bug's scope; the pipeline applies the
    exact edit-span filter afterwards, so the pre-filter only saves work.
    """

    def __init__(self, ranges: dict[str, list[list[int]]]) -> None:
        self.ranges = ranges

    def admits(self, path: str, first: int | None, last: int | None = None) -> bool:
        if not self.ranges:
            return True
        if first is None:
            return False
        last = first if last is None else last
        return any(start <= first and last <= end for start, end in self.ranges.get(path, []))

    def overlaps(self, path: str, first: int, last: int) -> bool:
        if not self.ranges:
            return True
        return any(first <= end and start <= last for start, end in self.ranges.get(path, []))

    def lines(self, path: str) -> list[int]:
        return sorted({line for start, end in self.ranges.get(path, []) for line in range(start, end + 1)})


def parse_args() -> tuple[Path, list[str], Path, Scope]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="the tree whose files are mutated")
    parser.add_argument("--files", required=True, help="comma-separated paths relative to the root")
    parser.add_argument("--out", required=True)
    parser.add_argument("--scope", default="{}", help='JSON {"path": [[start, end], ...]}')
    args = parser.parse_args()
    return Path(args.root), [f for f in args.files.split(",") if f], Path(args.out), Scope(json.loads(args.scope))


def run_tool(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run the tool's own command. A command killed by a signal (the container's out-of-memory killer) kills the
    adapter with the same signal: the generation driver sees the kill and repeats the run, instead of the adapter
    reading a partial output."""
    proc = subprocess.run(argv, capture_output=True, check=False, **kwargs)  # noqa: S603
    if proc.returncode < 0:
        signal.signal(-proc.returncode, signal.SIG_DFL)  # Python ignores some signals (SIGPIPE) by default
        os.kill(os.getpid(), -proc.returncode)
        raise SystemExit(f"{argv[0]} was killed by signal {-proc.returncode}")  # a signal whose default is not to end
    return proc


def emit(fh: IO[str], path: str, native_id: str, operator: str, mutated: str | None, status: str = "ok",
         **extra: Any) -> None:
    record = {"path": path, "native_id": native_id, "operator": operator, "status": status, **extra}
    if mutated is not None:
        record["mutated"] = mutated
    fh.write(json.dumps(record) + "\n")  # ASCII-escaped: no line separator inside a record
