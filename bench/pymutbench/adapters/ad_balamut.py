"""balamut: the production-source kinds of ``balamut diff`` (src, extreme, concurrency, fault), as text edits.

Every fact emits a byte span and its replacement (``apply_mutant`` is balamut's own splice). A ``fault``
replacement calls a helper balamut injects at run time; such a mutant has no plain-source equivalent and is
recorded as unrepresentable. The ``oracle`` kind mutates test files and is outside a source-mutation scope.
The mutants whose spliced lines lie outside the bug's scope are skipped before splicing.
"""

from __future__ import annotations

import hashlib

from _common import emit, parse_args  # type: ignore[import-not-found]
from balamut.core.mutants import apply_mutant
from balamut.engine.enumeration import load_builtin_kind
from balamut.operators.facts import enumerate_facts

KINDS = ("src", "extreme", "concurrency", "fault")


def main() -> None:
    root, files, out, scope = parse_args()
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            source = (root / rel).read_text(encoding="utf-8")
            data = source.encode("utf-8")
            for kind in KINDS:
                facts = load_builtin_kind(kind)
                for mutant in enumerate_facts(rel, source, (facts,), family=kind):
                    # The spliced bytes bound the edit: a mutant whose replaced lines and site line miss the scope
                    # cannot be in scope, and is not written out (each one is a complete copy of the file).
                    first = data.count(b"\n", 0, mutant.start_byte) + 1
                    last = first + data.count(b"\n", mutant.start_byte, mutant.end_byte)
                    site = mutant.site_lineno
                    if not scope.overlaps(rel, first, last) and not scope.overlaps(rel, site, site):
                        continue
                    mutated = apply_mutant(source, mutant)
                    digest = hashlib.sha256(mutated.encode("utf-8")).hexdigest()[:10]
                    native = f"{kind}:{mutant.operator}@{mutant.start_byte}-{mutant.end_byte}:{digest}"
                    if "balamut_" in mutant.mutated:
                        emit(fh, rel, native, mutant.operator, None, status="unrepresentable_runtime_helper",
                             line=mutant.site_lineno, kind=kind)
                        continue
                    emit(fh, rel, native, mutant.operator, mutated, line=mutant.site_lineno, kind=kind)


if __name__ == "__main__":
    main()
