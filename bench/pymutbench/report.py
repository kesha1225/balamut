"""Aggregate the real-fault track into the published results: data files, tables and figures.

    python3 -m pymutbench.report bugs --results bench/results --images docs/images

Per tool: the share of bugs with a coupling witness (pooled over bugs and, as a sensitivity check, the mean of
per-project shares), its 95% interval from a bootstrap over projects, the budgeted measures R(q) and S(k), the
accounting of every emitted mutant, the bugs only that tool witnesses and the paired comparison with every
other tool (discordant counts, McNemar mid-p, Holm-adjusted over the comparison family).
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench import svg  # noqa: E402
from pymutbench.metrics import K_BUDGETS, Q_BUDGETS, TOOLS, VARIANTS, BugResult, bug_result, to_json  # noqa: E402
from pymutbench.orchestrate import OUT, WORK  # noqa: E402
from pymutbench.stats import cluster_bootstrap, holm, mcnemar_midp, pooled_mean, project_mean  # noqa: E402

FOCUS = "balamut"


def _git_show(subject: str, rev: str, path: str) -> bytes | None:
    proc = subprocess.run(["git", "-C", str(WORK / "repos" / subject), "show", f"{rev}:{path}"],
                          capture_output=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def purity() -> dict[str, set[str]]:
    """subject@commit12 -> the fail-to-pass tests that check the fault (the purity labels)."""
    found: dict[str, set[str]] = {}
    for path in sorted((OUT / "review" / "purity").glob("batch-*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            found[item["id"]] = set(item.get("fault_tests", []))
    return found


def load(*, accept_incomplete: frozenset[str] = frozenset(), preview: bool = False) -> list[BugResult]:
    """The results of every selected bug. Publication requires a final and complete selection (or a subject
    named in ``accept_incomplete``), complete generation and judging of every selected bug, and a fresh
    re-judgement of every witness; a preview reports the gaps instead of refusing."""
    fault_tests = purity()
    labels = {row["id"]: row for row in _rows(OUT / "review" / "final.jsonl")}
    selection = json.loads((OUT / "bugs" / "selection.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    if selection.get("provisional"):
        problems.append("the selection uses provisional labels")
    for name, row in sorted(selection["subjects"].items()):
        if not row["complete"] and name not in accept_incomplete:
            problems.append(f"{name}: selection incomplete, open: {row['open'][:3]}")
    results = []
    excluded: list[dict[str, str]] = []
    unavailable: list[dict[str, Any]] = []
    fingerprints: dict[str, set[str]] = {}
    for subject, row in sorted(selection["subjects"].items()):
        bugs_file = OUT / "bugs" / f"{subject}.jsonl"
        listed = len(_rows(bugs_file))
        if listed != row["selected"]:
            problems.append(f"{subject}: {listed} bugs listed in {bugs_file.name}, {row['selected']} selected")
    for subject in sorted(selection["subjects"]):
        for bug in _rows(OUT / "bugs" / f"{subject}.jsonl"):
            directory = OUT / "gen" / subject / bug["commit"][:12]
            cid = f"{subject}@{bug['commit'][:12]}"
            if labels.get(cid, {}).get("label") != "bug":
                problems.append(f"{cid}: selected without a final bug label")
                continue
            if cid not in fault_tests:
                problems.append(f"{cid}: no purity decision (which fail-to-pass tests check the fault)")
                continue
            missing = [t for t in TOOLS if not (directory / f"{t}.ledger.json").exists()]
            if missing:
                problems.append(f"{cid}: no generation ledger for {missing}")
                continue
            judged = _judging_gap(directory) or _tool_gap(directory)
            if judged:
                problems.append(f"{cid}: {judged}")
                continue
            sources = {}
            for change in bug["src_files"]:
                if change["status"] == "D":
                    continue
                fixed = _git_show(subject, bug["commit"], change["path"])
                buggy = _git_show(subject, bug["parent"], change["path"]) if change["status"] != "A" else b""
                if fixed is not None and buggy is not None:
                    sources[change["path"]] = (fixed, buggy.decode("utf-8", "replace"))
            fault = labels[cid].get("fault_class", "unlabelled")
            meta = [r for r in _rows(directory / "oracle.jsonl") if r.get("kind") == "meta"][-1]
            if "ineligible" in meta:
                excluded.append({"id": cid, "reason": f"the oracle found the unmutated trees ineligible: {meta['ineligible']}"})
                continue
            # Only validated fail-to-pass tests that the oracle kept (green on the fixed tree) can make a witness.
            tests_of_fault = fault_tests[cid] & set(bug.get("t_fix", [])) & set(meta.get("t_fix", []))
            if not tests_of_fault:
                excluded.append({"id": cid, "reason": "no fail-to-pass test that checks the fault is left after the "
                                                      "oracle's baseline"})
                continue
            result = bug_result(directory, fault, sources, tests_of_fault)
            if result is None:
                problems.append(f"{cid}: no measures (judging incomplete)")
                continue
            if not result.confirmed:
                problems.append(f"{cid}: witnesses without a fresh re-judgement (pipeline confirm)")
            results.append(result)
            _collect_fingerprints(directory, fingerprints)
            _collect_unavailable(directory, cid, unavailable)
    for stage, seen in fingerprints.items():
        if len(seen) > 1:
            problems.append(f"mixed {stage} fingerprints {sorted(seen)}")
    problems += _sensitivity_gap()
    if problems and not preview:
        raise SystemExit("not publishable:\n  " + "\n  ".join(problems[:40]) + f"\n  ({len(problems)} problems)")
    EXCLUDED[:] = excluded
    UNAVAILABLE[:] = unavailable
    PROBLEMS[:] = problems
    return results


def _judging_gap(directory: Path) -> str:
    """Why a bug's judging is incomplete, or "" when every artifact has a verdict (or the oracle refused the bug)."""
    rows = _rows(directory / "oracle.jsonl")
    metas = [r for r in rows if r.get("kind") == "meta"]
    if not metas:
        return "not judged"
    if "ineligible" in metas[-1]:
        return ""
    missing = {r["aid"] for r in _rows(directory / "union.jsonl")} - {r["aid"] for r in rows if "aid" in r}
    return f"{len(missing)} artifacts without a verdict" if missing else ""


