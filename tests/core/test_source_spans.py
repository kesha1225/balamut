"""UTF-8 byte offsets shared by every AST enumerator (balamut.core.source_spans)."""

from __future__ import annotations

import ast

from balamut.core.source_spans import line_byte_starts, node_byte_span


def test_node_byte_span_computes_utf8_byte_span() -> None:
    src = 'x = "héllo"\n'
    tree = ast.parse(src)
    assign = tree.body[0]
    assert node_byte_span(assign, line_byte_starts(src)) == (0, len(src.encode("utf-8")) - 1)


def test_node_byte_span_returns_none_when_end_col_missing_but_lineno_present() -> None:
    # Only the end column is missing: the early return must fire (or a bogus span would be computed).
    node = ast.Constant(value=1)
    node.lineno = 1
    node.col_offset = 0
    node.end_lineno = 1
    node.end_col_offset = None
    assert node_byte_span(node, [0]) is None


def test_node_byte_span_returns_none_when_end_lineno_missing_but_col_present() -> None:
    node = ast.Constant(value=1)
    node.lineno = 1
    node.col_offset = 0
    node.end_lineno = None
    node.end_col_offset = 3
    assert node_byte_span(node, [0]) is None


def test_node_byte_span_returns_none_for_out_of_range_lines() -> None:
    node = ast.Constant(value=1)
    node.lineno = 0
    node.col_offset = 0
    node.end_lineno = 1
    node.end_col_offset = 1
    assert node_byte_span(node, [0]) is None

    beyond = ast.Constant(value=1)
    beyond.lineno = 1
    beyond.col_offset = 0
    beyond.end_lineno = 5
    beyond.end_col_offset = 1
    assert node_byte_span(beyond, [0, 10]) is None


def test_line_byte_starts_handles_lf() -> None:
    assert line_byte_starts("a\nb\n") == [0, 2, 4]


def test_line_byte_starts_handles_crlf() -> None:
    assert line_byte_starts("a\r\nb") == [0, 3]


def test_line_byte_starts_handles_lone_cr() -> None:
    assert line_byte_starts("a\rb") == [0, 2]
    assert line_byte_starts("a\r") == [0, 2]


def test_line_byte_starts_handles_crlf_then_lone_cr() -> None:
    assert line_byte_starts("a\r\n\rb") == [0, 3, 4]


def test_line_byte_starts_handles_trailing_crlf() -> None:
    # A CRLF at the very end: the line start after it is the full byte length.
    assert line_byte_starts("a\r\n") == [0, 3]
