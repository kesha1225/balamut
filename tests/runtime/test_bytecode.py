"""Surgical bytecode mutation (balamut.runtime.bytecode.mutate_code).

The key invariant is behavioural equivalence: running the bytecode-edited mutant equals running the source
recompiled mutant on executable functions. This is stronger than a byte comparison, because a recompile
itself drifts from the live bytecode, and the live bytecode is what gets patched. Plus: exactly one
instruction changes and ``co_code`` keeps its length; unsafe cases fall back (``None``) to a recompile.
"""

from __future__ import annotations

import ast
import builtins
import dis
import importlib
import types
from collections.abc import Callable, Sequence

import pytest

from balamut.core.mutants import BcEdit, BcEditKind, Mutant, apply_mutant
from balamut.operators.src import enumerate_mutants
from balamut.runtime import bytecode
from balamut.runtime.bytecode import (
    _BINARY_OP,
    _BINOP_PREC,
    _CONTAINS_ARG,
    _IS_ARG,
    _LOAD_CONST,
    _SMALL_INT,
    BytecodeFields,
    _code_valid,
    _Const,
    _edit_const,
    _find,
    _is_const_expr,
    _op_arg,
    _plain_instructions,
    _rebuild,
    _set_op_arg,
    _surgical_diff,
    bytecode_fields,
    mutate_code,
    unsafe_positions,
)
from balamut.runtime.inmemory import compile_function_code

_GLOBALS = {"__builtins__": builtins}
type _Outcome = tuple[str, object]


def _col_span(source: str, mutant: Mutant) -> tuple[int, int]:
    data = source.encode("utf-8")
    line_start = data.rfind(b"\n", 0, mutant.start_byte) + 1
    return mutant.start_byte - line_start, mutant.end_byte - line_start


def _first_mutant(source: str, predicate: Callable[[str], bool]) -> Mutant:
    for mutant in enumerate_mutants("t.py", source):
        if predicate(mutant.operator):
            return mutant
    raise AssertionError(f"no mutant matches the predicate in {source!r}")


def _edited_and_recompiled(
    source: str, qualname: str, predicate: Callable[[str], bool]
) -> tuple[Mutant, types.CodeType, types.CodeType | None, types.CodeType]:
    base = compile_function_code(source, qualname, "t.py")
    assert base is not None
    mutant = _first_mutant(source, predicate)
    assert mutant.bc is not None, mutant.operator
    col, end_col = _col_span(source, mutant)
    edited = mutate_code(base, lineno=mutant.lineno, col=col, end_col=end_col, edit=mutant.bc)
    recompiled = compile_function_code(apply_mutant(source, mutant), qualname, "t.py")
    assert recompiled is not None
    return mutant, base, edited, recompiled


def _run(code: types.CodeType, *args: object) -> _Outcome:
    fn = types.FunctionType(code, _GLOBALS)
    try:
        return ("v", fn(*args))
    except (ArithmeticError, TypeError, ValueError, IndexError, KeyError, AttributeError) as exc:
        return ("e", type(exc).__name__)


def _assert_behaves_like_recompile(
    source: str, qualname: str, predicate: Callable[[str], bool], arg_sets: Sequence[tuple[object, ...]]
) -> None:
    mutant, _base, edited, recompiled = _edited_and_recompiled(source, qualname, predicate)
    assert edited is not None, ("expected a bytecode edit, got the fallback", mutant.operator)
    for args in arg_sets:
        assert _run(edited, *args) == _run(recompiled, *args), (mutant.operator, args)


def test_const_num_behaves_like_recompile() -> None:
    _assert_behaves_like_recompile(
        "def f(a):\n    return a + 10\n", "f", lambda op: op.startswith("lang:const:num"), [(-3,), (0,), (100,)]
    )


def test_large_const_behaves_like_recompile() -> None:
    _assert_behaves_like_recompile(
        "def f(a):\n    return a + 1000\n", "f", lambda op: op.startswith("lang:const:num"), [(-3,), (0,)]
    )


def test_const_bool_behaves_like_recompile() -> None:
    _assert_behaves_like_recompile(
        "def f(a):\n    return a and True\n", "f", lambda op: op.startswith("lang:const:bool"), [(True,), (0,)]
    )


def test_binop_same_precedence_behaves_like_recompile() -> None:
    _assert_behaves_like_recompile(
        "def f(a, b):\n    return a + b\n", "f", lambda op: op.startswith("lang:binop:Add"), [(3, 5), (0, 0), (-2, 7)]
    )