def _tool_gap(directory: Path) -> str:
    """Why some tool artifact has no verdict, or "": a union written before a tool's artifacts would hide them
    from ``_judging_gap``."""
    rows = _rows(directory / "oracle.jsonl")
    judged = {r["aid"] for r in rows if "aid" in r}
    missing = {row["aid"] for tool in TOOLS for row in _rows(directory / f"{tool}.jsonl")} - judged
    return f"{len(missing)} tool artifacts without a verdict (stale union)" if missing else ""


def _sensitivity_gap() -> list[str]:
    """The documented oracle-shortcut sensitivity must be complete: every sampled mutant judged again."""
    path = OUT / "bugs" / "sensitivity.json"
    if not path.exists():
        return ["the oracle-shortcut sensitivity sample is not drawn (pipeline sensitivity)"]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    by_bug: dict[tuple[str, str], set[str]] = {}
    for subject, commit12, aid in manifest["sample"]:
        by_bug.setdefault((subject, commit12), set()).add(aid)
    missing = sum(len(aids - {r["aid"] for r in _rows(OUT / "gen" / s / c / "oracle.full.jsonl") if "aid" in r})
                  for (s, c), aids in by_bug.items())
    return [f"{missing} sampled mutants without a full-suite-only verdict (pipeline sensitivity)"] if missing else []


EXCLUDED: list[dict[str, str]] = []  # bugs dropped at reporting time, with the reason (published)
UNAVAILABLE: list[dict[str, Any]] = []  # (tool, bug) pairs whose generation failed, with the evidence (published)
PROBLEMS: list[str] = []  # publication gaps, reported by a preview


def _collect_unavailable(directory: Path, cid: str, into: list[dict[str, Any]]) -> None:
    for ledger in sorted(directory.glob("*.ledger.json")):
        data = json.loads(ledger.read_text(encoding="utf-8"))
        if data.get("failed"):
            into.append({"tool": ledger.name.removesuffix(".ledger.json"), "bug": cid, "rc": data.get("rc"),
                         "failure": data.get("failure"), "attempts": data.get("attempts", 1),
                         "stderr_tail": str(data.get("stderr", ""))[-600:]})


def _collect_fingerprints(directory: Path, into: dict[str, set[str]]) -> None:
    for ledger in directory.glob("*.ledger.json"):
        tool = ledger.name.removesuffix(".ledger.json")
        into.setdefault(f"generation of {tool}", set()).add(
            json.loads(ledger.read_text(encoding="utf-8")).get("fingerprint", "?"))
    for name in ("oracle.jsonl", "oracle.confirm.jsonl", "oracle.full.jsonl"):
        for row in _rows(directory / name):
            if row.get("kind") == "meta":
                into.setdefault("oracle", set()).add(row.get("fingerprint", "?"))


def _clusters(results: list[BugResult], value: Any) -> dict[str, list[float]]:
    clusters: dict[str, list[float]] = {}
    for result in results:
        clusters.setdefault(result.subject, []).append(float(value(result)))
    return clusters


def _detects(result: BugResult, tool: str) -> bool:
    """Operational: a tool unavailable for a bug (its generator failed there) delivered no witness."""
    return result.tools[tool].available and result.tools[tool].detected


