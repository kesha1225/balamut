"""Machine and human output of a campaign: raw JSON rows, invalidation keys, the flat bucket report."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import TypedDict

from balamut.config import DEFAULT_SEMANTIC
from balamut.core.hashing import sha
from balamut.core.semantic import SemanticPatterns, semantic_classes
from balamut.core.triage import Bucket, classify
from balamut.engine.results import MutantResult, MutantStatus

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type SourceCache = dict[str, str]
type BucketGroups = dict[Bucket, list[MutantResult]]
type ClassScore = dict[str, list[int]]  # semantic class -> [killed, total]

# Most actionable first: HIGH is worth a look, LOW is probably noise.
_BUCKET_ORDER = (Bucket.HIGH, Bucket.REVIEW, Bucket.LOW)
_BUCKET_LABEL = {
    Bucket.HIGH: "HIGH: decisions, arithmetic, state mutation, returns (look first)",
    Bucket.REVIEW: "REVIEW: ambiguous, worth a check (numeric constants, unary, augmented assignment)",
    Bucket.LOW: "LOW: probably noise (a removed logging call)",
}


class ResultJson(TypedDict):
    key: str
    path: str
    lineno: int
    site_lineno: int
    site_end_lineno: int
    start_byte: int
    end_byte: int
    operator: str
    status: str
    covering: int
    covering_tests: list[str]
    killed_by: list[str]
    bucket: str
    reason: str
    original: str
    mutated: str


def result_key(result: MutantResult) -> str:
    """The runtime key, or a structural key for a site that never ran (no identity, no cache row)."""
    if result.key:
        return result.key
    m = result.mutant
    parts = ["result-v1", m.path, str(m.lineno), str(m.site_lineno), str(m.site_end_lineno), str(m.start_byte)]
    return sha("\0".join([*parts, str(m.end_byte), m.operator, m.original, m.mutated]))


def result_json(result: MutantResult) -> ResultJson:
    m = result.mutant
    return {
        "key": result_key(result),
        "path": m.path,
        "lineno": m.lineno,
        "site_lineno": m.site_lineno,
        "site_end_lineno": m.site_end_lineno,
        "start_byte": m.start_byte,
        "end_byte": m.end_byte,
        "operator": m.operator,
        "status": result.status.value,
        "covering": result.covering,
        "covering_tests": list(result.covering_tests),
        "killed_by": list(result.killed_by),
        "bucket": classify(m).value if result.status is MutantStatus.SURVIVED else "",
        "reason": result.reason.value,
        "original": m.original,
        "mutated": m.mutated,
    }


def _invalidation_records(payload: JsonValue) -> tuple[list[JsonValue], bool]:
    """The records of a raw result array or of a final report, and whether each must carry a key."""
    if isinstance(payload, list):
        return payload, False
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON array of results or a final report object")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise ValueError("a final report must hold a findings array")
    return findings, True


def read_invalidation_keys(path: Path) -> set[str]:
    """The mutant keys of a raw result array or of a final report's findings, for cache invalidation."""
    try:
        payload: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read JSON {path}: {exc}") from exc
    records, key_required = _invalidation_records(payload)
    keys: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("invalidation records must be objects")
        if "key" not in record:
            if key_required:
                raise ValueError("every finding must carry a direct key")
            continue
        key = record["key"]
        if not isinstance(key, str):
            raise ValueError("the key field must be a string")
        if key:
            keys.add(key)
    if not keys:
        raise ValueError("the invalidation JSON holds no non-empty mutant key")
    return keys


def _group_by_bucket(survivors: list[MutantResult]) -> BucketGroups:
    groups: BucketGroups = {}
    for result in survivors:
        groups.setdefault(classify(result.mutant), []).append(result)
    return groups


def flat_report(results: list[MutantResult], *, semantic: SemanticPatterns = DEFAULT_SEMANTIC) -> None:
    """Survivors by bucket, the mutation score and the score by semantic class, on stdout."""
    counts = Counter(result.status.value for result in results)
    killed = counts.get("killed", 0)
    survived = counts.get("survived", 0)
    covered = killed + survived
    score = killed / covered if covered else 0.0
    groups = _group_by_bucket([result for result in results if result.status is MutantStatus.SURVIVED])
    for bucket in _BUCKET_ORDER:
        group = groups.get(bucket, [])
        if not group:
            continue
        sys.stdout.write(f"\n=== {_BUCKET_LABEL[bucket]}: {len(group)} ===\n")
        for result in sorted(group, key=lambda item: (-item.covering, item.mutant.path, item.mutant.lineno)):
            m = result.mutant
            sys.stdout.write(
                f"  {m.path}:{m.lineno}  [{m.operator}]  {m.original!r} -> {m.mutated!r}  ({result.covering} tests)\n"
            )
    buckets = ", ".join(f"{bucket.value} {len(groups.get(bucket, []))}" for bucket in _BUCKET_ORDER)
    sys.stdout.write(
        f"\nMUTATION SCORE: {score:.0%}  (killed {killed} / survived {survived} [{buckets}]"
        f" / timeout {counts.get('timeout', 0)} / no_coverage {counts.get('no_coverage', 0)}"
        f" / unsupported {counts.get('unsupported', 0)})\n"
    )
    by_class: ClassScore = {}
    for result in results:
        if result.status not in {MutantStatus.KILLED, MutantStatus.SURVIVED}:
            continue
        for cls in semantic_classes(result.mutant, semantic):
            slot = by_class.setdefault(cls, [0, 0])
            slot[1] += 1
            slot[0] += result.status is MutantStatus.KILLED
    if by_class:
        parts = [f"{cls} {kt[0] / kt[1]:.0%} ({kt[0]}/{kt[1]})" for cls, kt in sorted(by_class.items())]
        sys.stdout.write(f"  by semantic class: {' . '.join(parts)}\n")
