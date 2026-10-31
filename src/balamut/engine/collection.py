"""Exact pytest collection of oracle tests with a content-addressed cache.

An oracle mutant edits a test, so its covering tests are the node ids that test file collects: parametrized ids
included, which coverage contexts do not name exactly. The cache key covers everything a collection depends on,
and only a non-empty successful collection is stored.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from balamut.coverage import substrate
from balamut.coverage.measure import original_environment
from balamut.coverage.store import Store
from balamut.diff.scope import DiffScope

type ReexportView = Mapping[tuple[str, str], str]

_COLLECT_TIMEOUT_SECONDS = 300


class CollectionError(RuntimeError):
    """pytest could not build the exact collection of oracle tests."""


@dataclass(frozen=True, slots=True)
class CollectionKey:
    """The content-addressed key of one exact pytest collection."""

    value: str


def build_collection_key(
    paths: frozenset[str],
    *,
    scope: DiffScope,
    metadata: substrate.SubstrateMetadata,
    reexport: ReexportView,
    root: Path,
) -> CollectionKey:
    """A key from the pinned scope, the substrate, the re-export graph and the content of the targets."""
    target_hashes = [(path, substrate.file_sha256(root / path)) for path in sorted(paths)]
    reexport_entries = sorted((definition, name, wrapper) for (definition, name), wrapper in reexport.items())
    payload = {
        "metadata": asdict(metadata),
        "reexport": reexport_entries,
        "scope": asdict(scope),
        "targets": target_hashes,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return CollectionKey(hashlib.sha256(encoded.encode("utf-8")).hexdigest())


def _streams(result: subprocess.CompletedProcess[str]) -> str:
    return f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def collect_test_nodeids(
    paths: frozenset[str],
    *,
    store: Store,
    cache_key: CollectionKey | None,
    root: Path,
) -> frozenset[str]:
    """The exact node ids of ``paths``, reusing only a provably compatible cached collection.

    pytest runs in the project root on the original code (an inherited mutation plugin is dropped) and with the
    project's marker filter cleared: a mutant is judged by every test that collects, not by a default selection."""
    if not paths:
        return frozenset()
    if cache_key is not None:
        cached = store.get_collection(cache_key.value)
        if cached is not None:
            return cached

    argv = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "", "-p", "no:cacheprovider", *sorted(paths)]
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            env=original_environment(),
            capture_output=True,
            text=True,
            timeout=_COLLECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CollectionError(f"could not run pytest --collect-only: {error}") from error
    if result.returncode != 0:
        raise CollectionError(f"pytest --collect-only exited with {result.returncode}\n{_streams(result)}")

    nodeids = frozenset(line.strip() for line in result.stdout.splitlines() if "::" in line)
    if not nodeids:
        raise CollectionError(f"pytest --collect-only succeeded but returned no node ids\n{_streams(result)}")
    if cache_key is not None:
        store.put_collection(cache_key.value, nodeids)
    return nodeids