def test_compare_ordering_behaves_like_recompile() -> None:
    _assert_behaves_like_recompile(
        "def f(a, b):\n    return a < b\n", "f", lambda op: op.startswith("lang:compare:Lt"), [(1, 2), (2, 1), (3, 3)]
    )


def test_compare_is_behaves_like_recompile() -> None:
    _assert_behaves_like_recompile(
        "def f(a):\n    return a is None\n", "f", lambda op: op.startswith("lang:compare:Is"), [(None,), (5,)]
    )


def test_compare_in_behaves_like_recompile() -> None:
    _assert_behaves_like_recompile(
        "def f(a):\n    return a in (1, 2, 3)\n", "f", lambda op: op.startswith("lang:compare:In"), [(2,), (9,)]
    )


def test_nested_comprehension_const_behaves_like_recompile() -> None:
    _assert_behaves_like_recompile(
        "def f(items):\n    return [x + 1 for x in items]\n",
        "f",
        lambda op: op.startswith("lang:const:num"),
        [([1, 2, 3],), ([],)],
    )


def test_edit_is_size_preserving_and_localized() -> None:
    _mutant, base, edited, _rec = _edited_and_recompiled(
        "def f(a):\n    return a + 10\n", "f", lambda op: op.startswith("lang:const:num")
    )
    assert edited is not None
    assert len(edited.co_code) == len(base.co_code)
    diffs = [i for i, (x, y) in enumerate(zip(base.co_code, edited.co_code, strict=True)) if x != y]
    assert len(diffs) <= 2, ("more than one instruction changed", diffs)  # opcode + arg
    assert len(edited.co_consts) - len(base.co_consts) in (0, 1)


def test_cross_precedence_binop_falls_back() -> None:
    # `**` -> `*` regroups the source splice (`a * 2 * x` is `(a * 2) * x`), the bytecode edit would not.
    source = "def f(a, x):\n    return a * 2 ** x\n"
    base = compile_function_code(source, "f", "t.py")
    assert base is not None
    mutant = _first_mutant(source, lambda op: op.startswith("lang:binop:Pow->Mult"))
    assert mutant.bc is not None
    col, end_col = _col_span(source, mutant)
    assert mutate_code(base, lineno=mutant.lineno, col=col, end_col=end_col, edit=mutant.bc) is None


def test_ternary_branch_duplicated_binop_falls_back() -> None:
    # CPython duplicates `+ x` into both ternary branches: two BINARY_OPs on one span. Editing one of them would
    # diverge from compiling the mutated source, which changes both.
    source = "def f(flag, x):\n    return (1 if flag else 2) + x\n"
    base = compile_function_code(source, "f", "t.py")
    assert base is not None
    mutant = _first_mutant(source, lambda op: op.startswith("lang:binop:Add"))
    assert mutant.bc is not None
    col, end_col = _col_span(source, mutant)
    assert mutate_code(base, lineno=mutant.lineno, col=col, end_col=end_col, edit=mutant.bc) is None


def test_malformed_bc_edit_falls_back_not_crash() -> None:
    base = compile_function_code("def f(a, b):\n    return a + b\n", "f", "t.py")
    assert base is not None
    for edit in (
        BcEdit(BcEditKind.CONST, "", "", "not a literal"),
        BcEdit(BcEditKind.BINOP, "Add", "NotAnOp", ""),
        BcEdit(BcEditKind.COMPARE, "Lt", "NotAnOp", ""),
    ):
        assert mutate_code(base, lineno=2, col=11, end_col=16, edit=edit) is None, edit


def test_unsafe_positions_detects_folding() -> None:
    assert unsafe_positions("def f():\n    return 1 / 0\n")


def test_unsafe_positions_detects_chained_compare() -> None:
    assert unsafe_positions("def f(a, b, c):\n    return a < b < c\n")


def test_unsafe_positions_empty_for_plain_expr() -> None:
    assert not unsafe_positions("def f(a, b):\n    return a + b < 5\n")


def _fields(source: str, predicate: Callable[[str], bool]) -> tuple[BytecodeFields | None, Mutant]:
    mutant = _first_mutant(source, predicate)
    if mutant.bc is None:
        return None, mutant
    fields = bytecode_fields(
        source,
        edit=mutant.bc,
        lineno=mutant.lineno,
        start_byte=mutant.start_byte,
        end_byte=mutant.end_byte,
    )
    return fields, mutant


