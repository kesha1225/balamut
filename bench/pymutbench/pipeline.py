"""Host-side driver of the real-fault track: select bugs, generate every tool's mutants, merge, judge.

    python3 -m pymutbench.pipeline select            # admitted + reviewed bugs -> $PMB_OUT_DIR/bugs/<subject>.jsonl
    python3 -m pymutbench.pipeline generate [SUBJECT ...]
    python3 -m pymutbench.pipeline judge [SUBJECT ...] --parallel 3 --jobs 4

Generation runs in the tools image, judging in each subject's image; both without network. Every stage is
resumable: a bug whose outputs exist is skipped.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.merge import merge_bug  # noqa: E402
from pymutbench.orchestrate import BENCH, OUT, WORK, docker_run, subject_image  # noqa: E402
from pymutbench.subjects import SUBJECTS  # noqa: E402

TOOLS_IMAGE = "pymutbench-tools:1"
PER_SUBJECT_CAP = 15  # predeclared: at most this many bugs per project, newest first in mainline order


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def reviewed_bugs() -> dict[str, str]:
    """subject@commit12 -> final adjudicated label (``bug`` admits); empty until the review stage has run."""
    path = OUT / "review" / "final.jsonl"
    return {row["id"]: row["label"] for row in load_jsonl(path)}


def provisional_labels() -> dict[str, str]:
    """A bug when either primary rater says bug, a superset of the adjudicated bugs: used only to start the
    expensive stages early; the report admits final labels only."""
    labels: dict[str, str] = {}
    for rater in ("claude", "claude_c"):
        for path in sorted((OUT / "review" / rater).glob("batch-*.json")):
            for item in json.loads(path.read_text(encoding="utf-8")).get("labels", []):
                if labels.get(item["id"]) != "bug":
                    labels[item["id"]] = "bug" if item["label"] == "bug" else "rejected"
    return labels


def select(cap: int, validation: str = "validate3", screening: str = "validate", provisional: bool = False) -> None:
    """Per subject, the first ``cap`` candidates in mainline order (newest first) that the screening validation
    admits, the review labels a bug and the final validation admits again.

    Validation, review and re-validation proceed in the same order, so a subject may stop early: the selection is
    complete when every candidate ahead of the last selected bug (every candidate, while the cap is not reached)
    is validated, adjudicated when admitted and re-validated when labelled a bug. ``bugs/selection.json``
    records the counts and every candidate that leaves a selection incomplete.
    """
    labels = provisional_labels() if provisional else reviewed_bugs()
    if not labels:
        raise SystemExit("no final review labels (review/final.jsonl): a bug is admitted only by adjudication")
    out_dir = OUT / "bugs"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"cap": cap, "screening": screening, "validation": validation,
                               "provisional": provisional, "subjects": {}}
    total = 0
    for name in sorted(SUBJECTS):
        order = load_jsonl(OUT / screening / f"{name}.candidates.jsonl")
        if not order:
            raise SystemExit(f"{name}: no candidate list ({screening}/{name}.candidates.jsonl): run validate-all --list")
        screened = {r["commit"]: r for r in load_jsonl(OUT / screening / f"{name}.jsonl")}
        final = {r["commit"]: r for r in load_jsonl(OUT / validation / f"{name}.jsonl")}
        chosen: list[dict[str, Any]] = []
        open_: list[str] = []
        counts = {"candidates": len(order), "screened": 0, "admitted": 0, "adjudicated": 0, "bugs": 0,
                  "revalidated": 0, "readmitted": 0}
        for cand in order:
            commit = cand["commit"]
            label = labels.get(f"{name}@{commit[:12]}")
            first, second = screened.get(commit), final.get(commit)
            admitted = bool(first and first.get("admitted"))
            bug = admitted and label == "bug"
            counts["screened"] += first is not None
            counts["admitted"] += admitted
            counts["adjudicated"] += admitted and label in {"bug", "rejected"}
            counts["bugs"] += bug
            counts["revalidated"] += bug and second is not None
            counts["readmitted"] += bool(bug and second and second.get("admitted"))
            if len(chosen) == cap:
                continue  # past the cap: counted only
            if first is None:
                open_.append(f"{commit[:12]} not validated")
            elif not admitted or label == "rejected":
                continue
            elif label != "bug":
                open_.append(f"{commit[:12]} not adjudicated")
            elif second is None:
                open_.append(f"{commit[:12]} not re-validated")
            elif second.get("admitted"):
                chosen.append(second)
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as fh:
            for row in chosen:
                fh.write(json.dumps(row) + "\n")
        summary["subjects"][name] = {**counts, "selected": len(chosen), "complete": not open_, "open": open_}
        total += len(chosen)
        state = "complete" if not open_ else f"INCOMPLETE ({len(open_)} open: {', '.join(open_[:3])})"
        print(f"{name:<18} candidates={len(order):>4} screened={counts['screened']:>4} admitted={counts['admitted']:>3} "
              f"bugs={counts['bugs']:>3} readmitted={counts['readmitted']:>3} selected={len(chosen):>3} {state}")
    (out_dir / "selection.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("total", total)


def generate(name: str, tools: str = "", force: bool = False) -> int:
    bugs = OUT / "bugs" / f"{name}.jsonl"
    wanted = [t for t in tools.split(",") if t] or list(NATIVE_TOOLS)
    todo = [b for b in load_jsonl(bugs) if force or not all(  # a bug is complete when every tool has its ledger
        (OUT / "gen" / name / b["commit"][:12] / f"{t}.ledger.json").exists() for t in wanted)]
    if not todo:
        return 0
    pending = OUT / "gen" / name / "pending.jsonl"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("".join(json.dumps(b) + "\n" for b in todo), encoding="utf-8")
    repo = WORK / "repos" / name
    cmd = [*docker_run(TOOLS_IMAGE, f"{repo}:/repos/{name}:ro"), "bash", "-c",
           f"git clone -q /repos/{name} /work/repo && python3 /bench/pymutbench/generate.py "
           f"--bugs /out/gen/{name}/pending.jsonl --out /out/gen/{name}" + (f" --tools {tools}" if tools else "")]
    with (OUT / "gen" / name / "generate.log").open("a", encoding="utf-8") as log:
        code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    for bug in todo:
        directory = OUT / "gen" / name / bug["commit"][:12]
        if directory.exists():
            (directory / "bug.json").write_text(json.dumps(bug, ensure_ascii=False), encoding="utf-8")
            merge_bug(directory)
    return code


def judge(name: str, jobs: int) -> int:
    image = subject_image(name)
    code = 0
    for bug in load_jsonl(OUT / "bugs" / f"{name}.jsonl"):
        directory = OUT / "gen" / name / bug["commit"][:12]
        union, result = directory / "union.jsonl", directory / "oracle.jsonl"
        if not union.exists() or (result.exists() and _complete(union, result)):
            continue
        rel = directory.relative_to(OUT)
        cmd = [*docker_run(image), "python3", "/bench/pymutbench/oracle.py", "--subject", name,
               "--bug", f"/out/{rel}/bug.json", "--mutants", f"/out/{rel}/union.jsonl",
               "--out", f"/out/{rel}/oracle.jsonl", "--jobs", str(jobs)]
        with (directory / "oracle.log").open("a", encoding="utf-8") as log:
            code |= subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    return code


SENSITIVITY_SEED = 20261105


def sensitivity(sample: int, jobs: int, parallel: int) -> None:
    """The oracle's shortcuts against the canonical suite alone: a seeded uniform sample of the mutants that
    stage 0 or 1 killed (a single test or the covering subset), judged again with ``--full-only`` (the whole
    historical suite in canonical order decides). Writes ``oracle.full.jsonl`` per sampled bug, ``parallel``
    bugs at a time: the sample spreads over most bugs, and each needs its own baselines. Only the artifacts of
    the current unions are drawn: a verdict file keeps the kills of artifacts that only an earlier adapter version
    generated, and those cannot be judged again."""
    import random  # noqa: PLC0415

    killed: list[tuple[str, str, str]] = []  # (subject, commit12, aid)
    for name in sorted(SUBJECTS):
        for bug in load_jsonl(OUT / "bugs" / f"{name}.jsonl"):
            directory = OUT / "gen" / name / bug["commit"][:12]
            current = {row["aid"] for row in load_jsonl(directory / "union.jsonl")}
            killed += sorted((name, bug["commit"][:12], row["aid"]) for row in load_jsonl(directory / "oracle.jsonl")
                             if row.get("status") == "killed" and row.get("stage") in {"killer", "subset"}
                             and row["aid"] in current)
    chosen = random.Random(SENSITIVITY_SEED).sample(killed, min(sample, len(killed)))
    manifest = {"seed": SENSITIVITY_SEED, "population": len(killed), "sample": sorted(chosen)}
    (OUT / "bugs" / "sensitivity.json").write_text(json.dumps(manifest), encoding="utf-8")
    by_bug: dict[tuple[str, str], set[str]] = {}
    for name, commit12, aid in chosen:
        by_bug.setdefault((name, commit12), set()).add(aid)
    def judge_sample(item: tuple[tuple[str, str], set[str]]) -> None:
        (name, commit12), aids = item
        directory = OUT / "gen" / name / commit12
        sampled = directory / "sensitivity.union.jsonl"
        sampled.write_text("".join(line + "\n" for line in (directory / "union.jsonl").read_text(encoding="utf-8")
                                   .splitlines() if line.strip() and json.loads(line)["aid"] in aids), encoding="utf-8")
        rel = directory.relative_to(OUT)
        cmd = [*docker_run(subject_image(name)), "python3", "/bench/pymutbench/oracle.py", "--subject", name,
               "--bug", f"/out/{rel}/bug.json", "--mutants", f"/out/{rel}/sensitivity.union.jsonl",
               "--out", f"/out/{rel}/oracle.full.jsonl", "--jobs", str(jobs), "--full-only"]
        with (directory / "oracle.log").open("a", encoding="utf-8") as log:
            subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False)
        print(f"sensitivity {name}@{commit12}: {len(aids)} mutants", flush=True)

    with ThreadPoolExecutor(parallel) as pool:
        list(pool.map(judge_sample, sorted(by_bug.items())))


def confirm(name: str, jobs: int) -> int:
    """Every witness of a judged bug judged again from fresh trees by the canonical suite alone, then the
    fail-to-pass tests (``oracle.confirm.jsonl``): a witness counts only when this second judgement repeats it."""
    image = subject_image(name)
    code = 0
    for bug in load_jsonl(OUT / "bugs" / f"{name}.jsonl"):
        directory = OUT / "gen" / name / bug["commit"][:12]
        rows = load_jsonl(directory / "oracle.jsonl")
        witnesses = {r["aid"] for r in rows if r.get("status") == "survived" and r.get("witness")}
        done = {r["aid"] for r in load_jsonl(directory / "oracle.confirm.jsonl") if "aid" in r}
        if not witnesses or witnesses <= done:
            continue
        rel = directory.relative_to(OUT)
        cmd = [*docker_run(image), "python3", "/bench/pymutbench/oracle.py", "--subject", name,
               "--bug", f"/out/{rel}/bug.json", "--mutants", f"/out/{rel}/union.jsonl",
               "--witnesses-of", f"/out/{rel}/oracle.jsonl", "--full-only",
               "--out", f"/out/{rel}/oracle.confirm.jsonl", "--jobs", str(jobs)]
        with (directory / "oracle.log").open("a", encoding="utf-8") as log:
            code |= subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    return code


NATIVE_TOOLS = ("balamut", "mutmut", "cosmic-ray", "poodle", "pytest-gremlins", "pytest-leela", "mutation",
                "universalmutator", "fest", "irradiate", "deployproof")


def native_campaigns(image: str, root: str, files: list[str], tests: list[str], out_dir: Path, tag: str,
                     tools: tuple[str, ...] = NATIVE_TOOLS, limit: int = 7200) -> None:
    """One fresh container per tool campaign, strictly one at a time (the timings must not share the machine);
    ``limit`` is the seconds one command of a campaign may run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        target, failed = out_dir / f"{tool}.{tag}.json", out_dir / f"{tool}.{tag}.failed.json"
        # A recorded failure is as final as a report: a rerun of the stage never repeats a campaign (deleting the
        # file does).
        if target.exists() or failed.exists():
            continue
        rel = target.relative_to(OUT)
        cmd = [*docker_run(image, engine=True), "python3", "/bench/pymutbench/native.py", "--tool", tool,
               "--root", root, "--files", ",".join(files), "--tests", ",".join(tests), "--out", f"/out/{rel}",
               "--limit", str(limit)]
        with (out_dir / "native.log").open("a", encoding="utf-8") as log:
            try:
                code: int | str = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False,
                                                 timeout=4 * 3600).returncode
            except subprocess.TimeoutExpired:
                # Campaigns run one at a time: the image's only running container is this campaign's.
                running = subprocess.run(["docker", "ps", "-q", "--filter", f"ancestor={image}"], capture_output=True,
                                         text=True, check=False).stdout.split()
                for container in running:
                    subprocess.run(["docker", "kill", container], capture_output=True, check=False)
                code = "timeout after 4 h"
        # A campaign that leaves no report is a result too: the report shows it as failed, with the exit status.
        if not target.exists():
            failed.write_text(json.dumps({"tool": tool, "returncode": code, "log": "native.log"}), encoding="utf-8")


