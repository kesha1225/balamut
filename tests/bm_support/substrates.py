"""Substrate fixtures: expected metadata and a minimal valid coverage file."""

from __future__ import annotations

from pathlib import Path

from coverage import CoverageData

from balamut.coverage.substrate import SubstrateMetadata


def expected_substrate_metadata(*, config: str = "config-v1", inventory: str = "inventory-v1") -> SubstrateMetadata:
    """Expected substrate metadata; the fingerprints are labels compared with each other, not real values."""
    return SubstrateMetadata(
        schema_version=1,
        complete=False,
        generation="",
        config_fingerprint=config,
        test_inventory_fingerprint=inventory,
        runtime_fingerprint="runtime-v1",
        schema_fingerprint="schema-v1",
        coverage_sha256="",
    )


def write_valid_coverage(path: Path) -> int:
    """Write a minimal valid coverage file with one test context; a builder that succeeds."""
    data = CoverageData(basename=str(path))
    data.set_context("tests/x_test.py::test_x|run")
    data.add_lines({"src/x.py": {1}})
    data.write()
    return 0
