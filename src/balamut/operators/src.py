"""The ``src`` kind: kit compositions over the decision-logic mutation tables.

Every fact is a ``Rewrite``/``Replace`` composition; the swap and drop tables are plain data
passed into the kit's site finders. The kind's own concerns are the non-body exclusion (a
``body_site()`` matcher on every fact) and the default-argument phase (the only site whose
covering line is the callers' body line, via the general ``emit(lineno=)`` parameter).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from balamut.core.mutants import ApplyMechanism, Mutant
from balamut.core.triage import Bucket
from balamut.operators.facts import EnumContext, OperatorKind, enumerate_facts
from balamut.operators.kit import (
    AugAssignSwapSite,
    BinopSwapSite,
    BoolopSwapSite,
    CalleeDropSite,
    CalleeSwapSite,
    CompareSwapSite,
    DropEachArgument,
    DropEachKeyword,
    FlipBoolSite,
    Library,
    Matcher,
    MethodDropSite,
    MethodSwapSite,
    NumberShiftSite,
    Replace,
    Rewrite,
    SwapAdjacentArgs,
    UnaryOperand,
    const_of,
    context,
    expr_value,
    node_is,
    operator_name,
    return_has_value,
    unary_op,
)

FAMILY = "src"


@dataclass(frozen=True, slots=True)
class BodySite(Matcher):
    """The node is inside a function body — not a default, annotation or decorator header
    (``ctx.non_body`` carries those)."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        return id(node) not in ctx.non_body


def body_site() -> BodySite:
    return BodySite()


@dataclass(frozen=True, slots=True)
class SrcRewrite(Rewrite):
    """A src walk fact with its declared triage bucket (a fixed priority level, as data)."""

    triage: Bucket = Bucket.REVIEW

    def bucket(self, mutant: Mutant) -> Bucket:
        return self.triage


@dataclass(frozen=True, slots=True)
class SrcReplace(Replace):
    """A src replacement fact with its declared triage bucket (a fixed priority level, as data)."""

    triage: Bucket = Bucket.REVIEW

    def bucket(self, mutant: Mutant) -> Bucket:
        return self.triage


@dataclass(frozen=True, slots=True)
class DefaultPosition(Matcher):
    """A literal positional default of a reachable (non-nested) function."""

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        if not isinstance(node, ast.Constant):
            return False
        args = ctx.parent_of(node)
        if not isinstance(args, ast.arguments):
            return False
        if not any(default is node for default in args.defaults):
            return False
        func = ctx.parent_of(args)
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            return False  # a lambda's defaults are not reachable either
        ancestor = ctx.parent_of(func)
        while ancestor is not None:
            if isinstance(ancestor, ast.FunctionDef | ast.AsyncFunctionDef):
                return False  # a nested function's __defaults__ cannot be reached
            ancestor = ctx.parent_of(ancestor)
        return True


def default_position() -> DefaultPosition:
    return DefaultPosition()


