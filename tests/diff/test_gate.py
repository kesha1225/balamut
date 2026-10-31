"""The diff gate (balamut.diff.gate): the changed-line filter and the classification of scoped mutants."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from balamut.core.triage import ColdKind
from balamut.diff import gate
from balamut.diff import scope as diff_scope
from balamut.diff.gate import Mutant, classify, main, parse_mutants, render
from balamut.diff.scope import DiffScope, changed_lines_from_diff, mark_untracked_whole
from balamut.report.agent import build_report

type _Raw = dict[str, str | int | list[str]]


def _rec(
    path: str,
    lineno: int,
    status: str,
    bucket: str = "",
    *,
    site_lineno: int | None = None,
    site_end_lineno: int | None = None,
    start_byte: int | None = None,
    end_byte: int | None = None,
    key: str = "key-1",
    covering_tests: list[str] | None = None,
    reason: str | None = None,
) -> _Raw:
    rec: _Raw = {
        "path": path,
        "lineno": lineno,
        "operator": "compare:Lt->LtE",
        "status": status,
        "covering": 3,
        "bucket": bucket,
        "original": "a < b",
        "mutated": "a <= b",
        "key": key,
        "covering_tests": covering_tests or [],
    }
    optional = {
        "site_lineno": site_lineno,
        "site_end_lineno": site_end_lineno,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "reason": reason,
    }
    rec.update({name: value for name, value in optional.items() if value is not None})
    return rec


def _mutants(*recs: _Raw) -> list[Mutant]:
    return parse_mutants(json.dumps(list(recs)))


def test_gate_records_cluster_scoped_survivors_like_the_agent_report() -> None:
    mutants = _mutants(
        _rec("src/a.py", 10, "survived", "high"),
        _rec("src/a.py", 12, "survived", "high"),
        _rec("src/b.py", 5, "no_coverage"),
    )
    report = build_report(gate.gate_records(mutants))
    assert report["actionable"] == 2
    assert len(report["actionable_clusters"]) == 1
    assert report["actionable_clusters"][0]["sites"] == [{"file": "src/a.py", "lines": [10, 12]}]
    assert report["uncovered_hotspots"][0]["file"] == "src/b.py"


def test_gate_records_preserve_exact_covering_tests() -> None:
    mutants = _mutants(
        _rec("src/a.py", 10, "survived", "high", key="key-a", covering_tests=["tests/a_test.py::test_a[param]"])
    )

    records = gate.gate_records(mutants)

    assert records[0]["covering"] == ["tests/a_test.py::test_a[param]"]
    assert records[0].get("key") == "key-a"


def test_parse_mutants_reads_optional_fields_and_falls_back_to_lineno() -> None:
    full, bare = _mutants(
        _rec("src/x.py", 12, "timeout", site_lineno=10, site_end_lineno=11, start_byte=3, end_byte=4, reason="hang"),
        _rec("src/x.py", 7, "killed"),
    )
    assert (full.site_lineno, full.site_end_lineno, full.start_byte, full.end_byte) == (10, 11, 3, 4)
    assert full.reason == "hang"
    assert (bare.site_lineno, bare.site_end_lineno, bare.start_byte, bare.end_byte) == (7, 7, None, None)
    assert bare.reason == ""


def test_parse_record_defaults_missing_key_and_covering_tests() -> None:
    rec: _Raw = {
        "path": "src/x.py",
        "lineno": 7,
        "operator": "compare:Lt->LtE",
        "status": "killed",
        "covering": 3,
        "bucket": "",
        "original": "a < b",
        "mutated": "a <= b",
    }
    mutant = parse_mutants(json.dumps([rec]))[0]
    assert mutant.key == ""
    assert mutant.covering_tests == ()


_DIFF = (
    "diff --git a/src/x.py b/src/x.py\n"
    "--- a/src/x.py\n"
    "+++ b/src/x.py\n"
    "@@ -10,0 +11,2 @@\n"
    "+    new1\n"
    "+    new2\n"
    "@@ -20 +22 @@\n"
    "+    changed\n"
    "diff --git a/src/y.py b/src/y.py\n"
    "--- a/src/y.py\n"
    "+++ b/src/y.py\n"
    "@@ -5,2 +5,0 @@\n"
    "-    del1\n"
    "-    del2\n"
)


def test_changed_lines_parses_added_and_ignores_pure_deletions() -> None:
    changed = changed_lines_from_diff(_DIFF)
    assert changed["src/x.py"] == (11, 12, 22)
    assert "src/y.py" not in changed  # +5,0 is a pure deletion: no added line is in scope


def test_dev_null_target_skipped() -> None:
    assert changed_lines_from_diff("+++ /dev/null\n@@ -1,2 +0,0 @@\n-gone\n") == {}


def test_changed_lines_handles_mnemonic_prefix() -> None:
    """With diff.mnemonicPrefix the destination prefix is ``w/``: a key kept as ``w/src/x.py`` would never match the
    mutant's path, and the changed file would silently drop out of the gate."""
    diff = (
        "diff --git c/src/x.py w/src/x.py\n--- c/src/x.py\n+++ w/src/x.py\n@@ -77,0 +78 @@ async def f():\n+    a = 1\n"
    )
    assert changed_lines_from_diff(diff) == {"src/x.py": (78,)}


