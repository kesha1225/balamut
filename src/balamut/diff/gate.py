"""The gate over the engine's ``--json`` verdicts of a diff run: harness logic, not the engine.

The diff run mutates the CHANGED FILES whole. The gate narrows the verdict to the **changed lines** of the pinned
scope (the diff against the base plus untracked files): a mutant on an untouched line of a changed file is someone
else's debt and is not gated ("no new survivors on changed lines").

The scoped mutants are classified as:

- **unresolved**: a timeout without an observed test result; **gates** (exit 1);
- **actionable**: a HIGH survivor (a significant mutation nobody killed) or no coverage INSIDE a function body (new
  code without a single covering test); **gates**, so the loop repeats until the tests are adequate;
- **cold no coverage**: no coverage outside the ordinary runtime body of a function. Per-test coverage could not
  attribute a killer to the site; the AST category says whether an import or schema runner is needed. The split is
  ``code_reachable``; the result is advisory and **does not gate** an ordinary diff run;
- **review**: a REVIEW survivor (``const:num`` / unary / augassign, prone to equivalence); shown, **not gated**:
  gating them pushes an agent into vacuous change-detector tests;
- **low** survivors (a deleted logging call): suppressed.

An empty diff or no mutants is clean, exit 0.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NotRequired, TypedDict

from balamut.core.triage import cold_kind
from balamut.diff import scope as diff_scope
from balamut.report.agent import MutantRecord, build_report
from balamut.runtime.inmemory import code_reachable


class _Status(Enum):
    SURVIVED = "survived"
    KILLED = "killed"
    TIMEOUT = "timeout"
    NO_COVERAGE = "no_coverage"
    UNSUPPORTED = "unsupported"


class _Bucket(Enum):
    HIGH = "high"
    REVIEW = "review"
    LOW = "low"
    NONE = ""  # mutants that did not survive carry no bucket


class _Record(TypedDict):
    """One element of the engine's ``--json`` array."""

    key: NotRequired[str]
    path: str
    lineno: int
    # The mutated span [site_lineno, site_end_lineno] decides the scope (multi-line mutants); a record without it
    # falls back to lineno.
    site_lineno: NotRequired[int]
    site_end_lineno: NotRequired[int]
    start_byte: NotRequired[int]
    end_byte: NotRequired[int]
    operator: str
    status: str
    covering: int
    covering_tests: NotRequired[list[str]]
    bucket: str
    original: str
    mutated: str
    # Why the engine reached the verdict: why a timeout stayed unresolved or that a kill came from a hang.
    reason: NotRequired[str]


@dataclass(frozen=True, slots=True)
class Mutant:
    path: str
    lineno: int
    site_lineno: int
    site_end_lineno: int
    start_byte: int | None
    end_byte: int | None
    operator: str
    status: _Status
    covering: int
    bucket: _Bucket
    original: str
    mutated: str
    key: str
    covering_tests: tuple[str, ...]
    reason: str = ""


def _parse_record(rec: _Record) -> Mutant:
    return Mutant(
        path=rec["path"],
        lineno=rec["lineno"],
        site_lineno=rec.get("site_lineno", rec["lineno"]),
        site_end_lineno=rec.get("site_end_lineno", rec.get("site_lineno", rec["lineno"])),
        start_byte=rec.get("start_byte"),
        end_byte=rec.get("end_byte"),
        operator=rec["operator"],
        status=_Status(rec["status"]),
        covering=rec["covering"],
        bucket=_Bucket(rec["bucket"]),
        original=rec["original"],
        mutated=rec["mutated"],
        key=rec.get("key", ""),
        covering_tests=tuple(rec.get("covering_tests", [])),
        reason=rec.get("reason", ""),
    )


def parse_mutants(text: str) -> list[Mutant]:
    """The engine's ``--json`` array as mutants; empty input or ``[]`` is an empty diff, nothing to gate."""
    stripped = text.strip()
    if not stripped:
        return []
    records: list[_Record] = json.loads(stripped)
    return [_parse_record(rec) for rec in records]


type Reachable = Callable[[str, int], bool]  # (file, lineno) -> inside a function body (not a cold site)?
type SourceFor = Callable[[str], str | None]
type ColdCounts = dict[str, int]
type _SourceCache = dict[str, str | None]  # file -> its source, None when it is not on disk


def _always_body(_file: str, _lineno: int) -> bool:
    """Without a reachability predicate every site counts as a function body."""
    return True


def _no_source(_file: str) -> None:
    """No source supplied: a cold site keeps the honest ``unattributed`` category."""


# Construction dunders run when an object or class is CREATED. For an Enum subclass that is import time (members
# are built at class definition), not inside a test, so per-test coverage cannot attribute them: a cold site. A
# plain (non-Enum) ``__init__`` runs when a test instantiates the class and is not cold.
_ENUM_BASES = frozenset({"Enum", "StrEnum", "IntEnum", "IntFlag", "Flag", "ReprEnum"})
_CONSTRUCTION_DUNDERS = frozenset({"__new__", "__init__", "__init_subclass__"})


