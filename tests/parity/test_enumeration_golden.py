"""Golden enumeration parity: every built-in kind over a fixed corpus, byte for byte.

The golden pins, per (file, kind): each mutant's lineno, UTF-8 byte span, original/mutated text,
operator name and site lines — in emission order. Any refactor of the enumerators (the capability
rework) must reproduce it exactly; a deliberate change is a golden diff to review, never a silent
regeneration. Regenerate only with ``BALAMUT_WRITE_GOLDEN=1`` (a review artifact).

The corpus enumerates every built-in kind (the typed kind was removed).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

from balamut.config import QueueFamily
from balamut.core.mutants import Mutant
from balamut.operators.concurrency import enumerate_concurrency
from balamut.operators.db import enumerate_chain, enumerate_db
from balamut.operators.extreme import enumerate_extreme
from balamut.operators.fault import enumerate_fault
from balamut.operators.oracle import enumerate_oracle
from balamut.operators.src import enumerate_mutants

GOLDEN = Path(__file__).parent / "golden" / "enumeration.json"
CORPUS = Path(__file__).parent / "corpus"

type Enumerator = Callable[[str, str], list[Mutant]]

# The queue family the concurrency kind is configured with in this golden: the torture module's Task model.
QUEUE_FAMILY = QueueFamily(
    model="Task",
    family="tasks",
    fence=("token",),
    due="run_at",
    lease="expires_at",
    backstop="attempts",
    progress="done_at",
)

KINDS: dict[str, Enumerator] = {
    "src": enumerate_mutants,
    "concurrency": partial(enumerate_concurrency, queues=(QUEUE_FAMILY,)),
    "fault": enumerate_fault,
    "oracle": enumerate_oracle,
    "extreme": enumerate_extreme,
    "db": enumerate_db,
    "chain": enumerate_chain,
}


def _row(mutant: Mutant) -> list[int | str]:
    return [
        mutant.lineno,
        mutant.start_byte,
        mutant.end_byte,
        mutant.original,
        mutant.mutated,
        mutant.operator,
        mutant.site_lineno,
        mutant.site_end_lineno,
    ]


def _enumerate_all() -> dict[str, dict[str, list[list[int | str]]]]:
    result: dict[str, dict[str, list[list[int | str]]]] = {}
    for file in sorted(CORPUS.rglob("*.py")):
        # The engine reads sources with default newline handling (universal newlines): mirror it.
        source = file.read_text(encoding="utf-8")
        relative = file.relative_to(CORPUS).as_posix()
        result[relative] = {kind: [_row(mutant) for mutant in enum(relative, source)] for kind, enum in KINDS.items()}
    return result


def test_enumeration_golden() -> None:
    actual = _enumerate_all()
    if os.environ.get("BALAMUT_WRITE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, indent=1, ensure_ascii=False), encoding="utf-8")
        pytest.skip("golden written")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert actual == expected, "enumeration drifted from the golden: review the diff, never regenerate blindly"
