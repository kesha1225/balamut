"""``balamut substrate``: build or reuse the coverage substrate of the current tree, without git."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from balamut.cli import substrate_cmd
from balamut.config import load_profile
from balamut.coverage import substrate
from balamut.coverage.measure import MeasureRequest, MeasureResult, cost_file_for, failure_file_for
from balamut.isolation import isolation_for
from balamut.paths import substrate_path
from bm_support import make_project, write_file
from bm_support.substrates import write_valid_coverage


class _Measure:
    def __init__(self, *, returncode: int = 0, failed: tuple[str, ...] = (), detail: str = "") -> None:
        super().__init__()
        self.calls: list[MeasureRequest] = []
        self.returncode = returncode
        self.failed = failed
        self.detail = detail

    def __call__(self, request: MeasureRequest) -> MeasureResult:
        self.calls.append(request)
        write_valid_coverage(request.coverage_file)
        file = request.coverage_file
        return MeasureResult(
            self.returncode,
            file,
            cost_file_for(file),
            failure_file_for(file),
            failed_nodeids=self.failed,
            detail=self.detail,
        )


def _project(tmp_path: Path) -> Path:
    return make_project(tmp_path / "proj")


def test_a_missing_substrate_is_built_with_its_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    measure = _Measure()

    assert substrate_cmd.main([], root=root, measure=measure) == 0

    path = substrate_path(load_profile(root))
    published = substrate.substrate_inspect(path)
    assert published is not None
    assert substrate.tree_read(path, published.generation) is not None
    assert len(measure.calls) == 1
    assert f"built generation {published.generation[:12]}" in capsys.readouterr().out


def test_a_substrate_of_the_current_tree_is_reused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    measure = _Measure()
    substrate_cmd.main([], root=root, measure=measure)
    capsys.readouterr()

    assert substrate_cmd.main([], root=root, measure=measure) == 0

    assert len(measure.calls) == 1
    assert "reused generation" in capsys.readouterr().out


def test_an_edited_tree_rebuilds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The command makes the substrate itself describe the current tree: a divergence rebuilds, not a refresh."""
    root = _project(tmp_path)
    measure = _Measure()
    substrate_cmd.main([], root=root, measure=measure)
    calc = root / "src/pkg/calc.py"
    calc.write_text(calc.read_text(encoding="utf-8") + "\n\nLIMIT = 3\n", encoding="utf-8")
    capsys.readouterr()

    assert substrate_cmd.main([], root=root, measure=measure) == 0

    assert len(measure.calls) == 2
    assert "(reason: tree_changed_outside_diff)" in capsys.readouterr().out


def test_rebuild_forces_a_new_generation(tmp_path: Path) -> None:
    root = _project(tmp_path)
    measure = _Measure()
    substrate_cmd.main([], root=root, measure=measure)
    first = substrate.substrate_inspect(substrate_path(load_profile(root)))

    assert substrate_cmd.main(["--rebuild"], root=root, measure=measure) == 0

    second = substrate.substrate_inspect(substrate_path(load_profile(root)))
    assert first is not None
    assert second is not None
    assert second.generation != first.generation


def _ensure(root: Path, measure: _Measure, *, refresh_drift: bool) -> substrate_cmd.Ensured:
    profile = load_profile(root)
    return substrate_cmd.ensure_substrate(
        profile, isolation_for(profile), substrate_path(profile), measure=measure, refresh_drift=refresh_drift
    )


def test_a_refreshable_divergence_keeps_the_generation_for_the_caller(tmp_path: Path) -> None:
    """A direct campaign refreshes an edited test into the store: a whole suite under coverage is not needed."""
    root = _project(tmp_path)
    measure = _Measure()
    built = _ensure(root, measure, refresh_drift=True)
    tests = root / "tests/test_calc.py"
    minor = "\n\ndef test_minor() -> None:\n    assert not is_adult(17)\n"
    tests.write_text(tests.read_text(encoding="utf-8") + minor, encoding="utf-8")

    kept = _ensure(root, measure, refresh_drift=True)

    assert built.built_reason == "missing_coverage"
    assert kept.built_reason is None
    assert kept.metadata.generation == built.metadata.generation
    assert len(measure.calls) == 1


def test_a_source_file_the_build_never_saw_rebuilds_despite_refresh(tmp_path: Path) -> None:
    root = _project(tmp_path)
    measure = _Measure()
    _ensure(root, measure, refresh_drift=True)
    (root / "src/pkg/extra.py").write_text("LIMIT = 3\n", encoding="utf-8")

    rebuilt = _ensure(root, measure, refresh_drift=True)

    assert rebuilt.built_reason == "tree_new_src"
    assert len(measure.calls) == 2


