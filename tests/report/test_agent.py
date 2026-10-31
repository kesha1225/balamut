"""The agent report (balamut.report.agent): the pure core plus the store adapter."""

from __future__ import annotations

from pathlib import Path

from balamut.core.triage import ColdKind
from balamut.coverage.store import Store
from balamut.report.agent import (
    MutantRecord,
    _family,
    _sites_str,
    _strings,
    build_analysis,
    build_report,
    records_from_store,
    render_agent_report,
    render_analysis,
    split_cold_records,
)


def _rec(
    file: str,
    lineno: int,
    operator: str,
    verdict: str,
    *,
    bucket: str = "high",
    original: str = "a < b",
    mutated: str = "a <= b",
    covering: list[str] | None = None,
    semantic: list[str] | None = None,
) -> MutantRecord:
    return {
        "file": file,
        "lineno": lineno,
        "operator": operator,
        "verdict": verdict,
        "bucket": bucket,
        "original": original,
        "mutated": mutated,
        "covering": covering or [],
        "semantic": semantic or [],
    }


def test_high_survivors_cluster_by_idiom_with_test_files() -> None:
    records = [
        _rec("src/a.py", 10, "compare:Lt->LtE", "survived", covering=["tests/a_test.py::t1"]),
        _rec("src/a.py", 20, "compare:Gt->GtE", "survived", covering=["tests/a_test.py::t2", "tests/b_test.py::t3"]),
        _rec("src/a.py", 30, "return:None", "survived", original="return x", covering=["tests/a_test.py::t4"]),
    ]
    report = build_report(records)
    compare = next(c for c in report["actionable_clusters"] if c["operator_family"] == "compare")
    assert compare["sites"] == [{"file": "src/a.py", "lines": [10, 20]}]  # one idiom "a < b" -> one cluster
    assert compare["count"] == 2
    assert compare["covering_test_files"] == ["tests/a_test.py", "tests/b_test.py"]  # node id -> file, dedup + sort
    assert any(c["operator_family"] == "return" for c in report["actionable_clusters"])  # another idiom: its own
    assert report["actionable"] == 3


def test_same_idiom_across_files_merges_into_one_cluster() -> None:
    records = [
        _rec("src/login.py", 347, "compare:IsNot->Is", "survived", original="user_id is not None"),
        _rec("src/signup.py", 133, "compare:IsNot->Is", "survived", original="user_id is not None"),
    ]
    report = build_report(records)
    assert len(report["actionable_clusters"]) == 1
    cluster = report["actionable_clusters"][0]
    assert cluster["count"] == 2
    assert cluster["sites"] == [
        {"file": "src/login.py", "lines": [347]},
        {"file": "src/signup.py", "lines": [133]},
    ]


def test_only_high_survivors_are_actionable() -> None:
    records = [
        _rec("src/a.py", 10, "compare:Lt->LtE", "survived", bucket="high"),
        _rec("src/a.py", 11, "const:num:1->2", "survived", bucket="review"),
        _rec("src/a.py", 12, "delete:call", "survived", bucket="low"),
        _rec("src/a.py", 13, "compare:Eq->NotEq", "killed", bucket="high"),
    ]
    report = build_report(records)
    assert report["actionable"] == 1
    assert report["survived"] == 3
    assert report["killed"] == 1


def test_no_coverage_hotspots_grouped_by_file_sorted() -> None:
    records = [
        _rec("src/a.py", 10, "compare:Lt->LtE", "no_coverage"),
        _rec("src/a.py", 20, "return:None", "no_coverage"),
        _rec("src/b.py", 5, "binop:Add->Sub", "no_coverage"),
    ]
    report = build_report(records)
    hotspots = report["uncovered_hotspots"]
    assert hotspots[0]["file"] == "src/a.py"  # more uncovered sites first
    assert hotspots[0]["uncovered"] == 2
    assert hotspots[0]["lines"] == [10, 20]
    assert report["no_coverage"] == 3


