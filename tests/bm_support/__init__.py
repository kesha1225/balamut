"""Shared helpers for balamut's own test suite."""

from __future__ import annotations

from pathlib import Path

from balamut.config import DEFAULT_TEST_PATTERNS, Layout


def write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_layout(root: Path, *, tests: tuple[str, ...] = ("tests",), source: tuple[str, ...] = ("src",)) -> Layout:
    return Layout(
        root=root.resolve(),
        source=source,
        import_roots=("src",) if source == ("src",) else (".",),
        tests=tests,
        test_patterns=DEFAULT_TEST_PATTERNS,
    )


SAMPLE_SOURCE = """\
def clamp(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


def is_adult(age: int) -> bool:
    return age >= 18
"""

SAMPLE_TESTS = """\
from pkg.calc import clamp, is_adult


def test_clamp_inside() -> None:
    assert clamp(5, 0, 10) == 5


def test_clamp_low() -> None:
    assert clamp(-3, 0, 10) == 0


def test_adult() -> None:
    assert is_adult(30)
"""


def make_project(
    root: Path,
    files: dict[str, str] | None = None,
    *,
    balamut: str = "",
    pytest_ini: str = "",
) -> Path:
    """A minimal src-layout project: ``src/pkg/calc.py`` with tests, importable through pytest's pythonpath."""
    write_file(
        root / "pyproject.toml",
        '[project]\nname = "sample"\nversion = "0"\n\n'
        f'[tool.pytest.ini_options]\npythonpath = ["src"]\naddopts = "-p no:cacheprovider"\n{pytest_ini}\n'
        f"[tool.balamut]\n{balamut}\n",
    )
    write_file(root / "src/pkg/__init__.py", "")
    write_file(root / "src/pkg/calc.py", SAMPLE_SOURCE)
    write_file(root / "tests/test_calc.py", SAMPLE_TESTS)
    for relative, content in (files or {}).items():
        write_file(root / relative, content)
    return root
