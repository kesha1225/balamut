"""``balamut confirm``: check that new or edited tests kill the mutants of some paths.

The tests are re-measured into the coverage store first, so the campaign sees them as covering tests, and every
mutant of the paths runs again (cached verdicts are not read). The raw results are printed as JSON.
"""

from __future__ import annotations

import argparse

from balamut.cli.mutate import main as mutate_main


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="balamut confirm",
        description="Re-run the mutants of PATHS after measuring the given tests; other options go to `mutate`.",
    )
    parser.add_argument("paths", nargs="+", help="source files whose mutants to re-run")
    parser.add_argument("--tests", required=True, help="comma-separated test files to measure first")
    args, rest = parser.parse_known_args(argv)
    return mutate_main([*args.paths, "--refresh-tests", args.tests, "--no-cache", "--json", *rest])
