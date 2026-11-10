"""Per-bug and per-tool measures of the real-fault track, from the oracle's verdicts.

For tool ``x`` and bug ``b``, ``M`` is the tool's native in-scope mutants (one entry per emission; two
emissions of the same mutated file share one artifact and one verdict). A **witness** is an artifact the
historical suite does not kill and the bug's fail-to-pass tests do kill. The per-bug measures are

* ``detected`` — at least one witness among the tool's mutants (unrestricted volume); a witness counts only when
  an independent re-judgement from fresh trees (``oracle.confirm.jsonl``: the whole historical suite, then the
  fail-to-pass tests) confirms it;
* ``R(q)`` — the probability that ``q`` emissions drawn uniformly without replacement from ``M`` contain a
  witness (a fixed generation budget, which controls for the number of mutants a tool emits);
* ``S(k)`` — the probability that ``k`` artifacts drawn uniformly without replacement from the unique survivors
  of the tool's mutants, as the reference oracle judges them, contain a witness (a fixed inspection budget; the
  tool's own engine may report other survivors, which the engine track measures);
* both draw ``min(budget, population)`` items and are 0 for an empty population;
* the accounting of every emission: invalid, no-op, TCE-equivalent, gated out, killed, survived, unresolved.

Two coverage strata, both retrospective (they use the fix's scope and fail-to-pass tests, which did not exist
before the fix): a bug is coverage-guided when the fail-to-pass tests execute a line of the scope that the
historical suite never ran, and its fault lines are covered when every line the fix changed that the
fail-to-pass run executes was also executed by the historical suite (fail-to-pass coverage includes every
fail-to-pass test, feature tests too).
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.scope import changed_lines_in  # noqa: E402
from pymutbench.stats import hit_probability  # noqa: E402

TOOLS = ("balamut", "mutmut", "cosmic-ray", "poodle", "pytest-gremlins", "pytest-leela", "mutation",
         "universalmutator", "fest", "irradiate", "deployproof")
# balamut's generation covers the source-mutating kinds `balamut diff` runs by default (src, concurrency, fault,
# extreme; its default `oracle` kind mutates tests, outside a source scope). Its ranking row keeps the source kinds
# `balamut mutate` runs by default (src, concurrency, fault): the file-level default configuration, as every other
# tool gets, and the one of its engine-track campaign.
DEFAULT_DROP: dict[str, str] = {"balamut": "extreme:"}  # tool -> native-id prefix of emissions outside its row
# Variants: a tool's emissions under another documented configuration, as (tool, native-id prefix to drop; ""
# keeps every emission); reported apart from the ranking, the comparison family and the overlap matrix.
VARIANTS: dict[str, tuple[str, str]] = {"balamut + extreme": ("balamut", "")}
Q_BUDGETS = (1, 5, 10, 25, 50, 100)
K_BUDGETS = (1, 3, 5, 10)


@dataclass
class ToolBug:
    tool: str
    available: bool = True  # False when the generation adapter failed for this bug (excluded, reported)
    emitted: int = 0
    unique: int = 0
    status: Counter[str] = field(default_factory=Counter)
    witnesses: int = 0  # unique witness artifacts
    witness_emissions: int = 0
    survivors: int = 0  # unique survivor artifacts (witnesses included)
    detected: bool = False
    detected_any_fix_test: bool = False  # sensitivity: a witness by any fail-to-pass test, feature tests included
    # The witness flow, in unique artifacts: first-judgement witnesses; of those, re-judgements without an outcome
    # (unknown), re-judgements that killed the mutant or found no red fail-to-pass test (unconfirmed), witnesses
    # whose only red outcome is a collection or startup failure of the whole fail-to-pass run in either judgement
    # (synthetic), and confirmed witnesses red only on tests of a feature the commit added (feature only).
    first_witnesses: int = 0
    unknown_confirmations: int = 0
    unconfirmed_witnesses: int = 0
    synthetic_witnesses: int = 0
    feature_only_witnesses: int = 0
    unknown_witness: int = 0  # survivors whose fail-to-pass run gave no outcome
    undecided: int = 0  # artifacts that could still be witnesses: unknown fix outcome or confirmation, unresolved
    exact_fault: bool | None = None  # single-file fixes only: some mutant re-creates the fault (``_reproduces``)
    r: dict[int, float] = field(default_factory=dict)
    s: dict[int, float] = field(default_factory=dict)


@dataclass
class BugResult:
    subject: str
    commit: str
    fault_class: str
    repair_shape: str
    scope_lines: int
    fault_covered_by_old: bool
    coverage_guided: bool
    unresolved: int
    tools: dict[str, ToolBug]
    files: int = 1
    region_kind: str = "function"  # function, statement (class body or module level) or mixed
    confirmed: bool = True  # every first-judgement witness has a fresh re-judgement
    fault_lines_run: bool = True  # the fail-to-pass run executed a changed line (``fault_covered_by_old`` needs one)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def repair_shape(diff: str) -> str:
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    if added and not removed:
        return "pure_addition"
    if removed and not added:
        return "pure_deletion"
    return "modification"


def bug_result(directory: Path, fault_class: str, sources: dict[str, tuple[bytes, str]] | None = None,
               fault_tests: set[str] | None = None) -> BugResult | None:
    """The measures of one bug. ``sources`` maps a scope file to (fixed bytes, buggy text) for the exact
    fault-reproduction check; without it that check is skipped."""
    bug = json.loads((directory / "bug.json").read_text(encoding="utf-8"))
    oracle_rows = _jsonl(directory / "oracle.jsonl")
    metas = [r for r in oracle_rows if r.get("kind") == "meta"]
    meta = metas[-1] if metas else None
    verdicts = {r["aid"]: r for r in oracle_rows if "aid" in r}
    confirm_path = directory / "oracle.confirm.jsonl"
    confirmations = {r["aid"]: r for r in _jsonl(confirm_path) if "aid" in r}
    if meta is None or "ineligible" in meta:
        return None  # an ineligible bug is excluded from the corpus and reported in the exclusion ledger
    scope = json.loads((directory / "scope.json").read_text(encoding="utf-8"))
    scope_lines = {path: {ln for start, end, *_ in regions for ln in range(start, end + 1)}
                   for path, regions in scope.items()}
    old_cov = {p: set(v) for p, v in meta.get("covered_lines", {}).items()}
    fix_cov = {p: set(v) for p, v in meta.get("fix_covered_lines", {}).items()}
    guided = any((fix_cov.get(p, set()) & lines) - old_cov.get(p, set()) for p, lines in scope_lines.items())
    fault_lines = {(path, ln) for path in scope for ln in changed_lines_in(bug.get("src_diff", ""), path)
                   if ln in fix_cov.get(path, set())}
    fault_covered = bool(fault_lines) and all(ln in old_cov.get(path, set()) for path, ln in fault_lines)
    buggy_canon = {p: _canon(text) for p, (_fixed, text) in (sources or {}).items()}
    changed = {p: changed_lines_in(bug.get("src_diff", ""), p) for p in (sources or {})}
    reproduced: dict[str, bool] = {}  # by artifact: identical mutated files of several tools are checked once

    def reproduces(row: dict[str, Any]) -> bool:
        if row["aid"] not in reproduced:
            reproduced[row["aid"]] = _reproduces(row, sources or {}, buggy_canon, changed)
        return reproduced[row["aid"]]

    tools: dict[str, ToolBug] = {}
    unresolved_aids: set[str] = set()
    for tool in (*TOOLS, *VARIANTS):
        base, drop = VARIANTS.get(tool, (tool, DEFAULT_DROP.get(tool, "")))
        rows = _jsonl(directory / f"{base}.jsonl")
        if drop:
            rows = [row for row in rows if not any(o.get("tool") == base and str(o.get("native_id", "")).startswith(drop)
                                                   for o in row.get("origins", []))]
        ledger_path = directory / f"{base}.ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"failed": True}
        if tool == "mutation":
            rows = [row for row in rows if not _gated(row, old_cov)]  # what the tool itself would generate
        lost = int(ledger.get("unmaterializable_in_scope", 0))  # emissions without a faithful text still cost
        tb = ToolBug(tool, available=not ledger.get("failed", False), emitted=len(rows) + lost,
                     unique=len({row["aid"] for row in rows}))
        witness_aids: set[str] = set()
        survivor_aids: set[str] = set()
        undecided_aids: set[str] = set()
        for aid in {row["aid"] for row in rows}:
            verdict = verdicts.get(aid, {"status": "missing"})
            tb.status[verdict["status"]] += 1
            if verdict["status"] == "survived":
                survivor_aids.add(aid)
                if verdict.get("witness"):
                    tb.first_witnesses += 1
                    second = confirmations.get(aid, {"status": "missing"})
                    if second["status"] in {"unresolved", "missing"} or (
                            second["status"] == "survived" and second.get("witness") is None):
                        tb.unknown_confirmations += 1
                        undecided_aids.add(aid)
                        continue
                    if not (second["status"] == "survived" and second.get("witness")):
                        tb.unconfirmed_witnesses += 1
                        continue
                    # The same fail-to-pass test red in both judgements, each time with an outcome of its own: an
                    # unattributed collection or startup failure marks every fail-to-pass test without any running.
                    red = _attributed_red(verdict) & _attributed_red(second)
                    if not red:
                        tb.synthetic_witnesses += 1
                        continue
                    tb.detected_any_fix_test = True
                    # A witness must be killed by a test of the fault itself, not by a test of a feature that
                    # the same commit added (``fault_tests`` from the purity labels; all tests when absent).
                    if fault_tests is None or red & fault_tests:
                        witness_aids.add(aid)
                    else:
                        tb.feature_only_witnesses += 1
                elif verdict.get("witness") is None and tb_has_fix(meta):
                    tb.unknown_witness += 1
                    undecided_aids.add(aid)
            elif verdict["status"] in {"unresolved", "missing"}:
                unresolved_aids.add(aid)
                undecided_aids.add(aid)  # an undecided historical outcome could still hide a witness
        tb.witness_emissions = sum(1 for row in rows if row["aid"] in witness_aids)
        tb.witnesses, tb.survivors, tb.undecided = len(witness_aids), len(survivor_aids), len(undecided_aids)
        tb.detected = tb.witnesses > 0
        tb.r = {q: hit_probability(tb.emitted, tb.witness_emissions, q) for q in Q_BUDGETS}
        tb.s = {k: hit_probability(tb.survivors, tb.witnesses, k) for k in K_BUDGETS}
        if sources and len(bug.get("src_files", [])) == 1:  # a deleted file is a changed file too
            tb.exact_fault = any(reproduces(row) for row in rows)
        tools[tool] = tb
    kinds = {"function" if region[2] == "function" else "statement" for regions in scope.values() for region in regions}
    first_witnesses = {aid for aid, v in verdicts.items() if v.get("status") == "survived" and v.get("witness")}
    return BugResult(bug["subject"], bug["commit"], fault_class, repair_shape(bug.get("src_diff", "")),
                     sum(len(v) for v in scope_lines.values()), fault_covered, guided, len(unresolved_aids), tools,
                     files=len(scope), region_kind=kinds.pop() if len(kinds) == 1 else "mixed",
                     confirmed=first_witnesses <= confirmations.keys(), fault_lines_run=bool(fault_lines))


def tb_has_fix(meta: dict[str, Any]) -> bool:
    return bool(meta.get("t_fix"))


SYNTHETIC_RED = {"collect", "startup"}  # the oracle's marks for a failure of the whole fail-to-pass run


def _attributed_red(verdict: dict[str, Any]) -> set[str]:
    """The fail-to-pass tests a judgement recorded red with an outcome of their own (failed, errored or crashed when
    they ran in the fail-to-pass run), not through a collection or startup failure of the whole run."""
    return {test for test, outcome in verdict.get("fix_red", {}).items() if outcome[0] not in SYNTHETIC_RED}


def _gated(row: dict[str, Any], old_cov: dict[str, set[int]]) -> bool:
    """A ``mutation`` emission the tool itself would not produce: its node line was never executed."""
    lines = [o.get("gate_line") for o in row.get("origins", []) if o.get("tool") == "mutation" and "gate_line" in o]
    return bool(lines) and all(line not in old_cov.get(row["path"], set()) for line in lines)


def _canon(source: str) -> str | None:
    try:
        return ast.dump(ast.parse(source), include_attributes=False)
    except SyntaxError:
        return None


def _reproduces(row: dict[str, Any], sources: dict[str, tuple[bytes, str]], buggy: dict[str, str | None],
                changed: dict[str, set[int]]) -> bool:
    """The mutant re-creates the real fault where the fault is: its edit touches a line the fix changed
    (``changed``, fixed-file lines) and turns the fixed file into the buggy one (equal ASTs). The line test comes
    first because parsing and dumping a whole file per mutant dominates the report's cost."""
    if row["path"] not in sources or buggy.get(row["path"]) is None:
        return False
    fixed, _ = sources[row["path"]]
    edit = row["edit"]
    first = fixed.count(b"\n", 0, edit["start"]) + 1
    last = fixed.count(b"\n", 0, max(edit["start"], edit["end"] - 1)) + 1
    if not any(first <= line <= last for line in changed.get(row["path"], ())):
        return False
    mutated = fixed[: edit["start"]] + edit["replacement"].encode("utf-8") + fixed[edit["end"] :]
    try:
        return _canon(mutated.decode("utf-8")) == buggy[row["path"]]
    except UnicodeDecodeError:
        return False


def collect(out_dir: Path, labels: dict[str, str]) -> list[BugResult]:
    results = []
    for bug_json in sorted(out_dir.glob("gen/*/*/bug.json")):
        directory = bug_json.parent
        cid = f"{directory.parent.name}@{directory.name}"
        result = bug_result(directory, labels.get(cid, "unlabelled"))
        if result is not None:
            results.append(result)
    return results


def to_json(results: list[BugResult]) -> list[dict[str, Any]]:
    rows = []
    for result in results:
        row = asdict(result)
        row["tools"] = {name: {**asdict(tb), "status": dict(tb.status)} for name, tb in result.tools.items()}
        rows.append(row)
    return rows
