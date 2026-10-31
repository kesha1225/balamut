"""The "hang means killed" policy: causality of a hang kill and the reasons of unresolved verdicts.

A mutant on which a covering test stops reporting would not pass the suite. The kill counts only when the hang is
causal: the mutated code object executed, the hung test is known, and the same test passes on the original in the
same budget. Every other outcome stays a TIMEOUT with a reason: without it an unstable test looks like a hang.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from balamut.coverage.store import Store
from balamut.engine.daemon import Hang, RunResult
from balamut.engine.output import result_json
from balamut.engine.results import MutantStatus, VerdictReason
from bm_support.campaign import FakeIsolation, mutant, options, run, stub_campaign_runtime, work

_HUNG = "tests/x_test.py::test_x"


@pytest.mark.parametrize(
    ("original", "expected", "reason"),
    [
        (RunResult(0, {}, []), MutantStatus.KILLED, VerdictReason.HANG),
        (None, MutantStatus.TIMEOUT, VerdictReason.ORIGINAL_HANG),
        (RunResult(1, {}, [_HUNG]), MutantStatus.TIMEOUT, VerdictReason.ORIGINAL_FAILED),
    ],
)
def test_final_touched_hang_is_killed_only_when_the_original_passes_the_hung_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    original: RunResult | None,
    expected: MutantStatus,
    reason: VerdictReason,
) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [None, original], touched_on_hang=True)
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = run(tmp_path, [item], store=store)
        cached_killers = dict(store.killed_with_killers()).get("key-0", [])
    finally:
        store.close()

    assert results[0].status is expected
    assert results[0].reason is reason
    assert results[0].killed_by == ((_HUNG,) if expected is MutantStatus.KILLED else ())
    assert stub.persisted == [expected]
    assert [nodeids for nodeids, _spec in stub.requests] == [[_HUNG], [_HUNG]]
    assert not stub.requests[1][1]
    assert stub.fresh == [False, True]
    assert stub.timeouts == [8.0, 8.0]
    assert cached_killers == ([_HUNG] if expected is MutantStatus.KILLED else [])
    assert result_json(results[0])["reason"] == reason.value


def test_final_untouched_hang_stays_unresolved_without_an_original_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], [None])

    results = run(tmp_path, [item])

    assert results[0].status is MutantStatus.TIMEOUT
    assert results[0].reason is VerdictReason.HANG_UNCONFIRMED
    assert len(stub.requests) == 1


@pytest.mark.parametrize(
    ("completed", "expected", "blamed"),
    [
        (frozenset({"tests/a_test.py::test_a"}), MutantStatus.KILLED, "tests/b_test.py::test_b"),
        (frozenset({"tests/b_test.py::test_b"}), MutantStatus.TIMEOUT, None),
    ],
)
def test_hang_blames_the_first_covering_test_that_never_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed: frozenset[str],
    expected: MutantStatus,
    blamed: str | None,
) -> None:
    covering = ("tests/a_test.py::test_a", "tests/b_test.py::test_b")
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(
        monkeypatch,
        tmp_path,
        [work(0, item, covering)],
        [Hang(ordered=covering, completed=completed), RunResult(0, {}, [])],
        touched_on_hang=True,
    )

    results = run(tmp_path, [item])

    assert results[0].status is expected
    if blamed is None:
        assert results[0].reason is VerdictReason.HANG_UNCONFIRMED
        assert len(stub.requests) == 1
    else:
        assert results[0].killed_by == (blamed,)
        assert stub.requests[1][0] == [blamed]


def test_hang_kill_is_decided_only_after_the_isolated_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = mutant("src/x.py", 10)
    stub = stub_campaign_runtime(
        monkeypatch, tmp_path, [work(0, item)], [None, None, RunResult(0, {}, [])], touched_on_hang=True
    )
    store = Store(tmp_path / "campaign.sqlite")
    try:
        results = run(tmp_path, [item], store=store, opts=options(tmp_path, timeout_retries=1))
    finally:
        store.close()

    assert results[0].status is MutantStatus.KILLED
    assert results[0].reason is VerdictReason.HANG
    assert stub.persisted == [MutantStatus.KILLED]
    assert stub.timeouts == [8.0, 24.0, 24.0]


def test_unstable_killer_in_fresh_confirmation_is_unresolved_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = mutant("src/x.py", 10)
    responses = [RunResult(1, {}, [_HUNG]) for _ in range(4)]  # distinct objects: a verdict edits its result
    stub_campaign_runtime(monkeypatch, tmp_path, [work(0, item)], responses)

    results = run(tmp_path, [item], opts=options(tmp_path, isolation=FakeIsolation()))

    assert results[0].status is MutantStatus.TIMEOUT
    assert results[0].reason is VerdictReason.ORIGINAL_FAILED
