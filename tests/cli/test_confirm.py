"""``balamut confirm``: does the new test kill the survivor? (balamut.cli.confirm)."""

from __future__ import annotations

import pytest

from balamut.cli import confirm


def test_confirm_refreshes_the_named_tests_and_reruns_the_paths_uncached(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_mutate(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(confirm, "mutate_main", fake_mutate)

    assert confirm.main(["src/pkg/calc.py", "--tests", "tests/test_calc.py,tests/test_more.py", "--kind", "src"]) == 0
    assert seen == [
        [
            "src/pkg/calc.py",
            "--refresh-tests",
            "tests/test_calc.py,tests/test_more.py",
            "--no-cache",
            "--json",
            "--kind",
            "src",
        ]
    ]


def test_confirm_needs_paths_and_tests(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        confirm.main(["src/pkg/calc.py"])
    assert "--tests" in capsys.readouterr().err
