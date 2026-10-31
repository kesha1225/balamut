"""Content hashes for the incremental verdict cache.

The source side is hashed per function (editing function A leaves the mutants of B cached); the test side
per file. Keys are content-addressed within a module, so shifting a function down the file without editing its
body keeps every key and every cached verdict.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from balamut.core.astcache import parse_module


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return sha(path.read_text(encoding="utf-8"))


def function_source(source: str, qualname: str, *, lineno: int | None = None) -> str | None:
    """Exact source of the function ``qualname`` (``def`` through its last line): the unit of the content hash.

    ``lineno`` (the mutation site) disambiguates same-named siblings: a property getter and setter, ``@overload``
    stubs and the implementation, a conditional redefinition. They share one ``co_qualname``; without the line
    the first sibling would be hashed and an edit of the real body would never invalidate its verdicts."""
    node = _find_func(parse_module(source), qualname.split("."), lineno)
    if node is None:
        return None
    return ast.get_source_segment(source, node)


def function_hash(source: str, qualname: str, *, lineno: int | None = None) -> str | None:
    segment = function_source(source, qualname, lineno=lineno)
    return sha(segment) if segment is not None else None


def function_start_lineno(source: str, qualname: str, *, lineno: int | None = None) -> int | None:
    """Line of the function's ``def``: the anchor for a site's position inside the function."""
    node = _find_func(parse_module(source), qualname.split("."), lineno)
    return None if node is None else node.lineno


def mutant_key(path: str, qualname: str, func_src: str, operator: str, mutated: str, rel_byte: int) -> str:
    """Content-addressed key: editing the function body yields a new key.

    ``path`` keeps identical functions of two modules apart: one key would give them one verdict row, each copy
    overwriting the other. ``rel_byte`` is the site's byte offset relative to the function start: unique per site
    (it separates two identical mutations on one line) yet stable when the function moves within its file."""
    return sha("\0".join([path, qualname, func_src, operator, mutated, str(rel_byte)]))


def covering_hash(parts: list[str]) -> str:
    """Order-independent hash of the covering side (file hashes, node ids)."""
    return sha("\0".join(sorted(parts)))


def _pick_sibling_by_line(
    matches: list[ast.FunctionDef | ast.AsyncFunctionDef], target_lineno: int | None
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The sibling whose ``[def, end]`` span contains ``target_lineno``; the first one without a line.

    The span starts at the ``def`` line (decorators above it are excluded), matching the enclosing-function
    index that produced the site line in the first place."""
    if not matches:
        return None
    if target_lineno is not None:
        for node in matches:
            if node.lineno <= target_lineno <= (node.end_lineno or node.lineno):
                return node
    return matches[0]


def _find_func(
    node: ast.AST, parts: list[str], target_lineno: int | None = None
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    if not parts:
        return None
    head, rest = parts[0], parts[1:]
    if head == "<locals>":  # nested functions are hashed through their top-level ancestor
        return None
    terminal: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for child in ast.iter_child_nodes(node):
        match child:
            case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name) if name == head:
                if not rest:
                    terminal.append(child)
                else:
                    nested = _find_func(child, rest, target_lineno)
                    if nested is not None:
                        return nested
            case ast.ClassDef(name=name) if name == head:
                nested = _find_func(child, rest, target_lineno)
                if nested is not None:
                    return nested
            case _:
                continue
    return _pick_sibling_by_line(terminal, target_lineno)
