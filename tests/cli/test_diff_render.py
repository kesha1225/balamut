"""The human output of ``balamut diff``: statuses, gating classes, one line per gap and the expected wait."""

from __future__ import annotations

from pathlib import Path

import pytest

from balamut.cli import diff
from balamut.coverage import costs as pytest_costs
from balamut.coverage import substrate
from bm_support.diff import Scenario, coverage_path, findings, raw_mutant, run_main


def _finding(line: int, operator: str, status: str = "no_coverage", reason: str = "") -> diff.JsonObject:
    return {
        "key": f"{line}-{operator}",
        "path": "src/x.py",
        "line": line,
        "span": {"start": line, "end": line},
        "operator": operator,
        "status": status,
        "bucket": "high" if status == "survived" else "",
        "reason": reason,
        "original": "return value",
        "mutated": "pass",
        "covering_tests": [],
    }


def test_render_lists_the_substrate_miss_reason() -> None:
    report = diff.report_build(diff.RunState(substrate_miss_reason="config_fingerprint"), diff.RunStatus.CLEAN)

    assert "substrate-miss: config_fingerprint" in diff.render_human(report)


def test_render_counts_every_gating_class_despite_status_precedence() -> None:
    """UNRESOLVED wins the status word; hundreds of survivors must not hide behind it."""
    rows = [("timeout", "hang_unconfirmed"), ("survived", ""), ("survived", ""), ("no_coverage", "")]
    gating = [_finding(index + 1, "compare:Lt->LtE", status, reason) for index, (status, reason) in enumerate(rows)]
    report = diff.report_build(diff.RunState(), diff.RunStatus.UNRESOLVED, findings=gating)

    rendered = diff.render_human(report)

    assert "gate: unresolved=1 actionable=2 uncovered=1" in rendered
    assert "TIMEOUT src/x.py:1 [compare:Lt->LtE] hang_unconfirmed" in rendered


def test_a_timeout_finding_carries_the_engine_reason(tmp_path: Path) -> None:
    unresolved = raw_mutant("timeout", key="timeout-key", bucket="")
    unresolved["reason"] = "original_failed"

    _return_code, report = run_main(Scenario(tmp_path, rows=[unresolved]))

    assert report["status"] == diff.RunStatus.UNRESOLVED.value
    assert [finding["reason"] for finding in findings(report)] == ["original_failed"]


def test_the_report_has_a_complete_nonnegative_timing_shape() -> None:
    report = diff.report_build(diff.RunState(), diff.RunStatus.CLEAN)
    timings = report["timings"]

    assert isinstance(timings, dict)
    assert set(timings) == set(diff.REPORT_TIMING_KEYS)
    assert all(type(seconds) is float and seconds >= 0 for seconds in timings.values())
    assert report["failure_stage"] is None
    assert report["error_code"] is None


def test_render_lists_actionable_locations_operators_and_the_next_action() -> None:
    findings = [
        {**_finding(17, "compare:Lt->LtE", "survived"), "path": "src/pkg/limits.py"},
        {**_finding(29, "bool:not"), "path": "src/pkg/flags.py"},
    ]
    report = diff.report_build(
        diff.RunState(), diff.RunStatus.FINDINGS, findings=findings, next_action="fix_findings_and_rerun"
    )

    rendered = diff.render_human(report)

    assert "balamut diff: FINDINGS" in rendered
    assert "NO_COVERAGE src/pkg/flags.py:29 [bool:not]" in rendered
    assert "SURVIVED src/pkg/limits.py:17 [compare:Lt->LtE]" in rendered
    assert "next: fix_findings_and_rerun" in rendered


def test_findings_of_one_line_render_as_one_line_with_every_operator() -> None:
    findings = [_finding(10, "extreme:sdl"), _finding(10, "return:None"), _finding(12, "compare:Lt->LtE", "survived")]
    report = diff.report_build(diff.RunState(), diff.RunStatus.FINDINGS, findings=findings)

    rendered = diff.render_human(report).splitlines()

    assert "NO_COVERAGE src/x.py:10 [extreme:sdl, return:None]" in rendered
    assert "SURVIVED src/x.py:12 [compare:Lt->LtE]" in rendered
    assert "gate: unresolved=0 actionable=1 uncovered=2 lines=2" in rendered


def test_findings_of_one_line_with_different_outcomes_keep_every_detail() -> None:
    findings = [_finding(10, "extreme:sdl"), _finding(10, "return:None", "survived", "original_failed")]
    report = diff.report_build(diff.RunState(), diff.RunStatus.UNRESOLVED, findings=findings)

    rendered = diff.render_human(report)

    assert (
        "MIXED src/x.py:10 [extreme:sdl (no_coverage), return:None (survived: original_failed)]"
        in rendered.splitlines()
    )
    assert "gate: unresolved=0 actionable=1 uncovered=1 lines=1" in rendered


def test_the_human_output_names_the_time_of_each_stage() -> None:
    state = diff.RunState()
    state.timings.update({"preflight": 2.0, "substrate": 250.4, "mutation": 61.0})

    rendered = diff.render_human(diff.report_build(state, diff.RunStatus.CLEAN))

    assert "time: substrate=250s mutation=61s total=313s" in rendered


def test_the_build_estimate_divides_the_last_build_costs_by_its_workers(tmp_path: Path) -> None:
    costs = tmp_path / "coverage.substrate.costs.json"
    pytest_costs.write_costs(costs, {"tests/a_test.py::test_a": 300.0, "tests/b_test.py::test_b": 180.0})

    assert diff.build_estimate(costs, 4) == pytest.approx(120.0)
    assert diff.build_estimate(tmp_path / "missing.json", 4) is None
    assert diff.build_estimate(costs, 0) is None


def test_the_run_announces_the_pinned_scope_and_the_expected_build_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest_costs.write_costs(
        substrate.costs_path(coverage_path(tmp_path)), {f"tests/t{index}_test.py::test_x": 60.0 for index in range(12)}
    )

    return_code, _report = run_main(Scenario(tmp_path))

    stderr = capsys.readouterr().err
    assert return_code == 0
    assert "balamut diff: scope pinned (1 files in the diff)" in stderr
    assert "balamut diff: full coverage substrate build (reason: missing_coverage), ~3 min by the last build" in stderr
