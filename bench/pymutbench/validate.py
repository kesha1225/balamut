"""Mine and validate real-fault candidates of one subject (runs inside the subject's container image).

A candidate is a first-parent commit that changes at most ``--max-src-files`` source files of the subject's
packages by at most ``--max-src-lines`` lines and also changes at least one test file. Validation follows the
Defects4J / SWE-bench recipe on CPython 3.14 (see ``gitops`` for the four trees):

1. the fix's changed test files run on ``fixed`` and ``buggy``; FAIL_TO_PASS = passed on ``fixed`` and red on
   ``buggy``;
2. each fail-to-pass test's failure on ``buggy`` is classified (``assert``, ``exception``, ``timeout``,
   ``api`` for missing names and changed signatures, ``collect`` for a module that no longer imports); only the
   behavioural classes form ``T_fix`` — a test failing only because the fixed API did not exist yet tests a
   feature, not a fault;
3. ``T_fix`` is re-run twice more on both trees and must keep its outcome;
4. the historical suite runs three times on ``old`` and once on ``parent``; ``T_old`` is the set of tests
   green in all four runs. An old test green on ``old`` but red on ``parent`` means the historical suite
   already detects the fault on CPython 3.14, and the candidate is rejected.

Every candidate yields one JSON line: admitted with its tests and fix statistics, or rejected with the stage
and the reason (the exclusion ledger).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.gitops import (  # noqa: E402
    FileChange,
    add_worktree,
    changed_files,
    diff_text,
    git,
    is_source_path,
    is_test_path,
    remove_worktree,
    take_files,
)
from pymutbench.pytestrun import GREEN, PytestEnv, RunResult, TestOutcome, run_pytest  # noqa: E402
from pymutbench.subjects import SUBJECTS, Subject  # noqa: E402

VENV_PYTHON = "/work/venv/bin/python"
_SIGNATURE = re.compile(r"unexpected keyword argument|positional argument|takes \d+ |got multiple values|"
                        r"missing \d+ required|takes no arguments")


def import_roots(tree: Path, subject: Subject) -> tuple[str, ...]:
    src = tree / "src"
    if src.is_dir() and any((src / pkg).exists() or (src / f"{pkg}.py").exists() for pkg in subject.packages):
        return (str(src),)
    return (str(tree),)


# The compatibility policy, the same for every subject: a historical test written against an older pytest may
# trigger pytest's own removal warnings, which a project's `filterwarnings = error` turns into collection errors.
COMPAT_ARGS = ("-W", "ignore::pytest.PytestRemovedIn10Warning")


def env_for(tree: Path, subject: Subject) -> PytestEnv:
    return PytestEnv(VENV_PYTHON, import_roots(tree, subject), subject.packages, (*COMPAT_ARGS, *subject.pytest_args))


def write_generated(tree: Path, subject: Subject) -> None:
    for rel, text in subject.generated:
        path = tree / rel
        if path.parent.exists() and not path.exists():
            path.write_text(text, encoding="utf-8")


def origin_ok(result: RunResult, tree: Path) -> bool:
    """Every package of the subject that the run imported came from inside ``tree`` (resolved paths)."""
    root = os.path.realpath(tree)
    for origin in result.origins.values():
        if not origin:
            continue
        path = os.path.realpath(str(origin).strip("[]'\""))
        if os.path.commonpath([root, path]) != root:
            return False
    return True


def failure_class(test: TestOutcome | None, collect_error: bool) -> str:
    if test is None:
        return "collect" if collect_error else "missing"
    if test.outcome == "timeout":
        return "timeout"
    if test.outcome == "crashed":
        return "crash"
    exc = test.exc_type
    if exc in {"ImportError", "ModuleNotFoundError", "NameError"}:
        return "api"
    if exc == "TypeError" and _SIGNATURE.search(test.exc_msg):
        return "api"
    if exc == "AttributeError" and "has no attribute" in test.exc_msg:
        return "attribute"  # a missing attribute is often a new API; adjudicated by review, not admitted blindly
    if exc in {"AssertionError", "Failed"}:
        return "assert"
    return "exception"


BEHAVIOURAL = frozenset({"assert", "exception", "timeout"})


def candidates(repo: Path, subject: Subject, max_src_files: int, max_src_lines: int) -> list[dict[str, object]]:
    rows = []
    log = git(repo, "log", "--first-parent", f"--since={subject.since}", "--format=%H %P%x09%aI%x09%s", "HEAD")
    for line in log.splitlines():
        head, date, subject_line = line.split("\t", 2)
        commit, *parents = head.split()
        if not parents:
            continue
        parent = parents[0]
        changes = changed_files(repo, parent, commit)
        src = [c for c in changes if is_source_path(c.path, subject.packages) and c.path not in subject.exclude_src]
        tests = [c for c in changes if is_test_path(c.path) and c.status != "D"]
        churn = sum(c.added + c.removed for c in src)
        if src and tests and len(src) <= max_src_files and churn <= max_src_lines:
            rows.append({"commit": commit, "parent": parent, "date": date, "title": subject_line})
    return rows


def _outcomes(result: RunResult) -> dict[str, str]:
    return {nodeid: test.outcome for nodeid, test in result.tests.items()}


def validate(repo: Path, work: Path, subject: Subject, cand: dict[str, object]) -> dict[str, object]:
    commit, parent = str(cand["commit"]), str(cand["parent"])
    record: dict[str, object] = {"subject": subject.name, **cand}
    changes = changed_files(repo, parent, commit)
    src = [c for c in changes if is_source_path(c.path, subject.packages) and c.path not in subject.exclude_src]
    test_files = [c.path for c in changes if is_test_path(c.path) and c.status != "D" and not c.path.endswith("conftest.py")]
    record["src_files"] = [{"path": c.path, "status": c.status, "added": c.added, "removed": c.removed} for c in src]
    record["test_files"] = test_files
    record["src_diff"] = diff_text(repo, parent, commit, [c.path for c in src])[:20000]
    base = work / commit[:12]
    trees = {name: base / name for name in ("fixed", "buggy", "old", "parent")}
    started = time.monotonic()
    try:
        add_worktree(repo, trees["fixed"], commit)
        add_worktree(repo, trees["buggy"], commit)
        take_files(trees["buggy"], parent, src, from_side="parent")
        for tree in (trees["fixed"], trees["buggy"]):
            write_generated(tree, subject)
        result = _validate_trees(repo, subject, record, trees, src, test_files, parent, commit)
    except Exception as exc:  # noqa: BLE001 - the ledger records harness failures instead of losing the candidate
        result = {**record, "admitted": False, "stage": "harness", "reason": f"{type(exc).__name__}: {exc}"[:500]}
    finally:
        for tree in trees.values():
            remove_worktree(repo, tree)
        shutil.rmtree(base, ignore_errors=True)
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def _validate_trees(
    repo: Path,
    subject: Subject,
    record: dict[str, object],
    trees: dict[str, Path],
    src: list[FileChange],
    test_files: list[str],
    parent: str,
    commit: str,
) -> dict[str, object]:
    fixed, buggy = trees["fixed"], trees["buggy"]
    targets = [path for path in test_files if (fixed / path).exists()]
    res_fixed = run_pytest(env_for(fixed, subject), fixed, targets, run_timeout=900)
    if not res_fixed.finished or not origin_ok(res_fixed, fixed):
        return {**record, "admitted": False, "stage": "fixed_run", "reason": _run_problem(res_fixed, fixed)}
    res_buggy = run_pytest(env_for(buggy, subject), buggy, targets, run_timeout=900)
    if res_buggy.finished and not origin_ok(res_buggy, buggy):
        return {**record, "admitted": False, "stage": "buggy_run", "reason": _run_problem(res_buggy, buggy)}
    passed_fixed = {n for n, t in res_fixed.tests.items() if t.outcome == "passed"}
    errored_files = {n.split("::")[0] for n in res_buggy.collect_errors}
    f2p: dict[str, dict[str, str]] = {}
    for nodeid in sorted(passed_fixed):
        test = res_buggy.tests.get(nodeid)
        if test is not None and test.outcome in GREEN:
            continue
        cls = failure_class(test, nodeid.split("::")[0] in errored_files or bool(res_buggy.collect_errors))
        f2p[nodeid] = {"class": cls, "exc_type": test.exc_type if test else "", "exc_msg": test.exc_msg if test else ""}
    record["fail_to_pass_all"] = f2p
    t_fix = sorted(n for n, info in f2p.items() if info["class"] in BEHAVIOURAL)
    if not f2p:
        return {**record, "admitted": False, "stage": "fail_to_pass", "reason": "no_fail_to_pass"}
    if not t_fix:
        classes = sorted({info["class"] for info in f2p.values()})
        return {**record, "admitted": False, "stage": "fail_to_pass", "reason": "non_behavioural:" + ",".join(classes)}
    for _ in range(2):
        again_fixed = run_pytest(env_for(fixed, subject), fixed, t_fix, run_timeout=900)
        again_buggy = run_pytest(env_for(buggy, subject), buggy, t_fix, run_timeout=900)
        if not (again_fixed.finished and origin_ok(again_fixed, fixed) and origin_ok(again_buggy, buggy)):
            return {**record, "admitted": False, "stage": "stability", "reason": "rerun_unfinished_or_origin"}
        # A fail-to-pass test stays only if it passes again on fixed and fails again on buggy with the same
        # behavioural class (an assertion that turns into a missing-API error is not the same failure).
        t_fix = [n for n in t_fix if _outcomes(again_fixed).get(n) == "passed"
                 and n in again_buggy.tests and again_buggy.tests[n].outcome not in GREEN
                 and failure_class(again_buggy.tests[n], False) == f2p[n]["class"]]
    if not t_fix:
        return {**record, "admitted": False, "stage": "stability", "reason": "unstable_fail_to_pass"}
    record["t_fix"] = t_fix
    add_worktree(repo, trees["old"], parent)
    take_files(trees["old"], commit, src, from_side="fix")
    add_worktree(repo, trees["parent"], parent)
    for tree in (trees["old"], trees["parent"]):
        write_generated(tree, subject)
    old, par = trees["old"], trees["parent"]
    roots_old = [root for root in subject.tests if (old / root).exists()]
    roots_par = [root for root in subject.tests if (par / root).exists()]
    if not roots_old or roots_old != roots_par:
        return {**record, "admitted": False, "stage": "old_suite", "reason": f"test_roots {roots_old} {roots_par}"}
    old_runs = [run_pytest(env_for(old, subject), old, roots_old, run_timeout=1800) for _ in range(3)]
    par_run = run_pytest(env_for(par, subject), par, roots_par, run_timeout=1800)
    for run, tree in [*((r, old) for r in old_runs), (par_run, par)]:
        if not run.finished or not origin_ok(run, tree):
            return {**record, "admitted": False, "stage": "old_suite", "reason": _run_problem(run, tree)}
        if run.collect_errors:
            first = sorted(run.collect_errors)[0]
            return {**record, "admitted": False, "stage": "old_suite",
                    "reason": "collect_errors:" + ",".join(sorted(run.collect_errors))[:300]
                    + " | " + run.collect_errors[first][-600:]}
    green_old = set.intersection(*(run.green() for run in old_runs))
    detects = sorted(n for n in green_old if n in par_run.tests and par_run.tests[n].outcome not in GREEN)
    if detects:
        return {**record, "admitted": False, "stage": "old_suite", "reason": "old_suite_detects_fault",
                "detecting_old_tests": detects[:20]}
    t_old = sorted(green_old & par_run.green())
    if not t_old:
        return {**record, "admitted": False, "stage": "old_suite", "reason": "empty_historical_suite"}
    collected = len(old_runs[0].tests)
    record.update(
        admitted=True,
        stage="admitted",
        t_old_size=len(t_old),
        t_old=t_old,
        t_old_excluded=sorted(set().union(*(set(r.tests) for r in old_runs), par_run.tests) - set(t_old)),
        old_collected=collected,
        old_suite_seconds=round(sum(run.wall for run in old_runs) / len(old_runs), 2),
        python=sys.version.split()[0],
    )
    return record


def _run_problem(result: RunResult, tree: Path) -> str:
    if not result.finished:
        return f"unfinished rc={result.returncode} hung={result.hung} crashed={result.crashed} {result.stderr_tail[-300:]}"
    if not origin_ok(result, tree):
        return f"import_origin {result.origins}"
    return f"exitstatus={result.exitstatus} collect_errors={list(result.collect_errors)[:5]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True, choices=sorted(SUBJECTS))
    parser.add_argument("--repo", default="/work/repo")
    parser.add_argument("--work", default="/work/trees")
    parser.add_argument("--out", required=True)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--max-src-files", type=int, default=3)
    parser.add_argument("--max-src-lines", type=int, default=40)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only", default="", help="comma-separated commit prefixes")
    parser.add_argument("--list", action="store_true", help="write the candidate list and exit")
    args = parser.parse_args()
    subject = SUBJECTS[args.subject]
    repo, work = Path(args.repo), Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    rows = candidates(repo, subject, args.max_src_files, args.max_src_lines)
    out = Path(args.out)
    # Every candidate in mainline order, newest first: validation follows this order, and the corpus selection
    # checks against it that no candidate ahead of a selected bug is left unprocessed.
    listing = out.with_name(f"{out.stem}.candidates.jsonl")
    listing.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    if args.list:
        return
    if args.only:
        prefixes = tuple(args.only.split(","))
        rows = [row for row in rows if str(row["commit"]).startswith(prefixes)]
    if args.limit:
        rows = rows[: args.limit]
    done = set()
    if out.exists():
        done = {json.loads(line)["commit"] for line in out.read_text(encoding="utf-8").splitlines() if line.strip()}
    rows = [row for row in rows if row["commit"] not in done]
    print(f"{subject.name}: {len(rows)} candidates to validate ({len(done)} already done)", flush=True)
    with ThreadPoolExecutor(args.jobs) as pool, out.open("a", encoding="utf-8") as fh:
        for record in pool.map(lambda row: validate(repo, work, subject, row), rows):
            fh.write(json.dumps(record) + "\n")  # ASCII-escaped: one record per line
            fh.flush()
            mark = "ADMIT" if record.get("admitted") else f"reject[{record.get('stage')}:{str(record.get('reason'))[:60]}]"
            print(f"{str(record['commit'])[:10]} {record.get('seconds', '?')}s {mark}", flush=True)


if __name__ == "__main__":
    main()
