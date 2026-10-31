"""Content hashes behind the incremental verdict cache (balamut.core.hashing)."""

from __future__ import annotations

import ast

from balamut.core import hashing
from balamut.core.astcache import parse_module


def test_function_source_top_level_and_method() -> None:
    src = "def f(a):\n    return a + 1\nclass C:\n    def m(self):\n        return 2\n"
    assert hashing.function_source(src, "f") == "def f(a):\n    return a + 1"
    assert hashing.function_source(src, "C.m") == "def m(self):\n        return 2"
    assert hashing.function_source(src, "missing") is None
    assert hashing.function_source(src, "C.outer.<locals>.inner") is None


def test_function_source_nested_function_by_dotted_qualname() -> None:
    src = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    assert hashing.function_source(src, "outer.inner") == "def inner():\n        return 1"
    assert hashing.function_start_lineno(src, "outer.inner") == 2


def test_nested_sibling_defs_disambiguate_by_line() -> None:
    # Two same-named nested functions in one body share the qualname `outer.inner`; the site line
    # must survive the recursion so the right sibling is hashed (the drop loses it to the first).
    src = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    def inner():\n"  # 4
        "        return 2\n"
        "    return inner\n"
    )
    assert hashing.function_start_lineno(src, "outer.inner", lineno=4) == 4
    assert hashing.function_start_lineno(src, "outer.inner", lineno=2) == 2
    first = hashing.function_source(src, "outer.inner", lineno=2)
    second = hashing.function_source(src, "outer.inner", lineno=4)
    assert first is not None
    assert second is not None
    assert "return 1" in first
    assert "return 2" in second
    assert "return 2" not in first


def test_mutant_key_content_addressed() -> None:
    body_a = "def f():\n    return 1"
    body_b = "def f():\n    return 2"
    key1 = hashing.mutant_key("src/x.py", "f", body_a, "return:None", "return None", 1)
    key2 = hashing.mutant_key("src/x.py", "f", body_a, "return:None", "return None", 1)
    key3 = hashing.mutant_key("src/x.py", "f", body_b, "return:None", "return None", 1)
    assert key1 == key2
    assert key1 != key3


def test_mutant_key_distinguishes_the_module() -> None:
    # The same function text in two modules is two mutants: one key would give them one verdict row.
    body = "def f():\n    return 1"
    assert hashing.mutant_key("src/x.py", "f", body, "return:None", "return None", 1) != hashing.mutant_key(
        "src/copy.py", "f", body, "return:None", "return None", 1
    )


def test_mutant_key_distinguishes_operator_and_mutated() -> None:
    body = "def f(a, b):\n    return a < b"
    assert hashing.mutant_key("src/x.py", "f", body, "compare:Lt->LtE", "a <= b", 1) != hashing.mutant_key(
        "src/x.py", "f", body, "compare:Lt->Gt", "a > b", 1
    )


def test_mutant_key_distinguishes_rel_byte() -> None:
    body = "def f():\n    x = a + b\n    y = a + b"
    assert hashing.mutant_key("src/x.py", "f", body, "binop:Add->Sub", "a - b", 1) != hashing.mutant_key(
        "src/x.py", "f", body, "binop:Add->Sub", "a - b", 2
    )


def test_mutant_key_distinguishes_same_line_by_byte() -> None:
    # Two identical mutations on one line (`return (1, 1)`) differ by column; sharing a key would let one
    # verdict overwrite the other.
    body = "def f():\n    return (1, 1)"
    assert hashing.mutant_key("src/x.py", "f", body, "const:num:1->2", "2", 12) != hashing.mutant_key(
        "src/x.py", "f", body, "const:num:1->2", "2", 15
    )


def test_function_start_lineno() -> None:
    src = "x = 1\n\ndef f():\n    return 1\nclass C:\n    def m(self):\n        return 2\n"
    assert hashing.function_start_lineno(src, "f") == 3
    assert hashing.function_start_lineno(src, "C.m") == 6
    assert hashing.function_start_lineno(src, "missing") is None


_PROPERTY_SRC = (
    "class C:\n"
    "    @property\n"
    "    def x(self):\n"  # 3: getter
    "        return self._x + 1\n"
    "    @x.setter\n"
    "    def x(self, v):\n"  # 6: setter
    "        self._x = v * 2\n"
)


