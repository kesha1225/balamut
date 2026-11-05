"""Bytecode mutation: edit the already-compiled code object of the target function surgically (one instruction
or one constant) instead of recompiling the whole module per mutant.

Only size-preserving edits are made, so ``co_code`` keeps its alignment and the jump table, exception table and
inline caches stay valid: the arg of ``BINARY_OP``/``COMPARE_OP``/``IS_OP``/``CONTAINS_OP``, a swap between
``LOAD_SMALL_INT``, ``LOAD_CONST`` and ``LOAD_COMMON_CONSTANT`` (all two bytes, no cache; ``True``/``False``/
``None``/``-1`` load through the last one since 3.15) or a new entry in ``co_consts``. Everything else
returns None and the caller falls back to ``compile_function_code``: structural operators, a constant inside a
foldable constant expression (CPython would fold it, so the bytecode would diverge from a recompile), a
``co_consts`` index of 256 or more (it needs ``EXTENDED_ARG``, a size change).

The guarantee is behavioural equivalence with a source recompile, stronger than a byte comparison (a recompile
itself drifts from the live bytecode). ``co_positions`` gives each instruction's column span, so the site is
matched exactly.
"""

from __future__ import annotations

import ast
import dis
import types
from ast import literal_eval
from collections.abc import Iterator
from functools import lru_cache
from typing import TypedDict

from balamut.core.mutants import BcEdit, BcEditKind

type _UnsafePositions = frozenset[tuple[int, int, int]]  # (lineno, col, end_col) of sites outside the edit path
type _Found = tuple[types.CodeType, int, int]  # (nested code, instruction byte offset, opcode)
type _Const = int | float | complex | str | bytes | bool | None


class BytecodeFields(TypedDict):
    """The bc-fields of a mutant spec: site position and the structured edit."""

    bc_kind: str
    bc_source: str
    bc_target: str
    bc_lineno: int
    bc_col: int
    bc_end_col: int
    bc_mutated: str


_SMALL_INT = dis.opmap["LOAD_SMALL_INT"]
_LOAD_CONST = dis.opmap["LOAD_CONST"]
_BINARY_OP = dis.opmap["BINARY_OP"]
_COMPARE_OP = dis.opmap["COMPARE_OP"]
_IS_OP = dis.opmap["IS_OP"]
_CONTAINS_OP = dis.opmap["CONTAINS_OP"]
_EXTENDED_ARG = dis.opmap["EXTENDED_ARG"]
# `LOAD_COMMON_CONSTANT` (3.15+): True/False/None/-1 no longer sit in co_consts but load with a fixed arg.
# Absent on 3.14, where they load through LOAD_CONST and the table below stays empty.
_COMMON_CONST = dis.opmap.get("LOAD_COMMON_CONSTANT")


def _common_const_args() -> tuple[tuple[_Const, int], ...]:
    """The ``(value, arg)`` of the constants this interpreter loads through ``LOAD_COMMON_CONSTANT``.

    Read from compiled probes, like the operator-arg tables: the opcode's arg assignment is an interpreter
    detail, never hardcoded. A probe whose constant loads any other way (3.14: LOAD_CONST) contributes nothing."""
    args: list[tuple[_Const, int]] = []
    for literal, value in (("True", True), ("False", False), ("None", None), ("-1", -1)):
        module_code = compile(f"def _probe(): return {literal}", "<bcmut>", "exec")
        fn_code = next(const for const in module_code.co_consts if isinstance(const, types.CodeType))
        load = next(
            (
                instr
                for instr in dis.get_instructions(fn_code)
                if instr.opcode in (_LOAD_CONST, _SMALL_INT, _COMMON_CONST)
            ),
            None,
        )
        if load is not None and load.arg is not None and _COMMON_CONST is not None and load.opcode == _COMMON_CONST:
            args.append((value, load.arg))
    return tuple(args)