def test_score_and_terse_summary() -> None:
    records = [
        _rec("src/a.py", 1, "compare:Lt->LtE", "killed"),
        _rec("src/a.py", 2, "compare:Gt->GtE", "killed"),
        _rec("src/a.py", 3, "compare:Eq->NotEq", "killed"),
        _rec("src/a.py", 4, "return:None", "survived", bucket="high"),
        _rec("src/b.py", 5, "binop:Add->Sub", "no_coverage"),
    ]
    report = build_report(records)
    assert abs(report["score"] - 0.75) < 1e-9  # killed 3 / (killed 3 + survived 1)
    assert report["summary"] == "SCORE 75% | 1 actionable / 1 clusters | 1 uncovered"


def test_score_of_nothing_covered_is_zero() -> None:
    report = build_report([_rec("src/a.py", 1, "return:None", "no_coverage")])
    assert report["score"] == 0.0


def test_sample_collapses_multiline_whitespace() -> None:
    rec = _rec(
        "src/a.py", 1, "call:all->any", "survived", original="all(\n    x for x in y\n)", mutated="any((x for x in y))"
    )
    cluster = build_report([rec])["actionable_clusters"][0]
    assert cluster["sample"] == "all( x for x in y ) → any((x for x in y))"


def test_render_agent_report_human_pretty() -> None:
    records = [
        _rec("src/a.py", 10, "compare:Lt->LtE", "survived", covering=["tests/a_test.py::t1"]),
        _rec("src/b.py", 5, "return:None", "no_coverage", original="return x"),
    ]
    text = render_agent_report(build_report(records))
    assert "SCORE" in text
    assert "compare" in text
    assert "tests/a_test.py" in text
    assert "src/b.py" in text


def test_render_caps_clusters_and_points_to_the_full_json() -> None:
    records = [_rec("src/a.py", line, "compare:Lt->LtE", "survived", original=f"a < {line}") for line in range(5)]
    text = render_agent_report(build_report(records), max_clusters=2)
    assert "+3 clusters" in text


def test_timeouts_are_unresolved_clusters_rendered_before_actionable() -> None:
    records = [
        _rec("src/timeout.py", 10, "compare:Lt->LtE", "timeout", covering=["tests/timeout_test.py::test_timeout"]),
        _rec("src/a.py", 20, "return:None", "survived", original="return x"),
    ]

    report = build_report(records)

    assert report["timeout"] == 1
    assert report["unresolved"] == 1
    assert report["unresolved_clusters"][0]["sites"] == [{"file": "src/timeout.py", "lines": [10]}]
    assert report["unresolved_clusters"][0]["covering_test_files"] == ["tests/timeout_test.py"]
    text = render_agent_report(report)
    assert text.index("UNRESOLVED") < text.index("ACTIONABLE")


def _reachable_not_schema(file: str, lineno: int) -> bool:
    _ = lineno
    return "schema" not in file  # schema.py -> cold (class-level), everything else -> function body


def test_build_analysis_cold_body_split_and_semantic_survivors() -> None:
    records = [
        _rec("src/a.py", 1, "compare:Eq->NotEq", "killed"),
        _rec("src/access.py", 84, "boolop:And->Or", "survived", semantic=["auth"]),
        _rec("src/a.py", 10, "compare:Lt->LtE", "survived"),
        _rec("src/schema.py", 5, "const:num:1->2", "no_coverage"),
        _rec("src/h.py", 50, "return:None", "no_coverage"),
        _rec("src/h.py", 60, "binop:Add->Sub", "no_coverage"),
    ]
    report = build_analysis(
        records,
        _reachable_not_schema,
        cold_resolver=lambda _file, _line: ColdKind.SCHEMA_DEFAULT,
    )
    assert report["cold_no_coverage"] == 1
    assert report["body_no_coverage"] == 2
    assert report["cold_kinds"] == {ColdKind.SCHEMA_DEFAULT.value: 1}
    assert report["body_nocov_by_file"][0] == {"file": "src/h.py", "count": 2}
    assert len(report["semantic_survivors"]) == 1
    assert report["semantic_survivors"][0]["file"] == "src/access.py"
    assert report["semantic_survivors"][0]["semantic"] == ["auth"]
    assert report["survivors_by_family"]["compare"] == 1
    assert report["survivors_by_semantic"]["auth"] == 1
    assert report["survivors_by_semantic"]["(untagged)"] == 1
    text = render_analysis(report)
    assert "SEMANTIC" in text
    assert "COLD" in text
    assert "BODY" in text
    assert "schema_default=1" in text
    assert "killer is not attributed" in text