def test_changed_lines_handles_no_prefix() -> None:
    assert changed_lines_from_diff("+++ src/x.py\n@@ -0,0 +1 @@\n+new\n") == {"src/x.py": (1,)}


def test_high_survivor_on_changed_line_is_actionable() -> None:
    muts = _mutants(_rec("src/x.py", 11, "survived", "high"))
    result = classify(muts, {"src/x.py": (11, 12)})
    assert result.actionable == muts
    assert result.failed()


def test_high_survivor_on_unchanged_line_is_out_of_scope() -> None:
    result = classify(_mutants(_rec("src/x.py", 99, "survived", "high")), {"src/x.py": (11, 12)})
    assert result.actionable == []
    assert not result.failed()  # a mutant on an untouched line of a changed file is someone else's debt


def test_no_coverage_on_changed_line_gates() -> None:
    muts = _mutants(_rec("src/x.py", 11, "no_coverage"))
    result = classify(muts, {"src/x.py": (11,)})
    assert result.uncovered == muts
    assert result.failed()


def test_timeout_on_changed_line_is_unresolved_and_gates() -> None:
    mutants = _mutants(_rec("src/x.py", 11, "timeout"))

    result = classify(mutants, {"src/x.py": (11,)})

    assert result.unresolved == mutants
    assert result.failed()


def test_default_mutant_scoped_by_site_line_not_body_line() -> None:
    """A default mutant selects tests by a body line but mutates the ``def`` line: a diff of only the def line must
    still bring it into scope, or a changed default would pass clean."""
    muts = _mutants(_rec("src/x.py", 12, "survived", "high", site_lineno=10))
    result = classify(muts, {"src/x.py": (10,)})
    assert result.actionable == muts
    assert result.failed()


def test_record_without_site_line_falls_back_to_lineno() -> None:
    muts = _mutants(_rec("src/x.py", 11, "survived", "high"))
    assert classify(muts, {"src/x.py": (11,)}).failed()
    assert classify(muts, {"src/x.py": (99,)}).actionable == []


def test_multiline_mutant_scoped_by_its_whole_span() -> None:
    muts = _mutants(_rec("src/x.py", 10, "survived", "high", site_lineno=10, site_end_lineno=12))
    assert classify(muts, {"src/x.py": (12,)}).actionable == muts
    assert classify(muts, {"src/x.py": (11,)}).actionable == muts
    assert classify(muts, {"src/x.py": (99,)}).actionable == []


def test_review_shown_not_gated() -> None:
    muts = _mutants(_rec("src/x.py", 11, "survived", "review"))
    result = classify(muts, {"src/x.py": (11,)})
    assert result.review == muts
    assert result.actionable == []
    assert not result.failed()


def test_low_survivor_suppressed() -> None:
    result = classify(_mutants(_rec("src/x.py", 11, "survived", "low")), {"src/x.py": (11,)})
    assert result.actionable == []
    assert result.review == []
    assert result.uncovered == []
    assert not result.failed()


def test_killed_counted_and_not_gating() -> None:
    result = classify(_mutants(_rec("src/x.py", 11, "killed")), {"src/x.py": (11,)})
    assert result.killed == 1
    assert not result.failed()


def test_untracked_whole_file_scopes_any_line() -> None:
    changed = mark_untracked_whole({}, ["src/new.py"])
    muts = _mutants(_rec("src/new.py", 500, "survived", "high"))
    assert classify(muts, changed).actionable == muts  # an empty tuple means the whole file


def test_file_not_in_diff_out_of_scope() -> None:
    assert not classify(_mutants(_rec("src/other.py", 11, "survived", "high")), {"src/x.py": (11,)}).failed()


def test_parse_mutants_empty_input() -> None:
    assert parse_mutants("") == []
    assert parse_mutants("[]") == []