def _zero_numeric(node: ast.expr) -> bool:
    """A numeric literal equal to 0 (bool excluded: True == 1 in Python)."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
        and node.value == 0
    )


@dataclass(frozen=True, slots=True)
class SwappableBinop(Matcher):
    """A BinOp/AugAssign whose operator is in the table and whose swap changes semantics
    (``x + 0`` and ``x - 0`` are the same value and type)."""

    swaps: tuple[tuple[type[ast.operator], type[ast.operator]], ...]

    def matches(self, node: ast.AST, ctx: EnumContext) -> bool:
        match node:
            case ast.BinOp(op=op, right=right) | ast.AugAssign(op=op, value=right):
                if any(type(op) is from_ for from_, _to in self.swaps) and not _zero_numeric(right):
                    return True
            case _:
                pass
        return False


_COMPARE_SWAP: tuple[tuple[type[ast.cmpop], type[ast.cmpop]], ...] = (
    (ast.Lt, ast.LtE),
    (ast.LtE, ast.Lt),
    (ast.Gt, ast.GtE),
    (ast.GtE, ast.Gt),
    (ast.Eq, ast.NotEq),
    (ast.NotEq, ast.Eq),
    (ast.Is, ast.IsNot),
    (ast.IsNot, ast.Is),
    (ast.In, ast.NotIn),
    (ast.NotIn, ast.In),
)
_BINOP_SWAP: tuple[tuple[type[ast.operator], type[ast.operator]], ...] = (
    (ast.Add, ast.Sub),
    (ast.Sub, ast.Add),
    (ast.Mult, ast.Div),
    (ast.Div, ast.Mult),
    (ast.Mod, ast.Mult),
    (ast.Pow, ast.Mult),
    (ast.FloorDiv, ast.Div),
    # Bitwise and shift operators: flags, masks and permissions.
    (ast.BitAnd, ast.BitOr),
    (ast.BitOr, ast.BitAnd),
    (ast.BitXor, ast.BitAnd),
    (ast.LShift, ast.RShift),
    (ast.RShift, ast.LShift),
)
_BOOLOP_SWAP: tuple[tuple[type[ast.boolop], type[ast.boolop]], ...] = (
    (ast.And, ast.Or),
    (ast.Or, ast.And),
)
# Builtin callee swaps: direction of clamps/quantifiers. A new swap is one table row.
_BUILTIN_SWAP: tuple[tuple[str, str], ...] = (("min", "max"), ("max", "min"), ("all", "any"), ("any", "all"))
# Method swaps: only known directional pairs; the receiver type is not checked.
_METHOD_SWAP: tuple[tuple[str, str], ...] = (
    ("startswith", "endswith"),
    ("endswith", "startswith"),
    ("lstrip", "rstrip"),
    ("rstrip", "lstrip"),
    ("find", "rfind"),
    ("rfind", "find"),
    # `index -> rindex` is excluded: lists and tuples have `.index()` but no `.rindex()`, so the mutant would
    # only "die" of AttributeError. `rindex -> index` is safe: `.rindex()` exists only where `.index()` does.
    ("rindex", "index"),
)
# Normalizer drops: the call returns its own operand rounded, so the operand stands in for it.
_BUILTIN_DROP = frozenset({"round"})
_METHOD_DROP = frozenset({"quantize"})


src_kind: OperatorKind = (
    SrcRewrite(
        match=body_site() & node_is(ast.Compare),
        transform=CompareSwapSite(_COMPARE_SWAP),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.BinOp) & SwappableBinop(_BINOP_SWAP),
        transform=BinopSwapSite(_BINOP_SWAP),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.BoolOp),
        transform=BoolopSwapSite(_BOOLOP_SWAP),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.AugAssign) & SwappableBinop(_BINOP_SWAP),
        transform=AugAssignSwapSite(_BINOP_SWAP),
    ),
    SrcRewrite(
        match=body_site() & unary_op(ast.Not),
        transform=UnaryOperand(),
        operator=operator_name(Library.LANG, "unary", "not-removal"),
    ),
    SrcReplace(
        match=body_site() & node_is(ast.Break),
        text="continue",
        operator=operator_name(Library.LANG, "control", "break->continue"),
        triage=Bucket.HIGH,
    ),
    SrcReplace(
        match=body_site() & node_is(ast.Continue),
        text="break",
        operator=operator_name(Library.LANG, "control", "continue->break"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=CalleeSwapSite(_BUILTIN_SWAP, namespace=operator_name(Library.STD, "builtins")),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=MethodSwapSite(_METHOD_SWAP, namespace=operator_name(Library.STD, "str")),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=CalleeDropSite(_BUILTIN_DROP, namespace=operator_name(Library.STD, "builtins")),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=MethodDropSite(_METHOD_DROP, namespace=operator_name(Library.STD, "decimal")),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=SwapAdjacentArgs(),
        operator=operator_name(Library.LANG, "call", "arg-swap"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=DropEachArgument(),
        operator=operator_name(Library.LANG, "call", "arg-drop"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(
        match=body_site() & node_is(ast.Call),
        transform=DropEachKeyword(),
        operator=operator_name(Library.LANG, "call", "kwarg-drop"),
        triage=Bucket.HIGH,
    ),
    SrcRewrite(match=body_site() & (const_of(int) | const_of(float)), transform=NumberShiftSite(1)),
    SrcRewrite(match=body_site() & const_of(bool), transform=FlipBoolSite(), triage=Bucket.HIGH),
    SrcReplace(
        match=body_site() & return_has_value(),
        text="return None",
        operator=operator_name(Library.LANG, "return", "None"),
        triage=Bucket.HIGH,
    ),
    SrcReplace(
        match=body_site() & node_is(ast.Expr) & expr_value(node_is(ast.Call)),
        text="pass",
        operator=operator_name(Library.LANG, "delete", "call"),
        triage=Bucket.HIGH,
    ),
)


@dataclass(frozen=True, slots=True)
class DefaultsFact:
    """The src kind's trailing phase: literal positional defaults through ``__defaults__``.

    The covering line is the first executable body line (covering tests call the function, not the
    ``def`` line), which is exactly the general ``emit(lineno=)`` parameter.
    """

    def enumerate(self, path: str, source: str) -> list[Mutant]:
        tree = ast.parse(source)
        ctx = context(path, source, tree=tree)
        matcher = default_position()
        for func in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)):
            body_stmts = func.body[1:] if _is_docstring(func.body[0]) else func.body
            body_line = (body_stmts[0] if body_stmts else func.body[0]).lineno
            for index, default in enumerate(func.args.defaults):
                ctx.node = default
                if not matcher.matches(default, ctx):
                    continue
                for finder in (NumberShiftSite(1), FlipBoolSite()):
                    for target, text, _span, op, _bc in finder.find(default, ctx):
                        if op is None:
                            continue
                        ctx.emit(
                            target,
                            text,
                            op,
                            fact=self,
                            lineno=body_line,
                            site_lineno=default.lineno,
                            default_index=index,
                        )
        return ctx.mutants

    def mechanism(self) -> ApplyMechanism:
        return ApplyMechanism.DEFAULTS

    def bucket(self, mutant: Mutant) -> Bucket:
        return Bucket.HIGH  # a default value is part of the call contract


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def enumerate_mutants(path: str, source: str) -> list[Mutant]:
    """All syntactic (``src``) mutants of a module: the kit facts plus the defaults phase."""
    return enumerate_facts(path, source, ((*src_kind, DefaultsFact()),), family=FAMILY)


def enumerate_defaults(path: str, source: str) -> list[Mutant]:
    """Mutants of literal positional default values, applied through ``__defaults__``."""
    return enumerate_facts(path, source, ((DefaultsFact(),),), family=FAMILY)