def test_bytecode_fields_const_gives_exact_col_span() -> None:
    source = "def f(a):\n    return a + 10\n"
    fields, mutant = _fields(source, lambda op: op.startswith("lang:const:num"))
    assert fields is not None
    assert fields["bc_kind"] == "const"
    assert fields["bc_mutated"] == mutant.mutated
    assert fields["bc_lineno"] == 2
    line = source.splitlines()[1]
    assert line[fields["bc_col"] : fields["bc_end_col"]] == "10"


def test_bytecode_fields_none_for_folding_const() -> None:
    fields, _ = _fields("def f():\n    return 1 + 2 + 3\n", lambda op: op.startswith("lang:const:num"))
    assert fields is None


def test_bytecode_fields_none_for_chained_compare() -> None:
    fields, _ = _fields("def f(a, b, c):\n    return a < b < c\n", lambda op: op.startswith("lang:compare"))
    assert fields is None


def test_bytecode_fields_none_for_structural_operator() -> None:
    fields, _ = _fields("def f(a):\n    return a\n", lambda op: op.startswith("lang:return"))
    assert fields is None


def test_const_type_alias_materializes_to_union_of_literal_types() -> None:
    value = _Const.__value__
    for sample in (1, 1.0, 1j, "x", b"x", True, None):
        assert isinstance(sample, value)


def test_op_arg_reads_the_probe_instruction_arg() -> None:
    assert isinstance(_op_arg("+", "BINARY_OP"), int)
    assert _op_arg("==", "COMPARE_OP") >= 0


def test_is_arg_table_maps_is_and_is_not() -> None:
    assert _IS_ARG == {"Is": 0, "IsNot": 1}


def test_contains_arg_table_maps_in_and_not_in() -> None:
    assert _CONTAINS_ARG == {"In": 0, "NotIn": 1}