def test_cold_no_coverage_is_advisory_not_gating() -> None:
    muts = _mutants(_rec("src/x.py", 11, "no_coverage"))
    result = classify(muts, {"src/x.py": (11,)}, reachable=lambda _f, _l: False)
    assert result.cold == muts
    assert result.uncovered == []
    assert not result.failed()


def test_cold_no_coverage_has_honest_ast_categories() -> None:
    mutants = _mutants(
        _rec("src/type.py", 1, "no_coverage"),
        _rec("src/schema.py", 2, "no_coverage"),
        _rec("src/runtime.py", 1, "no_coverage"),
        _rec("src/missing.py", 1, "no_coverage"),
    )
    sources = {
        "src/type.py": "value: int\n",
        "src/schema.py": "class Model:\n    limit: int = 10\n",
        "src/runtime.py": "DEFAULT_LIMIT = 10\n",
        "src/missing.py": None,
    }

    result = classify(
        mutants,
        {mutant.path: (mutant.lineno,) for mutant in mutants},
        reachable=lambda _file, _line: False,
        source_for=lambda file: sources[file],
    )

    assert result.cold_counts == {
        ColdKind.TYPE_ONLY.value: 1,
        ColdKind.SCHEMA_DEFAULT.value: 1,
        ColdKind.IMPORT_RUNTIME.value: 1,
        ColdKind.UNATTRIBUTED.value: 1,
    }
    output = render(result, "origin/main")
    assert "schema_default=1" in output
    assert "killer is not attributed" in output


def test_cold_categories_use_exact_site_span_on_shared_line() -> None:
    source = "SCHEMA: int = 10; RUNTIME = 20\n"
    schema_start = source.index("10")
    runtime_start = source.index("20")
    mutants = _mutants(
        _rec("src/x.py", 1, "no_coverage", start_byte=schema_start, end_byte=schema_start + 2, key="schema"),
        _rec("src/x.py", 1, "no_coverage", start_byte=runtime_start, end_byte=runtime_start + 2, key="runtime"),
    )

    result = classify(
        mutants, {"src/x.py": (1,)}, reachable=lambda _file, _line: False, source_for=lambda _file: source
    )

    assert result.cold_counts == {ColdKind.SCHEMA_DEFAULT.value: 1, ColdKind.IMPORT_RUNTIME.value: 1}


def test_gate_uses_disk_source_for_cold_categories(tmp_path: Path) -> None:
    src = tmp_path / "src/x.py"
    src.parent.mkdir()
    src.write_text("SCHEMA: int = 10\n", encoding="utf-8")
    muts = _mutants(_rec("src/x.py", 1, "no_coverage"))
    scope = DiffScope(
        base_ref="origin/main",
        base_sha="base-sha",
        diff_base_sha="diff-base-sha",
        head_sha="head-sha",
        changed_lines={"src/x.py": (1,)},
        file_hashes={"src/x.py": "hash"},
    )
    result = gate.gate(muts, scope, root=tmp_path)
    assert result.cold_counts == {ColdKind.SCHEMA_DEFAULT.value: 1}


def test_body_no_coverage_gates_but_cold_sibling_does_not() -> None:
    body = _rec("src/x.py", 11, "no_coverage")
    cold = _rec("src/x.py", 12, "no_coverage")
    result = classify(_mutants(body, cold), {"src/x.py": (11, 12)}, reachable=lambda _f, ln: ln == 11)
    assert [m.lineno for m in result.uncovered] == [11]
    assert [m.lineno for m in result.cold] == [12]
    assert result.failed()


def test_enum_construction_line_flags_enum_new_but_not_plain_init() -> None:
    src = (
        "from enum import StrEnum\n"  # 1
        "class E(StrEnum):\n"  # 2
        "    def __new__(cls, code, status):\n"  # 3
        "        obj = str.__new__(cls, code)\n"  # 4: enum __new__ body runs at import time
        "        return obj\n"  # 5
        "    a = ('a', 400)\n"  # 6: class-level member (code_reachable handles it, not this check)
        "class Plain:\n"  # 7
        "    def __init__(self, x):\n"  # 8
        "        self.x = x\n"  # 9: a plain __init__ runs when a test instantiates the class
    )
    assert gate.enum_construction_line(src, 4) is True
    assert gate.enum_construction_line(src, 5) is True
    assert gate.enum_construction_line(src, 9) is False
    assert gate.enum_construction_line("def broken(:\n", 1) is False