def test_a_red_baseline_names_the_failing_tests_and_publishes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    measure = _Measure(returncode=1, failed=("tests/test_calc.py::test_adult",))

    assert substrate_cmd.main([], root=root, measure=measure) == 2

    err = capsys.readouterr().err
    assert "error:" in err
    assert "BASELINE tests/test_calc.py::test_adult" in err
    assert substrate.substrate_inspect(substrate_path(load_profile(root))) is None


def test_a_red_build_wraps_the_message_and_the_failing_nodeids(tmp_path: Path) -> None:
    """``ensure_substrate`` wraps the build error with its message and the failing test node ids in order."""
    root = _project(tmp_path)
    measure = _Measure(returncode=1, failed=("tests/test_calc.py::test_adult",))
    profile = load_profile(root)

    with pytest.raises(substrate_cmd.SubstrateBuildFailedError) as excinfo:
        substrate_cmd.ensure_substrate(profile, isolation_for(profile), substrate_path(profile), measure=measure)

    error = excinfo.value
    assert error.nodeids == ("tests/test_calc.py::test_adult",)


def test_main_defaults_the_measure_to_the_configured_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    captured: dict[str, object] = {}

    def fake_measure(request: MeasureRequest, *, isolation: object) -> MeasureResult:
        captured["isolation"] = isolation
        write_valid_coverage(request.coverage_file)
        return MeasureResult(
            0, request.coverage_file, cost_file_for(request.coverage_file), failure_file_for(request.coverage_file)
        )

    monkeypatch.setattr(substrate_cmd, "measure_coverage", fake_measure)

    assert substrate_cmd.main([], root=root) == 0

    assert isinstance(captured["isolation"], isolation_for(load_profile(root)).__class__)


def test_exit_baseline_constant_is_two() -> None:
    assert substrate_cmd._EXIT_BASELINE == 2


def test_a_build_announces_it_is_measuring(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    measure = _Measure()

    assert substrate_cmd.main([], root=root, measure=measure) == 0

    assert "measuring the suite under coverage" in capsys.readouterr().err


def test_main_ensures_the_state_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    measure = _Measure()
    calls = 0

    def tracked_ensure(profile: object) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(substrate_cmd, "ensure_state_dir", tracked_ensure)

    assert substrate_cmd.main([], root=root, measure=measure) == 0
    assert calls == 1


def test_forced_rebuild_names_the_forced_reason_not_the_drift(tmp_path: Path) -> None:
    """With --rebuild the reason is forced_rebuild, not the tree drift the rebuild also repairs."""
    root = _project(tmp_path)
    profile = load_profile(root)
    path = substrate_path(profile)
    measure = _Measure()
    substrate_cmd.ensure_substrate(profile, isolation_for(profile), path, measure=measure)
    calc = root / "src/pkg/calc.py"
    calc.write_text(calc.read_text(encoding="utf-8") + "\n\nLIMIT = 3\n", encoding="utf-8")

    ensured = substrate_cmd.ensure_substrate(profile, isolation_for(profile), path, measure=_Measure(), rebuild=True)

    assert ensured.built_reason == "forced_rebuild"


def test_built_elapsed_is_the_measured_duration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    measure = _Measure()

    assert substrate_cmd.main([], root=root, measure=measure) == 0

    out = capsys.readouterr().out
    matched = re.search(r"in ([\d.]+)s", out)
    assert matched is not None
    elapsed = float(matched.group(1))
    assert 0 <= elapsed < time.monotonic()  # a difference, not a sum of clock readings


def test_built_generation_is_reported_as_twelve_characters(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    measure = _Measure()

    assert substrate_cmd.main([], root=root, measure=measure) == 0

    out = capsys.readouterr().out
    matched = re.search(r"built generation (\S+) at", out)
    assert matched is not None
    assert len(matched.group(1)) == 12


def test_a_build_with_a_detail_announces_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    measure = _Measure(detail="coverage measured 3 tests")

    assert substrate_cmd.main([], root=root, measure=measure) == 0

    assert "coverage measured 3 tests" in capsys.readouterr().err


def test_main_rejects_an_invalid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "missing.py"\n')

    assert substrate_cmd.main([], root=tmp_path) == 2
    assert "invalid [tool.balamut]" in capsys.readouterr().err