def test_binop_precedence_classes_group_operators() -> None:
    assert _BINOP_PREC == {
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


def test_is_const_expr_recognizes_foldable_operators_and_literals() -> None:
    assert _is_const_expr(ast.parse("1 + 2", mode="eval").body) is True
    assert _is_const_expr(ast.parse("-1", mode="eval").body) is True
    assert _is_const_expr(ast.parse("(1, 2)", mode="eval").body) is True
    assert _is_const_expr(ast.parse("x", mode="eval").body) is False
    assert _is_const_expr(ast.parse("(1, x)", mode="eval").body) is False


def test_bytecode_fields_const_col_span_on_later_line() -> None:
    source = "def f(a):\n    b = a\n    return a + 10\n"
    fields, _mutant = _fields(source, lambda op: op.startswith("lang:const:num"))
    assert fields is not None
    line = source.splitlines()[2]
    assert line[fields["bc_col"] : fields["bc_end_col"]] == "10"


def test_bytecode_fields_col_span_respects_leading_newline() -> None:
    source = "\nx = 1\n"
    edit = BcEdit(BcEditKind.CONST, "", "", "2")
    start_byte = source.index("1")
    fields = bytecode_fields(source, edit=edit, lineno=2, start_byte=start_byte, end_byte=start_byte + 1)
    assert fields is not None
    line = source.splitlines()[1]
    assert line[fields["bc_col"] : fields["bc_end_col"]] == "1"


def test_bytecode_fields_none_for_multiline_site() -> None:
    source = "def f(a, z):\n    return (a +\n        z)\n\nx = 1\n"
    edit = BcEdit(BcEditKind.BINOP, "Add", "Sub", "a - z")
    plus = source.index("+")
    z = source.index("z", plus)
    assert bytecode_fields(source, edit=edit, lineno=2, start_byte=plus, end_byte=z + 1) is None


def test_bytecode_fields_single_line_site_at_end_of_file() -> None:
    source = "x = 1"
    edit = BcEdit(BcEditKind.CONST, "", "", "2")
    start_byte = source.index("1")
    fields = bytecode_fields(source, edit=edit, lineno=1, start_byte=start_byte, end_byte=start_byte + 1)
    assert fields is not None


def test_const_bool_mutation_preserves_bool_type() -> None:
    source = "def f():\n    return True\n"
    base = compile_function_code(source, "f", "t.py")
    assert base is not None
    mutant = _first_mutant(source, lambda op: op.startswith("lang:const:bool"))
    assert mutant.bc is not None
    col, end_col = _col_span(source, mutant)
    edited = mutate_code(base, lineno=mutant.lineno, col=col, end_col=end_col, edit=mutant.bc)
    assert edited is not None
    fn = types.FunctionType(edited, _GLOBALS)
    assert fn() is False


def _first_const_load_offset(code: types.CodeType, value: object) -> int:
    for instr in dis.get_instructions(code):
        if instr.opname in ("LOAD_SMALL_INT", "LOAD_CONST") and instr.argval == value:
            return instr.offset
    raise AssertionError(f"no const-load instruction for {value!r}")


def test_edit_const_zero_uses_small_int_opcode() -> None:
    def f() -> int:
        return 0

    code = f.__code__
    offset = _first_const_load_offset(code, 0)
    edited = _edit_const(code, offset, 0)
    assert edited is not None
    assert edited.co_code[offset] == _SMALL_INT
    assert len(edited.co_consts) == len(code.co_consts)


def test_edit_const_256_uses_load_const_opcode() -> None:
    def f() -> int:
        return 0

    code = f.__code__
    offset = _first_const_load_offset(code, 0)
    edited = _edit_const(code, offset, 256)
    assert edited is not None
    assert edited.co_code[offset] == _LOAD_CONST
    assert edited.co_consts[-1] == 256


def test_edit_const_minus_one_float_is_not_the_common_int() -> None:
    # `-1.0 == -1`, but the load must keep the float: a `0.0` mutated by a -1 delta yields -1.0, and on 3.15+
    # `-1` loads through LOAD_COMMON_CONSTANT, so an equality lookup would load the int.
    def f() -> int:
        return 0

    code = f.__code__
    offset = _first_const_load_offset(code, 0)
    edited = _edit_const(code, offset, -1.0)
    assert edited is not None
    assert _run(edited) == ("v", -1.0)
    assert isinstance(_run(edited)[1], float)


def test_edit_const_refuses_256th_const_index() -> None:
    def f() -> int:
        return 0

    base = f.__code__
    code = base.replace(co_consts=tuple(range(256)))
    assert _edit_const(code, 0, "x") is None


def test_rebuild_returns_identity_when_consts_unchanged() -> None:
    def f() -> int:
        return 0

    code = f.__code__
    other = compile("x = 1\n", "x.py", "exec")
    assert _rebuild(code, other, other) is code


def test_code_valid_rejects_malformed_code() -> None:
    def f() -> int:
        return 0

    base = f.__code__
    bad = base.replace(co_code=bytes([_LOAD_CONST, 200, dis.opmap["RETURN_VALUE"], 0]))
    assert _code_valid(bad) is False


def test_set_op_arg_writes_only_the_target_instruction() -> None:
    def f() -> int:
        return 0

    code = f.__code__
    offset = _first_const_load_offset(code, 0)
    edited = _set_op_arg(code, offset, _LOAD_CONST, 5)
    assert edited.co_code[offset] == _LOAD_CONST
    assert edited.co_code[offset + 1] == 5
    assert edited.co_code[:offset] == code.co_code[:offset]
    assert edited.co_code[offset + 2 :] == code.co_code[offset + 2 :]


def test_find_matches_the_start_line_of_a_multiline_instruction() -> None:
    # `a + b` continued over a backslash puts BINARY_OP on (start_line=2, end_line=3): `_find` keys on the
    # instruction's start line, not its end line.
    source = "def f(a, b):\n    return a + \\\n        b\n"
    code = compile_function_code(source, "f", "t.py")
    assert code is not None
    position = next(pos for word, pos in enumerate(code.co_positions()) if code.co_code[word * 2] == _BINARY_OP)
    lineno, _end_lineno, col, end_col = position
    assert lineno is not None
    assert col is not None
    assert end_col is not None
    found = _find(code, lineno=lineno, col=col, end_col=end_col, opcodes=frozenset({_BINARY_OP}))
    assert found is not None


def test_find_rejects_a_const_beyond_the_extended_arg_limit() -> None:
    lines = [f"    a{i} = 's{i}'" for i in range(257)]
    source = "def f():\n" + "\n".join(lines) + "\n    return a256\n"
    code = compile_function_code(source, "f", "t.py")
    assert code is not None
    offsets = [
        word * 2
        for word in range(len(code.co_code) // 2)
        if word * 2 >= 2
        and code.co_code[word * 2] == _LOAD_CONST
        and code.co_code[word * 2 - 2] == dis.opmap["EXTENDED_ARG"]
    ]
    assert len(offsets) == 1
    offset = offsets[0]
    pos = tuple(code.co_positions())[offset // 2]
    lineno, _end_lineno, col, end_col = pos
    assert lineno is not None
    assert col is not None
    assert end_col is not None
    found = _find(code, lineno=lineno, col=col, end_col=end_col, opcodes=frozenset({_LOAD_CONST, _SMALL_INT}))
    assert found is None


def test_plain_instructions_keeps_binary_slice() -> None:
    # A wrong `!= 1` would drop BINARY_SLICE (opcode 1), a real instruction, alongside the cache entries (opcode 0).
    def f(a: list[int], x: int, y: int) -> list[int]:
        return a[x:y]

    assert any(instruction.opname == "BINARY_SLICE" for instruction in _plain_instructions(f.__code__))


def test_op_arg_distinguishes_the_operator_from_the_prologue() -> None:
    # A wrong `!=` would match the RESUME prologue (arg 0) for every operator and lose the real opcode arg.
    assert _op_arg("+", "BINARY_OP") != _op_arg("-", "BINARY_OP")


def _false_validator(_code: types.CodeType) -> bool:
    return False


def _true_validator(_code: types.CodeType) -> bool:
    return True


def test_find_rejects_a_site_shadowed_by_the_next_extended_arg() -> None:
    # `offset - 2` reads the PREVIOUS word: a `Sub->Add` mutant would read the NEXT one, and on the last
    # instruction of the stream that read is out of range. RETURN_VALUE sits on the final word, so the mutant
    # raises instead of finding it.
    def f() -> int:
        return 0

    code = f.__code__
    position = next(
        pos for word, pos in enumerate(code.co_positions()) if code.co_code[word * 2] == dis.opmap["RETURN_VALUE"]
    )
    lineno, _end_lineno, col, end_col = position
    assert lineno is not None
    assert col is not None
    assert end_col is not None
    found = _find(code, lineno=lineno, col=col, end_col=end_col, opcodes=frozenset({dis.opmap["RETURN_VALUE"]}))
    assert found is not None


def test_mutate_code_drops_a_result_that_fails_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    # `result is not None and _code_valid(result)` — an `and->or` mutant would return the invalid edit instead of
    # the fallback.
    source = "def f(a):\n    return a + 10\n"
    base = compile_function_code(source, "f", "t.py")
    assert base is not None
    monkeypatch.setattr(bytecode, "_code_valid", _false_validator)
    mutant = next(m for m in enumerate_mutants("t.py", source) if m.operator.startswith("lang:const:num"))
    assert mutant.bc is not None
    data = source.encode("utf-8")
    line_start = data.rfind(b"\n", 0, mutant.start_byte) + 1
    col = mutant.start_byte - line_start
    end_col = mutant.end_byte - line_start
    assert mutate_code(base, lineno=mutant.lineno, col=col, end_col=end_col, edit=mutant.bc) is None


def test_mutate_code_returns_none_for_an_unapplyable_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    # `result is None or not _code_valid(result)` — an `or->and` mutant would fall through to the diff of a None
    # result and crash instead of returning the fallback.
    def f(a: int, b: int) -> int:
        return a + b

    base = f.__code__
    edit = BcEdit(BcEditKind.BINOP, "Add", "Pow", "a ** b")  # precedence mismatch: the match returns None
    monkeypatch.setattr(bytecode, "_code_valid", _true_validator)
    assert mutate_code(base, lineno=1, col=12, end_col=13, edit=edit) is None


def test_binop_and_compare_arg_tables_built_from_probes() -> None:
    # The module-level dict comprehensions build the op-arg tables from a compiled probe at import. Re-executing
    # the module body inside the test attributes those lines to this test and re-checks the contract: every
    # operator name maps to the arg of its probe instruction, and the tables cover every symbol.
    reloaded = importlib.reload(bytecode)
    assert set(reloaded._BINOP_ARG) == set(reloaded._BINOP_SYM)
    assert set(reloaded._COMPARE_ARG) == set(reloaded._CMP_SYM)
    for name, sym in reloaded._BINOP_SYM.items():
        assert reloaded._BINOP_ARG[name] == reloaded._op_arg(sym, "BINARY_OP")
    for name, sym in reloaded._CMP_SYM.items():
        assert reloaded._COMPARE_ARG[name] == reloaded._op_arg(sym, "COMPARE_OP")
    # The arg distinguishes the operator from the RESUME prologue (arg 0).
    assert reloaded._BINOP_ARG["Add"] != reloaded._BINOP_ARG["Sub"]


def test_rebuild_replaces_a_deeply_nested_code_object() -> None:
    # A nested code object is replaced in place, up the co_consts chain, not by rebuilding the whole consts tuple
    # with the wrong entry. Two levels of nesting exercise the recursive call (`_rebuild(const, old, new)`).
    def outer() -> int:
        def middle() -> int:
            def inner() -> int:
                return 1

            return inner()

        return middle()

    code = outer.__code__
    nested = next(c for c in code.co_consts if isinstance(c, types.CodeType) and c.co_name == "middle")
    deeper = next(c for c in nested.co_consts if isinstance(c, types.CodeType) and c.co_name == "inner")
    replacement = compile("x = 1\n", "<replacement>", "exec")
    rebuilt = _rebuild(code, deeper, replacement)
    assert rebuilt is not code
    rebuilt_middle = next(c for c in rebuilt.co_consts if isinstance(c, types.CodeType) and c.co_name == "middle")
    assert replacement in rebuilt_middle.co_consts
    assert deeper not in rebuilt_middle.co_consts


def test_surgical_diff_reports_new_opcode_after_original() -> None:
    # The diff tuple is (position, old opcode, new opcode): a swapped zip would report the original opcode as the
    # edited one. A const edit LOAD_SMALL_INT -> LOAD_CONST changes the opcode, so the order is observable.
    def f() -> int:
        return 0

    code = f.__code__
    offset = _first_const_load_offset(code, 0)
    edited = _edit_const(code, offset, 256)
    assert edited is not None
    diff = _surgical_diff(code, edited)
    assert diff is not None
    _position, before, after = diff
    assert before == _SMALL_INT
    assert after == _LOAD_CONST


def test_mutate_code_diffs_original_against_edited(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_surgical_diff(original, edited)` must receive the original first: the third element of its result is the
    # edited opcode, which mutate_code validates as the target.
    source = "def f(a):\n    return a + 10\n"
    base = compile_function_code(source, "f", "t.py")
    assert base is not None
    mutant = _first_mutant(source, lambda op: op.startswith("lang:const:num"))
    assert mutant.bc is not None
    col, end_col = _col_span(source, mutant)
    real_diff = bytecode._surgical_diff
    calls: list[tuple[types.CodeType, types.CodeType]] = []

    def spy(original: types.CodeType, edited: types.CodeType) -> tuple[int, int, int] | None:
        calls.append((original, edited))
        return real_diff(original, edited)

    monkeypatch.setattr(bytecode, "_surgical_diff", spy)
    edited = mutate_code(base, lineno=mutant.lineno, col=col, end_col=end_col, edit=mutant.bc)
    assert edited is not None
    assert len(calls) == 1
    assert calls[0][0] is base
    assert calls[0][1] is edited


def _const_num_predicate(op: str) -> bool:
    return op.startswith("lang:const:num")


def _binop_add_predicate(op: str) -> bool:
    return op.startswith("lang:binop:Add")


def _compare_lt_predicate(op: str) -> bool:
    return op.startswith("lang:compare:Lt")


@pytest.mark.parametrize(
    ("source", "predicate"),
    [
        ("def f(a):\n    return (lambda x: x + 1)(a)\n", _const_num_predicate),
        ("def f(a, b):\n    return (lambda x, y: x + y)(a, b)\n", _binop_add_predicate),
        ("def f(a, b):\n    return (lambda x, y: x < y)(a, b)\n", _compare_lt_predicate),
    ],
)
def test_mutate_code_rebuilds_outer_code_around_nested_edit(
    source: str, predicate: Callable[[str], bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lambda is a true nested code object (a comprehension is inlined on 3.14). `_rebuild` must be called on the
    # OUTER code with the nested lambda as the code object to replace: a swapped call rebuilds from the lambda and
    # leaves the outer code untouched.
    base = compile_function_code(source, "f", "t.py")
    assert base is not None
    mutant = _first_mutant(source, predicate)
    assert mutant.bc is not None
    col, end_col = _col_span(source, mutant)
    real_rebuild = bytecode._rebuild
    calls: list[tuple[types.CodeType, types.CodeType, types.CodeType]] = []

    def spy(code: types.CodeType, old: types.CodeType, new: types.CodeType) -> types.CodeType:
        calls.append((code, old, new))
        return real_rebuild(code, old, new)

    monkeypatch.setattr(bytecode, "_rebuild", spy)
    mutate_code(base, lineno=mutant.lineno, col=col, end_col=end_col, edit=mutant.bc)
    assert calls
    assert calls[0][0] is base
    assert calls[0][1].co_name == "<lambda>"
