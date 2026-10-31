"""In-memory mutation by swapping ``__code__`` (balamut.runtime.inmemory).

The essence: replacing a live function's ``__code__`` changes its behaviour, is visible through an alias
(the ``from M import f`` case) and is reverted, without touching any file.
"""

from __future__ import annotations

import ast
import types

from balamut.runtime import inmemory
from balamut.runtime.inmemory import (
    code_reachable,
    compile_function_code,
    find_enclosing_function,
    inject_yields,
    patch_code,
    resolve_function,
)


def test_inject_yields_wraps_target_awaits_only() -> None:
    src = "async def f():\n    async def g():\n        await inner()\n    x = await a()\n    return await b(x)\n"
    out = inject_yields(src, "f")
    compile(out, "t.py", "exec")
    assert "await balamut_fuzz_yield(a(), 1)" in out
    assert "await balamut_fuzz_yield(b(x), 2)" in out
    assert "await inner()" in out  # the nested g() is left alone
    assert "balamut_fuzz_yield(inner" not in out


def test_inject_yields_noop_when_qualname_absent() -> None:
    src = "async def f():\n    return await a()\n"
    assert inject_yields(src, "missing") == src


def _live(source: str, name: str) -> types.FunctionType:
    namespace: dict[str, object] = {}
    exec(compile(source, "m.py", "exec"), namespace)
    func = namespace[name]
    assert isinstance(func, types.FunctionType)
    return func


def test_patch_mutates_live_function_and_restores() -> None:
    add = _live("def add(a, b):\n    return a + b\n", "add")
    assert add(2, 3) == 5
    new_code = compile_function_code("def add(a, b):\n    return a - b\n", "add", "m.py")
    assert new_code is not None
    restore = patch_code(add, new_code)
    try:
        assert add(2, 3) == -1
    finally:
        restore()
    assert add(2, 3) == 5


def test_alias_reference_sees_mutation() -> None:
    func = _live("def f():\n    return 1\n", "f")
    alias = func
    new_code = compile_function_code("def f():\n    return 2\n", "f", "m.py")
    assert new_code is not None
    restore = patch_code(func, new_code)
    try:
        assert alias() == 2
    finally:
        restore()
    assert alias() == 1


def test_compile_function_code_returns_none_on_syntax_error() -> None:
    assert compile_function_code("def f(:\n", "f", "m.py") is None


def test_compile_function_code_refuses_ambiguous_qualname() -> None:
    # Two top-level `def f` share a qualname: picking one at random could graft a dead definition's body.
    assert compile_function_code("def f():\n    return 1\ndef f():\n    return 2\n", "f", "m.py") is None


def test_find_enclosing_function_method() -> None:
    src = "class C:\n    def m(self, x):\n        return x + 1\n"
    assert find_enclosing_function(src, 3) == "C.m"


def test_find_enclosing_function_nested_locals() -> None:
    src = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    assert find_enclosing_function(src, 3) == "outer.<locals>.inner"


def test_find_enclosing_function_module_level_is_none() -> None:
    assert find_enclosing_function("X = 1\nY = 2\n", 1) is None


def test_enclosing_function_index_is_built_once_per_source() -> None:
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner\n"
        "\n"
        "class C:\n"
        "    def method(self):\n"
        "        return 2\n"
    )
    inmemory.enclosing_function_index.cache_clear()
    try:
        index = inmemory.enclosing_function_index(source)
        assert find_enclosing_function(source, 3) == "outer.<locals>.inner"
        assert find_enclosing_function(source, 4) == "outer"
        assert find_enclosing_function(source, 8) == "C.method"
        assert find_enclosing_function(source, 5) is None
        assert inmemory.enclosing_function_index(source) is index
    finally:
        inmemory.enclosing_function_index.cache_clear()


def test_code_reachable_body_vs_module_vs_nested() -> None:
    assert code_reachable("def f():\n    x = 1\n    return x\n", 2) is True
    assert code_reachable("class C:\n    def m(self):\n        return 1\n", 3) is True
    assert code_reachable("def outer():\n    def inner():\n        return 1\n", 3) is True
    assert code_reachable("X = 1\nY = 2\n", 1) is False


def test_nested_function_mutation_applies_via_enclosing_patch() -> None:
    # A nested function is mutated by patching its top-level ancestor: the ancestor's co_consts carry the
    # mutated nested code object, built afresh on every call.
    orig = "def outer():\n    def inner():\n        return 1\n    return inner\n"
    mutated = "def outer():\n    def inner():\n        return 2\n    return inner\n"
    module = types.ModuleType("m_nested")
    exec(compile(orig, "m.py", "exec"), vars(module))
    outer = resolve_function(module, "outer")
    assert outer is not None
    assert outer()() == 1
    new_code = compile_function_code(mutated, "outer", "m.py")
    assert new_code is not None
    restore = patch_code(outer, new_code)
    try:
        assert outer()() == 2
    finally:
        restore()
    assert outer()() == 1


