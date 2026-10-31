"""Thin git subprocess helpers (balamut.gitutil)."""

from __future__ import annotations

from pathlib import Path

import pytest

from balamut import gitutil


def test_git_output_raw_decodes_stdout_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``text=True`` is what turns the captured bytes into a str; without it callers would receive bytes.
    def fake_run(
        command: list[str], *, capture_output: bool, text: bool, check: bool, **kwargs: object
    ) -> _FakeCompleted:
        assert capture_output is True
        assert check is True
        raw = b"out\n" if not text else "out\n"
        return _FakeCompleted(raw)

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    result = gitutil.git_output_raw("status")
    assert result == "out\n"
    assert isinstance(result, str)


def test_git_output_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git_output_raw(*args: str, **kwargs: object) -> str:
        return "  main  \n"

    monkeypatch.setattr(gitutil, "git_output_raw", fake_git_output_raw)
    assert gitutil.git_output("rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_git_output_in_is_none_outside_a_repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(command: list[str], **kwargs: object) -> _FakeCompleted:
        return _FakeCompleted("", returncode=128)

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    assert gitutil.git_output_in(tmp_path, "rev-parse", "--show-toplevel") is None


class _FakeCompleted:
    def __init__(self, stdout: str | bytes, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
