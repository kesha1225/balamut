"""pytest-gremlins: every non-pardoned gremlin of ``transform_source``, as a verified source edit.

A gremlin is an (original node, mutated node) pair executed by mutation switching; its module is the original
tree with that node swapped, materialized by ``pymutbench.astmat``.
"""

from __future__ import annotations

import ast

from _common import emit, parse_args  # type: ignore[import-not-found]
from pymutbench.astmat import materialize, replace_node  # type: ignore[import-not-found]
from pytest_gremlins.instrumentation import transform_source


def main() -> None:
    root, files, out, scope = parse_args()
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            source = (root / rel).read_text(encoding="utf-8")
            gremlins, _tree = transform_source(source, str(root / rel))
            tree = ast.parse(source)
            for gremlin in gremlins:
                if gremlin.pardoned:
                    continue
                node = gremlin.original_node
                if not scope.admits(rel, getattr(node, "lineno", None), getattr(node, "end_lineno", None)):
                    continue
                try:
                    mutated_tree = replace_node(tree, gremlin.original_node, gremlin.mutated_node)
                except LookupError:
                    emit(fh, rel, gremlin.gremlin_id, gremlin.operator_name, None, status="node_not_found")
                    continue
                text, status = materialize(source, mutated_tree)
                emit(fh, rel, gremlin.gremlin_id, gremlin.operator_name, text, status=status,
                     line=gremlin.line_number, description=gremlin.description)


if __name__ == "__main__":
    main()