def test_resolve_function_module_level_and_method() -> None:
    module = types.ModuleType("sample")
    exec(
        compile("def top():\n    return 1\nclass C:\n    def m(self):\n        return 2\n", "s.py", "exec"),
        vars(module),
    )
    assert resolve_function(module, "top") is not None
    assert resolve_function(module, "C.m") is not None
    assert resolve_function(module, "missing") is None
    assert resolve_function(module, "outer.<locals>.inner") is None


def test_resolve_function_does_not_crash_on_shadowed_namespace() -> None:
    module = types.ModuleType("shadow")
    exec(compile("C = 5\n", "s.py", "exec"), vars(module))
    assert resolve_function(module, "C.m") is None


_DECORATED_SRC = (
    "import functools\n"
    "def deco(fn):\n"
    "    @functools.wraps(fn)\n"
    "    def wrapper(*a, **k):\n"
    "        return fn(*a, **k)\n"
    "    return wrapper\n"
    "@deco\n"
    "def compute(x):\n"
    "    return x + 1\n"
)


def _decorated_module() -> types.ModuleType:
    module = types.ModuleType("deco_mod")
    exec(compile(_DECORATED_SRC, "deco_mod.py", "exec"), vars(module))
    return module


def test_resolve_function_unwraps_functools_wraps() -> None:
    # The module attribute is the wrapper; the mutation must hit the wrapped body, whose code differs between
    # the original and the mutant (the wrapper's code is identical in both).
    module = _decorated_module()
    wrapper = vars(module)["compute"]
    inner = resolve_function(module, "compute")
    assert inner is not None
    assert inner is not wrapper
    assert inner.__code__.co_qualname == "compute"


def test_decorated_function_mutation_applies_through_wrapper() -> None:
    module = _decorated_module()
    assert module.compute(10) == 11
    func = resolve_function(module, "compute")
    assert func is not None
    new_code = compile_function_code(_DECORATED_SRC.replace("return x + 1", "return x - 1"), "compute", "deco_mod.py")
    assert new_code is not None
    restore = patch_code(func, new_code)
    try:
        assert module.compute(10) == 9
    finally:
        restore()
    assert module.compute(10) == 11


_LRU_SRC = "import functools\n@functools.lru_cache\ndef cached(x):\n    return x + 1\n"


def test_resolve_function_unwraps_lru_cache() -> None:
    # `@lru_cache` leaves a wrapper that is not even a FunctionType: it must be unwrapped before type checks.
    module = types.ModuleType("lru_mod")
    exec(compile(_LRU_SRC, "lru_mod.py", "exec"), vars(module))
    wrapper = vars(module)["cached"]
    inner = resolve_function(module, "cached")
    assert inner is not None
    assert inner is not wrapper
    assert inner.__code__.co_qualname == "cached"


def test_resolve_function_clears_lru_cache_so_mutation_is_observed() -> None:
    # A cache filled before the patch would return the original result around the mutated body.
    module = types.ModuleType("lru_clear_mod")
    exec(compile(_LRU_SRC, "lru_clear_mod.py", "exec"), vars(module))
    assert module.cached(5) == 6
    func = resolve_function(module, "cached")
    assert func is not None
    new_code = compile_function_code(_LRU_SRC.replace("x + 1", "x + 100"), "cached", "lru_clear_mod.py")
    assert new_code is not None
    restore = patch_code(func, new_code)
    try:
        assert module.cached(5) == 105
    finally:
        restore()


_PROPERTY_SRC = (
    "class Box:\n    def __init__(self, n):\n        self.n = n\n    @property\n    def doubled(self):\n"
    "        return self.n * 2\n"
)


def test_resolve_function_unwraps_property_descriptor() -> None:
    module = types.ModuleType("prop_mod")
    exec(compile(_PROPERTY_SRC, "prop_mod.py", "exec"), vars(module))
    func = resolve_function(module, "Box.doubled")
    assert func is not None
    assert func.__code__.co_qualname == "Box.doubled"


def test_property_mutation_applies_through_descriptor() -> None:
    module = types.ModuleType("prop_mod")
    exec(compile(_PROPERTY_SRC, "prop_mod.py", "exec"), vars(module))
    box = module.Box(5)
    assert box.doubled == 10
    func = resolve_function(module, "Box.doubled")
    assert func is not None
    new_code = compile_function_code(_PROPERTY_SRC.replace("self.n * 2", "self.n * 3"), "Box.doubled", "prop_mod.py")
    assert new_code is not None
    restore = patch_code(func, new_code)
    try:
        assert box.doubled == 15
    finally:
        restore()
    assert box.doubled == 10