def test_function_source_disambiguates_property_getter_setter_by_line() -> None:
    # Getter and setter share the qualname `C.x`; without the site line the setter would be hashed by the
    # getter's body and an edit of the setter would never invalidate its cached verdicts.
    getter = hashing.function_source(_PROPERTY_SRC, "C.x", lineno=4)
    setter = hashing.function_source(_PROPERTY_SRC, "C.x", lineno=7)
    assert getter is not None
    assert setter is not None
    assert "self._x + 1" in getter
    assert "v * 2" not in getter
    assert "v * 2" in setter
    assert "self._x + 1" not in setter
    assert hashing.function_start_lineno(_PROPERTY_SRC, "C.x", lineno=4) == 3
    assert hashing.function_start_lineno(_PROPERTY_SRC, "C.x", lineno=7) == 6
    edited = _PROPERTY_SRC.replace("v * 2", "v * 3")
    assert hashing.function_hash(_PROPERTY_SRC, "C.x", lineno=7) != hashing.function_hash(edited, "C.x", lineno=7)
    assert hashing.function_hash(_PROPERTY_SRC, "C.x", lineno=4) == hashing.function_hash(edited, "C.x", lineno=4)


def test_function_source_disambiguates_overload_impl_by_line() -> None:
    src = (
        "from typing import overload\n"
        "@overload\n"
        "def f(a: int) -> int: ...\n"
        "@overload\n"
        "def f(a: str) -> str: ...\n"
        "def f(a):\n"  # 6: implementation
        "    return a + 100\n"
    )
    impl = hashing.function_source(src, "f", lineno=7)
    assert impl is not None
    assert "a + 100" in impl
    assert not impl.strip().endswith("...")
    assert hashing.function_start_lineno(src, "f", lineno=7) == 6
    edited = src.replace("a + 100", "a + 200")
    assert hashing.function_hash(src, "f", lineno=7) != hashing.function_hash(edited, "f", lineno=7)


def test_function_source_disambiguates_when_site_on_def_line() -> None:
    # A default argument literal sits on the `def` line itself: the lower bound must be inclusive.
    src = (
        "class C:\n"
        "    @property\n"
        "    def x(self):\n"
        "        return self._x\n"
        "    @x.setter\n"
        "    def x(self, v=99):\n"  # 6
        "        self._x = v\n"
    )
    assert hashing.function_start_lineno(src, "C.x", lineno=6) == 6
    seg = hashing.function_source(src, "C.x", lineno=6)
    assert seg is not None
    assert "v=99" in seg


def test_function_source_no_lineno_keeps_first_match_behavior() -> None:
    assert hashing.function_source(_PROPERTY_SRC, "C.x") == hashing.function_source(_PROPERTY_SRC, "C.x", lineno=4)
    assert hashing.function_start_lineno(_PROPERTY_SRC, "C.x") == 3


def test_covering_hash_order_independent() -> None:
    assert hashing.covering_hash(["h1", "h2", "h3"]) == hashing.covering_hash(["h3", "h1", "h2"])
    assert hashing.covering_hash(["h1", "h2"]) != hashing.covering_hash(["h1", "h2", "h3"])


def test_line_shift_keeps_key_stable() -> None:
    src1 = "def f():\n    return price.amount"
    src2 = "# leading comment\n# another\ndef f():\n    return price.amount"
    body1 = hashing.function_source(src1, "f")
    body2 = hashing.function_source(src2, "f")
    assert body1 is not None
    assert body2 is not None
    assert body1 == body2
    assert hashing.mutant_key("src/x.py", "f", body1, "return:None", "return None", 1) == hashing.mutant_key(
        "src/x.py", "f", body2, "return:None", "return None", 1
    )


def test_parse_module_memoized_returns_same_object() -> None:
    src = "def f(a, b):\n    return a + b\n"
    assert parse_module(src) is parse_module(src)


def test_parse_module_distinct_sources_distinct_trees() -> None:
    assert parse_module("def a():\n    return 1\n") is not parse_module("def b():\n    return 2\n")


def test_parse_module_equivalent_to_ast_parse() -> None:
    src = "class C:\n    @property\n    def x(self):\n        return 1 + 2\n"
    assert ast.dump(parse_module(src)) == ast.dump(ast.parse(src))