def union_of_native(out_dir: Path, tag: str) -> Path:
    from pymutbench.artifacts import Artifact, Edit, merge, write_jsonl  # noqa: PLC0415

    artifacts = []
    for path in sorted(out_dir.glob(f"*.{tag}.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        native = {row["aid"]: row for row in data.get("mutants", []) if row.get("aid")}
        for art in data.get("artifacts", []):
            if art["aid"] in native:
                origin = {"tool": data["tool"], "native_id": str(native[art["aid"]]["native_id"])}
                artifacts.append(Artifact(art["path"], Edit(**art["edit"]), art["aid"], [origin]))
    union = out_dir / f"union.{tag}.jsonl"
    write_jsonl(union, merge(artifacts))
    return union


def judged(result: Path, union: Path) -> bool:
    """Whether the reference run judges every artifact of the union. A campaign repeated after its record was
    deleted can add artifacts; the suite-mode oracle has no resume, so such a union is judged again whole."""
    verdicts = {row["aid"] for row in load_jsonl(result) if "aid" in row}
    return result.exists() and {row["aid"] for row in load_jsonl(union)} <= verdicts


# The engine track: subjects at HEAD, the files every campaign mutates, the test roots, the import root.
ENGINE_SUBJECTS: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "python-semver": (("src/semver/version.py",), ("tests",), "src"),
    "cachetools": (("src/cachetools/__init__.py",), ("tests",), "src"),
    "humanize": (("src/humanize/number.py",), ("tests",), "src"),
    "toolz": (("toolz/itertoolz.py",), ("toolz/tests",), "."),
    "sqlparse": (("sqlparse/utils.py", "sqlparse/filters/others.py"), ("tests",), "."),
    "attrs": (("src/attr/validators.py", "src/attr/converters.py"), ("tests",), "src"),
}


def engine(names: list[str], reps: int, jobs: int) -> None:
    from pymutbench.orchestrate import native_image  # noqa: PLC0415

    for name in names:
        files, tests, import_root = ENGINE_SUBJECTS[name]
        image = native_image(name)
        out_dir = OUT / "engine" / name
        for rep in range(1, reps + 1):
            native_campaigns(image, "/work/run", list(files), list(tests), out_dir, f"r{rep}", tools=NATIVE_TOOLS)
        union = union_of_native(out_dir, "r1")
        result = out_dir / "oracle.r1.jsonl"
        if not judged(result, union):
            rel_u, rel_r = union.relative_to(OUT), result.relative_to(OUT)
            packages = ",".join(SUBJECTS[name].packages)
            cmd = [*docker_run(image), "python3", "/bench/pymutbench/oracle.py", "--suite-root", "/work/run",
                   "--python",
                   "/work/venv/bin/python", "--import-root", import_root, "--packages", packages,
                   "--tests", ",".join(tests), "--mutants",
                   f"/out/{rel_u}", "--out", f"/out/{rel_r}", "--jobs", str(jobs), "--work", "/work/oracle"]
            with (out_dir / "oracle.log").open("a", encoding="utf-8") as log:
                code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
            if code:
                # An oracle that did not finish (137: the out-of-memory killer took the oracle process itself)
                # leaves artifacts without a verdict; the next run of the stage judges the union again whole.
                print(f"engine {name}: reference oracle exited with status {code}", flush=True)
        print(f"engine {name}: done", flush=True)


def conformance(jobs: int) -> None:
    from pymutbench.orchestrate import CONFORMANCE_IMAGE, fixtures  # noqa: PLC0415

    for name in fixtures():
        module = next((BENCH / "conformance" / name / "src" / name).glob("[!_]*.py")).name
        files = [f"src/{name}/{module}"]
        out_dir = OUT / "conformance" / name
        # A trap fixture's suite runs in under a second: a command still running after 10 minutes is hung.
        native_campaigns(CONFORMANCE_IMAGE, f"/work/cf/{name}", files, ["tests"], out_dir, "r1", limit=600)
        union = union_of_native(out_dir, "r1")
        result = out_dir / "oracle.r1.jsonl"
        if judged(result, union):
            continue
        rel_u, rel_r = union.relative_to(OUT), result.relative_to(OUT)
        cmd = [*docker_run(CONFORMANCE_IMAGE), "python3", "/bench/pymutbench/oracle.py",
               "--suite-root", f"/work/cf/{name}",
               "--python", "/work/venv/bin/python", "--import-root", "src", "--packages", name, "--tests", "tests",
               "--mutants", f"/out/{rel_u}", "--out", f"/out/{rel_r}", "--jobs", str(jobs), "--work", "/work/oracle"]
        with (out_dir / "oracle.log").open("a", encoding="utf-8") as log:
            code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
        if code:
            print(f"conformance {name}: reference oracle exited with status {code}", flush=True)
        print(f"conformance {name}: done", flush=True)


UNFINISHED_BASELINE = "historical baseline: finished=False"
BASELINE_ATTEMPTS = 5


def _complete(union: Path, result: Path) -> bool:
    rows = load_jsonl(result)
    metas = [row for row in rows if row.get("kind") == "meta"]
    if metas and "ineligible" in metas[-1]:
        # The oracle refused the unmutated baseline: no further mutant of the bug is judged. A historical baseline
        # that left no end record is tried again, up to BASELINE_ATTEMPTS runs: the line recorder can fail after
        # the last test when a test leaves a thread running, which says nothing about the bug.
        unfinished = sum(1 for meta in metas if str(meta.get("ineligible", "")).startswith(UNFINISHED_BASELINE))
        return not (str(metas[-1]["ineligible"]).startswith(UNFINISHED_BASELINE) and unfinished < BASELINE_ATTEMPTS)
    artifacts = load_jsonl(union)
    status = {row["aid"]: row.get("status") for row in rows if "aid" in row}  # the latest verdict of each artifact
    if not {art["aid"] for art in artifacts} <= status.keys():
        return False
    # The oracle judges a gated-out artifact again once its gate no longer applies (``Oracle.gated_out``): a newer
    # tool generation can add an origin to it without adding an artifact to the union.
    covered = metas[-1].get("covered_lines", {}) if metas else {}
    return not any(status[art["aid"]] == "gated_out" and _gate_lifted(art, covered.get(art["path"], []))
                   for art in artifacts)


def _gate_lifted(art: dict[str, Any], covered: list[int]) -> bool:
    """True when some origin is not coverage-gated, or its gate line ran in the historical suite."""
    lines = set(covered)
    return any("gate_line" not in origin or origin["gate_line"] in lines for origin in art["origins"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("conformance")
    p.add_argument("--jobs", type=int, default=2)
    p = sub.add_parser("sensitivity", help="full-suite-only re-judging of a seeded sample of stage 0/1 kills")
    p.add_argument("--sample", type=int, default=1000)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--parallel", type=int, default=3, help="bugs judged at a time")
    p = sub.add_parser("engine")
    p.add_argument("names", nargs="*")
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--jobs", type=int, default=4)
    p = sub.add_parser("select")
    p.add_argument("--cap", type=int, default=PER_SUBJECT_CAP)
    p.add_argument("--validation", default="validate3", help="the final validation stage directory")
    p.add_argument("--screening", default="validate", help="the screening validation stage directory")
    p.add_argument("--provisional", action="store_true", help="rater B's labels, to start generation early")
    for name in ("generate", "judge", "confirm"):
        p = sub.add_parser(name)
        p.add_argument("names", nargs="*")
        p.add_argument("--parallel", type=int, default=2)
        p.add_argument("--jobs", type=int, default=4)
        p.add_argument("--tools", default="", help="generate: only these tools (comma-separated)")
        p.add_argument("--force", action="store_true", help="generate: regenerate bugs that already have output")
    args = parser.parse_args()
    if args.cmd == "select":
        select(args.cap, args.validation, args.screening, args.provisional)
        return
    if args.cmd == "conformance":
        conformance(args.jobs)
        return
    if args.cmd == "sensitivity":
        sensitivity(args.sample, args.jobs, args.parallel)
        return
    if args.cmd == "engine":
        engine(args.names or list(ENGINE_SUBJECTS), args.reps, args.jobs)
        return
    names = args.names or sorted(SUBJECTS)
    with ThreadPoolExecutor(args.parallel) as pool:
        if args.cmd == "generate":
            results = pool.map(lambda n: generate(n, args.tools, args.force), names)
        elif args.cmd == "judge":
            results = pool.map(lambda n: judge(n, args.jobs), names)
        else:
            results = pool.map(lambda n: confirm(n, args.jobs), names)
        for name, code in zip(names, results, strict=True):
            print(f"{args.cmd} {name}: exit {code}", flush=True)


if __name__ == "__main__":
    main()
