"""The analysis of a finished mutation run from the verdict cache.

A post-processor of the cache (it does not touch the engine): it reads the verdicts and the sources (for the cold /
body split through ``code_reachable``) and answers "what matters here" without a rerun, a database or a daemon:
semantic-tagged survivors, no-coverage split into cold sites and real gaps, and the distributions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from balamut.config import load_profile
from balamut.core.triage import ColdKind, cold_kind
from balamut.coverage.store import Store, default_cache_path
from balamut.gitutil import git_output_raw
from balamut.report.agent import build_analysis, records_from_store, render_analysis
from balamut.runtime.inmemory import code_reachable

type _Sources = dict[str, str]


def _tracked_python(root: Path, paths: list[str]) -> list[str]:
    """Tracked ``.py`` files under ``paths`` (files or directories), relative to the root like the store's paths."""
    listed = git_output_raw("ls-files", "-z", "--", *paths, cwd=root)
    return [path for path in listed.split("\0") if path.endswith(".py")]


def _sources(root: Path, files: set[str]) -> _Sources:
    return {file: (root / file).read_text(encoding="utf-8") for file in files if (root / file).is_file()}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="balamut analyze", description="Analysis of a mutation run from the cache.")
    parser.add_argument("paths", nargs="+", help="files or directories (the same paths the run mutated)")
    parser.add_argument("--cache", default="", help="path to the SQLite cache (default: the branch and worktree one)")
    parser.add_argument("--json", action="store_true", help="the JSON contract instead of the human-readable analysis")
    args = parser.parse_args(argv)

    profile = load_profile(Path.cwd())
    root = profile.root
    files = _tracked_python(root, args.paths)
    if not files:
        sys.stderr.write(f"balamut analyze: no tracked .py under {args.paths}\n")
        return 2
    store = Store(Path(args.cache) if args.cache else default_cache_path(root, profile.state_dir))
    records = records_from_store(store, files)
    store.close()
    if not records:
        sys.stderr.write(f"balamut analyze: no cached verdicts for {args.paths}; run `balamut mutate` on them first\n")
        return 2

    sources = _sources(root, {record["file"] for record in records})

    def reachable(file: str, lineno: int) -> bool:
        source = sources.get(file)
        return code_reachable(source, lineno) if source is not None else True  # a missing file counts as body

    def resolve_cold(file: str, lineno: int) -> ColdKind:
        return cold_kind(sources.get(file), line=lineno)

    report = build_analysis(records, reachable, cold_resolver=resolve_cold)
    if args.json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(render_analysis(report))
    return 0