def aggregate(results: list[BugResult]) -> dict[str, Any]:
    """Every measure is operational over all bugs (a generation failure is a bug the tool does not detect); the
    rate conditional on the tool's generation succeeding is reported beside it."""
    tools: dict[str, Any] = {}
    for tool in (*TOOLS, *VARIANTS):
        available = [r for r in results if r.tools[tool].available]
        clusters = _clusters(results, lambda r, t=tool: _detects(r, t))
        conditional = _clusters(available, lambda r, t=tool: r.tools[t].detected)
        status = Counter()
        emitted = unique = 0
        for result in available:
            tb = result.tools[tool]
            status.update(tb.status)
            emitted += tb.emitted
            unique += tb.unique
        r_means = {q: pooled_mean(_clusters(results, lambda r, t=tool, q=q: r.tools[t].r[q] if r.tools[t].available else 0.0))
                   for q in Q_BUDGETS}
        s_means = {k: pooled_mean(_clusters(results, lambda r, t=tool, k=k: r.tools[t].s[k] if r.tools[t].available else 0.0))
                   for k in K_BUDGETS}
        rivals = [o for o in TOOLS if o not in {tool, VARIANTS.get(tool, ("",))[0]}]
        leave_one_out = [pooled_mean({p: v for p, v in clusters.items() if p != project}) for project in clusters]
        tools[tool] = {
            "bugs": len(results),
            "available_bugs": len(available),
            "unavailable_bugs": len(results) - len(available),
            "detected": sum(1 for r in results if _detects(r, tool)),
            "rate": pooled_mean(clusters),
            "rate_ci": cluster_bootstrap(clusters, pooled_mean),
            "rate_project_mean": project_mean(clusters),
            "rate_leave_one_project_out": [min(leave_one_out), max(leave_one_out)] if leave_one_out else None,
            "rate_without_arrow": pooled_mean({p: v for p, v in clusters.items() if p != "arrow"}),
            "rate_conditional": pooled_mean(conditional),
            "rate_conditional_ci": cluster_bootstrap(conditional, pooled_mean),
            "r": r_means,
            "r10_ci": cluster_bootstrap(_clusters(results, lambda r, t=tool: r.tools[t].r[10] if r.tools[t].available
                                                  else 0.0), pooled_mean),
            "s": s_means,
            "s3_ci": cluster_bootstrap(_clusters(results, lambda r, t=tool: r.tools[t].s[3] if r.tools[t].available
                                                 else 0.0), pooled_mean),
            "emitted": emitted,
            "unique": unique,
            "status": dict(status),
            "witness_mutants": sum(r.tools[tool].witnesses for r in available),
            "witness_flow": {field: sum(getattr(r.tools[tool], field) for r in available) for field in (
                "first_witnesses", "unknown_confirmations", "unconfirmed_witnesses", "synthetic_witnesses",
                "feature_only_witnesses", "witnesses")},
            "unconfirmed_witnesses": sum(r.tools[tool].unconfirmed_witnesses for r in available),
            "undecided_mutants": sum(r.tools[tool].undecided for r in available),
            "undecided_bugs": sum(1 for r in available if not r.tools[tool].detected and r.tools[tool].undecided),
            "survivors_per_bug_median": _median([r.tools[tool].survivors for r in available]),
            "emitted_per_bug_median": _median([r.tools[tool].emitted for r in available]),
            "exact_fault": sum(1 for r in available if r.tools[tool].exact_fault is True),
            "exact_fault_applicable": sum(1 for r in available if r.tools[tool].exact_fault is not None),
            "only_this_tool": sum(1 for r in results if _detects(r, tool) and not any(_detects(r, o) for o in rivals)),
            "covered_fault_rate": _rate([r for r in results if r.fault_covered_by_old], tool),
            "gap_fault_rate": _rate([r for r in results if r.fault_lines_run and not r.fault_covered_by_old], tool),
            "unrun_fault_rate": _rate([r for r in results if not r.fault_lines_run], tool),
            "by_region": {kind: _rate([r for r in results if r.region_kind == kind], tool)
                          for kind in ("function", "statement", "mixed")},
            # Optimistic: every artifact the oracle left undecided (unknown fail-to-pass outcome or confirmation,
            # unresolved historical run) counted as a witness.
            "rate_optimistic": sum(1 for r in available if r.tools[tool].detected or r.tools[tool].undecided)
            / max(1, len(results)),
            "rate_any_fix_test": sum(1 for r in available if r.tools[tool].detected_any_fix_test) / max(1, len(results)),
        }
    pairs = []
    for tool in TOOLS:
        if tool == FOCUS:
            continue
        focus_only = sum(1 for r in results if _detects(r, FOCUS) and not _detects(r, tool))
        other_only = sum(1 for r in results if _detects(r, tool) and not _detects(r, FOCUS))
        diff = _clusters(results, lambda r, t=tool: float(_detects(r, FOCUS)) - float(_detects(r, t)))
        both = [r for r in results if r.tools[FOCUS].available and r.tools[tool].available]
        pairs.append({"other": tool, "focus_only": focus_only, "other_only": other_only,
                      "p_midp": mcnemar_midp(focus_only, other_only), "difference": pooled_mean(diff),
                      "difference_ci": cluster_bootstrap(diff, pooled_mean),
                      "both_available": {"bugs": len(both), "projects": len({r.subject for r in both})}})
    for pair, adjusted in zip(pairs, holm([p["p_midp"] for p in pairs]), strict=True):
        pair["p_holm"] = adjusted
    overlap = [[sum(1 for r in results if _detects(r, a) and not _detects(r, b)) for b in TOOLS] for a in TOOLS]
    projects = Counter(r.subject for r in results)
    return {
        "bugs": len(results),
        "projects": dict(sorted(projects.items())),
        "tools": tools,
        "versus_focus": pairs,
        "overlap": {"labels": list(TOOLS), "values": overlap},
        "union_rate": sum(1 for r in results if any(_detects(r, t) for t in TOOLS)) / max(1, len(results)),
        "coverage_guided_rate": sum(1 for r in results if r.coverage_guided) / max(1, len(results)),
        "fault_covered_by_old": sum(1 for r in results if r.fault_covered_by_old),
        "fault_coverage_gap": sum(1 for r in results if r.fault_lines_run and not r.fault_covered_by_old),
        "fault_lines_unrun": sum(1 for r in results if not r.fault_lines_run),
        "region_kinds": dict(Counter(r.region_kind for r in results)),
        "repair_shapes": dict(Counter(r.repair_shape for r in results)),
        "fault_classes": dict(Counter(r.fault_class for r in results)),
        "by_fault_class": {
            cls: {tool: _rate([r for r in results if r.fault_class == cls], tool) for tool in TOOLS}
            for cls in sorted({r.fault_class for r in results})
        },
        "unresolved_mutants": sum(r.unresolved for r in results),
    }


