"""The analysis of a finished run from the cache (balamut.cli.analyze)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from balamut.cli import analyze
from balamut.core.triage import ColdKind
from balamut.coverage.store import Store
from balamut.report.agent import MutantRecord
from bm_support import write_file


def _repo(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        write_file(root / relative, content)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def _put(store: Store, key: str, file: str, lineno: int, verdict: str, *, semantic: list[str] | None = None) -> None:
    store.put_verdict(
        key=key,
        file=file,
        lineno=lineno,
        operator="boolop:And->Or",
        original="a and b",
        mutated="a or b",
        qualname="f",
        func_hash="h",
        verdict=verdict,
        bucket="high",
        covering=[],
        covering_hash="",
        semantic=semantic,
    )


def test_tracked_python_passes_the_paths_and_root_to_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_git(*args: object, **kwargs: object) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "src/a.py\0src/b.py\0tests/c.py\0"

    monkeypatch.setattr(analyze, "git_output_raw", fake_git)

    files = analyze._tracked_python(tmp_path, ["src", "tests"])

    assert files == ["src/a.py", "src/b.py", "tests/c.py"]
    assert captured["args"] == ("ls-files", "-z", "--", "src", "tests")
    assert captured["kwargs"] == {"cwd": tmp_path}


def test_main_defaults_the_cache_to_root_then_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_default_cache_path(root: Path, state_dir: Path) -> Path:
        seen["args"] = (root, state_dir)
        return tmp_path / "c.sqlite"

    class _FakeStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def close(self) -> None:
            return None

    def fake_tracked_python(_root: Path, _paths: list[str]) -> list[str]:
        return ["x.py"]

    def fake_records_from_store(_store: Store, _files: list[str]) -> list[MutantRecord]:
        return []

    monkeypatch.setattr(analyze, "_tracked_python", fake_tracked_python)
    monkeypatch.setattr(analyze, "default_cache_path", fake_default_cache_path)
    monkeypatch.setattr(analyze, "records_from_store", fake_records_from_store)
    monkeypatch.setattr(analyze, "Store", _FakeStore)
    write_file(tmp_path / "pyproject.toml", "[tool.balamut]\n")
    monkeypatch.chdir(tmp_path)

    analyze.main(["x.py"])

    profile = analyze.load_profile(tmp_path)
    assert seen["args"] == (profile.root, profile.state_dir)


def test_main_json_reports_semantic_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, {"src/pkg/access.py": "def allowed(a, b):\n    return a and b\n"})
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    _put(store, "k", "src/pkg/access.py", 2, "survived", semantic=["auth"])
    store.close()

    assert analyze.main(["src/pkg", "--cache", str(cache), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["survived"] == 1
    assert [item["semantic"] for item in report["semantic_survivors"]] == [["auth"]]


def test_main_json_reports_cold_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, {"src/pkg/kinds.py": 'from enum import Enum\n\n\nclass Kind(Enum):\n    A = "a"\n'})
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    _put(store, "cold", "src/pkg/kinds.py", 5, "no_coverage")
    store.close()

    assert analyze.main(["src/pkg/kinds.py", "--cache", str(cache), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["body_no_coverage"] == 0
    assert report["cold_no_coverage"] == 1
    assert report["cold_kinds"] == {ColdKind.IMPORT_RUNTIME.value: 1}


def test_main_human_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(tmp_path, {"src/pkg/access.py": "def allowed(a, b):\n    return a and b\n"})
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    _put(store, "k", "src/pkg/access.py", 2, "no_coverage")
    store.close()

    assert analyze.main(["src/pkg", "--cache", str(cache)]) == 0

    out = capsys.readouterr().out
    assert out.startswith("═══ SCORE")
    assert "src/pkg/access.py" in out


def test_main_rejects_paths_without_tracked_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, {"README.md": "x\n"})
    monkeypatch.chdir(tmp_path)

    assert analyze.main(["src", "--cache", str(tmp_path / "c.sqlite")]) == 2
    assert "no tracked .py" in capsys.readouterr().err


def test_main_rejects_files_without_cached_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, {"src/pkg/access.py": "def allowed(a, b):\n    return a and b\n"})
    monkeypatch.chdir(tmp_path)

    assert analyze.main(["src/pkg", "--cache", str(tmp_path / "c.sqlite")]) == 2
    assert "no cached verdicts" in capsys.readouterr().err


def test_main_closes_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, {"src/pkg/access.py": "def allowed(a, b):\n    return a and b\n"})
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    _put(store, "k", "src/pkg/access.py", 2, "survived", semantic=["auth"])
    store.close()
    closes = 0
    original_close = Store.close

    def tracked_close(store: Store) -> None:
        nonlocal closes
        closes += 1
        original_close(store)

    monkeypatch.setattr(Store, "close", tracked_close)

    assert analyze.main(["src/pkg", "--cache", str(cache), "--json"]) == 0
    assert closes == 1


def test_main_missing_source_counts_as_body_not_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tracked file missing on disk counts as a body gap (reachable), not a cold site."""
    _repo(tmp_path, {"src/pkg/gone.py": "def f():\n    return 1\n"})
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src/pkg/gone.py").unlink()  # keep it tracked but delete it on disk
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    store.put_verdict(
        key="k",
        file="src/pkg/gone.py",
        lineno=2,
        operator="boolop:And->Or",
        original="a and b",
        mutated="a or b",
        qualname="f",
        func_hash="h",
        verdict="no_coverage",
        bucket="high",
        covering=[],
        covering_hash="",
    )
    store.close()

    assert analyze.main(["src/pkg", "--cache", str(cache), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["body_no_coverage"] == 1
    assert report["cold_no_coverage"] == 0


def test_main_json_two_space_indent_and_raw_unicode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, {"src/pkg/access.py": "def allowed(a, b):\n    return a and b\n"})
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "c.sqlite"
    store = Store(cache)
    store.put_verdict(
        key="k",
        file="src/pkg/access.py",
        lineno=2,
        operator="boolop:And->Or",
        original="a ≤ b",
        mutated="a < b",
        qualname="f",
        func_hash="h",
        verdict="survived",
        bucket="high",
        covering=[],
        covering_hash="",
        semantic=["auth"],
    )
    store.close()

    assert analyze.main(["src/pkg", "--cache", str(cache), "--json"]) == 0

    out = capsys.readouterr().out
    assert '\n  "survived":' in out  # two-space indent
    assert "a ≤ b" in out  # ensure_ascii=False keeps the raw character
