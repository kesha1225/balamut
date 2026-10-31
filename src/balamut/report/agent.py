"""The agent report: compact, actionable-only and clustered, for an edit loop driven by an agent.

A flat list of verdicts floods the context and duplicates work (copy-pasted idioms produce identical mutants). Here
timeouts and HIGH survivors are clustered by idiom, each cluster names the covering test files, and no-coverage
sites fold into per-file hotspots. The core is pure (``list[MutantRecord]`` -> report) and needs no database.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable
from operator import itemgetter
from typing import NotRequired, TypedDict

from balamut.core.triage import ColdKind
from balamut.coverage.store import Store

_COVERING_FILES_CAP = 6  # covering test files per cluster: enough to act on, without flooding
_HOTSPOT_LINES_CAP = 8  # sample lines per no-coverage hotspot
_UNTAGGED = "(untagged)"


class MutantRecord(TypedDict):
    """One verdict row (from the store): the input of the report."""

    key: NotRequired[str]
    file: str
    lineno: int
    operator: str
    verdict: str  # killed | survived | timeout | no_coverage | unsupported
    bucket: str  # high | review | low
    original: str
    mutated: str
    covering: list[str]  # node ids of the covering tests
    semantic: list[str]
    kind: NotRequired[str]  # the mutant's family as stored; absent in hand-built rows


class Site(TypedDict):
    """Where an idiom cluster lives: a file and its lines."""

    file: str
    lines: list[int]


class Cluster(TypedDict):
    """Mutants of ONE idiom (operator family plus normalized original text).

    It merges copy-paste across files and is built separately for unresolved and actionable verdicts."""

    operator_family: str
    sample: str  # "original → mutated", whitespace collapsed
    sites: list[Site]  # may span several files
    count: int
    covering_test_files: list[str]  # test files to strengthen (empty: no test covers the idiom)
    semantic: list[str]


class Hotspot(TypedDict):
    """A file with no-coverage sites on the ordinary runtime path."""

    file: str
    uncovered: int
    lines: list[int]


type CountMap = dict[str, int]
type Reachable = Callable[[str, int], bool]
type RecordColdResolver = Callable[[MutantRecord], ColdKind]
type ColdResolver = Callable[[str, int], ColdKind]
type _GroupKey = tuple[str, str]  # (operator family, normalized idiom)
type _Grouped = dict[_GroupKey, list[MutantRecord]]
type _FileLines = dict[str, list[int]]
type _JsonValue = str | int | float | bool | list[_JsonValue] | dict[str, _JsonValue] | None


class AgentReport(TypedDict):
    score: float
    killed: int
    survived: int
    timeout: int
    unresolved: int
    no_coverage: int
    cold_no_coverage: int
    cold_kinds: CountMap
    unresolved_clusters: list[Cluster]
    actionable: int  # HIGH survivors (all of them, clustered)
    actionable_clusters: list[Cluster]
    uncovered_hotspots: list[Hotspot]
    summary: str


def split_cold_records(
    records: list[MutantRecord],
    reachable: Reachable,
    cold_resolver: RecordColdResolver,
) -> tuple[list[MutantRecord], CountMap]:
    """Separate cold no-coverage records from runtime ones and count the static cold categories."""
    runtime_records: list[MutantRecord] = []
    cold_counts: Counter[str] = Counter()
    for record in records:
        if record["verdict"] == "no_coverage" and not reachable(record["file"], record["lineno"]):
            cold_counts[cold_resolver(record).value] += 1
        else:
            runtime_records.append(record)
    return runtime_records, dict(sorted(cold_counts.items()))


def _family(record: MutantRecord) -> str:
    """The record's family label as stored; an operator prefix only for hand-built rows."""
    kind = record.get("kind")
    if kind is not None and kind:
        return kind
    return record["operator"].partition(":")[0]


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _covering_test_files(records: list[MutantRecord]) -> list[str]:
    """Test FILES of a cluster's covering tests (node id up to ``::``), deduplicated, sorted and capped."""
    files = {nodeid.partition("::")[0] for record in records for nodeid in record["covering"]}
    return sorted(files)[:_COVERING_FILES_CAP]


