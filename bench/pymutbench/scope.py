"""The mutation scope of a bug: the definitions of the fixed file that the fix touched.

A changed line of the fixed file belongs to its innermost enclosing function (decorators included); a changed
line outside any function belongs to the innermost class-body statement or top-level statement containing it.
A pure deletion marks the lines on both sides of where the removed lines were. The scope is the union of those
definitions' line ranges; a mutant is in scope when its whole edit lies inside one range. Every tool is
filtered by the same ranges after generating mutants for the whole file.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Region:
    start: int
    end: int
    kind: str  # function, class-statement or module-statement
    name: str


def changed_lines(unified_diff_u0: str) -> set[int]:
    """Lines of the new file that a ``-U0`` diff adds or modifies (pure deletions mark both neighbours)."""
    lines: set[int] = set()
    for match in _HUNK.finditer(unified_diff_u0):
        start, count = int(match.group(1)), int(match.group(2) or "1")
        if count:
            lines.update(range(start, start + count))
        else:
            lines.update({start, start + 1})
    return lines


def changed_lines_in(unified_diff: str, path: str) -> set[int]:
    """Lines of the new version of ``path`` that a unified diff (any context size) adds or modifies; a pure
    deletion marks the lines on both sides of where the removed lines were."""
    lines: set[int] = set()
    current, new_line, in_hunk, pending_deletion = None, 0, False, False
    for raw in unified_diff.splitlines():
        if raw.startswith("+++ "):
            if pending_deletion:
                lines.update({max(1, new_line - 1), new_line})
                pending_deletion = False
            current = raw[6:] if raw.startswith("+++ b/") else raw[4:]
            in_hunk = False
            continue
        if raw.startswith("--- ") and not in_hunk:
            continue
        match = _HUNK.match(raw)
        if match:
            if pending_deletion and current == path:
                lines.update({max(1, new_line - 1), new_line})
            new_line, in_hunk, pending_deletion = int(match.group(1)), True, False
            continue
        if not in_hunk or current != path:
            continue
        if raw.startswith("+"):
            lines.add(new_line)
            pending_deletion = False
            new_line += 1
        elif raw.startswith("-"):
            pending_deletion = True
        elif raw.startswith("\\"):
            continue
        else:
            if pending_deletion:  # removed lines not replaced: mark both neighbours of the gap
                lines.update({max(1, new_line - 1), new_line})
                pending_deletion = False
            new_line += 1
    if pending_deletion:
        lines.update({max(1, new_line - 1), new_line})
    return lines


def _span(node: ast.stmt) -> tuple[int, int]:
    start = node.lineno
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.decorator_list:
        start = min(start, *(dec.lineno for dec in node.decorator_list))
    return start, node.end_lineno or node.lineno


def regions(source: str, lines: set[int]) -> list[Region]:
    tree = ast.parse(source)
    found: dict[tuple[int, int], Region] = {}
    for line in sorted(lines):
        region = _region_for(tree, line)
        if region is not None:
            found.setdefault((region.start, region.end), region)
    merged: list[Region] = []
    for region in sorted(found.values(), key=lambda r: (r.start, -r.end)):
        if merged and region.start >= merged[-1].start and region.end <= merged[-1].end:
            continue  # nested inside a region already in scope
        merged.append(region)
    return merged


def _contains(node: ast.stmt, line: int) -> bool:
    start, end = _span(node)
    return start <= line <= end


def _name(node: ast.stmt) -> str:
    return str(getattr(node, "name", type(node).__name__))


def _region_for(tree: ast.Module, line: int) -> Region | None:
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _contains(node, line)
    ]
    if functions:
        innermost = min(functions, key=lambda node: _span(node)[1] - _span(node)[0])
        return Region(*_span(innermost), "function", innermost.name)
    top = next((stmt for stmt in tree.body if _contains(stmt, line)), None)
    if top is None:
        return None  # a blank or comment line between statements
    if not isinstance(top, ast.ClassDef):
        return Region(*_span(top), "module-statement", _name(top))
    cls: ast.ClassDef = top
    while True:
        inner = next((stmt for stmt in cls.body if _contains(stmt, line)), None)
        if inner is None:
            return Region(*_span(cls), "class-statement", cls.name)  # the class header or a decorator
        if isinstance(inner, ast.ClassDef):
            cls = inner
            continue
        return Region(*_span(inner), "class-statement", f"{cls.name}.{_name(inner)}")


def in_scope(regions_: list[Region], first_line: int, last_line: int) -> bool:
    return any(region.start <= first_line and last_line <= region.end for region in regions_)
