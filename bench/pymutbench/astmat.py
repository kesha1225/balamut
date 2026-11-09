"""Materialize an AST-level mutant as a minimal source edit, verified against the tool's own mutated AST.

Tools that mutate the AST (pytest-gremlins, pytest-leela, mutation) execute a module compiled from a mutated
tree, not a text. To judge such a mutant as a source file, the smallest differing subtree is located and the
original text of that node is replaced by ``ast.unparse`` of its mutated counterpart — bare, then
parenthesized, then the whole enclosing statement re-indented. A materialization counts only if the edited
source parses to exactly the tool's mutated tree (positions ignored) or compiles to the same bytecode as that
tree (line tables ignored: ``-1`` is ``UnaryOp(USub, 1)`` in source and folds to the constant a tool may have
written as ``Constant(-1)``); otherwise the mutant is recorded as unmaterializable, never silently dropped.
"""

from __future__ import annotations

import ast
from typing import Any

from pymutbench.artifacts import code_key


def dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _positioned(node: ast.AST) -> bool:
    return isinstance(node, ast.expr | ast.stmt) and getattr(node, "end_lineno", None) is not None


def _deepest(orig: ast.AST, mut: ast.AST, chain: list[tuple[ast.AST, ast.AST]]) -> list[tuple[ast.AST, ast.AST]]:
    """The chain of (original, mutated) node pairs from the root down to the smallest differing pair."""
    chain = [*chain, (orig, mut)]
    if type(orig) is not type(mut):
        return chain
    differing: list[tuple[ast.AST, ast.AST]] = []
    for name in orig._fields:
        a, b = getattr(orig, name, None), getattr(mut, name, None)
        if isinstance(a, ast.AST) and isinstance(b, ast.AST):
            if dump(a) != dump(b):
                differing.append((a, b))
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return chain
            for x, y in zip(a, b, strict=True):
                if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                    if dump(x) != dump(y):
                        differing.append((x, y))
                elif x != y:
                    return chain
        elif a != b:
            return chain
    if len(differing) == 1:
        return _deepest(*differing[0], chain)
    return chain


def _offset(lines: list[bytes], lineno: int, col: int) -> int:
    return sum(len(line) for line in lines[: lineno - 1]) + col


def _candidates(node: ast.AST, mutated: ast.AST, indent: str) -> list[str]:
    text = ast.unparse(mutated)
    if isinstance(node, ast.stmt):
        body = text.splitlines()
        return ["\n".join([body[0], *(indent + line if line else line for line in body[1:])])]
    return [text, f"({text})"]


def _bytecode(tree_or_source: ast.Module | str) -> tuple[Any, ...] | None:
    try:
        if isinstance(tree_or_source, str):
            return code_key(compile(tree_or_source, "<mutant>", "exec", dont_inherit=True))
        import copy  # noqa: PLC0415

        tree = ast.fix_missing_locations(copy.deepcopy(tree_or_source))
        return code_key(compile(tree, "<mutant>", "exec", dont_inherit=True))
    except (SyntaxError, ValueError, TypeError):
        return None


def materialize(source: str, mutated_tree: ast.Module) -> tuple[str | None, str]:
    """The mutated source and ``ok``, or ``None`` and the reason no faithful text was found."""
    original_tree = ast.parse(source)
    target = dump(mutated_tree)
    target_code: tuple[Any, ...] | None = None
    if dump(original_tree) == target:
        return None, "identical_tree"
    chain = _deepest(original_tree, mutated_tree, [])
    data = source.encode("utf-8")
    lines = data.splitlines(keepends=True)
    for orig, mut in reversed(chain):
        if not _positioned(orig):
            continue
        start = _offset(lines, orig.lineno, orig.col_offset)  # type: ignore[attr-defined]
        end = _offset(lines, orig.end_lineno, orig.end_col_offset)  # type: ignore[attr-defined]
        line = lines[orig.lineno - 1].decode("utf-8")  # type: ignore[attr-defined]
        indent = line[: len(line) - len(line.lstrip())]
        decorators = getattr(orig, "decorator_list", None)
        if decorators:
            # A definition's position starts at `def`/`class`; its decorators sit on the lines above and belong
            # to the replaced text (a removed decorator must disappear with it).
            first = min(dec.lineno for dec in decorators)
            start = _offset(lines, first, 0) + len(indent.encode("utf-8"))
        for text in _candidates(orig, mut, indent):
            candidate = (data[:start] + text.encode("utf-8") + data[end:]).decode("utf-8")
            try:
                if dump(ast.parse(candidate)) == target:
                    return candidate, "ok"
            except SyntaxError:
                continue
            if target_code is None:
                target_code = _bytecode(mutated_tree) or ()
            if target_code and _bytecode(candidate) == target_code:
                return candidate, "ok"
    return None, "no_faithful_text"


def replace_node(tree: ast.Module, original: ast.AST, replacement: ast.AST) -> ast.Module:
    """A copy of ``tree`` whose node at ``original``'s position (same type and shape) is ``replacement``."""
    import copy  # noqa: PLC0415

    key = (type(original), getattr(original, "lineno", None), getattr(original, "col_offset", None),
           getattr(original, "end_lineno", None), getattr(original, "end_col_offset", None), dump(original))
    tree = copy.deepcopy(tree)

    class _Swap(ast.NodeTransformer):
        done = False

        def generic_visit(self, node: ast.AST) -> Any:
            here = (type(node), getattr(node, "lineno", None), getattr(node, "col_offset", None),
                    getattr(node, "end_lineno", None), getattr(node, "end_col_offset", None))
            if not self.done and here == key[:5] and dump(node) == key[5]:
                self.done = True
                return copy.deepcopy(replacement)
            return super().generic_visit(node)

    swapper = _Swap()
    swapped = swapper.visit(tree)
    if not swapper.done:
        raise LookupError("the original node is not in the tree")
    return swapped
