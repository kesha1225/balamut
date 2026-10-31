"""Mutation operators authored as ast-grep rules: a declarative alternative to hand-rolled AST enumerators.

An operator is an ast-grep ``Rule`` (the site finder: the matched node is the mutation target) plus a ``rewrite``
template whose single metavariables ``$VAR`` are filled from the match. The match is spliced byte-exactly, the
same way the hand-rolled operators splice.

These complement the AST enumerators rather than replace them: context-dependent operators (an awaited result
that is discarded, an assert inside a collectable test, a comparator that is a variable rather than a literal)
cannot be expressed as a plain pattern and stay on ``ast``. Rules cover the structurally simple ones: dropping a
link of a call chain or flipping a method.

Limits: ``rewrite`` supports single ``$VAR`` metavariables only (no ``$$$`` variadics in the output); a helper
name a rewrite introduces is listed in ``requires_globals`` so the runtime can inject it into the mutant's globals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ast_grep_py import Rule, SgNode, SgRoot

from balamut.core.mutants import Mutant
from balamut.operators.kit import Library, operator_name

_METAVAR_RE = re.compile(r"\$([A-Z][A-Z0-9_]*)")


@dataclass(frozen=True, slots=True)
class AstGrepOperator:
    name: str  # e.g. "sa:limit-drop"; the family comes from the emitting phase fact
    rule: Rule  # ast-grep site finder: the matched node is the target
    rewrite: str  # replacement template; single $VAR metavariables from the match
    requires_globals: tuple[str, ...] = ()  # helper names the rewrite introduces, injected into the mutant's globals


def _substitute(rewrite: str, node: SgNode) -> str:
    """Fill ``$VAR`` from the match's captured metavariables; an uncaptured name stays as written."""

    def repl(match: re.Match[str]) -> str:
        captured = node.get_match(match.group(1))
        return captured.text() if captured is not None else match.group(0)

    return _METAVAR_RE.sub(repl, rewrite)


def enumerate_ast_grep_operators(path: str, source: str, operators: tuple[AstGrepOperator, ...]) -> list[Mutant]:
    """Every mutant of ``operators`` in ``source``: an ast-grep match spliced with its ``rewrite``."""
    root = SgRoot(source, "python").root()
    mutants: list[Mutant] = []
    for operator in operators:
        for node in root.find_all({"rule": operator.rule}):
            span = node.range()
            # ast-grep reports character (code point) offsets, while mutants splice UTF-8 bytes like
            # ast.col_offset: convert through the prefix, or a splice would land inside a multi-byte character.
            start = len(source[: span.start.index].encode("utf-8"))
            end = len(source[: span.end.index].encode("utf-8"))
            original = node.text()
            mutated = _substitute(operator.rewrite, node)
            if mutated == original:
                continue
            mutants.append(
                Mutant(
                    path,
                    span.start.line + 1,  # ast-grep lines are 0-based, mutant lines 1-based
                    start,
                    end,
                    original,
                    mutated,
                    operator.name,
                    site_end_lineno=span.end.line + 1,
                )
            )
    return mutants


# Query-contract operators (kind ``db``): drop pagination, ordering or a limit from a query chain, flip a sort
# direction or a conflict policy. A survivor means no covering test asserts the bounded / ordered contract.
DB_OPERATORS: tuple[AstGrepOperator, ...] = (
    AstGrepOperator(name=operator_name(Library.SA, "limit-drop"), rule={"pattern": "$C.limit($$$A)"}, rewrite="$C"),
    AstGrepOperator(
        name=operator_name(Library.SA, "order-by-drop"), rule={"pattern": "$C.order_by($$$A)"}, rewrite="$C"
    ),
    AstGrepOperator(name=operator_name(Library.SA, "asc-to-desc"), rule={"pattern": "$C.asc()"}, rewrite="$C.desc()"),
    AstGrepOperator(name=operator_name(Library.SA, "desc-to-asc"), rule={"pattern": "$C.desc()"}, rewrite="$C.asc()"),
    AstGrepOperator(name=operator_name(Library.SA, "distinct-drop"), rule={"pattern": "$C.distinct()"}, rewrite="$C"),
    AstGrepOperator(
        name=operator_name(Library.SA, "on-conflict-update-to-nothing"),
        rule={"pattern": "$C.on_conflict_do_update($$$A)"},
        rewrite="$C.on_conflict_do_nothing()",
    ),
)


# Chain-contract operators (kind ``chain``): drop one link of a method chain as a whole. A survivor means the
# chain's semantics (timezone awareness, filter scope) is not asserted by a covering test.
#
# Drops only. A method swap onto a method the receiver may not have (``.first()`` -> ``.last()``) is deliberately
# absent: on a result object without ``.last()`` the mutant crashes with AttributeError and is "killed" by the
# crash, not by a check of which row was chosen, inflating the score. ``db:order-by-drop`` covers ordering.
CHAIN_OPERATORS: tuple[AstGrepOperator, ...] = (
    # `.astimezone(tz)` dropped: a naive or local-zone datetime
    AstGrepOperator(
        name=operator_name(Library.STD, "datetime", "astimezone-drop"),
        rule={"pattern": "$X.astimezone($$$A)"},
        rewrite="$X",
    ),
    # `datetime.now(UTC)` -> `datetime.now()`: naive vs aware, off by a day near midnight
    AstGrepOperator(
        name=operator_name(Library.STD, "datetime", "now-tz-drop"), rule={"pattern": "$D.now($TZ)"}, rewrite="$D.now()"
    ),
    # `.filter(...)` dropped from a query chain: a wider selection
    AstGrepOperator(name=operator_name(Library.SA, "filter-drop"), rule={"pattern": "$C.filter($$$A)"}, rewrite="$C"),
)
