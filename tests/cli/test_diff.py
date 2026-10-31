"""The ``balamut diff`` gate: preflight, scope, substrate, mutation boundary, statuses and the report."""

from __future__ import annotations

import json
import math
import textwrap
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from balamut.cli import diff
from balamut.coverage import substrate
from balamut.diff.scope import ScopeChanges
from balamut.isolation import IsolationError, NoIsolation
from bm_support import write_file
from bm_support.diff import (
    PREFLIGHT,
    Report,
    Row,
    Scenario,
    coverage_path,
    expected,
    main_args,
    mutation_argv,
    project_profile,
    published_metadata,
    raw_mutant,
    run_main,
    section,
)
from bm_support.substrates import write_valid_coverage


def test_preflight_runs_before_the_substrate_build(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert report["status"] == diff.RunStatus.CLEAN.value
    assert scenario.events[:4] == ["isolation-probe", "scope-resolve", "scope-write", "preflight"]
    assert scenario.events.index("preflight") < scenario.events.index("coverage-build")
    build = scenario.measures[0]
    assert build.targets == ()
    assert build.coverage_file == tmp_path / "temporary.coverage"
    argv = mutation_argv(scenario)
    assert argv[argv.index("--diff") + 1] == "diff-base-sha"
    assert argv[argv.index("--costs") + 1] == str(substrate.costs_path(coverage_path(tmp_path)))
    assert argv[argv.index("--kind") + 1] == ",".join(project_profile(tmp_path).diff_kinds)


def test_preflight_timing_sums_the_probe_and_every_command(tmp_path: Path) -> None:
    moments = iter((0.0, 7.0, 10.0, 13.0, 20.0, 25.0))
    scenario = Scenario(tmp_path, preflight_returncode=1, clock_fn=lambda: next(moments))

    return_code, report = run_main(scenario)

    assert return_code != 0
    preflight = section(report, "timings")["preflight"]
    assert isinstance(preflight, float)
    assert math.isclose(preflight, 10.0)


def test_worker_cap_of_the_profile_shapes_the_build(tmp_path: Path) -> None:
    write_file(
        tmp_path / "pyproject.toml",
        '[tool.balamut]\nconfig = "balamut_conf.py"\n',
    )
    write_file(
        tmp_path / "balamut_conf.py",
        textwrap.dedent(
            """
            from balamut.config import init

            init(preflight=(("check-environment",),), coverage_workers_cap=8)
            """
        ),
    )
    scenario = Scenario(tmp_path)

    run_main(scenario)

    assert scenario.measures[0].profile.coverage.workers_cap == 8


def test_preflight_failure_writes_a_terminal_report_without_a_build(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, preflight_returncode=2)

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert report["status"] == diff.RunStatus.INFRA_FAILURE.value
    assert (report["failure_stage"], report["error_code"]) == ("preflight", "preflight_failed")
    assert report["error_detail"] == "check-environment exited with 2"
    assert report["next_action"] == "fix_preflight_and_rerun"
    assert "substrate-ensure" not in scenario.events
    assert "coverage-build" not in scenario.events


def test_preflight_exception_still_writes_a_terminal_report(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, preflight_error=OSError("no such command"))

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert (report["failure_stage"], report["error_code"]) == ("preflight", "preflight_error")
    assert (tmp_path / "report.json").is_file()


def test_unreachable_isolation_target_fails_before_the_scope(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, probe_error=IsolationError("connection refused"))

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert (report["failure_stage"], report["error_code"]) == ("preflight", "isolation_unreachable")
    assert report["next_action"] == "fix_isolation_target_and_rerun"
    assert "scope-resolve" not in scenario.events


def test_scope_resolution_exception_still_writes_a_terminal_report(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, scope_error=OSError("git unavailable"))

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert report["status"] == diff.RunStatus.INFRA_FAILURE.value
    assert report["scope"] == {"base_sha": "", "diff_base_sha": "", "head_sha": ""}
    assert (report["failure_stage"], report["error_code"]) == ("scope", "scope_resolve_error")


def test_failed_build_without_failed_nodeids_is_an_infrastructure_failure(tmp_path: Path) -> None:
    coverage = coverage_path(tmp_path)
    coverage.write_text("last-complete-generation", encoding="utf-8")
    scenario = Scenario(tmp_path, coverage_returncode=1)

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert (report["failure_stage"], report["error_code"]) == ("substrate", "baseline_artifact_missing")
    assert report["next_action"] == "fix_substrate_infrastructure_and_rerun"
    assert coverage.read_text(encoding="utf-8") == "last-complete-generation"
    assert not (tmp_path / "raw.json").exists()
    assert "mutation" not in scenario.events


def test_scope_drift_stops_before_the_gate(tmp_path: Path) -> None:
    changes = ScopeChanges(current=False, paths=("src/x.py",), moved_refs=())
    scenario = Scenario(tmp_path, rows=[raw_mutant("survived")], scope_changes=changes)

    return_code, report = run_main(scenario)

    assert return_code == 3
    assert report["status"] == diff.RunStatus.SCOPE_CHANGED.value
    assert report["findings"] == []
    assert report["next_action"] == "restart_with_fresh_scope"


def _mutation_failure(scenario: Scenario) -> None:
    scenario.mutation_returncode = 2


def _mutation_error(scenario: Scenario) -> None:
    scenario.mutation_error = OSError("mutator unavailable")


def _missing_raw(scenario: Scenario) -> None:
    scenario.omit_raw = True


def _malformed_raw(scenario: Scenario) -> None:
    scenario.raw_text = "not-json"


def _missing_stats(scenario: Scenario) -> None:
    scenario.omit_stats = True


def _partial_stats(scenario: Scenario) -> None:
    scenario.stats_payload = {"schema_version": 1, "complete": False}


@pytest.mark.parametrize(
    "configure", [_mutation_failure, _mutation_error, _missing_raw, _malformed_raw, _missing_stats, _partial_stats]
)
def test_mutation_boundary_failures_never_fabricate_clean(
    tmp_path: Path, configure: Callable[[Scenario], None]
) -> None:
    scenario = Scenario(tmp_path)
    configure(scenario)

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert report["status"] == diff.RunStatus.INFRA_FAILURE.value
    assert report["next_action"] == "fix_mutation_infrastructure_and_rerun"


def test_inconsistent_complete_stats_fail_closed(tmp_path: Path) -> None:
    stats: Row = {
        "schema_version": 1,
        "complete": True,
        "enumerated": 2,
        "cache_resolved": 0,
        "executed": 1,
        "terminal": 1,
        "status_counts": {"killed": 1, "survived": 0, "timeout": 0, "no_coverage": 0, "unsupported": 0},
    }
    scenario = Scenario(tmp_path, rows=[raw_mutant("killed", bucket="")], stats_payload=stats)

    return_code, report = run_main(scenario)

    assert return_code != 0
    assert report["status"] == diff.RunStatus.INFRA_FAILURE.value


def test_counts_use_the_current_campaign_cache_and_execution_stats(tmp_path: Path) -> None:
    rows = [raw_mutant("killed", key="killed-key", bucket=""), raw_mutant("survived")]
    scenario = Scenario(tmp_path, rows=rows, cache_resolved=1, executed=1)

    _return_code, report = run_main(scenario)

    counts = section(report, "counts")
    assert (counts["enumerated"], counts["cache_resolved"], counts["executed"]) == (2, 1, 1)
    assert (counts["killed"], counts["survived"]) == (1, 1)


def test_counts_include_every_terminal_status(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, rows=[raw_mutant("unsupported", bucket="")])

    _return_code, report = run_main(scenario)

    counts = section(report, "counts")
    assert counts["unsupported"] == 1
    statuses = ("killed", "survived", "timeout", "no_coverage", "unsupported")
    assert sum(int(str(counts[status])) for status in statuses) == counts["enumerated"]


def test_evaluations_include_only_mutants_of_the_exact_scope(tmp_path: Path) -> None:
    outside = raw_mutant("survived", key="outside")
    outside.update(lineno=20, site_lineno=20, site_end_lineno=20)
    scenario = Scenario(tmp_path, rows=[raw_mutant("killed", key="inside", bucket=""), outside])

    _return_code, report = run_main(scenario)

    assert report["evaluations"] == [
        {
            "key": "inside",
            "status": "killed",
            "path": "src/x.py",
            "line": 9,
            "operator": "compare:Lt->LtE",
            "bucket": "",
            "original": "a < b",
            "mutated": "a <= b",
        }
    ], "an evaluation names the line of the edit (site_lineno), not the body line tests are selected by"


def test_report_carries_a_sanitized_invocation_identity(tmp_path: Path) -> None:
    _return_code, report = run_main(Scenario(tmp_path), "--limit", "7")

    invocation = section(report, "invocation")
    assert (invocation["mode"], invocation["base_mode"], invocation["base_ref"]) == (
        "fast",
        "merge-base",
        "origin/main",
    )
    assert len(str(invocation["mutate_args_digest"])) == 64
    assert "limit" not in json.dumps(invocation)


@pytest.mark.parametrize(
    ("rows", "status"),
    [
        ([raw_mutant("killed", bucket="")], diff.RunStatus.CLEAN),
        ([raw_mutant("survived")], diff.RunStatus.FINDINGS),
        ([raw_mutant("no_coverage", bucket="")], diff.RunStatus.FINDINGS),
        ([raw_mutant("survived"), raw_mutant("timeout", key="timeout-key", bucket="")], diff.RunStatus.UNRESOLVED),
    ],
)
def test_terminal_status_is_deterministic(tmp_path: Path, rows: list[Row], status: diff.RunStatus) -> None:
    return_code, report = run_main(Scenario(tmp_path, rows=rows))

    assert report["status"] == status.value
    assert return_code == diff.status_exit_code(status)


def test_findings_have_a_direct_key_exact_covering_tests_and_span(tmp_path: Path) -> None:
    covering = ["tests/x_test.py::test_first", "tests/x_test.py::test_second[param]"]
    scenario = Scenario(tmp_path, rows=[raw_mutant("survived", key="direct-key", covering_tests=covering)])

    _return_code, report = run_main(scenario)

    assert report["findings"] == [
        {
            "key": "direct-key",
            "path": "src/x.py",
            "line": 10,
            "span": {"start": 9, "end": 10},
            "operator": "compare:Lt->LtE",
            "status": "survived",
            "bucket": "high",
            "reason": "",
            "original": "a < b",
            "mutated": "a <= b",
            "covering_tests": covering,
        }
    ]


def test_actionable_record_with_an_empty_key_fails_closed(tmp_path: Path) -> None:
    return_code, report = run_main(Scenario(tmp_path, rows=[raw_mutant("survived", key="")]))

    assert return_code != 0
    assert report["status"] == diff.RunStatus.INFRA_FAILURE.value
    assert report["findings"] == []


def test_agent_json_is_a_single_stdout_json_and_silences_engine_progress(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path, rows=[raw_mutant("killed", bucket="")])

    return_code, report = run_main(scenario, "--agent-json")

    assert return_code == 0
    assert json.loads(capsys.readouterr().out) == report
    argv = mutation_argv(scenario)
    assert argv[argv.index("--progress") + 1] == "none"


def test_mutate_options_pass_through_after_the_defaults(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)

    run_main(scenario, "--kind", "src", "--daemons", "3")

    argv = mutation_argv(scenario)
    kinds = [index for index, token in enumerate(argv) if token == "--kind"]
    assert len(kinds) == 2, "the profile's default first, the caller's own after it (argparse keeps the last)"
    assert argv[kinds[-1] + 1] == "src"
    assert argv[argv.index("--daemons") + 1] == "3"


def test_rebuild_substrate_forces_a_new_generation(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    scenario.cached_metadata = published_metadata(expected(tmp_path), generation="old-generation")

    return_code, report = run_main(scenario, "--rebuild-substrate")

    assert return_code == 0
    assert scenario.ensure_force is True
    assert report["substrate"] == {
        "generation": "rebuilt-generation",
        "reused": False,
        "miss_reason": "forced_rebuild",
        "drift_files": 0,
        "seeded_from": None,
    }
    assert "coverage-build" in scenario.events


def test_compatible_substrate_reuse_has_no_miss_reason(tmp_path: Path) -> None:
    Scenario(tmp_path)  # writes the project table the expected metadata depends on
    scenario = Scenario(
        tmp_path, cached_metadata=published_metadata(expected(tmp_path), generation="cached-generation")
    )

    return_code, report = run_main(scenario)

    assert return_code == 0
    assert report["substrate"] == {
        "generation": "cached-generation",
        "reused": True,
        "miss_reason": None,
        "drift_files": 0,
        "seeded_from": None,
    }


def test_config_fingerprint_follows_the_substrate_inputs(tmp_path: Path) -> None:
    write_file(tmp_path / "pytest.ini", "[pytest]\naddopts = -q\n")
    profile = project_profile(tmp_path)
    initial = diff.config_fingerprint(profile)

    write_file(tmp_path / "pytest.ini", "[pytest]\naddopts = -x\n")

    assert diff.config_fingerprint(profile) != initial


def test_config_fingerprint_follows_the_layout(tmp_path: Path) -> None:
    initial = diff.config_fingerprint(project_profile(tmp_path))
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "balamut_conf.py"\n')
    write_file(
        tmp_path / "balamut_conf.py",
        textwrap.dedent(
            """
            from balamut.config import init

            init(source=("app",), import_roots=("app",))
            """
        ),
    )

    assert diff.config_fingerprint(project_profile(tmp_path)) != initial


def test_schema_fingerprint_is_the_isolation_fingerprint(tmp_path: Path) -> None:
    metadata = diff.expected_metadata(project_profile(tmp_path), NoIsolation())

    assert metadata.schema_fingerprint == NoIsolation().fingerprint()


def test_substrate_force_rebuild_publishes_a_new_atomic_generation(tmp_path: Path) -> None:
    coverage = coverage_path(tmp_path)
    wanted = expected(tmp_path)
    first = substrate.substrate_ensure(coverage, wanted, build=write_valid_coverage)

    second = substrate.substrate_ensure(coverage, wanted, build=write_valid_coverage, force_rebuild=True)

    assert second.generation != first.generation
    assert substrate.substrate_inspect(coverage) == second


def test_report_write_is_atomic_on_a_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("last-complete-report", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        diff.report_write(report_path, {"status": "CLEAN"})

    assert report_path.read_text(encoding="utf-8") == "last-complete-report"
    assert list(tmp_path.glob(".report.json.*.tmp")) == []


def test_report_boundary_failure_retries_with_an_infrastructure_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(tmp_path, preflight_returncode=1)
    actual_write = diff.report_write
    attempts = 0

    def fail_once(path: Path, report: diff.JsonMapping) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient replace failure")
        actual_write(path, report)

    monkeypatch.setattr(diff, "report_write", fail_once)

    return_code = diff.main(main_args(tmp_path), runtime=scenario.runtime(), root=tmp_path)

    report: Report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert return_code != 0
    assert attempts == 2
    assert (report["status"], report["error_code"]) == (diff.RunStatus.INFRA_FAILURE.value, "report_write_error")
    assert report["next_action"] == "fix_report_storage_and_rerun"
    assert report["error_detail"] is None


def test_report_wall_excludes_stages_nested_in_mutation() -> None:
    """The wall of a run is the sum of its top-level stages: campaign stages live inside ``mutation``."""
    report: diff.JsonMapping = {
        "timings": {
            "preflight": 1.0,
            "substrate": 2.0,
            "mutation": 50.0,
            "gate": 0.5,
            "coverage_refresh": 20.0,
            "daemon_startup": 9.0,
            "mutant_execution": 6.0,
            "pytest_collection": 4.0,
        }
    }

    assert math.isclose(diff.report_wall_seconds(report), 53.5)


def test_every_terminal_run_reaches_the_report_sink(tmp_path: Path) -> None:
    clean = Scenario(tmp_path / "clean")
    failing = Scenario(tmp_path / "failing", preflight_returncode=1)

    run_main(clean)
    run_main(failing)

    assert [str(report["status"]) for report in clean.reports] == [diff.RunStatus.CLEAN.value]
    assert [str(report["status"]) for report in failing.reports] == [diff.RunStatus.INFRA_FAILURE.value]


def test_report_sinks_come_from_entry_points_and_never_fail_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    def good(report: diff.JsonMapping) -> None:
        seen.append(str(report["status"]))

    def broken(_report: diff.JsonMapping) -> None:
        raise RuntimeError("sink is down")

    monkeypatch.setattr(diff, "load_report_sinks", lambda: [("good", good), ("broken", broken)])

    diff.entry_point_sink({"status": "CLEAN"})

    assert seen == ["CLEAN"]
    assert "report sink broken failed: sink is down" in capsys.readouterr().err


def test_doctor_is_read_only_and_reports_a_stale_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    Scenario(tmp_path)
    stale = replace(published_metadata(expected(tmp_path), generation="generation"), config_fingerprint="stale-config")
    coverage = coverage_path(tmp_path)
    coverage.write_text("coverage", encoding="utf-8")
    substrate.metadata_path(coverage).write_text("{}", encoding="utf-8")
    scenario = Scenario(tmp_path, cached_metadata=stale)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    return_code = diff.doctor_main(["--coverage", str(coverage)], runtime=scenario.runtime(), root=tmp_path)

    doctor = json.loads(capsys.readouterr().out)
    assert return_code != 0
    assert doctor == {"status": "invalid", "issues": ["config_fingerprint"]}
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before
    assert "scope-resolve" not in scenario.events
    assert "substrate-ensure" not in scenario.events
    assert scenario.events[-2:] == ["isolation-probe", "preflight"]


def test_doctor_does_not_judge_the_test_inventory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A new or removed test file is re-measured by a refresh through the tree record: not a doctor issue."""
    Scenario(tmp_path)
    stale = replace(
        published_metadata(expected(tmp_path), generation="generation"), test_inventory_fingerprint="stale-inventory"
    )
    coverage = coverage_path(tmp_path)
    coverage.write_text("coverage", encoding="utf-8")
    substrate.metadata_path(coverage).write_text("{}", encoding="utf-8")
    scenario = Scenario(tmp_path, cached_metadata=stale)

    return_code = diff.doctor_main(["--coverage", str(coverage)], runtime=scenario.runtime(), root=tmp_path)

    assert return_code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "valid", "issues": []}


def test_doctor_distinguishes_missing_coverage_and_metadata(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = Scenario(tmp_path)

    return_code = diff.doctor_main(
        ["--coverage", str(coverage_path(tmp_path))], runtime=scenario.runtime(), root=tmp_path
    )

    assert return_code != 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "invalid",
        "issues": ["missing_coverage", "missing_metadata"],
    }


def test_doctor_reports_an_invalid_coverage_checksum_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path, real_substrate=True)
    coverage = coverage_path(tmp_path)
    coverage.write_bytes(b"coverage")
    metadata = published_metadata(expected(tmp_path), generation="generation")
    payload = vars_of(metadata)
    payload["coverage_sha256"] = "wrong-checksum"
    substrate.metadata_path(coverage).write_text(json.dumps(payload), encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    return_code = diff.doctor_main(["--coverage", str(coverage)], runtime=scenario.runtime(), root=tmp_path)

    assert return_code != 0
    assert json.loads(capsys.readouterr().out) == {"status": "invalid", "issues": ["coverage_checksum"]}
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_doctor_reports_the_isolation_target_and_the_preflight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = Scenario(tmp_path, probe_error=IsolationError("refused"), preflight_returncode=1)

    diff.doctor_main(["--coverage", str(coverage_path(tmp_path))], runtime=scenario.runtime(), root=tmp_path)

    issues = json.loads(capsys.readouterr().out)["issues"]
    assert issues[-2:] == ["isolation_unreachable", "preflight_failed"]
    assert PREFLIGHT in [command.argv for command in scenario.commands]


def vars_of(metadata: substrate.SubstrateMetadata) -> dict[str, object]:
    return {
        "schema_version": metadata.schema_version,
        "complete": metadata.complete,
        "generation": metadata.generation,
        "config_fingerprint": metadata.config_fingerprint,
        "test_inventory_fingerprint": metadata.test_inventory_fingerprint,
        "runtime_fingerprint": metadata.runtime_fingerprint,
        "schema_fingerprint": metadata.schema_fingerprint,
        "coverage_sha256": metadata.coverage_sha256,
        "costs_sha256": metadata.costs_sha256,
    }