_COMMON_CONST_ARGS = _common_const_args()
_CONST_LOAD_OPS = frozenset((_SMALL_INT, _LOAD_CONST) + ((_COMMON_CONST,) if _COMMON_CONST is not None else ()))
_COMPARE_OPS = frozenset({_COMPARE_OP, _IS_OP, _CONTAINS_OP})


def _op_arg(sym: str, opname: str) -> int:
    """The arg of ``opname`` in a trivial ``a <sym> b``, read from a compiled probe (never executed)."""
    module_code = compile(f"def _probe(a, b): return a {sym} b", "<bcmut>", "exec")
    fn_code = next(const for const in module_code.co_consts if isinstance(const, types.CodeType))
    arg = next(instr.arg for instr in dis.get_instructions(fn_code) if instr.opname == opname)
    if arg is None:
        raise RuntimeError(f"no arg for {opname} with {sym!r}")
    return arg


_BINOP_SYM = {
    "Add": "+",
    "Sub": "-",
    "Mult": "*",
    "Div": "/",
    "FloorDiv": "//",
    "Mod": "%",
    "Pow": "**",
    "LShift": "<<",
    "RShift": ">>",
    "BitOr": "|",
    "BitAnd": "&",
    "BitXor": "^",
    "MatMult": "@",
}
_CMP_SYM = {"Lt": "<", "LtE": "<=", "Gt": ">", "GtE": ">=", "Eq": "==", "NotEq": "!="}
_BINOP_ARG = {name: _op_arg(sym, "BINARY_OP") for name, sym in _BINOP_SYM.items()}
_COMPARE_ARG = {name: _op_arg(sym, "COMPARE_OP") for name, sym in _CMP_SYM.items()}
_IS_ARG = {"Is": 0, "IsNot": 1}
_CONTAINS_ARG = {"In": 0, "NotIn": 1}
# Precedence class of an operator: changing the class (Pow<->Mult, Mult<->Add, BitOr<->BitAnd, ...) can regroup
# the expression in a source splice (`base * 2 ** x` -> `base * 2 * x` is `(base * 2) * x`), while a bytecode
# edit keeps the AST grouping. Inside one class the grouping is the same.
_BINOP_PREC = {
    "Pow": 8,
    "Mult": 7,
    "Div": 7,
    "FloorDiv": 7,
    "Mod": 7,
    "MatMult": 7,
    "Add": 6,
    "Sub": 6,
    "LShift": 5,
    "RShift": 5,
    "BitAnd": 4,
    "BitXor": 3,
    "BitOr": 2,
}


def _is_const_expr(node: ast.expr) -> bool:
    """An expression CPython can fold into one constant."""
    match node:
        case ast.Constant():
            return True
        case ast.BinOp():
            return _is_const_expr(node.left) and _is_const_expr(node.right)
        case ast.UnaryOp():
            return _is_const_expr(node.operand)
        case ast.Tuple() | ast.List() | ast.Set():
            return all(_is_const_expr(elt) for elt in node.elts)
        case _:
            return False


@lru_cache(maxsize=256)
def unsafe_positions(source: str) -> _UnsafePositions:
    """Sites where a bytecode edit is not attempted (the caller recompiles instead):

    - a constant, binop or unary op inside a foldable constant expression;
    - a chained comparison ``a < b <= c`` (several COMPARE_OPs on overlapping spans)."""
    positions: set[tuple[int, int, int]] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.BinOp | ast.UnaryOp) and _is_const_expr(node):
            for sub in ast.walk(node):
                if isinstance(sub, ast.expr) and sub.end_col_offset is not None:
                    positions.add((sub.lineno, sub.col_offset, sub.end_col_offset))
        elif isinstance(node, ast.Compare) and len(node.ops) > 1 and node.end_col_offset is not None:
            positions.add((node.lineno, node.col_offset, node.end_col_offset))
    return frozenset(positions)


