"""mutation (amirouche): ``iter_deltas``' mutated trees with the default mutation set, as verified edits.

The tool mutates only nodes on lines its coverage run executed and runs ``ast.unparse`` of the mutated tree;
the adapter emits every candidate with the node's line, and the pipeline applies the coverage gate with the
historical suite's executed lines. Candidates whose unparsed text does not parse are dropped, as the tool
drops them.
"""

from __future__ import annotations

import ast
import random

import mutation as tool
from _common import emit, parse_args  # type: ignore[import-not-found]
from pymutbench.astmat import materialize  # type: ignore[import-not-found]


SEED = 0  # MutateNumber draws random literals; the adapter fixes the seed, as `mutation play --randomly-seed=0`


def main() -> None:
    root, files, out, scope = parse_args()
    random.seed(SEED)
    mutations = [m for m in tool.Mutation.ALL if tool.mutation_all(m)]
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            source = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for index, node in enumerate(tool.ast_walk(tree)):
                if not scope.admits(rel, getattr(node, "lineno", None), getattr(node, "end_lineno", None)):
                    continue  # the tool never mutates a node without a line (its coverage gate drops it)
                # `tool.mutate` inlined, to keep the name of the mutation class that produced each tree.
                produced = (
                    (type(mutation).__name__, item)
                    for mutation in mutations
                    if mutation.predicate(node)
                    for item in mutation.mutate(node, index, tree)
                )
                for count, (operator, (tree_copy, new_node)) in enumerate(produced):
                    line = getattr(new_node, "lineno", None)
                    try:
                        ast.parse(ast.unparse(tree_copy))
                    except SyntaxError:
                        continue
                    text, status = materialize(source, tree_copy)
                    emit(fh, rel, f"{index}:{count}:{operator}", operator, text, status=status, gate_line=line)


if __name__ == "__main__":
    main()
