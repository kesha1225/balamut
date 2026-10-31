"""Load coverage data with per-test contexts into a queryable map.

Reads a ``.coverage`` SQLite file recorded with one dynamic context per test (the context is the test node id)
into ``{relpath: {lineno: frozenset[nodeid]}}``. For a mutant on line L only the tests covering L are run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from coverage import CoverageData

type TestIds = frozenset[str]
type LineTests = dict[int, TestIds]
type FileCoverage = dict[str, LineTests]
type FilesByTest = dict[str, TestIds]  # reverse index: node id -> files it executes
type _MutableFilesByTest = dict[str, set[str]]

_CONTEXT_PHASES = ("|run", "|setup", "|teardown")


def normalize_context(context: str) -> str:
    """A context's node id, cutting only a final ``|run``/``|setup``/``|teardown`` phase suffix.

    Splitting on the first ``|`` would break a parametrized id containing ``|`` inside the brackets
    (``test_f[a|b]|run``) and send a node id that does not exist."""
    for phase in _CONTEXT_PHASES:
        if context.endswith(phase):
            return context[: -len(phase)]
    return context


@dataclass(frozen=True, slots=True)
class CoverageMap:
    by_file: FileCoverage
    files_by_test: FilesByTest = field(init=False, default_factory=dict[str, TestIds])

    def __post_init__(self) -> None:
        # The reverse index is built once per load: the engine asks `files_covered_by` per mutant.
        index: _MutableFilesByTest = {}
        for relpath, lines in self.by_file.items():
            for tests in lines.values():
                for test in tests:
                    index.setdefault(test, set()).add(relpath)
        object.__setattr__(self, "files_by_test", {test: frozenset(files) for test, files in index.items()})

    def files_covered_by(self, test_ids: set[str]) -> set[str]:
        """Files executed by the given tests: the inverse of ``tests_covering``."""
        covered: set[str] = set()
        for test in test_ids:
            covered |= self.files_by_test.get(test, frozenset())
        return covered

    def files(self) -> list[str]:
        return sorted(self.by_file)

    def covered_lines(self, relpath: str) -> frozenset[int]:
        lines = self.by_file.get(relpath)
        return frozenset(lines) if lines else frozenset()

    def tests_covering(self, relpath: str, line: int) -> TestIds:
        lines = self.by_file.get(relpath)
        if lines is None:
            return frozenset()
        return lines.get(line, frozenset())

    def total_lines(self) -> int:
        return sum(len(lines) for lines in self.by_file.values())


def _relativize(measured: str, root: Path | None) -> str:
    path = Path(measured)
    if root is not None and path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return measured
    return measured


def load_coverage_map(coverage_path: Path, *, root: Path | None = None) -> CoverageMap:
    """Load a ``.coverage`` file; ``root`` relativizes absolute measured paths."""
    data = CoverageData(basename=str(coverage_path))
    data.read()
    by_file: FileCoverage = {}
    for measured in data.measured_files():
        contexts_by_line = data.contexts_by_lineno(measured)
        line_map: LineTests = {}
        for line, contexts in contexts_by_line.items():
            tests = frozenset(normalize_context(context) for context in contexts if context)
            if tests:
                line_map[line] = tests
        if line_map:
            by_file[_relativize(measured, root)] = line_map
    return CoverageMap(by_file)