def _base_name(base: ast.expr) -> str | None:
    match base:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
        case _:
            return None


def _has_enum_base(node: ast.ClassDef) -> bool:
    return any(_base_name(base) in _ENUM_BASES for base in node.bases)


def enum_construction_line(source: str, lineno: int) -> bool:
    """Is the line inside ``__new__`` / ``__init__`` / ``__init_subclass__`` of an Enum subclass? It runs while the
    members are created (import time), not inside a test: a cold site like module level."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and _has_enum_base(node)):
            continue
        for item in node.body:
            if (
                isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and item.name in _CONSTRUCTION_DUNDERS
                and item.lineno <= lineno <= (item.end_lineno or item.lineno)
            ):
                return True
    return False


def disk_source(root: Path) -> SourceFor:
    """Cached reads of sources under ``root`` for one run; a missing file reads as None."""
    cache: _SourceCache = {}

    def source_for(file: str) -> str | None:
        if file not in cache:
            try:
                cache[file] = (root / file).read_text(encoding="utf-8")
            except OSError:
                cache[file] = None
        return cache[file]

    return source_for


def source_reachable(source_for: SourceFor) -> Reachable:
    """The "body gap that gates" predicate over a shared source cache.

    A body gap is a line inside a function body, except the construction dunders of an Enum subclass; anything off
    that runtime path is cold. A file missing on disk counts as body, so the unknown cannot hide a gap."""

    def reachable(file: str, lineno: int) -> bool:
        source = source_for(file)
        if source is None:
            return True
        return code_reachable(source, lineno) and not enum_construction_line(source, lineno)

    return reachable


@dataclass(frozen=True, slots=True)
class GateResult:
    unresolved: list[Mutant]  # a timeout without an observed test failure: gates
    actionable: list[Mutant]  # a HIGH survivor: gates
    uncovered: list[Mutant]  # no coverage INSIDE a function body: a real gap, gates
    cold: list[Mutant]  # no coverage at module or class level (import time): advisory, does not gate
    cold_counts: ColdCounts  # AST categories of the cold sites; no killer is attributed to them
    review: list[Mutant]  # a REVIEW survivor: shown, does not gate
    killed: int  # killed on changed lines

    def failed(self) -> bool:
        return bool(self.unresolved or self.actionable or self.uncovered)


def _scoped(mutants: Iterable[Mutant], changed: diff_scope.ChangedLines) -> list[Mutant]:
    return [
        mutant
        for mutant in mutants
        if diff_scope.mutant_in_scope(mutant.path, mutant.site_lineno, mutant.site_end_lineno, changed)
    ]


def _cold_counts(cold: Iterable[Mutant], source_for: SourceFor) -> ColdCounts:
    return dict(
        Counter(
            cold_kind(
                source_for(mutant.path), line=mutant.site_lineno, start_byte=mutant.start_byte, end_byte=mutant.end_byte
            ).value
            for mutant in cold
        )
    )


def classify(
    mutants: Iterable[Mutant],
    changed: diff_scope.ChangedLines,
    reachable: Reachable = _always_body,
    source_for: SourceFor = _no_source,
) -> GateResult:
    """Narrow the mutants to the changed lines and split them into the gate's classes.

    ``reachable(file, lineno)`` separates no coverage inside a function body from a cold site; ``source_for(file)``
    gives the AST category of a cold site, and a missing source reads as ``unattributed``."""
    scoped = _scoped(mutants, changed)
    nocov = [m for m in scoped if m.status is _Status.NO_COVERAGE]
    uncovered: list[Mutant] = []
    cold: list[Mutant] = []
    for mutant in nocov:
        (uncovered if reachable(mutant.path, mutant.lineno) else cold).append(mutant)
    return GateResult(
        unresolved=[m for m in scoped if m.status is _Status.TIMEOUT],
        actionable=[m for m in scoped if m.status is _Status.SURVIVED and m.bucket is _Bucket.HIGH],
        uncovered=uncovered,
        cold=cold,
        cold_counts=_cold_counts(cold, source_for),
        review=[m for m in scoped if m.status is _Status.SURVIVED and m.bucket is _Bucket.REVIEW],
        killed=sum(1 for m in scoped if m.status is _Status.KILLED),
    )


def _format_mutant(mutant: Mutant) -> str:
    return (
        f"  {mutant.path}:{mutant.lineno}  [{mutant.operator}]  «{mutant.original}» → «{mutant.mutated}»"
        f"  ({mutant.covering} test(s))"
    )


def _formatted(mutants: list[Mutant]) -> list[str]:
    return [_format_mutant(mutant) for mutant in sorted(mutants, key=lambda m: (m.path, m.lineno))]


def render(result: GateResult, base: str) -> str:
    """The unresolved and actionable findings, or the clean verdict."""
    lines: list[str] = []
    if not result.failed():
        lines.append(
            f"✅ diff gate clean vs {base}: every actionable mutant on the changed lines is killed "
            f"(killed {result.killed}, review shown {len(result.review)})."
        )
    else:
        count = len(result.unresolved) + len(result.actionable) + len(result.uncovered)
        lines.append(f"❌ diff gate vs {base}: {count} unresolved or actionable finding(s) on the changed lines:")
        if result.unresolved:
            lines.append(f"\n  TIMEOUT / UNRESOLVED: no observed test result ({len(result.unresolved)}):")
            lines.extend(_formatted(result.unresolved))
        if result.actionable:
            lines.append(f"\n  HIGH survived: no test catches the mutation ({len(result.actionable)}):")
            lines.extend(_formatted(result.actionable))
        if result.uncovered:
            lines.append(f"\n  NO COVERAGE: new code without a covering test ({len(result.uncovered)}):")
            lines.extend(_formatted(result.uncovered))
        lines.append(
            "\n  → look into the timeouts, write or strengthen tests for the other findings and run `balamut diff`"
            f" again. (killed {result.killed}, review shown {len(result.review)}, not gated)"
        )
    if result.review:
        lines.append(
            "\n  REVIEW (const:num / unary / augassign, prone to equivalence; not gated, read them): "
            f"{len(result.review)}"
        )
        lines.extend(_formatted(result.review))
    if result.cold:
        categories = ", ".join(f"{kind}={count}" for kind, count in sorted(result.cold_counts.items()))
        lines.append(
            "\n  COLD no coverage: the killer is not attributed; type, schema and import-time sites need a runner of "
            f"their own. They do not gate an ordinary diff run: {len(result.cold)} ({categories})"
        )
        lines.extend(_formatted(result.cold))
    return "\n".join(lines)


def gate(mutants: list[Mutant], scope: diff_scope.DiffScope, *, root: Path) -> GateResult:
    """Classify the mutants inside the pinned scope of a run; sources are read under ``root``."""
    source_for = disk_source(root)
    return classify(mutants, scope.changed_lines, source_reachable(source_for), source_for)


def gate_records(scoped: list[Mutant]) -> list[MutantRecord]:
    """Gate mutants on the changed lines as agent report records.

    The exact covering node ids come from the raw JSON; semantic classes are not part of it."""
    return [
        {
            "key": mutant.key,
            "file": mutant.path,
            "lineno": mutant.lineno,
            "operator": mutant.operator,
            "verdict": mutant.status.value,
            "bucket": mutant.bucket.value,
            "original": mutant.original,
            "mutated": mutant.mutated,
            "covering": list(mutant.covering_tests),
            "semantic": [],
        }
        for mutant in scoped
    ]


def _agent_report(mutants: list[Mutant], scope: diff_scope.DiffScope, root: Path) -> int:
    """The clustered agent report narrowed to the changed lines; exit 1 when it holds a gating finding."""
    source_for = disk_source(root)
    reachable = source_reachable(source_for)
    runtime: list[Mutant] = []
    cold: list[Mutant] = []
    for mutant in _scoped(mutants, scope.changed_lines):
        if mutant.status is _Status.NO_COVERAGE and not reachable(mutant.path, mutant.lineno):
            cold.append(mutant)
        else:
            runtime.append(mutant)
    report = build_report(gate_records(runtime), cold_counts=_cold_counts(cold, source_for))
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 1 if (report["unresolved"] or report["actionable"] or report["no_coverage"]) else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="balamut-gate", description="Gate over the engine's diff --json verdicts.")
    parser.add_argument("--json", required=True, help="file with the engine's --json output of a diff run")
    parser.add_argument("--base", default="origin/main", help="git ref of the base the changed lines are taken from")
    parser.add_argument("--scope", help="JSON of the pinned scope of the run")
    parser.add_argument(
        "--agent-json", action="store_true", help="clustered agent report (JSON, changed lines) instead of the render"
    )
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        text = Path(args.json).read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"balamut-gate: cannot read {args.json} ({exc})\n")
        return 2
    mutants = parse_mutants(text)
    if args.scope:
        try:
            scope = diff_scope.read_scope(Path(args.scope))
        except OSError as exc:
            sys.stderr.write(f"balamut-gate: cannot read {args.scope} ({exc})\n")
            return 2
    else:
        scope = diff_scope.resolve_scope(args.base, repo_root=root, base_mode=diff_scope.BaseMode.DIRECT)
    if args.agent_json:
        return _agent_report(mutants, scope, root)
    result = gate(mutants, scope, root=root)
    sys.stdout.write(render(result, scope.base_ref) + "\n")
    return 1 if result.failed() else 0
