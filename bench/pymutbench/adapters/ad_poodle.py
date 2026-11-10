"""poodle: ``create_mutants_for_file`` with the default configuration, applied by poodle's ``mutate_lines``.

The text is composed exactly as poodle's runner writes the trial file: the file's lines (newlines kept) with
the mutant's column span replaced by its text.
"""

from __future__ import annotations

from pathlib import Path

from _common import emit, parse_args  # type: ignore[import-not-found]
from poodle.config import build_config
from poodle.data_types import PoodleWork
from poodle.mutate import create_mutants_for_file, initialize_mutators
from poodle.util import mutate_lines


def main() -> None:
    root, files, out, scope = parse_args()
    config = build_config((root,), None, 0, 0, None, (), (), (), None, None, None)
    work = PoodleWork(config)
    work.mutators = initialize_mutators(work)
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            file = root / rel
            lines = file.read_text("utf-8").splitlines(keepends=True)
            for index, mutant in enumerate(create_mutants_for_file(work, Path(root), file)):
                if not scope.admits(rel, mutant.lineno, mutant.end_lineno):
                    continue
                mutated = "".join(mutate_lines(mutant, lines))
                native = f"{mutant.mutator_name}@{mutant.lineno}:{mutant.col_offset}#{index}"
                emit(fh, rel, native, mutant.mutator_name, mutated, line=mutant.lineno)


if __name__ == "__main__":
    main()