def _sites(records: list[MutantRecord]) -> list[Site]:
    by_file: _FileLines = defaultdict(list)
    for record in records:
        by_file[record["file"]].append(record["lineno"])
    return [{"file": file, "lines": sorted(set(lines))} for file, lines in sorted(by_file.items())]


def _clusters(records: list[MutantRecord]) -> list[Cluster]:
    """Group one class of verdicts by idiom, the largest clusters first."""
    grouped: _Grouped = defaultdict(list)
    for record in records:
        grouped[_family(record), _collapse(record["original"])].append(record)
    clusters: list[Cluster] = []
    for (family, _idiom), members in grouped.items():
        first = members[0]
        clusters.append(
            {
                "operator_family": family,
                "sample": f"{_collapse(first['original'])} → {_collapse(first['mutated'])}",
                "sites": _sites(members),
                "count": len(members),
                "covering_test_files": _covering_test_files(members),
                "semantic": sorted({tag for record in members for tag in record["semantic"]}),
            }
        )
    clusters.sort(key=lambda cluster: (-cluster["count"], cluster["operator_family"], cluster["sample"]))
    return clusters


def _hotspots(no_cov: list[MutantRecord]) -> list[Hotspot]:
    by_file: _FileLines = defaultdict(list)
    for record in no_cov:
        by_file[record["file"]].append(record["lineno"])
    hotspots: list[Hotspot] = [
        {"file": file, "uncovered": len(lines), "lines": sorted(set(lines))[:_HOTSPOT_LINES_CAP]}
        for file, lines in by_file.items()
    ]
    hotspots.sort(key=lambda hotspot: (-hotspot["uncovered"], hotspot["file"]))
    return hotspots


def _mutation_score(*, killed: int, survived: int) -> float:
    """The share of killed mutants among the COVERED ones: no-coverage and timeouts are not in the denominator."""
    covered = killed + survived
    return killed / covered if covered else 0.0


def _high_survivors(records: list[MutantRecord]) -> list[MutantRecord]:
    """Surviving significant mutants: what the gate wants killed by a test."""
    return [record for record in records if record["verdict"] == "survived" and record["bucket"] == "high"]


def build_report(records: list[MutantRecord], *, cold_counts: CountMap | None = None) -> AgentReport:
    """The score, unresolved and HIGH clusters, no-coverage hotspots and a one-line summary."""
    cold_kinds = dict(sorted((cold_counts or {}).items()))
    cold_no_coverage = sum(cold_kinds.values())
    verdicts = Counter(record["verdict"] for record in records)
    killed = verdicts["killed"]
    survived = verdicts["survived"]
    timeout = verdicts["timeout"]
    no_coverage = verdicts["no_coverage"]
    score = _mutation_score(killed=killed, survived=survived)

    high_survivors = _high_survivors(records)
    clusters = _clusters(high_survivors)
    unresolved_clusters = _clusters([record for record in records if record["verdict"] == "timeout"])
    hotspots = _hotspots([record for record in records if record["verdict"] == "no_coverage"])
    summary = (
        f"SCORE {score:.0%} | {len(high_survivors)} actionable / {len(clusters)} clusters | {no_coverage} uncovered"
    )
    if timeout:
        summary += f" | {timeout} timeout unresolved"
    if cold_no_coverage:
        summary += f" | {cold_no_coverage} cold advisory"
    return {
        "score": round(score, 4),
        "killed": killed,
        "survived": survived,
        "timeout": timeout,
        "unresolved": timeout,
        "no_coverage": no_coverage,
        "cold_no_coverage": cold_no_coverage,
        "cold_kinds": cold_kinds,
        "unresolved_clusters": unresolved_clusters,
        "actionable": len(high_survivors),
        "actionable_clusters": clusters,
        "uncovered_hotspots": hotspots,
        "summary": summary,
    }


def _sites_str(sites: list[Site]) -> str:
    return " · ".join(f"{site['file']}:{','.join(str(line) for line in site['lines'])}" for site in sites)


def _categories(kinds: CountMap) -> str:
    return ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))