def sensitivity() -> dict[str, Any]:
    """The seeded sample of stage 0/1 kills (``bugs/sensitivity.json``, written by ``pipeline sensitivity``)
    judged again by the canonical suite alone: how many of the sampled kills the whole historical suite repeats."""
    manifest_path = OUT / "bugs" / "sensitivity.json"
    if not manifest_path.exists():
        return {"sampled": 0}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fault_tests = purity()
    outcome: Counter[str] = Counter()
    by_stage: dict[str, Counter[str]] = {}
    discordant: list[dict[str, Any]] = []
    for subject, commit12, aid in manifest["sample"]:
        directory = OUT / "gen" / subject / commit12
        first = {r["aid"]: r for r in _rows(directory / "oracle.jsonl") if "aid" in r}.get(aid, {})
        second = {r["aid"]: r for r in _rows(directory / "oracle.full.jsonl") if "aid" in r}.get(aid)
        verdict = second["status"] if second else "not judged"
        outcome[verdict] += 1
        by_stage.setdefault(first.get("stage", "?"), Counter())[verdict] += 1
        if second is not None and verdict != "killed":
            # A sampled kill the canonical suite does not repeat: which tools emitted it, and whether a test of the
            # fault is red on it (a candidate witness the staged oracle's shortcut hid; not a confirmed one).
            tools = sorted({o.get("tool", "?") for row in _rows(directory / "union.jsonl") if row["aid"] == aid
                            for o in row.get("origins", [])})
            red = set(second.get("fix_red", {})) & fault_tests.get(f"{subject}@{commit12}", set())
            discordant.append({"bug": f"{subject}@{commit12}", "aid": aid, "first_stage": first.get("stage"),
                               "first_killed_by": first.get("killed_by", []), "full_suite": verdict,
                               "reason": second.get("reason"), "tools": tools, "fault_tests_red": sorted(red)})
    killed, survived = outcome.get("killed", 0), outcome.get("survived", 0)
    return {"seed": manifest["seed"], "population": manifest["population"], "sampled": len(manifest["sample"]),
            "bugs": len({(s, c) for s, c, _ in manifest["sample"]}),
            "full_suite": dict(outcome), "by_first_stage": {k: dict(v) for k, v in by_stage.items()},
            "agreement": killed / (killed + survived) if killed + survived else math.nan,
            "killed_of_sampled": killed / len(manifest["sample"]) if manifest["sample"] else math.nan,
            "discordant": discordant}


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _rate(results: list[BugResult], tool: str) -> float:
    return sum(1 for r in results if _detects(r, tool)) / len(results) if results else math.nan