def bytecode_fields(source: str, *, edit: BcEdit, lineno: int, start_byte: int, end_byte: int) -> BytecodeFields | None:
    """The bc-fields for a bytecode-editable mutant, otherwise None (the plugin then recompiles).

    Conditions: the site is on one line and it is neither folded nor chained."""
    data = source.encode("utf-8")
    line_start = data.rfind(b"\n", 0, start_byte) + 1
    next_newline = data.find(b"\n", start_byte)
    if next_newline != -1 and end_byte > next_newline:
        return None  # a multi-line site: a line-relative column is meaningless
    col = start_byte - line_start
    end_col = end_byte - line_start
    if (lineno, col, end_col) in unsafe_positions(source):
        return None
    return {
        "bc_kind": edit.kind.value,
        "bc_source": edit.source,
        "bc_target": edit.target,
        "bc_lineno": lineno,
        "bc_col": col,
        "bc_end_col": end_col,
        "bc_mutated": edit.mutated,
    }


def _iter_codes(code: types.CodeType) -> Iterator[types.CodeType]:
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _iter_codes(const)


def _find(code: types.CodeType, lineno: int, col: int, end_col: int, opcodes: frozenset[int]) -> _Found | None:
    """The single instruction at the exact site position whose opcode is in ``opcodes``; None when there is no
    match or more than one.

    ``co_positions`` yields one position per two-byte word of ``co_code`` (cache words included; their opcode is
    never in ``opcodes``). An instruction with an ``EXTENDED_ARG`` prefix is left alone. Two instructions on one
    position are real: CPython duplicates an operation into both branches of a ternary, and editing one of them
    would diverge from compiling the mutated source (which changes both)."""
    found: _Found | None = None
    for sub in _iter_codes(code):
        co = sub.co_code
        for word, pos in enumerate(sub.co_positions()):
            offset = word * 2
            if co[offset] in opcodes and pos[0] == lineno and pos[2] == col and pos[3] == end_col:
                if offset >= 2 and co[offset - 2] == _EXTENDED_ARG:
                    return None
                if found is not None:
                    return None
                found = (sub, offset, co[offset])
    return found


def _set_op_arg(code: types.CodeType, offset: int, opcode: int, arg: int) -> types.CodeType:
    """Size-preserving: rewrite the opcode and arg bytes (arg < 256); the cache bytes after it are untouched."""
    raw = bytearray(code.co_code)
    raw[offset] = opcode
    raw[offset + 1] = arg
    return code.replace(co_code=bytes(raw))


def _common_arg(value: _Const) -> int | None:
    """The ``LOAD_COMMON_CONSTANT`` arg for ``value``; matched by exact type (``True``/``False``/``-1`` collide by
    ``==`` with ``1``/``0``/``-1.0``, and a ``0.0`` mutated by -1 yields the float)."""
    for val, arg in _COMMON_CONST_ARGS:
        if type(value) is type(val) and value == val:
            return arg
    return None


def _edit_const(code: types.CodeType, offset: int, value: _Const) -> types.CodeType | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 256:
        return _set_op_arg(code, offset, _SMALL_INT, value)
    common_arg = _common_arg(value)
    if common_arg is not None:
        assert _COMMON_CONST is not None  # the table is populated only when the opcode exists
        return _set_op_arg(code, offset, _COMMON_CONST, common_arg)
    # Always append a fresh constant instead of reusing one by `==`: `False == 0`, `True == 1` and `1 == 1.0` in
    # Python, so a lookup could load the wrong object. A redundant entry is harmless.
    index = len(code.co_consts)
    if index >= 256:
        return None
    return _set_op_arg(code.replace(co_consts=(*code.co_consts, value)), offset, _LOAD_CONST, index)


def _rebuild(code: types.CodeType, old: types.CodeType, new: types.CodeType) -> types.CodeType:
    """Replace a nested code object up the ``co_consts`` chain (a mutation inside a comprehension or lambda)."""
    if code is old:
        return new
    consts = tuple(
        _rebuild(const, old, new) if isinstance(const, types.CodeType) else const for const in code.co_consts
    )
    return code if consts == code.co_consts else code.replace(co_consts=consts)


