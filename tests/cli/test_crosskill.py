"""The cross-kill matrix (balamut.cli.crosskill): pure logic plus the command over a store."""

from __future__ import annotations

from pathlib import Path

import pytest

from balamut.cli import crosskill
from balamut.cli.crosskill import KillRow, fragile_keys, greedy_min_suite, invert_kills, subsuming_mutants, summarize
from balamut.coverage.store import Store


def test_invert_kills_builds_test_to_mutants() -> None:
    rows: list[KillRow] = [("m1", ["ta", "tb"]), ("m2", ["ta"]), ("m3", ["tc"])]
    assert invert_kills(rows) == {"ta": {"m1", "m2"}, "tb": {"m1"}, "tc": {"m3"}}


def test_fragile_keys_are_single_killer_mutants() -> None:
    rows: list[KillRow] = [("m1", ["ta", "tb"]), ("m2", ["ta"]), ("m3", ["tc", "tc"])]  # a repeated name is one killer
    assert fragile_keys(rows) == ["m2", "m3"]


def test_greedy_min_suite_covers_all_kills_minimally() -> None:
    test_kills = {"ta": {"m1", "m2", "m3"}, "tb": {"m1"}, "tc": {"m4"}}
    suite = greedy_min_suite(test_kills)
    assert set(suite) == {"ta", "tc"}  # tb is redundant: ta already kills m1
    assert {k for t in suite for k in test_kills[t]} == {"m1", "m2", "m3", "m4"}


def test_greedy_min_suite_empty() -> None:
    assert greedy_min_suite({}) == []


def test_subsuming_mutants_are_dominators() -> None:
    # m1 killed by {ta} ⊆ m2 {ta, tb}: m2 is subsumed (m1 is harder, its killer kills m2 too); m3 is unique.
    rows: list[KillRow] = [("m1", ["ta"]), ("m2", ["ta", "tb"]), ("m3", ["tc"])]
    assert set(subsuming_mutants(rows)) == {"m1", "m3"}


def test_subsuming_equal_killsets_keep_the_first() -> None:
    # Equal kill sets: the first (earlier) key stays; a strict-subset test only would keep both.
    assert subsuming_mutants([("m1", ["ta"]), ("m2", ["ta"])]) == ["m1"]


def test_subsuming_a_harder_key_dominates_a_softer_one() -> None:
    # m1 killed by {ta} ⊆ m2 {ta, tb}: m2 is subsumed; the self skip must not abort the comparison.
    assert subsuming_mutants([("m1", ["ta", "tb"]), ("m2", ["ta"])]) == ["m2"]


def test_greedy_min_suite_breaks_ties_by_first_seen() -> None:
    # Equal gains: the first test (insertion order) wins the tie.
    assert greedy_min_suite({"ta": {"m1"}, "tb": {"m2"}}) == ["ta", "tb"]


def test_summarize_exactly_half_fragile_is_not_degenerate() -> None:
    # 1 fragile of 2 killed keys is exactly 0.5, not strictly above it.
    assert summarize([("m1", ["ta"]), ("m2", ["ta", "tb"])]).degenerate is False


def test_summarize_counts_redundant_tests_as_removable() -> None:
    summary = summarize([("m1", ["ta", "tb"])])
    assert summary.min_suite == 1
    assert summary.removable == 1  # ta or tb alone kills m1


def test_report_caps_top_killers_at_ten(capsys: pytest.CaptureFixture[str]) -> None:
    rows: list[KillRow] = [(f"m{i}", [f"s{i:02d}"]) for i in range(11)]
    crosskill._report(rows)
    out = capsys.readouterr().out
    assert "FIRST-KILLER" in out  # a single-killer matrix is degenerate
    assert "s10" not in out  # the 11th killer is beyond the cap


def test_summarize_excludes_timeout_killed_from_matrix() -> None:
    """A kill with no killing test (a hang kill) has no test to map to: the matrix and its denominators count test
    kills only, or the minimal suite would falsely claim to keep every kill."""
    rows: list[KillRow] = [("m1", ["ta"]), ("m2", ["ta", "tb"]), ("m_to1", []), ("m_to2", [])]
    summary = summarize(rows)
    assert summary.timeout_killed == 2
    assert summary.test_killed == 2
    assert summary.killers == 2
    assert summary.min_suite == 1
    assert summary.dominators == 1
    assert summary.subsumed == summary.test_killed - summary.dominators
    assert summary.top_killers[0] == ("ta", 2)


def test_summarize_all_timeout_killed_no_division_error() -> None:
    rows: list[KillRow] = [("m_to1", []), ("m_to2", [])]
    summary = summarize(rows)
    assert summary.timeout_killed == 2
    assert summary.test_killed == 0
    assert summary.min_suite == 0
    assert summary.degenerate is False


def test_summarize_flags_first_killer_data_as_degenerate() -> None:
    assert summarize([("m1", ["ta"]), ("m2", ["tb"]), ("m3", ["ta", "tb"])]).degenerate is True


def _killed(store: Store, key: str, killers: list[str]) -> None:
    store.put_verdict(
        key=key,
        file="src/x.py",
        lineno=1,
        operator="compare:Lt->LtE",
        original="a < b",
        mutated="a <= b",
        qualname="f",
        func_hash="h",
        verdict="killed",
        bucket="high",
        covering=killers,
        covering_hash="c",
        killed_by=killers,
    )


def test_main_reports_the_matrix_of_the_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    _killed(store, "m1", ["tests/x_test.py::test_a", "tests/x_test.py::test_b"])
    _killed(store, "m2", ["tests/x_test.py::test_a"])
    store.close()

    assert crosskill.main(["--cache", str(cache)]) == 0

    out = capsys.readouterr().out
    assert "CROSS-KILL" in out
    assert "tests/x_test.py::test_a" in out


def test_main_on_an_empty_cache_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert crosskill.main([]) == 0  # the default cache lives under the project's state directory

    assert "no killed mutants" in capsys.readouterr().out
    assert (tmp_path / ".balamut" / "cache").is_dir()


def test_main_closes_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    _killed(store, "m1", ["tests/x_test.py::test_a"])
    store.close()
    closes = 0
    original_close = Store.close

    def tracked_close(store: Store) -> None:
        nonlocal closes
        closes += 1
        original_close(store)

    monkeypatch.setattr(Store, "close", tracked_close)

    assert crosskill.main(["--cache", str(cache)]) == 0
    assert closes == 1
