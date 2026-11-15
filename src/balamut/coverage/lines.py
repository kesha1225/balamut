"""Which recorded coverage lines stand for a line of source.

Coverage records a line when the interpreter emits a line event for it, and CPython emits one only for the lines
in ``co_lines()``. A line whose instructions all carry another line number is never recorded: CPython 3.14 folds the
constant arguments that precede ``*args`` in a multi-line call into one tuple constant numbered with the call's first
line, so no test is ever recorded on the lines of those arguments. Such a line runs exactly when an instruction whose
source extent spans it runs, and coverage records the line that instruction carries.

Only the instructions of the code object the line belongs to count: the enclosing code spans a whole nested
definition with the instructions that create it, and those run at definition time, not when the line does. The
``__annotate__`` functions of deferred annotations (PEP 649) are left out for the same reason: their instructions span
the whole annotated definition and run only when the annotations are read.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from types import CodeType

type _Extent = tuple[int, int]  # (first line, last line)

_ANNOTATE = "__annotate__"


@dataclass(frozen=True, slots=True)
class _Code:
    depth: int  # nesting below the module code
    first: int
    last: int
    extents: tuple[_Extent, ...]  # distinct extents of its own positioned instructions


@dataclass(frozen=True, slots=True)
class LineEvents:
    """The line events of a module's code: the lines that have one, and the instruction extents of each code object."""

    lines: frozenset[int]
    codes: tuple[_Code, ...]

    def recorded_as(self, line: int) -> frozenset[int]:
        """The lines coverage records when ``line`` runs: the line itself when it has a line event, otherwise the
        lines of the innermost instructions that span it in the deepest code object holding it; none when no
        instruction of that code spans it."""
        if line in self.lines:
            return frozenset((line,))
        holders = [code for code in self.codes if code.first <= line <= code.last]
        if not holders:
            return frozenset()
        depth = max(code.depth for code in holders)
        spans = [
            (first, last)
            for code in holders
            if code.depth == depth
            for first, last in code.extents
            if first <= line <= last
        ]
        if not spans:
            return frozenset()
        height = min(last - first for first, last in spans)
        # An instruction's line number is the first line of its extent.
        return frozenset(first for first, last in spans if last - first == height)


@lru_cache(maxsize=512)
def line_events(source: str) -> LineEvents | None:
    """The line events of ``source`` compiled as a module; None when it does not compile."""
    try:
        module = compile(source, "<balamut>", "exec", dont_inherit=True)
    except SyntaxError:
        return None
    lines: set[int] = set()
    codes: list[_Code] = []
    pending: list[tuple[CodeType, int]] = [(module, 0)]
    while pending:
        code, depth = pending.pop()
        lines.update(line for _start, _end, line in code.co_lines() if line is not None)
        extents = {
            (first, last)
            for first, last, _col, _end_col in code.co_positions()
            if first is not None and last is not None
        }
        if extents:
            codes.append(
                _Code(
                    depth=depth,
                    first=min(first for first, _last in extents),
                    last=max(last for _first, last in extents),
                    extents=tuple(sorted(extents)),
                )
            )
        pending.extend(
            (const, depth + 1) for const in code.co_consts if isinstance(const, CodeType) and const.co_name != _ANNOTATE
        )
    return LineEvents(frozenset(lines), tuple(codes))