def _compare_arg(opcode: int, target: str) -> int | None:
    if opcode == _COMPARE_OP:
        return _COMPARE_ARG.get(target)
    if opcode == _IS_OP:
        return _IS_ARG.get(target)
    if opcode == _CONTAINS_OP:
        return _CONTAINS_ARG.get(target)
    return None


def _code_valid(code: types.CodeType) -> bool:
    """Structural scan of every nested code object.

    Hand-edited bytecode that a recompile would never produce (an out-of-range const/name index, an unknown
    opcode, a corrupted argument) must be rejected here, not executed: the interpreter has no guard for it and
    segfaults in the evaluator."""
    try:
        for sub in _iter_codes(code):
            dis.Bytecode(sub).dis()
    except ValueError, IndexError, TypeError, KeyError:
        return False
    return True


def _plain_instructions(code: types.CodeType) -> list[dis.Instruction]:
    """The instruction stream without cache entries (opcode 0): they carry no semantics and dis does not
    validate them, so a corrupted write into one must be caught by comparing the real stream. Instrumented
    variants never reach this module: it edits freshly fetched, uninstrumented code objects."""
    return [instruction for instruction in dis.get_instructions(code) if instruction.opcode != 0]


def _surgical_diff(original: types.CodeType, edited: types.CodeType) -> tuple[int, int, int] | None:
    """Exactly one instruction may differ between the original and the edited code, as (position, old, new).

    An edit that shifted its offset onto a cache word or another instruction is not surgical: the evaluator reads
    the corrupted stream as valid bytecode and segfaults. ``dis`` cannot see that corruption; only the
    before/after comparison can."""
    if len(original.co_code) != len(edited.co_code):
        return None
    original_stream = _plain_instructions(original)
    edited_stream = _plain_instructions(edited)
    if len(original_stream) != len(edited_stream):
        return None
    diffs = [
        (position, before.opcode, after.opcode)
        for position, (before, after) in enumerate(zip(original_stream, edited_stream, strict=True))
        if (before.opcode, before.arg) != (after.opcode, after.arg)
    ]
    return diffs[0] if len(diffs) == 1 else None


def mutate_code(code: types.CodeType, *, lineno: int, col: int, end_col: int, edit: BcEdit) -> types.CodeType | None:
    """Mutate a function's code object by (position, edit). None means the edit is not size-preserving and
    the caller must recompile ``mutated_source``. ``col``/``end_col`` are byte columns, as in ``co_positions``."""
    result: types.CodeType | None = None
    match edit.kind:
        case BcEditKind.CONST:
            try:
                value = literal_eval(edit.mutated)
            except ValueError, SyntaxError:
                return None
            found = _find(code, lineno, col, end_col, _CONST_LOAD_OPS)
            if found is None:
                return None
            sub, offset, _opcode = found
            edited = _edit_const(sub, offset, value)
            result = None if edited is None else _rebuild(code, sub, edited)
            target = _CONST_LOAD_OPS
        case BcEditKind.BINOP:
            arg = _BINOP_ARG.get(edit.target)
            if _BINOP_PREC.get(edit.source) != _BINOP_PREC.get(edit.target):
                return None
            found = _find(code, lineno, col, end_col, frozenset({_BINARY_OP}))
            if arg is None or found is None:
                return None
            sub, offset, opcode = found
            result = _rebuild(code, sub, _set_op_arg(sub, offset, opcode, arg))
            target = _BINARY_OP
        case BcEditKind.COMPARE:
            found = _find(code, lineno, col, end_col, _COMPARE_OPS)
            if found is None:
                return None
            sub, offset, opcode = found
            arg = _compare_arg(opcode, edit.target)
            result = None if arg is None else _rebuild(code, sub, _set_op_arg(sub, offset, opcode, arg))
            target = opcode
        case _:
            return None
    if result is None or not _code_valid(result):
        return None
    diff = _surgical_diff(code, result)
    if diff is None:
        return None
    valid_target = diff[2] in target if isinstance(target, frozenset) else diff[2] == target
    return result if valid_target else None