def test_build_analysis_without_a_resolver_calls_cold_sites_unattributed() -> None:
    report = build_analysis([_rec("src/schema.py", 5, "const:num:1->2", "no_coverage")], _reachable_not_schema)
    assert report["cold_kinds"] == {ColdKind.UNATTRIBUTED.value: 1}


def test_agent_report_exposes_cold_categories_without_claiming_a_killer() -> None:
    report = build_report([], cold_counts={ColdKind.TYPE_ONLY.value: 2, ColdKind.IMPORT_RUNTIME.value: 1})

    assert report["cold_no_coverage"] == 3
    assert report["cold_kinds"] == {ColdKind.IMPORT_RUNTIME.value: 1, ColdKind.TYPE_ONLY.value: 2}
    assert report["summary"].endswith("| 3 cold advisory")
    text = render_agent_report(report)
    assert "type_only=2" in text
    assert "killer is not attributed" in text


def test_split_cold_records_keeps_runtime_records_and_counts_cold_kinds() -> None:
    body = _rec("src/a.py", 1, "return:None", "no_coverage")
    cold = _rec("src/schema.py", 2, "const:num:1->2", "no_coverage")
    survived_cold_line = _rec("src/schema.py", 3, "compare:Lt->LtE", "survived")

    runtime, counts = split_cold_records(
        [body, cold, survived_cold_line], _reachable_not_schema, lambda _record: ColdKind.SCHEMA_DEFAULT
    )

    assert runtime == [body, survived_cold_line]  # only no-coverage sites can be cold
    assert counts == {ColdKind.SCHEMA_DEFAULT.value: 1}


def _put(store: Store, key: str, file: str, verdict: str, **extra: list[str] | str) -> None:
    covering = extra.get("covering", [])
    semantic = extra.get("semantic", [])
    mutated = extra.get("mutated", "a <= b")
    assert isinstance(covering, list)
    assert isinstance(semantic, list)
    assert isinstance(mutated, str)
    store.put_verdict(
        key=key,
        file=file,
        lineno=10,
        operator="compare:Lt->LtE",
        original="a < b",
        mutated=mutated,
        qualname="f",
        func_hash="h",
        verdict=verdict,
        bucket="high",
        covering=covering,
        covering_hash="ch",
        semantic=semantic,
    )


