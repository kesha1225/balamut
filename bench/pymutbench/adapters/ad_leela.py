"""pytest-leela: the mutants its engine builds (type-enriched points, configured categories), as verified edits.

The engine's own steps are replayed: ``find_mutation_points``, ``enrich_mutation_points``, ``mutations_for``
with the allowed keys of the operator categories the plugin enables (``[tool.pytest-leela] operators``, by
default ``DEFAULT_OPERATORS``), and ``apply_mutation`` for the mutated tree. The engine numbers exactly these
mutants, one counter across the sorted target files, so the counter is its mutant id.
"""

from __future__ import annotations

from _common import emit, parse_args  # type: ignore[import-not-found]
from pymutbench.astmat import materialize  # type: ignore[import-not-found]
from pytest_leela.ast_analysis import find_mutation_points
from pytest_leela.config import load_config
from pytest_leela.import_hook import apply_mutation
from pytest_leela.models import Mutant
from pytest_leela.operators import ALL_OPERATORS, build_allowed_keys, mutations_for
from pytest_leela.type_extractor import enrich_mutation_points


def main() -> None:
    root, files, out, scope = parse_args()
    # The plugin's resolution: the project's configured categories, "all" for every category.
    operators = load_config(root).operators
    allowed = build_allowed_keys(ALL_OPERATORS if "all" in operators else operators)
    index = 0  # the engine's mutant id: one counter across the target files, which the plugin sorts
    with out.open("w", encoding="utf-8") as fh:
        for rel in sorted(files):
            path = root / rel
            source = path.read_text(encoding="utf-8")
            points = find_mutation_points(source, str(path), path.stem)
            points, _stats = enrich_mutation_points(source, points)
            for point in points:
                for replacement in mutations_for(point, True, allowed_keys=allowed):
                    native = str(index)
                    index += 1
                    if not scope.overlaps(rel, point.lineno, point.lineno):
                        continue
                    mutant = Mutant(point=point, replacement_op=replacement, mutant_id=int(native))
                    operator = f"{point.node_type}:{point.original_op}->{replacement}"
                    tree, applied = apply_mutation(source, mutant)
                    if not applied:
                        emit(fh, rel, native, operator, None, status="not_applied")
                        continue
                    text, status = materialize(source, tree)
                    emit(fh, rel, native, operator, text, status=status, line=point.lineno)


if __name__ == "__main__":
    main()