def _median(values: list[int]) -> float:
    values = sorted(values)
    if not values:
        return math.nan
    mid = len(values) // 2
    return float(values[mid]) if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def figures(agg: dict[str, Any], images: Path) -> list[str]:
    images.mkdir(parents=True, exist_ok=True)
    written = []
    tools = {t: v for t, v in agg["tools"].items() if t not in VARIANTS}
    n, p = agg["bugs"], len(agg["projects"])

    def rows_for(key: str, ci: str | None) -> list[svg.Row]:
        rows = [svg.Row(t, v[key] if not isinstance(v[key], dict) else math.nan,
                        *(v[ci] if ci else (None, None)), emphasis=t == FOCUS) for t, v in tools.items()]
        return sorted(rows, key=lambda r: -1 if math.isnan(r.value) else r.value, reverse=True)

    specs = [
        ("bench-detection", "Real bugs with a coupling witness",
         f"{n} bugs, {p} projects · a mutant the old tests miss and a test of the fault kills, twice · 95% CI over projects",
         rows_for("rate", "rate_ci")),
        ("bench-budget-r10", "Witness among 10 random mutants",
         f"mean over {n} bugs · up to 10 in-scope emissions drawn without replacement, unmaterializable ones included "
         "· 95% CI over projects",
         sorted([svg.Row(t, v["r"][10], *v["r10_ci"], emphasis=t == FOCUS) for t, v in tools.items()],
                key=lambda r: r.value, reverse=True)),
        ("bench-budget-s3", "Witness among 3 random survivors",
         f"mean over {n} bugs · up to 3 of the tool's mutants the reference oracle judged surviving, drawn without "
         "replacement · 95% CI over projects",
         sorted([svg.Row(t, v["s"][3], *v["s3_ci"], emphasis=t == FOCUS) for t, v in tools.items()],
                key=lambda r: r.value, reverse=True)),
    ]
    for name, title, subtitle, rows in specs:
        for theme in svg.THEMES:
            path = images / f"{name}.{theme.name}.svg"
            path.write_text(svg.ranked_bars(title, subtitle, rows, theme), encoding="utf-8")
            written.append(str(path))
    # Killed mutants are the recessive bulk; the survivors carry the colors (the accent stays the focus tool's
    # hue in every other figure, so it is not spent on a category here).
    parts = [svg.Part("killed", "gray_light"), svg.Part("survived: witness", "second"),
             svg.Part("survived: other", "third"),
             svg.Part("screened out (invalid, equivalent, no-op) or unresolved", "gray")]
    rows = []
    for tool, v in sorted(tools.items(), key=lambda kv: kv[1]["unique"], reverse=True):
        st = v["status"]
        rows.append((tool, [st.get("killed", 0), _witnesses(tool, agg), st.get("survived", 0) - _witnesses(tool, agg),
                            st.get("invalid", 0) + st.get("equivalent_tce", 0) + st.get("noop", 0)
                            + st.get("unresolved", 0) + st.get("missing", 0)], tool == FOCUS))
    for theme in svg.THEMES:
        path = images / f"bench-accounting.{theme.name}.svg"
        path.write_text(svg.stacked_bars("What each tool's in-scope mutants turned out to be",
                                         "unique in-scope mutants per tool over all bugs, by the reference oracle's first "
                                         "judgement · witnesses confirmed by the re-judgement",
                                         rows, parts, theme), encoding="utf-8")
        written.append(str(path))
    for theme in svg.THEMES:
        path = images / f"bench-overlap.{theme.name}.svg"
        path.write_text(svg.matrix("Bugs the row tool witnesses and the column tool does not",
                                   "complementarity of the witnessed bugs", agg["overlap"]["labels"],
                                   agg["overlap"]["values"], theme, emphasis=FOCUS), encoding="utf-8")
        written.append(str(path))
    return written


def _pct(value: float) -> str:
    return "–" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value * 100:.0f}%"


def _pct1(value: float) -> str:
    """A share with one decimal, for agreement levels a whole percent would round to 100%."""
    return "–" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value * 100:.1f}%"