def render_agent_report(report: AgentReport, *, max_clusters: int = 30, max_hotspots: int = 15) -> str:
    """Render the summary, the unresolved and actionable clusters and the no-coverage hotspots.

    The output is capped; the JSON report carries the full cluster list."""
    lines = [f"═══ {report['summary']} ═══"]
    unresolved = report["unresolved_clusters"]
    if unresolved:
        lines.append(f"\n▼ TIMEOUT / UNRESOLVED: {report['unresolved']} mutants need another look:")
        for cluster in unresolved[:max_clusters]:
            tests = ", ".join(cluster["covering_test_files"][:3]) or "NO coverage"
            lines.extend(
                (
                    f"  {cluster['count']}x {cluster['operator_family']}  {cluster['sample'][:100]}",
                    f"       ↳ {_sites_str(cluster['sites'])}",
                    f"       ↳ covered by: {tests}",
                )
            )
        if len(unresolved) > max_clusters:
            lines.append(f"  … +{len(unresolved) - max_clusters} unresolved clusters")
    clusters = report["actionable_clusters"]
    lines.append(f"\n▼ ACTIONABLE: {report['actionable']} HIGH survivors in {len(clusters)} clusters (largest first):")
    for cluster in clusters[:max_clusters]:
        semantic = f" [{','.join(cluster['semantic'])}]" if cluster["semantic"] else ""
        tests = ", ".join(cluster["covering_test_files"][:3]) or "NO coverage"
        lines.extend(
            (
                f"  {cluster['count']}x {cluster['operator_family']}{semantic}  {cluster['sample'][:100]}",
                f"       ↳ {_sites_str(cluster['sites'])}",
                f"       ↳ strengthen: {tests}",
            )
        )
    if len(clusters) > max_clusters:
        lines.append(f"  … +{len(clusters) - max_clusters} clusters (the JSON report has the full list)")
    hotspots = report["uncovered_hotspots"]
    if hotspots:
        lines.append(
            f"\n▼ NO COVERAGE: {report['no_coverage']} runtime sites; add a test that takes that path. Top files:"
        )
        lines.extend(f"  {hotspot['uncovered']:4}  {hotspot['file']}" for hotspot in hotspots[:max_hotspots])
        if len(hotspots) > max_hotspots:
            lines.append(f"  … +{len(hotspots) - max_hotspots} files")
    if report["cold_no_coverage"]:
        lines.append(
            f"\n▼ COLD: {report['cold_no_coverage']} advisory ({_categories(report['cold_kinds'])}); the killer is not "
            "attributed, type, schema and import-time sites need a runner of their own."
        )
    return "\n".join(lines) + "\n"


def _unattributed_cold(_file: str, _lineno: int) -> ColdKind:
    """The conservative category when the caller has no source at hand."""
    return ColdKind.UNATTRIBUTED


class SemanticSurvivor(TypedDict):
    """A HIGH survivor with a semantic tag (tenant / auth / time / transaction / ...): review it first."""

    file: str
    lineno: int
    operator_family: str
    semantic: list[str]
    sample: str


class FileCount(TypedDict):
    file: str
    count: int


class AnalysisReport(TypedDict):
    """The analysis of a finished run: priorities plus a reading of no-coverage (cold site vs a real gap)."""

    score: float
    killed: int
    survived: int
    no_coverage: int
    cold_no_coverage: int  # module/class level (schema fields, configuration): not a runtime path, not a test target
    cold_kinds: CountMap
    body_no_coverage: int  # inside a function body: a real coverage gap
    semantic_survivors: list[SemanticSurvivor]
    survivors_by_family: CountMap
    survivors_by_semantic: CountMap
    body_nocov_by_file: list[FileCount]  # real gaps per file (cold sites excluded)
    summary: str