def test_enum_construction_line_includes_the_def_line_itself() -> None:
    src = (
        "from enum import StrEnum\n"  # 1
        "class E(StrEnum):\n"  # 2
        "    def __new__(cls, code, status):\n"  # 3
        "        obj = str.__new__(cls, code)\n"  # 4
        "        return obj\n"  # 5
    )
    assert gate.enum_construction_line(src, 3) is True


def test_disk_reachability_treats_enum_construction_as_cold_and_missing_files_as_body(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/kinds.py").write_text(
        "from enum import Enum\n"
        "class Kind(Enum):\n"
        "    def __new__(cls, code):\n"
        "        return object.__new__(cls)\n"
        "def run(value):\n"
        "    return value + 1\n",
        encoding="utf-8",
    )
    source_for = gate.disk_source(tmp_path)
    reachable = gate.source_reachable(source_for)

    assert reachable("src/kinds.py", 4) is False
    assert reachable("src/kinds.py", 6) is True
    assert reachable("src/gone.py", 1) is True  # an unknown file must not hide a gap
    assert source_for("src/gone.py") is None


def test_render_marks_clean_vs_failed() -> None:
    clean = classify(_mutants(_rec("src/x.py", 11, "killed")), {"src/x.py": (11,)})
    assert render(clean, "origin/main").startswith("✅")
    failed = classify(_mutants(_rec("src/x.py", 11, "survived", "high")), {"src/x.py": (11,)})
    out = render(failed, "origin/main")
    assert out.startswith("❌")
    assert "src/x.py:11" in out


def test_render_shows_timeout_before_review_and_cold() -> None:
    mutants = _mutants(
        _rec("src/timeout.py", 1, "timeout"),
        _rec("src/review.py", 2, "survived", "review"),
        _rec("src/cold.py", 3, "no_coverage"),
    )
    result = classify(
        mutants,
        {"src/timeout.py": (1,), "src/review.py": (2,), "src/cold.py": (3,)},
        reachable=lambda file, _line: file != "src/cold.py",
    )

    output = render(result, "origin/main")

    assert output.index("TIMEOUT / UNRESOLVED") < output.index("REVIEW") < output.index("COLD")


def test_render_emits_every_finding_section_and_counts_all_of_them() -> None:
    mutants = _mutants(
        _rec("src/timeout.py", 1, "timeout"),
        _rec("src/action.py", 2, "survived", "high"),
        _rec("src/body.py", 3, "no_coverage"),
        _rec("src/review.py", 4, "survived", "review"),
        _rec("src/cold.py", 5, "no_coverage"),
    )
    result = classify(
        mutants,
        {mutant.path: (mutant.lineno,) for mutant in mutants},
        reachable=lambda file, _line: file != "src/cold.py",
    )

    output = render(result, "origin/main")

    assert "3 unresolved or actionable finding(s) on the changed lines" in output  # the count line
    assert "TIMEOUT / UNRESOLVED" in output
    assert "src/timeout.py:1" in output  # formatted unresolved mutant
    assert "HIGH survived" in output
    assert "src/action.py:2" in output  # formatted actionable mutant
    assert "NO COVERAGE" in output
    assert "src/body.py:3" in output  # formatted uncovered mutant
    assert "look into the timeouts" in output  # the trailing advice line
    assert "src/review.py:4" in output  # formatted review mutant
    assert "src/cold.py:5" in output  # formatted cold mutant


def _write_scope(path: Path, changed: dict[str, tuple[int, ...]]) -> None:
    diff_scope.write_scope(
        DiffScope(
            base_ref="origin/main",
            base_sha="base-sha",
            diff_base_sha="merge-base-sha",
            head_sha="head-sha",
            changed_lines=changed,
            file_hashes=dict.fromkeys(changed, "hash"),
        ),
        path,
    )


@pytest.mark.parametrize("agent_json", [False, True])
def test_main_uses_supplied_scope_for_both_report_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    agent_json: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    mutants_path = tmp_path / "mutants.json"
    mutants_path.write_text(json.dumps([_rec("src/x.py", 11, "survived", "high")]), encoding="utf-8")
    scope_path = tmp_path / "scope.json"
    _write_scope(scope_path, {"src/x.py": (11,)})
    read_scope = diff_scope.read_scope
    reads: list[Path] = []

    def counted_read_scope(path: Path) -> DiffScope:
        reads.append(path)
        return read_scope(path)

    def unexpected_resolve(*_args: str, **_kwargs: Path) -> DiffScope:
        raise AssertionError("a supplied --scope must not be resolved through git")

    monkeypatch.setattr(diff_scope, "read_scope", counted_read_scope)
    monkeypatch.setattr(diff_scope, "resolve_scope", unexpected_resolve)
    argv = ["--json", str(mutants_path), "--base", "must-not-resolve", "--scope", str(scope_path)]
    if agent_json:
        argv.append("--agent-json")

    assert main(argv) == 1
    assert reads == [scope_path]
    output = capsys.readouterr().out
    if agent_json:
        assert json.loads(output)["actionable"] == 1
    else:
        assert output.startswith("❌ diff gate vs origin/main")