def test_records_from_store_roundtrip(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    _put(store, "k1", "src/a.py", "survived", covering=["tests/a_test.py::t1"], semantic=["auth"])
    _put(store, "k2", "src/b.py", "no_coverage")
    records = records_from_store(store, ["src/a.py", "src/b.py"])
    store.close()
    report = build_report(records)
    assert report["survived"] == 1
    assert report["no_coverage"] == 1
    assert report["actionable_clusters"][0]["covering_test_files"] == ["tests/a_test.py"]
    assert report["actionable_clusters"][0]["semantic"] == ["auth"]
    assert {record.get("key") for record in records} == {"k1", "k2"}


def test_records_from_store_filters_stale_keys(tmp_path: Path) -> None:
    """The store also holds a mutant of an earlier version of the file (same path, another key): the keys of the
    current run filter it out; None means no filter (the analysis sees everything)."""
    store = Store(tmp_path / "c.sqlite")
    _put(store, "k_cur", "src/a.py", "survived", mutated="a <= b")
    _put(store, "k_stale", "src/a.py", "survived", mutated="a < b")
    filtered = records_from_store(store, ["src/a.py"], {"k_cur"})
    unfiltered = records_from_store(store, ["src/a.py"])
    store.close()
    assert [record["mutated"] for record in filtered] == ["a <= b"]
    assert len(unfiltered) == 2


def test_records_from_store_reads_only_the_given_files(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    _put(store, "k1", "src/a.py", "survived")
    _put(store, "k2", "src/other.py", "survived")
    records = records_from_store(store, ["src/a.py", "src/missing.py"])
    store.close()
    assert [record["file"] for record in records] == ["src/a.py"]


def test_records_from_store_skips_stale_keys_without_stopping(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite")
    _put(store, "k_stale", "src/a.py", "survived", mutated="stale")
    _put(store, "k_cur", "src/a.py", "survived", mutated="current")
    records = records_from_store(store, ["src/a.py"], {"k_cur"})
    store.close()
    assert [record.get("key") for record in records] == ["k_cur"]


def test_family_uses_stored_kind_over_operator_prefix() -> None:
    rec = _rec("src/a.py", 1, "compare:Lt->LtE", "survived")
    rec["kind"] = "concurrency"
    assert _family(rec) == "concurrency"
    # An empty stored kind falls back to the operator's prefix.
    rec["kind"] = ""
    assert _family(rec) == "compare"
    del rec["kind"]
    assert _family(rec) == "compare"


def test_clusters_are_sorted_by_count_descending() -> None:
    records = [
        _rec("src/a.py", 1, "op1", "survived", original="one"),
        _rec("src/a.py", 2, "op2", "survived", original="two"),
        _rec("src/a.py", 3, "op2", "survived", original="two"),
    ]
    clusters = build_report(records)["actionable_clusters"]
    assert [cluster["count"] for cluster in clusters] == [2, 1]


def test_hotspots_are_sorted_by_uncovered_descending() -> None:
    records = [
        _rec("src/b.py", 1, "op", "no_coverage"),
        _rec("src/a.py", 1, "op", "no_coverage"),
        _rec("src/a.py", 2, "op", "no_coverage"),
    ]
    hotspots = build_report(records)["uncovered_hotspots"]
    assert [hotspot["file"] for hotspot in hotspots] == ["src/a.py", "src/b.py"]


def test_sites_str_joins_file_and_lines() -> None:
    assert _sites_str([{"file": "src/a.py", "lines": [1, 2]}]) == "src/a.py:1,2"


def test_semantic_survivors_sorted_by_file_and_lineno() -> None:
    records = [
        _rec("src/b.py", 5, "op", "survived", semantic=["auth"]),
        _rec("src/a.py", 20, "op", "survived", semantic=["auth"]),
        _rec("src/a.py", 10, "op", "survived", semantic=["auth"]),
    ]
    report = build_analysis(records, _reachable_not_schema)
    assert [(s["file"], s["lineno"]) for s in report["semantic_survivors"]] == [
        ("src/a.py", 10),
        ("src/a.py", 20),
        ("src/b.py", 5),
    ]


def test_render_analysis_includes_distribution_lines() -> None:
    records = [_rec("src/a.py", 1, "compare:Lt->LtE", "survived")]
    text = render_analysis(build_analysis(records, _reachable_not_schema))
    assert "HIGH survivors by operator" in text
    assert "HIGH survivors by semantic class" in text


def test_render_analysis_lists_semantic_survivors() -> None:
    records = [_rec("src/a.py", 1, "op", "survived", semantic=["auth"])]
    text = render_analysis(build_analysis(records, _reachable_not_schema))
    assert "src/a.py:1" in text


def test_render_analysis_truncates_semantic_samples_at_eighty() -> None:
    rec = _rec("src/a.py", 1, "op", "survived", original="y" * 200, mutated="z", semantic=["auth"])
    text = render_analysis(build_analysis([rec], _reachable_not_schema))
    assert "y" * 80 in text
    assert "y" * 81 not in text


def test_score_is_rounded_to_four_decimals() -> None:
    records = [
        _rec("src/a.py", 1, "op1", "killed"),
        _rec("src/a.py", 2, "op2", "killed"),
        _rec("src/a.py", 3, "op3", "survived"),
    ]
    assert build_report(records)["score"] == 0.6667
    assert build_analysis(records, _reachable_not_schema)["score"] == 0.6667


def test_render_unresolved_shows_no_coverage_for_empty_tests() -> None:
    records = [_rec("src/t.py", 10, "compare:Lt->LtE", "timeout")]
    text = render_agent_report(build_report(records))
    assert "NO coverage" in text


def test_render_caps_covering_test_files_to_three() -> None:
    covering = [f"tests/t{i}.py::t" for i in range(5)]
    records = [
        _rec("src/a.py", 1, "op", "survived", covering=covering),
        _rec("src/t.py", 2, "op", "timeout", covering=covering),
    ]
    text = render_agent_report(build_report(records))
    assert "tests/t0.py, tests/t1.py, tests/t2.py" in text
    assert "tests/t3.py" not in text


def test_render_truncates_samples_at_one_hundred() -> None:
    records = [
        _rec("src/a.py", 1, "op", "survived", original="x" * 200, mutated="y"),
        _rec("src/t.py", 2, "op", "timeout", original="x" * 200, mutated="y"),
    ]
    text = render_agent_report(build_report(records))
    assert "x" * 100 in text
    assert "x" * 101 not in text


def test_render_shows_no_overflow_line_when_exactly_at_cap() -> None:
    records = [_rec("src/a.py", i, "op", "survived", original=f"o{i}") for i in range(3)]
    text = render_agent_report(build_report(records), max_clusters=3)
    assert "has the full list" not in text


def test_render_shows_no_unresolved_overflow_at_cap() -> None:
    records = [_rec("src/t.py", i, "op", "timeout", original=f"o{i}") for i in range(2)]
    text = render_agent_report(build_report(records), max_clusters=2)
    assert "unresolved clusters" not in text


def test_render_shows_no_hotspot_overflow_at_cap() -> None:
    records = [_rec(f"src/f{i}.py", 1, "op", "no_coverage") for i in range(2)]
    text = render_agent_report(build_report(records), max_hotspots=2)
    assert "+0 files" not in text


def test_render_unresolved_cluster_lists_its_sites() -> None:
    records = [_rec("src/t.py", 10, "compare:Lt->LtE", "timeout", covering=["tests/t_test.py::t"])]
    text = render_agent_report(build_report(records))
    assert "src/t.py:10" in text
    assert "covered by: tests/t_test.py" in text


def test_render_includes_no_coverage_section() -> None:
    records = [_rec("src/a.py", 1, "op", "no_coverage")]
    text = render_agent_report(build_report(records))
    assert "NO COVERAGE" in text
    assert "src/a.py" in text


def test_render_shows_unresolved_overflow_count() -> None:
    records = [_rec("src/t.py", i, "op", "timeout", original=f"o{i}") for i in range(5)]
    text = render_agent_report(build_report(records), max_clusters=2)
    assert "+3 unresolved clusters" in text


def test_render_shows_hotspot_overflow_count() -> None:
    records = [_rec(f"src/f{i}.py", 1, "op", "no_coverage") for i in range(5)]
    text = render_agent_report(build_report(records), max_hotspots=2)
    assert "+3 files" in text


def test_strings_returns_empty_for_invalid_json_and_non_lists() -> None:
    assert _strings("not json") == []
    assert _strings('{"a": 1}') == []
    assert _strings('["a", 1, "b"]') == ["a", "b"]


def test_covering_test_files_capped_at_six() -> None:
    rec = _rec("src/a.py", 1, "op", "survived", covering=[f"tests/t{i}.py::t" for i in range(10)])
    cluster = build_report([rec])["actionable_clusters"][0]
    assert cluster["covering_test_files"] == [f"tests/t{i}.py" for i in range(6)]


def test_hotspot_lines_capped_at_eight() -> None:
    records = [_rec("src/a.py", i, "op", "no_coverage") for i in range(1, 12)]
    hotspot = build_report(records)["uncovered_hotspots"][0]
    assert len(hotspot["lines"]) == 8
    assert hotspot["uncovered"] == 11
