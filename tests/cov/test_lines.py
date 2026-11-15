"""Which recorded coverage lines stand for a line of source (balamut.coverage.lines)."""

from __future__ import annotations

from balamut.coverage.lines import line_events

# CPython 3.14 folds three or more constants before ``*args`` into one tuple constant numbered with the call's first
# line.
_FOLDED = (
    "def run(call, base, *args):\n"  # 1
    "    return call(\n"  # 2
    '        "-c",\n'  # 3
    '        "diff.noprefix=false",\n'  # 4
    '        "diff",\n'  # 5
    "        *args,\n"  # 6
    "        base,\n"  # 7
    "    )\n"  # 8
)


def _recorded(source: str, line: int) -> set[int]:
    events = line_events(source)
    assert events is not None
    return set(events.recorded_as(line))


def test_a_folded_argument_stands_for_the_line_of_its_call() -> None:
    assert [_recorded(_FOLDED, line) for line in (3, 4, 5)] == [{2}, {2}, {2}]


def test_a_line_with_its_own_event_stands_for_itself() -> None:
    assert [_recorded(_FOLDED, line) for line in (1, 2, 6, 7)] == [{1}, {2}, {6}, {7}]


def test_the_closing_line_of_a_call_stands_for_the_line_of_the_call() -> None:
    assert _recorded(_FOLDED, 8) == {2}


def test_a_folded_list_inside_a_call_stands_for_the_line_of_the_list() -> None:
    source = (
        "def run(call, *args):\n"  # 1
        "    return call(\n"  # 2
        "        [\n"  # 3
        '            "a",\n'  # 4
        '            "b",\n'  # 5
        '            "c",\n'  # 6
        "        ],\n"  # 7
        "        *args,\n"  # 8
        "    )\n"  # 9
    )

    assert _recorded(source, 5) == {3}


def test_a_default_value_line_inside_a_function_header_stands_for_itself() -> None:
    # The function's own code spans the header, but the default value runs in the enclosing code at definition time.
    source = "def f(x=g(\n    1,\n)):\n    return x\n"

    assert _recorded(source, 2) == {2}


def test_annotations_follow_the_future_imports_of_the_module_itself() -> None:
    # Without ``from __future__ import annotations`` a class annotation is deferred and none of its lines runs with
    # the class body; with it, the class body stores the annotation from its own line.
    source = "class C:\n    x: dict[\n        str,\n        int,\n    ]\n"
    future = f"from __future__ import annotations\n\n\n{source}"

    assert (_recorded(source, 3), _recorded(future, 6)) == ({1}, {5})


def test_a_body_line_without_bytecode_never_stands_for_the_def_line() -> None:
    # The module code spans the whole definition with the instructions that create the function, and the deferred
    # annotations get an ``__annotate__`` function spanning it too: both run without the body.
    source = (
        "def f(x: int) -> int:\n"  # 1
        "    if not (\n"  # 2
        "        x > 1\n"  # 3
        "        and x < 5\n"  # 4
        "    ):\n"  # 5
        "        return 1\n"  # 6
        "    return 0\n"  # 7
    )

    assert _recorded(source, 2) == set()
    assert _recorded(source, 3) == {3}


def test_a_line_of_a_nested_function_is_read_in_the_nested_code() -> None:
    source = (
        "def outer():\n"  # 1
        "    def inner():\n"  # 2
        "        return [\n"  # 3
        "            1,\n"  # 4
        "            2,\n"  # 5
        "            3,\n"  # 6
        "        ]\n"  # 7
        "    return inner\n"  # 8
    )

    assert _recorded(source, 4) == {3}


def test_a_module_level_constant_stands_for_its_statement() -> None:
    source = 'NAMES = [\n    "a",\n    "b",\n    "c",\n]\n'

    assert _recorded(source, 3) == {1}


def test_a_line_outside_any_code_stands_for_nothing() -> None:
    assert _recorded(_FOLDED, 40) == set()


def test_source_that_does_not_compile_has_no_line_events() -> None:
    # It parses, so the enumerator sees it; only the compiler rejects a return outside a function.
    assert line_events("return 1\n") is None