def test_main_agent_json_reports_cold_categories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    source_path = Path("src/schema.py")
    source_path.parent.mkdir()
    source_path.write_text("class Model:\n    limit: int = 10\n", encoding="utf-8")
    mutants_path = tmp_path / "mutants.json"
    mutants_path.write_text(json.dumps([_rec(source_path.as_posix(), 2, "no_coverage")]), encoding="utf-8")
    scope_path = tmp_path / "scope.json"
    _write_scope(scope_path, {source_path.as_posix(): (2,)})

    assert main(["--json", str(mutants_path), "--scope", str(scope_path), "--agent-json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["no_coverage"] == 0
    assert report["cold_no_coverage"] == 1
    assert report["cold_kinds"] == {ColdKind.SCHEMA_DEFAULT.value: 1}


def test_main_agent_json_routes_body_no_coverage_to_runtime_not_cold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    source_path = Path("src/body.py")
    source_path.parent.mkdir()
    source_path.write_text("def f():\n    x = 1\n", encoding="utf-8")
    mutants_path = tmp_path / "mutants.json"
    mutants_path.write_text(json.dumps([_rec(source_path.as_posix(), 2, "no_coverage")]), encoding="utf-8")
    scope_path = tmp_path / "scope.json"
    _write_scope(scope_path, {source_path.as_posix(): (2,)})

    assert main(["--json", str(mutants_path), "--scope", str(scope_path), "--agent-json"]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["no_coverage"] == 1
    assert report["cold_no_coverage"] == 0


def test_main_agent_json_keeps_non_ascii_samples_readable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    source_path = Path("src/x.py")
    source_path.parent.mkdir()
    source_path.write_text("def f():\n    return 1\n", encoding="utf-8")
    mutants_path = tmp_path / "mutants.json"
    mutants_path.write_text(json.dumps([_rec(source_path.as_posix(), 2, "survived", "high")]), encoding="utf-8")
    scope_path = tmp_path / "scope.json"
    _write_scope(scope_path, {source_path.as_posix(): (2,)})

    assert main(["--json", str(mutants_path), "--scope", str(scope_path), "--agent-json"]) == 1

    output = capsys.readouterr().out
    assert "→" in output  # ensure_ascii=False keeps the sample arrow literal
    assert '\n  "' in output  # indent=2


def test_main_requires_the_json_argument(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_main_reports_an_unreadable_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", str(tmp_path / "missing.json"), "--scope", str(tmp_path / "scope.json")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_main_reports_an_unreadable_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mutants_path = tmp_path / "mutants.json"
    mutants_path.write_text("[]", encoding="utf-8")

    assert main(["--json", str(mutants_path), "--scope", str(tmp_path / "missing-scope.json")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_main_without_a_scope_resolves_the_base_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    mutants_path = tmp_path / "mutants.json"
    mutants_path.write_text("[]", encoding="utf-8")
    calls: list[tuple[str, Path, diff_scope.BaseMode]] = []

    def resolve(base_ref: str, *, repo_root: Path, base_mode: diff_scope.BaseMode) -> DiffScope:
        calls.append((base_ref, repo_root, base_mode))
        return DiffScope(base_ref, "b", "d", "h", {}, {})

    monkeypatch.setattr(diff_scope, "resolve_scope", resolve)

    assert main(["--json", str(mutants_path), "--base", "origin/release"]) == 0
    assert calls == [("origin/release", Path.cwd(), diff_scope.BaseMode.DIRECT)]
    assert capsys.readouterr().out.startswith("✅ diff gate clean vs origin/release")


def test_gate_type_aliases_are_unions() -> None:
    # Module-level type aliases execute only at import; a reload inside the test body re-runs them so the
    # coverage substrate attributes the lines to this test (a COLD mutant then reads as unsupported, not a gap).
    from collections.abc import Callable

    importlib.reload(gate)
    assert gate.SourceFor.__value__ == Callable[[str], str | None]
    assert gate._SourceCache.__value__ == dict[str, str | None]