def build_analysis(
    records: list[MutantRecord],
    reachable: Reachable,
    *,
    cold_resolver: ColdResolver = _unattributed_cold,
) -> AnalysisReport:
    """Semantic-tagged survivors first, the cold / body split of no-coverage, and the distributions.

    ``reachable(file, lineno)`` tells a site inside a function body from a module or class level one."""
    verdicts = Counter(record["verdict"] for record in records)
    killed, survived, no_cov = verdicts["killed"], verdicts["survived"], verdicts["no_coverage"]
    score = _mutation_score(killed=killed, survived=survived)
    high = _high_survivors(records)
    runtime_records, cold_kinds = split_cold_records(
        records, reachable, lambda record: cold_resolver(record["file"], record["lineno"])
    )
    body = [record for record in runtime_records if record["verdict"] == "no_coverage"]
    cold_count = sum(cold_kinds.values())
    tagged: list[SemanticSurvivor] = [
        {
            "file": record["file"],
            "lineno": record["lineno"],
            "operator_family": _family(record),
            "semantic": record["semantic"],
            "sample": f"{_collapse(record['original'])} → {_collapse(record['mutated'])}",
        }
        for record in high
        if record["semantic"]
    ]
    tagged.sort(key=itemgetter("file", "lineno"))
    by_family: CountMap = dict(Counter(_family(record) for record in high).most_common())
    by_semantic: CountMap = dict(
        Counter(tag for record in high for tag in (record["semantic"] or [_UNTAGGED])).most_common()
    )
    body_by_file: list[FileCount] = [
        {"file": file, "count": count} for file, count in Counter(record["file"] for record in body).most_common()
    ]
    summary = (
        f"SCORE {score:.0%} | {len(high)} high survivors ({len(tagged)} semantic-tagged)"
        f" | no_cov {no_cov} = {cold_count} cold + {len(body)} body"
    )
    return {
        "score": round(score, 4),
        "killed": killed,
        "survived": survived,
        "no_coverage": no_cov,
        "cold_no_coverage": cold_count,
        "cold_kinds": cold_kinds,
        "body_no_coverage": len(body),
        "semantic_survivors": tagged,
        "survivors_by_family": by_family,
        "survivors_by_semantic": by_semantic,
        "body_nocov_by_file": body_by_file,
        "summary": summary,
    }


def render_analysis(report: AnalysisReport, *, max_body_files: int = 15) -> str:
    """Human-readable analysis: semantic survivors first, the cold / body split and the distributions."""
    lines = [f"═══ {report['summary']} ═══"]
    if report["semantic_survivors"]:
        lines.append(f"\n🔴 SEMANTIC-TAGGED survivors ({len(report['semantic_survivors'])}): read their code first:")
        lines.extend(
            f"  {item['file']}:{item['lineno']} [{','.join(item['semantic'])}] {item['operator_family']}"
            f"  {item['sample'][:80]}"
            for item in report["semantic_survivors"]
        )
    lines.append(
        f"\n▼ no_cov {report['no_coverage']} = {report['cold_no_coverage']} COLD"
        f" ({_categories(report['cold_kinds']) or 'none'}; the killer is not attributed)"
        f" + {report['body_no_coverage']} BODY (real gaps). Top real gaps:"
    )
    lines.extend(f"  {item['count']:4}  {item['file']}" for item in report["body_nocov_by_file"][:max_body_files])
    families = " · ".join(f"{count}x{family}" for family, count in report["survivors_by_family"].items())
    tags = " · ".join(f"{count}x{tag}" for tag, count in report["survivors_by_semantic"].items())
    lines.extend((f"\n▼ HIGH survivors by operator: {families}", f"▼ HIGH survivors by semantic class: {tags}"))
    return "\n".join(lines) + "\n"


def _strings(raw: str) -> list[str]:
    """A JSON array column as strings; anything else reads as empty."""
    try:
        decoded: _JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str)]


def records_from_store(store: Store, files: list[str], keys: set[str] | None = None) -> list[MutantRecord]:
    """Verdicts of the given files from the store as report records.

    ``keys`` are the mutant keys of the current run: the store also holds mutants of earlier versions of a file
    (content-addressed by the function text, same path), and they must not reach the report. ``None`` means no
    filter (the analysis of the whole cache)."""
    records: list[MutantRecord] = []
    for row in store.mutant_rows_for_files(files):
        key, file, lineno, operator, verdict, bucket, original, mutated, covering_json, semantic_json, kind = row
        if keys is not None and key not in keys:
            continue
        records.append(
            {
                "key": str(key),
                "file": str(file),
                "lineno": int(lineno),
                "operator": str(operator),
                "verdict": str(verdict),
                "bucket": str(bucket),
                "original": str(original),
                "mutated": str(mutated),
                "covering": _strings(str(covering_json)),
                "semantic": _strings(str(semantic_json)),
                "kind": str(kind),
            }
        )
    return records
