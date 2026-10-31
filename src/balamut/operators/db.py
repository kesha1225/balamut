"""Query operators of the ``db`` and ``chain`` kinds.

The AST operators are kit facts: one predicate of a ``where``/``filter``, one key of a
``group_by``/``distinct`` (``DropEachArgument`` with a per-method matcher), one element of a
dedup/grouping key tuple passed by keyword (the domain ``DedupKeySite``). The ast-grep
query-chain rules (``ast_grep``) are capability values of their own; their engine does its own
matching pass, so they run before the fold. Splices are UTF-8 byte-exact.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

from balamut.core.mutants import ApplyMechanism, BcEdit, Mutant
from balamut.core.source_spans import node_byte_span
from balamut.core.triage import Bucket
from balamut.operators.ast_grep import CHAIN_OPERATORS, DB_OPERATORS, enumerate_ast_grep_operators
from balamut.operators.facts import EnumContext, OperatorKind, enumerate_facts
from balamut.operators.kit import DropEachArgument, Library, Matcher, Rewrite, SiteFinder, node_is, operator_name

# Keyword names whose tuple value is a dedup or grouping key; compared case-insensitively.
DEFAULT_KEY_KEYWORDS = frozenset(
    {
        "dedup_key",
        "dedup_keys",
        "group_key",
        "group_keys",
        "grouping_key",
        "grouping_keys",
    }
)


@dataclass(frozen=True, slots=True)
class _MethodCall(Matcher):
    """A call whose callee attribute is exactly ``attr`` (the shape the per-method drop sites share)."""

    attr: str

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == self.attr


@dataclass(frozen=True, slots=True)
class DbRewrite(Rewrite):
    """A db walk fact: query scope / order / keys / conflict policy — all HIGH, as data."""

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.HIGH


@dataclass(frozen=True, slots=True)
class DedupKeySite(SiteFinder):
    """Drops one element of a dedup/grouping key tuple passed by a configured keyword.

    A pair keeps one element but must stay a tuple: the whole tuple is rewritten as ``(kept,)``."""

    key_keywords: frozenset[str] = DEFAULT_KEY_KEYWORDS
    _lower: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_lower", frozenset(name.lower() for name in self.key_keywords))

    def find(
        self, node: ast.AST, ctx: EnumContext
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if not isinstance(node, ast.Call):
            return
        for keyword in node.keywords:
            if keyword.arg is not None and keyword.arg.lower() in self._lower and isinstance(keyword.value, ast.Tuple):
                yield from self._drop_tuple_elements(ctx, keyword.value)

    def _drop_tuple_elements(
        self, ctx: EnumContext, value: ast.Tuple
    ) -> Iterator[tuple[ast.expr | ast.stmt, str, tuple[int, int] | None, str | None, BcEdit | None]]:
        if len(value.elts) < 2:
            return
        tuple_span = node_byte_span(value, ctx.line_starts)
        element_spans = [node_byte_span(element, ctx.line_starts) for element in value.elts]
        concrete = [span for span in element_spans if span is not None]
        if tuple_span is None or len(concrete) != len(element_spans):
            return
        for index, element in enumerate(value.elts):
            if len(value.elts) == 2:
                # A pair keeps one element; it must stay a tuple, so the whole tuple is rewritten as `(kept,)`.
                remaining_start, remaining_end = concrete[1 - index]
                remaining = ctx.source_bytes[remaining_start:remaining_end].decode("utf-8")
                yield value, f"({remaining},)", tuple_span, operator_name(Library.SA, "dedup-key-drop"), None
                continue
            if index < len(concrete) - 1:
                start, end = concrete[index][0], concrete[index + 1][0]
            else:
                start, end = concrete[index - 1][1], concrete[index][1]
            yield element, "", (start, end), operator_name(Library.SA, "dedup-key-drop"), None


def db_ast_kind(key_keywords: frozenset[str] = DEFAULT_KEY_KEYWORDS) -> OperatorKind:
    """The walk-shaped half of the db kind, in emission order."""
    return (
        DbRewrite(
            match=_MethodCall("where"),
            transform=DropEachArgument(),
            operator=operator_name(Library.SA, "where-predicate-drop"),
        ),
        DbRewrite(
            match=_MethodCall("filter"),
            transform=DropEachArgument(),
            operator=operator_name(Library.SA, "filter-predicate-drop"),
        ),
        DbRewrite(
            match=_MethodCall("group_by"),
            transform=DropEachArgument(),
            operator=operator_name(Library.SA, "group-by-key-drop"),
        ),
        DbRewrite(
            match=_MethodCall("distinct"),
            transform=DropEachArgument(),
            operator=operator_name(Library.SA, "distinct-key-drop"),
        ),
        DbRewrite(
            match=node_is(ast.Call),
            transform=DedupKeySite(key_keywords),
        ),
    )


def enumerate_db_ast_operators(
    path: str, source: str, *, key_keywords: frozenset[str] = DEFAULT_KEY_KEYWORDS
) -> list[Mutant]:
    """Per-predicate and per-key DB mutants; ``key_keywords`` names the keyword arguments holding key tuples."""
    return enumerate_facts(path, source, (db_ast_kind(key_keywords),), family="db")


@dataclass(frozen=True, slots=True)
class DbRulesFact:
    """The db kind's leading phase: the ast-grep query-chain rules (their own matching pass)."""

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.HIGH

    def enumerate(self, path: str, source: str) -> list[Mutant]:
        mutants = enumerate_ast_grep_operators(path, source, DB_OPERATORS)
        return [replace(mutant, fact=self) for mutant in mutants]

    def mechanism(self) -> ApplyMechanism:
        return ApplyMechanism.CODE


@dataclass(frozen=True, slots=True)
class ChainRulesFact:
    """The chain kind: timezone and filter chain-contract rules (their own matching pass)."""

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.HIGH

    def enumerate(self, path: str, source: str) -> list[Mutant]:
        mutants = enumerate_ast_grep_operators(path, source, CHAIN_OPERATORS)
        return [replace(mutant, fact=self) for mutant in mutants]

    def mechanism(self) -> ApplyMechanism:
        return ApplyMechanism.CODE


def db_kind_for(key_keywords: frozenset[str] = DEFAULT_KEY_KEYWORDS) -> OperatorKind:
    """The db kind: the ast-grep query-chain rules first, then the per-predicate/per-key facts."""
    return (DbRulesFact(), *db_ast_kind(key_keywords))


db_kind: OperatorKind = db_kind_for()

chain_kind: OperatorKind = (ChainRulesFact(),)


def enumerate_db(path: str, source: str, *, key_keywords: frozenset[str] = DEFAULT_KEY_KEYWORDS) -> list[Mutant]:
    """The ``db`` kind: query-chain rules plus per-predicate / per-key AST operators."""
    return enumerate_facts(path, source, (db_kind_for(key_keywords),), family="db")


def enumerate_chain(path: str, source: str) -> list[Mutant]:
    """The ``chain`` kind: timezone and filter chain-contract rules."""
    return enumerate_facts(path, source, (chain_kind,), family="chain")
