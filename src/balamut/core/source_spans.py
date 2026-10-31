"""UTF-8 byte offsets shared by every AST enumerator.

CPython reports ``col_offset`` in UTF-8 bytes, so spans are computed in bytes as well: multi-byte text in
strings or comments must not shift a splice.
"""

from __future__ import annotations

import ast


def node_byte_span(node: ast.expr | ast.stmt, line_starts: list[int]) -> tuple[int, int] | None:
    """Byte span of an AST node, or ``None`` when the parser did not record an end position."""
    if node.end_lineno is None or node.end_col_offset is None:
        return None
    if node.lineno < 1 or node.end_lineno > len(line_starts):
        return None
    start = line_starts[node.lineno - 1] + node.col_offset
    end = line_starts[node.end_lineno - 1] + node.end_col_offset
    return start, end


def line_byte_starts(source: str) -> list[int]:
    """Byte offset of each line start; LF, CRLF and a lone CR end a line, exactly as the tokenizer counts."""
    data = source.encode("utf-8")
    starts = [0]
    index = 0
    while index < len(data):
        byte = data[index]
        if byte == 0x0A:
            starts.append(index + 1)
        elif byte == 0x0D:
            if index + 1 < len(data) and data[index + 1] == 0x0A:
                starts.append(index + 2)
                index += 1
            else:
                starts.append(index + 1)
        index += 1
    return starts