def test_end_to_end_compile_resolve_patch() -> None:
    # `return -1` in the else branch: an input of 0 distinguishes the original (-1) from the mutant (0).
    original = "def settle(amount):\n    if amount > 0:\n        return amount\n    return -1\n"
    module = types.ModuleType("settle_mod")
    exec(compile(original, "settle_mod.py", "exec"), vars(module))
    qualname = find_enclosing_function(original, 2)
    assert qualname == "settle"
    func = resolve_function(module, qualname)
    assert func is not None
    assert func(5) == 5
    assert func(0) == -1
    mutated = "def settle(amount):\n    if amount >= 0:\n        return amount\n    return -1\n"
    new_code = compile_function_code(mutated, qualname, "settle_mod.py")
    assert new_code is not None
    restore = patch_code(func, new_code)
    try:
        assert func(0) == 0
    finally:
        restore()
    assert func(0) == -1


def test_resolvable_type_alias_accepts_callable_or_none() -> None:
    value = inmemory._Resolvable.__value__
    assert isinstance(value, types.UnionType)


def test_inject_yields_targets_nested_function() -> None:
    src = "async def f():\n    async def g():\n        return await a()\n    return await g()\n"
    out = inject_yields(src, "f.<locals>.g")
    assert "balamut_fuzz_yield(a(), 1)" in out


def test_inject_yields_targets_method() -> None:
    src = "class C:\n    async def m(self):\n        return await a()\n"
    out = inject_yields(src, "C.m")
    assert "balamut_fuzz_yield(a(), 1)" in out


def test_inject_yields_finds_function_nested_in_block() -> None:
    src = "async def f():\n    if True:\n        async def g():\n            return await a()\n    return await g()\n"
    out = inject_yields(src, "f.<locals>.g")
    assert "balamut_fuzz_yield(a(), 1)" in out


def test_inject_yields_wraps_inner_awaits_first() -> None:
    src = "async def f():\n    return await g(await a())\n"
    out = inject_yields(src, "f")
    assert "balamut_fuzz_yield(a(), 1)" in out


def test_inject_yields_preserves_nested_sync_function() -> None:
    src = "async def f():\n    def helper():\n        return 1\n    return await a() + helper()\n"
    out = inject_yields(src, "f")
    assert "def helper():" in out
    assert "balamut_fuzz_yield(a(), 1)" in out


def test_find_enclosing_function_includes_function_def_line() -> None:
    src = "def f():\n    x = 1\n    return x\n"
    assert find_enclosing_function(src, 1) == "f"


def test_find_enclosing_function_nested_in_block() -> None:
    src = "def f():\n    if True:\n        def g():\n            return 1\n        return g()\n"
    assert find_enclosing_function(src, 4) == "f.<locals>.g"


def test_enclosing_function_index_depths() -> None:
    source = "def f():\n    def g():\n        return 1\n    return g\n"
    inmemory.enclosing_function_index.cache_clear()
    spans = inmemory.enclosing_function_index(source)
    by_qualname = {span.qualname: span for span in spans}
    assert by_qualname["f"].depth == 0
    assert by_qualname["f.<locals>.g"].depth == 1


def test_resolve_with_cache_clear_refuses_nested_qualname() -> None:
    module = types.ModuleType("nested_mod")
    exec(compile("def outer():\n    def inner():\n        return 1\n    return inner\n", "n.py", "exec"), vars(module))
    resolved, cache_clear = inmemory.resolve_with_cache_clear(module, "outer.<locals>.inner")
    assert resolved is None
    assert cache_clear is inmemory._noop_cache_clear


def test_resolve_function_unwraps_staticmethod() -> None:
    module = types.ModuleType("static_mod")
    exec(compile("class C:\n    @staticmethod\n    def m(x):\n        return x + 1\n", "s.py", "exec"), vars(module))
    func = resolve_function(module, "C.m")
    assert func is not None
    assert func(1) == 2


def test_is_method_descriptor_detects_static_and_class_methods() -> None:
    assert inmemory._is_method_descriptor(staticmethod(lambda: None)) is True
    assert inmemory._is_method_descriptor(classmethod(lambda _cls: None)) is True
    assert inmemory._is_method_descriptor(lambda: None) is False


def test_yield_injector_descends_into_root_and_returns_it() -> None:
    injector = inmemory._YieldInjector("balamut_fuzz_yield")
    tree = ast.parse("async def f():\n    return await a()\n")
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert injector._descend_if_root(fn) is fn
