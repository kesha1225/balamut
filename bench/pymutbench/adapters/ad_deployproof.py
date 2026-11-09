"""deployproof: the mutants its engine runs, as verified source edits.

``deployproof check`` judges a file's mutants in one in-memory schemata module (``generate_schemata_for_file``):
``MutationSchemataTransformer`` wraps every mutation site in ``MUTATED if _dp_m(N) else ORIGINAL`` and records
mutant ``N``, and the engine switches one ``N`` on at a time. Mutant ``N`` is that module with every switch
resolved: the mutated branch for ``N``, the original one for every other switch. Only when the tool's schemata
generation yields no module or no mutant does it fall back to ``generate_mutants_for_file``, whose enumeration is
replayed then: ``MutationCounter`` counts the locations, ``MutationTransformer(index)`` mutates a fresh tree, and
the mutants it skips (a tree it cannot unparse, a pragma-suppressed or dead-code line) are skipped.
``pymutbench.astmat`` turns each mutated tree into the smallest source edit with the same tree or bytecode, so
the mutant stays a local change of the file.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import IO, Any

from deployproof.mutator import (DeadCodeFinder, MutationCounter, MutationSchemataTransformer, MutationTransformer,
                                 generate_schemata_for_file)

from _common import emit, parse_args  # type: ignore[import-not-found]
from pymutbench.astmat import materialize  # type: ignore[import-not-found]

PRAGMAS = ("pragma: no mutate", "pragma: no-mutate", "mutmut: disable", "cosmic-ray: disable")


def _switch(node: ast.IfExp) -> int | None:
    """The mutant id of a schemata switch ``MUTATED if _dp_m(N) else ORIGINAL``; None for any other conditional."""
    test = node.test
    if (isinstance(test, ast.Call) and isinstance(test.func, ast.Name) and test.func.id == "_dp_m"
            and len(test.args) == 1 and isinstance(test.args[0], ast.Constant) and type(test.args[0].value) is int):
        return test.args[0].value
    return None


class _Resolve(ast.NodeTransformer):
    """The schemata module with mutant ``mid`` switched on and every other mutant off."""

    def __init__(self, mid: int) -> None:
        self.mid = mid

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        switch = _switch(node)
        if switch is None:
            return self.generic_visit(node)
        return self.visit(node.body if switch == self.mid else node.orelse)


class _Sites(MutationTransformer):
    """The fallback traversal with a target no site matches: it applies nothing and records, for every site index,
    the line span of the node the tool is visiting when it counts the site (the nearest node with a location,
    decorators included). The tool mutates that node or a part of it, so a site whose span does not overlap the
    scope cannot yield an in-scope edit: skipping it only saves re-parsing the module for that index."""

    def __init__(self) -> None:
        self._stack: list[ast.AST] = []
        self.spans: list[tuple[int, int] | None] = []
        super().__init__(-1)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "current_index" and hasattr(self, "spans") and isinstance(value, int) and value > len(self.spans):
            self.spans.append(self._span())
        super().__setattr__(name, value)

    def _span(self) -> tuple[int, int] | None:
        for node in reversed(self._stack):
            start = getattr(node, "lineno", None)
            if start is not None:
                start = min([start, *(dec.lineno for dec in getattr(node, "decorator_list", ()))])
                return start, getattr(node, "end_lineno", None) or node.lineno
        return None

    def visit(self, node: ast.AST) -> object:
        self._stack.append(node)
        try:
            return super().visit(node)
        finally:
            self._stack.pop()


def _schemata(fh: IO[str], rel: str, name: str, source: str, suppressed: set[int], dead: set[int], scope: Any,
              expected: int) -> None:
    transformer = MutationSchemataTransformer(suppressed, dead_lines=dead, line_ranges=None)
    schemata = transformer.visit(ast.parse(source))
    if len(transformer.mutant_records) != expected:
        raise RuntimeError(f"{rel}: {len(transformer.mutant_records)} schemata mutants, the tool records {expected}")
    # The line span of each switch's original branch: the edit of mutant N lies inside it.
    spans = {switch: (node.orelse.lineno, node.orelse.end_lineno or node.orelse.lineno)
             for node in ast.walk(schemata) if isinstance(node, ast.IfExp)
             if (switch := _switch(node)) is not None and hasattr(node.orelse, "lineno")}
    for mid, line, description, *_ in transformer.mutant_records:
        span = spans.get(mid)
        if span is not None and not scope.overlaps(rel, *span):
            continue
        mutated = _Resolve(mid).visit(copy.deepcopy(schemata))
        ast.fix_missing_locations(mutated)
        text, status = materialize(source, mutated)
        emit(fh, rel, f"{name}:{line}:mutant_{mid}", description, text, status=status, line=line)


def _fallback(fh: IO[str], rel: str, name: str, source: str, suppressed: set[int], dead: set[int], scope: Any) -> None:
    counter = MutationCounter()
    counter.visit(ast.parse(source))
    sites = _Sites()
    sites.visit(ast.parse(source))
    # The tool enumerates range(MutationCounter.count); its transformer may count fewer sites (it skips
    # ``yield None``, which the counter counts): an index without a recorded span is materialized anyway.
    for index in range(counter.count):
        span = sites.spans[index] if index < len(sites.spans) else None
        if span is not None and not scope.overlaps(rel, *span):
            continue
        transformer = MutationTransformer(index)
        mutated = transformer.visit(ast.parse(source))
        ast.fix_missing_locations(mutated)
        try:
            ast.unparse(mutated)
        except Exception:  # noqa: BLE001 - the tool skips a mutant whose tree it cannot render, on any error
            continue
        info = transformer.applied_info
        line = info[0] if info else 1
        if line in suppressed or line in dead:
            continue  # the tool's own exclusions, by the line it reports
        description = info[1] if info else f"Mutation #{index + 1}"
        # No filter on the reported line: for a match-case guard the tool reports line 1, and the edit span decides
        # the scope downstream.
        text, status = materialize(source, mutated)
        emit(fh, rel, f"{name}:{line}:mutant_{index + 1}", description, text, status=status, line=line)


def main() -> None:
    root, files, out, scope = parse_args()
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            path: Path = root / rel
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
            suppressed = {n for n, line in enumerate(source.splitlines(), start=1)
                          if "#" in line and any(p in line.split("#", 1)[1].lower() for p in PRAGMAS)}
            finder = DeadCodeFinder()
            finder.visit(tree)
            # The engine's choice: the schemata module when the tool's own generation yields one with mutants.
            module, mutants, _switches = generate_schemata_for_file(path)
            if module and mutants:
                _schemata(fh, rel, path.name, source, suppressed, finder.dead_lines, scope, len(mutants))
            else:
                _fallback(fh, rel, path.name, source, suppressed, finder.dead_lines, scope)


if __name__ == "__main__":
    main()
