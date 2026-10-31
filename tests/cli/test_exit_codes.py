"""Exit codes of ``balamut mutate`` (balamut.cli.exit_codes)."""

from __future__ import annotations

from balamut.cli import exit_codes


def test_exit_codes_are_stable_and_distinct() -> None:
    assert exit_codes.EXIT_USAGE == 2
    assert exit_codes.EXIT_REFRESH_BASELINE_FAILURE == 4
    assert exit_codes.EXIT_SCOPE_CHANGED == 5
    codes = {
        exit_codes.EXIT_USAGE,
        exit_codes.EXIT_REFRESH_BASELINE_FAILURE,
        exit_codes.EXIT_SCOPE_CHANGED,
    }
    assert len(codes) == 3