def tables(agg: dict[str, Any]) -> str:
    """The table twins of the figures, as markdown."""
    tools = agg["tools"]
    order = sorted((t for t in tools if t not in VARIANTS), key=lambda t: tools[t]["rate"], reverse=True)
    variants = [t for t in tools if t in VARIANTS]

    def span(pair: list[float] | None) -> str:
        return f"{_pct(pair[0])}–{_pct(pair[1])}" if pair else "–"

    def detection_row(t: str) -> str:
        v = tools[t]
        return (f"| {t} | {v['detected']}/{v['bugs']} | {_pct(v['rate'])} ({span(v['rate_ci'])}) "
                f"| {_pct(v['rate_project_mean'])} | {span(v['rate_leave_one_project_out'])} "
                f"| {_pct(v['rate_without_arrow'])} | {_pct(v['rate_conditional'])} ({v['available_bugs']}) "
                f"| {v['unavailable_bugs']} |")

    def budget_row(t: str) -> str:
        v = tools[t]
        return (f"| {t} | {_pct(v['r'][1])} | {_pct(v['r'][10])} ({span(v['r10_ci'])}) | {_pct(v['r'][100])} "
                f"| {_pct(v['s'][1])} | {_pct(v['s'][3])} ({span(v['s3_ci'])}) | {_pct(v['s'][10])} "
                f"| {v['emitted_per_bug_median']:g} | {v['survivors_per_bug_median']:g} |")

    detection_head = ["| tool | bugs detected | rate (95% CI) | project mean | leave one project out | without arrow "
                      "| conditional rate (bugs) | unavailable |", "|---|---|---|---|---|---|---|---|"]
    budget_head = ["| tool | R(1) | R(10) (95% CI) | R(100) | S(1) | S(3) (95% CI) | S(10) "
                   "| in-scope emissions / bug (median) | survivors / bug (median) |",
                   "|---|---|---|---|---|---|---|---|---|"]
    out = ["### Detection", "",
           "Operational rate: a bug the tool's generator failed on counts as not detected. Conditional rate: over "
           "the bugs the tool's generation succeeded on. Intervals: 95%, bootstrap over projects, pointwise.", "",
           *detection_head, *(detection_row(t) for t in order)]
    out += ["", "### Budgets", "",
            "R(q): q of the tool's in-scope emissions drawn without replacement (unmaterializable ones included). "
            "S(k): k of the unique artifacts of the tool that the reference oracle judges survivors (the tool's own "
            "engine may report others). Both use min(budget, population) draws and are 0 for an empty population.",
            "", *budget_head, *(budget_row(t) for t in order)]
    if variants:
        out += ["", "### Variants (outside the ranking and the comparisons)", "",
                "balamut's ranking row uses the source kinds `balamut mutate` runs by default (src, concurrency, "
                "fault; its default oracle kind mutates tests, outside the scope), the file-level default every tool "
                "gets. The variant adds the extreme kind, which `balamut diff` also runs by default on changed code: "
                "it measures that kind under the benchmark's scope and oracle, not the `balamut diff` workflow.", "",
                *detection_head, *(detection_row(t) for t in variants), "", *budget_head,
                *(budget_row(t) for t in variants)]
    out += ["", "### Paired with balamut", "",
            "Discordant bugs over all bugs (operational); the difference is balamut's rate minus the tool's, with a "
            "95% interval from the project bootstrap. McNemar mid-p and Holm are descriptive: they ignore the "
            "clustering by project.", "",
            "| tool | balamut only | tool only | difference (95% CI) | mid-p | Holm | both available (bugs / projects) |",
            "|---|---|---|---|---|---|---|"]
    for pair in sorted(agg["versus_focus"], key=lambda x: x["difference"]):
        lo, hi = pair["difference_ci"]
        both = pair["both_available"]
        out.append(f"| {pair['other']} | {pair['focus_only']} | {pair['other_only']} | "
                   f"{pair['difference'] * 100:+.1f} pp ({lo * 100:+.1f} to {hi * 100:+.1f}) | {pair['p_midp']:.3g} | "
                   f"{pair['p_holm']:.3g} | {both['bugs']} / {both['projects']} |")
    out += ["", "### Mutant accounting (unique in-scope mutants, bugs the tool's generation succeeded on)", "",
            "| tool | in-scope emissions | unique | killed | survived | of which witnesses | invalid "
            "| equivalent (TCE) | no-op | unresolved |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for t in sorted(order, key=lambda t: tools[t]["unique"], reverse=True):
        v, st = tools[t], tools[t]["status"]
        out.append(f"| {t} | {v['emitted']} | {v['unique']} | {st.get('killed', 0)} | {st.get('survived', 0)} | "
                   f"{v['witness_mutants']} | {st.get('invalid', 0)} | {st.get('equivalent_tce', 0)} | "
                   f"{st.get('noop', 0)} | {st.get('unresolved', 0) + st.get('missing', 0)} |")
    out += ["", "### Witness flow (unique artifacts)", "",
            "A first-judgement witness counts when the re-judgement from fresh trees reproduces it (re-judgements "
            "without an outcome are unknown, the others not reproduced), the same fail-to-pass test is red with an "
            "outcome of its own in both judgements (red only through a collection or startup failure of the whole run "
            "is synthetic) and that test checks the fault (otherwise feature only).", "",
            "| tool | first-judgement witnesses | re-judgement unknown | not reproduced | synthetic | feature only "
            "| witnesses |", "|---|---|---|---|---|---|---|"]
    for t in order:
        f = tools[t]["witness_flow"]
        out.append(f"| {t} | {f['first_witnesses']} | {f['unknown_confirmations']} | {f['unconfirmed_witnesses']} "
                   f"| {f['synthetic_witnesses']} | {f['feature_only_witnesses']} | {f['witnesses']} |")
    out += ["", "### Undecided outcomes", "",
            "Artifacts that could still be witnesses: an unresolved historical run, a survivor whose fail-to-pass run "
            "gave no outcome, or a witness whose re-judgement gave none. The optimistic rate counts every bug with one "
            "of them as detected.", "",
            "| tool | undecided artifacts | bugs undetected with an undecided artifact | rate | optimistic rate |",
            "|---|---|---|---|---|"]
    for t in order:
        v = tools[t]
        out.append(f"| {t} | {v['undecided_mutants']} | {v['undecided_bugs']} | {_pct(v['rate'])} "
                   f"| {_pct(v['rate_optimistic'])} |")
    kinds = agg["region_kinds"]
    out += ["", "### By stratum", "",
            f"Where the fix is: inside functions ({kinds.get('function', 0)} bugs), in class-body or module-level "
            f"statements ({kinds.get('statement', 0)}), both ({kinds.get('mixed', 0)}). Coverage, retrospective (the "
            "fail-to-pass run includes feature tests): the fail-to-pass run executes at least one line the fix changed "
            f"and the historical suite executed every such line (covered, {agg['fault_covered_by_old']} bugs) or not "
            f"every one (gap, {agg['fault_coverage_gap']}), or it executes none of them "
            f"({agg['fault_lines_unrun']}).", "",
            "| tool | fix in functions | fix in class/module statements | fix in both | changed lines covered "
            "| coverage gap | no changed line run | witness by any fail-to-pass test |",
            "|---|---|---|---|---|---|---|---|"]
    for t in order:
        v = tools[t]
        out.append(f"| {t} | {_pct(v['by_region']['function'])} | {_pct(v['by_region']['statement'])} "
                   f"| {_pct(v['by_region']['mixed'])} | {_pct(v['covered_fault_rate'])} "
                   f"| {_pct(v['gap_fault_rate'])} | {_pct(v['unrun_fault_rate'])} | {_pct(v['rate_any_fix_test'])} |")
    out += ["", "### Secondary measures", "",
            "Only this tool: bugs no other ranked tool witnesses. Re-creates the fault: single-file fixes where a "
            "mutant whose edit touches a line the fix changed turns the fixed file into the buggy one (equal ASTs).",
            "", "| tool | only this tool | re-creates the fault (applicable bugs) |", "|---|---|---|"]
    for t in order:
        v = tools[t]
        out.append(f"| {t} | {v['only_this_tool']} | {v['exact_fault']} ({v['exact_fault_applicable']}) |")
    out += ["", f"Bugs: {agg['bugs']} in {len(agg['projects'])} projects; witnessed by at least one tool: "
            f"{_pct(agg['union_rate'])}; coverage-guided (the fail-to-pass tests run a line of the repair scope that "
            f"the historical suite never ran; retrospective): {_pct(agg['coverage_guided_rate'])}."]
    sens = agg.get("oracle_sensitivity", {})
    if sens.get("sampled"):
        full = sens["full_suite"]
        killed, survived = full.get("killed", 0), full.get("survived", 0)
        out += ["", "### Oracle shortcuts", "",
                f"{sens['sampled']} kills by a single test or the covering subset, drawn uniformly without replacement "
                f"(seed {sens['seed']}) from {sens['population']} over {sens['bugs']} bugs, judged again by the whole "
                f"historical suite in canonical order alone: killed {killed}, survived {survived}, other "
                f"{sens['sampled'] - killed - survived} ({json.dumps(full, sort_keys=True)}). Killed of the decided "
                f"outcomes: {_pct1(sens['agreement'])} ({killed}/{killed + survived}); of the sample: "
                f"{_pct1(sens['killed_of_sampled'])} ({killed}/{sens['sampled']}). By the first judgement's stage: "
                f"{json.dumps(sens['by_first_stage'], sort_keys=True)}."]
        if sens["discordant"]:
            out += ["", "| bug | artifact | first stage | first killed by | whole suite | fault tests red | tools |",
                    "|---|---|---|---|---|---|---|"]
            for d in sens["discordant"]:
                out.append(f"| {d['bug']} | {d['aid'][:12]} | {d['first_stage']} | {', '.join(d['first_killed_by'])} "
                           f"| {d['full_suite']}{f' ({d['reason']})' if d['reason'] else ''} "
                           f"| {', '.join(d['fault_tests_red']) or '–'} | {', '.join(d['tools'])} |")
    return "\n".join(out) + "\n"


def corpus_table(results: list[BugResult]) -> str:
    """The selection funnel per project (``bugs/selection.json``) with the bugs the oracle judged."""
    selection = json.loads((OUT / "bugs" / "selection.json").read_text(encoding="utf-8"))
    judged = Counter(r.subject for r in results)
    keys = ("candidates", "screened", "admitted", "adjudicated", "bugs", "readmitted", "selected")
    out = ["| project | candidates | validated | admitted | adjudicated | labelled bug | re-admitted | selected "
           "| judged | selection |", "|---|---|---|---|---|---|---|---|---|---|"]
    totals = Counter()
    for name, row in sorted(selection["subjects"].items()):
        totals.update({k: row[k] for k in keys})
        totals["judged"] += judged.get(name, 0)
        state = "complete" if row["complete"] else f"{len(row['open'])} open"
        out.append(f"| {name} | " + " | ".join(str(row[k]) for k in keys) + f" | {judged.get(name, 0)} | {state} |")
    out.append("| **total** | " + " | ".join(f"**{totals[k]}**" for k in (*keys, "judged")) + " | |")
    return "\n".join(out) + "\n"


def export(results: list[BugResult], out: Path) -> list[str]:
    """The judged corpus as compressed JSON lines, one record per bug and item, for reuse without the harness:
    every artifact with its tools' origins, every oracle verdict, witness re-judgement and full-suite-only
    re-judgement of the shortcut sample, every generation ledger and every bug's scope."""
    import gzip  # noqa: PLC0415

    sources = {"artifacts": "union.jsonl", "verdicts": "oracle.jsonl", "confirmations": "oracle.confirm.jsonl",
               "full_suite": "oracle.full.jsonl"}
    written = []
    for name, filename in sources.items():
        path = out / f"{name}.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as fh:
            for result in results:
                directory = OUT / "gen" / result.subject / result.commit[:12]
                for row in _rows(directory / filename):
                    # A meta record names its bug by the full commit: kept as ``commit`` under the common key.
                    fields = {("commit" if key == "bug" else key): value for key, value in row.items()}
                    fh.write(json.dumps({"bug": f"{result.subject}@{result.commit[:12]}", **fields}) + "\n")
        written.append(str(path))
    path = out / "generation.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as fh:
        for result in results:
            directory = OUT / "gen" / result.subject / result.commit[:12]
            bug = f"{result.subject}@{result.commit[:12]}"
            fh.write(json.dumps({"bug": bug, "scope": json.loads((directory / "scope.json").read_text(encoding="utf-8")),
                                 "record": json.loads((directory / "bug.json").read_text(encoding="utf-8"))}) + "\n")
            for ledger in sorted(directory.glob("*.ledger.json")):
                fh.write(json.dumps({"bug": bug, "tool": ledger.name.removesuffix(".ledger.json"),
                                     "ledger": json.loads(ledger.read_text(encoding="utf-8"))}) + "\n")
    written.append(str(path))
    return written


def _witnesses(tool: str, agg: dict[str, Any]) -> int:
    return int(agg["tools"][tool].get("witness_mutants", 0))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bugs")
    p.add_argument("--results", required=True)
    p.add_argument("--images", required=True)
    p.add_argument("--accept-incomplete", default="",
                   help="comma-separated subjects whose incomplete selection is published as such")
    p.add_argument("--preview", action="store_true", help="report the publication gaps instead of refusing")
    args = parser.parse_args()
    accepted = frozenset(s for s in args.accept_incomplete.split(",") if s)
    results = load(accept_incomplete=accepted, preview=args.preview)
    agg = aggregate(results)
    agg["oracle_sensitivity"] = sensitivity()
    agg["selection_incomplete_accepted"] = sorted(accepted)
    agg["preview_problems"] = list(PROBLEMS) if args.preview else []
    out = Path(args.results)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bug_track.json").write_text(json.dumps(agg, indent=1, default=float), encoding="utf-8")
    with (out / "bugs.jsonl").open("w", encoding="utf-8") as fh:
        for row in to_json(results):
            fh.write(json.dumps(row) + "\n")
    written = figures(agg, Path(args.images))
    (out / "tables.md").write_text(tables(agg), encoding="utf-8")
    (out / "corpus.md").write_text(corpus_table(results), encoding="utf-8")
    # The corpus decisions behind the numbers: the selection funnel, every adjudication and the raters' agreement,
    # the purity rubric with the labels of the selected bugs, and the oracle-shortcut sample.
    for source in (OUT / "bugs" / "selection.json", OUT / "review" / "final.jsonl", OUT / "review" / "agreement.json",
                   OUT / "review" / "purity_rubric.md", OUT / "review" / "purity_schema.json",
                   OUT / "bugs" / "sensitivity.json"):
        if source.exists():
            (out / source.name).write_bytes(source.read_bytes())
    selected = {f"{r.subject}@{r.commit[:12]}" for r in results} | {e["id"] for e in EXCLUDED}
    labels = [item for path in sorted((OUT / "review" / "purity").glob("batch-*.json"))
              for item in json.loads(path.read_text(encoding="utf-8")).get("items", []) if item["id"] in selected]
    (out / "purity.jsonl").write_text("".join(json.dumps(item) + "\n" for item in sorted(labels, key=lambda i: i["id"])),
                                      encoding="utf-8")
    (out / "excluded.json").write_text(json.dumps(EXCLUDED, indent=1), encoding="utf-8")
    (out / "unavailable.json").write_text(json.dumps(UNAVAILABLE, indent=1), encoding="utf-8")
    data = out / "data"
    data.mkdir(exist_ok=True)
    export(results, data)
    print(f"{agg['bugs']} bugs; wrote {len(written)} figures")
    for tool, v in sorted(agg["tools"].items(), key=lambda kv: kv[1]["rate"], reverse=True):
        print(f"{tool:<17} detected {v['detected']:>3}/{v['bugs']} = {v['rate']:.1%} "
              f"[{v['rate_ci'][0]:.1%}, {v['rate_ci'][1]:.1%}]  R10={v['r'][10]:.2f}  S3={v['s'][3]:.2f}  "
              f"unique-only={v['only_this_tool']}")


if __name__ == "__main__":
    main()
