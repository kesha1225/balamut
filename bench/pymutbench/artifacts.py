"""Normalized mutants: one executable artifact per distinct mutated file, with its native origins.

Every tool's output is reduced to the same record: the path of the mutated file relative to the subject root,
one byte-precise edit of the original file (the common prefix and suffix of the original and the mutated bytes
are trimmed, so a multi-site artifact still is one span) and the tool's own identity for it. The artifact id
is the hash of the path and the complete mutated bytes: two tools emitting the same file share one judgement.
"""

from __future__ import annotations

import ast
import dis
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Edit:
    start: int  # byte offset in the original file
    end: int
    replacement: str


@dataclass(slots=True)
class Artifact:
    path: str
    edit: Edit
    aid: str  # sha256 over the path and the mutated bytes
    origins: list[dict[str, Any]] = field(default_factory=list)  # {"tool", "native_id", "operator", ...}

    def apply(self, original: bytes) -> bytes:
        return original[: self.edit.start] + self.edit.replacement.encode("utf-8") + original[self.edit.end :]

    def to_json(self) -> dict[str, Any]:
        return {"aid": self.aid, "path": self.path, "edit": asdict(self.edit), "origins": self.origins}

    @staticmethod
    def from_json(data: dict[str, Any]) -> Artifact:
        return Artifact(data["path"], Edit(**data["edit"]), data["aid"], data.get("origins", []))


def artifact_id(path: str, mutated: bytes) -> str:
    return hashlib.sha256(path.encode("utf-8") + b"\0" + mutated).hexdigest()[:24]


def minimal_edit(original: bytes, mutated: bytes) -> Edit:
    prefix = 0
    limit = min(len(original), len(mutated))
    while prefix < limit and original[prefix] == mutated[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and original[len(original) - 1 - suffix] == mutated[len(mutated) - 1 - suffix]
    ):
        suffix += 1
    end_orig, end_mut = len(original) - suffix, len(mutated) - suffix
    # Never split a UTF-8 sequence: a span edge on a continuation byte (0b10xxxxxx) moves outwards.
    while prefix > 0 and prefix < len(original) and _continuation(original[prefix]):
        prefix -= 1
    while end_orig < len(original) and _continuation(original[end_orig]):
        end_orig += 1
        end_mut += 1
    return Edit(prefix, end_orig, mutated[prefix:end_mut].decode("utf-8"))


def _continuation(byte: int) -> bool:
    return (byte & 0xC0) == 0x80


def from_mutated(path: str, original: bytes, mutated: bytes, origin: dict[str, Any]) -> Artifact:
    return Artifact(path, minimal_edit(original, mutated), artifact_id(path, mutated), [origin])


def edit_lines(original: bytes, edit: Edit) -> tuple[int, int]:
    """First and last original line (1-based) the edit touches; an insertion touches the line it lands on."""
    start_line = original.count(b"\n", 0, edit.start) + 1
    end_line = original.count(b"\n", 0, max(edit.start, edit.end - 1)) + 1
    return start_line, max(start_line, end_line)


def canonical(source: str) -> str | None:
    """The AST dump without positions: equal dumps mean equal programs up to formatting and comments."""
    try:
        return ast.dump(ast.parse(source), include_attributes=False)
    except SyntaxError:
        return None


def code_key(code: Any) -> tuple[Any, ...]:
    """A code object's identity as its instruction stream with resolved arguments, recursively.

    Constants are compared by value rather than by their index in ``co_consts``: constant folding can leave an
    unused entry in the table (``-1`` keeps the ``1`` it was folded from), which shifts every index without
    changing the program. Line tables and positions are ignored.
    """
    items = []
    for ins in dis.get_instructions(code):
        arg = ins.argval
        if hasattr(arg, "co_code"):
            arg = code_key(arg)
        elif not isinstance(arg, str | int | float | complex | bytes | bool | tuple | frozenset | type(None)):
            arg = repr(arg)
        else:
            arg = (type(arg).__name__, repr(arg))
        items.append((ins.opname, arg))
    return (tuple(items), code.co_varnames, code.co_freevars, code.co_cellvars, code.co_flags, code.co_argcount,
            code.co_kwonlyargcount, code.co_posonlyargcount, code.co_exceptiontable)


_code_key = code_key


def compiled_key(source: str, filename: str) -> tuple[Any, ...] | None:
    """A bytecode identity that ignores line tables (Trivial Compiler Equivalence for CPython)."""
    try:
        return _code_key(compile(source, filename, "exec", dont_inherit=True))
    except (SyntaxError, ValueError):
        return None


def merge(artifacts: list[Artifact]) -> list[Artifact]:
    by_id: dict[str, Artifact] = {}
    for art in artifacts:
        if art.aid in by_id:
            by_id[art.aid].origins.extend(art.origins)
        else:
            by_id[art.aid] = art
    return list(by_id.values())


def write_jsonl(path: Path, artifacts: list[Artifact]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for art in artifacts:
            fh.write(json.dumps(art.to_json()) + "\n")  # ASCII-escaped: one record per line


def read_jsonl(path: Path) -> list[Artifact]:
    return [Artifact.from_json(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]
