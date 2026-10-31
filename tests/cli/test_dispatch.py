"""The ``balamut`` dispatcher: subcommand routing and project root discovery (balamut.cli)."""

from __future__ import annotations

from pathlib import Path

import pytest

from balamut import __version__, cli
from bm_support import write_file


def test_project_root_is_the_nearest_directory_with_a_balamut_table(tmp_path: Path) -> None:
    write_file(tmp_path / "pyproject.toml", "[tool.balamut]\n")
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    write_file(tmp_path / "src" / "pyproject.toml", '[project]\nname = "inner"\n')

    assert cli.find_project_root(nested) == tmp_path


def test_project_root_falls_back_to_the_nearest_pyproject(tmp_path: Path) -> None:
    write_file(tmp_path / "pyproject.toml", '[project]\nname = "x"\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert cli.find_project_root(nested) == tmp_path


def test_project_root_falls_back_to_the_start_directory(tmp_path: Path) -> None:
    assert cli.find_project_root(tmp_path) == tmp_path


def test_dispatch_runs_the_subcommand_from_the_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_file(tmp_path / "pyproject.toml", "[tool.balamut]\n")
    nested = tmp_path / "src"
    nested.mkdir()
    monkeypatch.chdir(nested)
    seen: list[tuple[Path, list[str]]] = []

    def fake_main(argv: list[str]) -> int:
        seen.append((Path.cwd(), argv))
        return 7

    monkeypatch.setitem(cli.COMMANDS, "mutate", cli.Command(fake_main, "test double"))

    assert cli.dispatch(["mutate", "src/x.py", "--json"]) == 7
    assert seen == [(tmp_path, ["src/x.py", "--json"])]


def test_explicit_root_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    seen: list[Path] = []

    def fake_main(_argv: list[str]) -> int:
        seen.append(Path.cwd())
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(cli.COMMANDS, "analyze", cli.Command(fake_main, "test double"))

    assert cli.dispatch(["--root", str(other), "analyze"]) == 0
    assert seen == [other.resolve()]


@pytest.mark.parametrize("argv", [[], ["--help"], ["help"]])
def test_usage_lists_the_commands(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.dispatch(argv) == (0 if argv else 2)
    out = capsys.readouterr()
    assert "mutate" in out.out + out.err


def test_unknown_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.dispatch(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_declares_balamut_rejects_invalid_toml(tmp_path: Path) -> None:
    """A broken pyproject is not a declaration: the except branch returns False, not None."""
    pyproject = write_file(tmp_path / "pyproject.toml", "[tool\n")

    assert cli._declares_balamut(pyproject) is False


def test_usage_aligns_summaries_to_the_longest_command() -> None:
    """Every command is padded to the width of the longest name, so summaries share a column."""
    usage = cli._usage()
    width = max(len(name) for name in cli.COMMANDS)
    shortest = min(cli.COMMANDS, key=len)

    assert f"  {shortest:<{width}}  " in usage


def test_root_without_a_command_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--root DIR`` with no command shows the usage (not an unknown command for ``--root``)."""
    assert cli.dispatch(["--root", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "usage:" in err
    assert "unknown command" not in err


def test_command_registry_wires_each_subcommand_main_and_summary() -> None:
    """Each ``Command`` binds the real entry point and its one-line summary: a swapped or dropped argument would
    leave a name mapping to a summary string (or fail the import outright). The reload re-executes the module-level
    ``COMMANDS`` table inside this test so the coverage substrate attributes those lines to it."""
    import importlib

    from balamut.cli import analyze, confirm, crosskill, diff, estimate, live, mutate, new, substrate_cmd

    importlib.reload(cli)

    expected = {
        "diff": (diff.main, "the diff mutation gate: substrate, campaign over the diff, verdict report"),
        "doctor": (diff.doctor_main, "check the substrate, the isolation target and the preflight (read-only)"),
        "substrate": (substrate_cmd.main, "build or reuse the coverage substrate of the current tree"),
        "mutate": (mutate.main, "run a mutation campaign over paths or a diff"),
        "confirm": (confirm.main, "re-run the mutants of some paths after measuring new tests"),
        "live": (live.main, "run a campaign with a live dashboard and an honest ETA"),
        "estimate": (estimate.main, "estimate the duration of a campaign before running it"),
        "analyze": (analyze.main, "summarize the cached verdicts of the project"),
        "crosskill": (crosskill.main, "the kill matrix: redundant and fragile tests"),
        "new": (new.main, "scaffold the project config: pyproject wiring, the config file, layout and diff base"),
    }
    assert set(cli.COMMANDS) == set(expected)
    for name, (main, summary) in expected.items():
        command = cli.COMMANDS[name]
        # ``is`` is too strict: another test reloads ``diff``, giving new function objects. Name and module pin the
        # binding without relying on object identity.
        assert command.main.__name__ == main.__name__
        assert command.main.__module__ == main.__module__
        assert command.summary == summary


def test_version_flag_prints_the_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.dispatch(["--version"]) == 0
    assert capsys.readouterr().out == f"balamut {__version__}\n"


def test_main_cli_dispatches_argv_without_the_program_name(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    seen: list[list[str]] = []

    def fake_dispatch(argv: list[str]) -> int:
        seen.append(argv)
        return 5

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    monkeypatch.setattr(sys, "argv", ["balamut", "mutate", "src/x.py"])

    assert cli.main_cli() == 5
    assert seen == [["mutate", "src/x.py"]]
