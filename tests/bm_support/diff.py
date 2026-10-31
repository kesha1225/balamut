"""A scripted ``balamut diff`` run: every external boundary of the orchestrator replaced by a recorded fake.

The scenario owns a project directory with a ``[tool.balamut]`` table (one preflight command), answers the scope,
substrate, measurement and mutation boundaries from its fields and records what the orchestrator asked for.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from balamut.cli import diff, mutate
from balamut.config import Profile, load_profile
from balamut.coverage import substrate
from balamut.coverage.measure import MeasureRequest, MeasureResult, cost_file_for, failure_file_for
from balamut.diff.scope import BaseMode, DiffScope, ScopeChanges, write_scope
from balamut.isolation import IsolationError, NoIsolation
from bm_support import write_file
from bm_support.scopes import scope
from bm_support.substrates import write_valid_coverage

type Row = dict[str, object]
type Report = dict[str, object]

SCOPE_CURRENT = ScopeChanges(current=True, paths=(), moved_refs=())
PREFLIGHT = ("check-environment",)


def raw_mutant(
    status: str, *, key: str = "mutant-key", bucket: str = "high", covering_tests: list[str] | None = None
) -> Row:
    return {
        "key": key,
        "path": "src/x.py",
        "lineno": 10,
        "site_lineno": 9,
        "site_end_lineno": 10,
        "operator": "compare:Lt->LtE",
        "status": status,
        "covering": 1,
        "covering_tests": covering_tests or ["tests/x_test.py::test_x"],
        "bucket": bucket,
        "original": "a < b",
        "mutated": "a <= b",
    }


def published_metadata(expected: substrate.SubstrateMetadata, *, generation: str) -> substrate.SubstrateMetadata:
    return replace(expected, complete=True, generation=generation, coverage_sha256="coverage-sha")


def project_profile(root: Path) -> Profile:
    return load_profile(root)


def expected(root: Path) -> substrate.SubstrateMetadata:
    return diff.expected_metadata(project_profile(root), NoIsolation())


def coverage_path(root: Path) -> Path:
    return root / "coverage.substrate"


def write_project(root: Path, *, preflight: bool = True) -> None:
    write_file(root / "pyproject.toml", '[tool.balamut]\nconfig = "balamut_conf.py"\n')
    if preflight:
        body = "from balamut.config import init\n\ninit(preflight=(('check-environment',),))\n"
    else:
        body = "from balamut.config import init\n\ninit()\n"
    write_file(root / "balamut_conf.py", body)


@dataclass(slots=True)
class Scenario:
    root: Path
    rows: list[Row] = field(default_factory=list[Row])
    preflight_returncode: int = 0
    preflight_error: OSError | None = None
    probe_error: IsolationError | None = None
    coverage_returncode: int = 0
    focused_coverage_returncode: int | None = None
    coverage_failed_nodeids: tuple[str, ...] = ()
    mutation_returncode: int = 0
    mutation_error: OSError | None = None
    refresh_failed_nodeids: tuple[str, ...] = ()
    raw_text: str | None = None
    stats_payload: Row | None = None
    omit_raw: bool = False
    omit_stats: bool = False
    scope_error: OSError | None = None
    scope_changes: ScopeChanges = SCOPE_CURRENT
    cached_metadata: substrate.SubstrateMetadata | None = None
    # A cached substrate is taken by the current code, with its tree record; without one reuse is unprovable.
    tree_recorded: bool = True
    foreign_generation: str | None = None
    cache_resolved: int = 0
    executed: int | None = None
    pool_seed: substrate.SubstrateMetadata | None = None
    clock_fn: Callable[[], float] = time.monotonic
    events: list[str] = field(default_factory=list[str])
    commands: list[diff.CommandInvocation] = field(default_factory=list[diff.CommandInvocation])
    measures: list[MeasureRequest] = field(default_factory=list[MeasureRequest])
    ensure_force: bool | None = None
    pool_shared: list[str] = field(default_factory=list[str])
    reports: list[Report] = field(default_factory=list[Report])
    real_substrate: bool = False

    def __post_init__(self) -> None:
        if not (self.root / "pyproject.toml").exists():
            write_project(self.root)
        if self.cached_metadata is None or not self.tree_recorded:
            return
        layout = project_profile(self.root).layout
        tree = substrate.tree_snapshot(layout)
        drift = substrate.tree_drift(
            coverage_path(self.root), self.cached_metadata.generation, tree, in_diff=frozenset(), layout=layout
        )
        if drift is not None and drift.reason == "tree_unknown":
            substrate.tree_write(coverage_path(self.root), self.cached_metadata.generation, tree)

    def command_run(self, invocation: diff.CommandInvocation) -> int:
        self.commands.append(invocation)
        argv = invocation.argv
        if argv == PREFLIGHT:
            self.events.append("preflight")
            if self.preflight_error is not None:
                raise self.preflight_error
            return self.preflight_returncode
        if "mutate" in argv:
            return self._mutate(invocation)
        raise AssertionError(f"unexpected command: {argv}")

    def _mutate(self, invocation: diff.CommandInvocation) -> int:
        argv = invocation.argv
        self.events.append("mutation")
        if self.mutation_error is not None:
            raise self.mutation_error
        if self.refresh_failed_nodeids:
            failures_path = Path(argv[argv.index("--refresh-failures-out") + 1])
            failures_path.write_text(
                json.dumps({"schema_version": 1, "failed_nodeids": list(self.refresh_failed_nodeids)}), "utf-8"
            )
            return mutate.EXIT_REFRESH_BASELINE_FAILURE
        if invocation.stdout_path is not None and not self.omit_raw:
            invocation.stdout_path.write_text(
                json.dumps(self.rows) if self.raw_text is None else self.raw_text, "utf-8"
            )
        counts = Counter(str(row["status"]) for row in self.rows)
        executed = len(self.rows) if self.executed is None else self.executed
        payload = self.stats_payload or {
            "schema_version": 1,
            "complete": True,
            "enumerated": len(self.rows),
            "cache_resolved": self.cache_resolved,
            "executed": executed,
            "terminal": executed,
            "status_counts": {
                status: counts.get(status, 0)
                for status in ("killed", "survived", "timeout", "no_coverage", "unsupported")
            },
        }
        if not self.omit_stats:
            Path(argv[argv.index("--stats-json") + 1]).write_text(json.dumps(payload), encoding="utf-8")
        return self.mutation_returncode

    def measure(self, request: MeasureRequest) -> MeasureResult:
        self.measures.append(request)
        focused = bool(request.targets)
        self.events.append("coverage-confirm" if focused else "coverage-build")
        returncode = (
            self.focused_coverage_returncode
            if focused and self.focused_coverage_returncode is not None
            else self.coverage_returncode
        )
        if self.real_substrate and not focused:
            write_valid_coverage(request.coverage_file)
        failed = self.coverage_failed_nodeids if returncode != 0 else ()
        return MeasureResult(
            returncode,
            request.coverage_file,
            cost_file_for(request.coverage_file),
            failure_file_for(request.coverage_file),
            failed_nodeids=failed,
        )

    def isolation_probe(self) -> None:
        self.events.append("isolation-probe")
        if self.probe_error is not None:
            raise self.probe_error

    def scope_resolve(self, base_ref: str, base_mode: BaseMode) -> DiffScope:
        self.events.append("scope-resolve")
        if self.scope_error is not None:
            raise self.scope_error
        assert base_ref == "origin/main"
        assert base_mode is BaseMode.MERGE_BASE
        return scope()

    def scope_write(self, pinned: DiffScope, path: Path) -> None:
        self.events.append("scope-write")
        write_scope(pinned, path)

    def scope_check(self, pinned: DiffScope, repo_root: Path) -> ScopeChanges:
        self.events.append("scope-current")
        assert pinned == scope()
        assert repo_root == self.root.resolve()
        return self.scope_changes

    def substrate_inspect(self, path: Path) -> substrate.SubstrateMetadata | None:
        self.events.append("substrate-inspect")
        if self.real_substrate:
            return substrate.substrate_inspect(path)
        return self.cached_metadata

    def substrate_ensure(
        self,
        path: Path,
        wanted: substrate.SubstrateMetadata,
        build: substrate.SubstrateBuilder,
        build_mode: diff.SubstrateBuildMode,
        tree: substrate.TreeSnapshotter,
        unmeasured: substrate.UnmeasuredFiles,
    ) -> substrate.SubstrateMetadata:
        self.events.append("substrate-ensure")
        self.ensure_force = build_mode is diff.SubstrateBuildMode.REBUILD
        if self.real_substrate:
            return substrate.substrate_ensure(
                path, wanted, build=build, force_rebuild=self.ensure_force, tree=tree, unmeasured=unmeasured
            )
        if self.foreign_generation is not None:
            # A neighbouring run of the same tree published a generation while this one waited for the lock.
            return published_metadata(wanted, generation=self.foreign_generation)
        if self.cached_metadata is not None and build_mode is diff.SubstrateBuildMode.REUSE:
            return self.cached_metadata
        measured = tree()
        returncode = build(self.root / "temporary.coverage")
        if returncode != 0:
            raise substrate.SubstrateBuildError(f"the coverage build exited with {returncode}")
        substrate.tree_write(path, "rebuilt-generation", measured)  # published with its generation, as for real
        return published_metadata(wanted, generation="rebuilt-generation")

    def substrate_seed(
        self,
        _repo_root: Path,
        path: Path,
        _expected: substrate.SubstrateMetadata,
        current: substrate.TreeRecord,
        _in_diff: frozenset[str],
    ) -> substrate.SubstrateMetadata | None:
        self.events.append("substrate-seed")
        if self.pool_seed is not None:
            substrate.tree_write(path, self.pool_seed.generation, current)
            self.cached_metadata = self.pool_seed
        return self.pool_seed

    def substrate_share(self, _repo_root: Path, _path: Path, published: substrate.SubstrateMetadata) -> None:
        self.pool_shared.append(published.generation)

    def report_sink(self, report: Mapping[str, object]) -> None:
        self.reports.append(dict(report))

    def runtime(self) -> diff.DiffRuntime:
        return diff.DiffRuntime(
            command_run=self.command_run,
            measure=self.measure,
            isolation_probe=self.isolation_probe,
            scope_resolve=self.scope_resolve,
            scope_write=self.scope_write,
            scope_changes=self.scope_check,
            substrate_inspect=self.substrate_inspect,
            substrate_ensure=self.substrate_ensure,
            substrate_seed=self.substrate_seed,
            substrate_share=self.substrate_share,
            clock=self.clock_fn,
            report_sink=self.report_sink,
        )


def main_args(root: Path, *extra: str) -> list[str]:
    return [
        "--base",
        "origin/main",
        "--coverage",
        str(coverage_path(root)),
        "--json-out",
        str(root / "raw.json"),
        "--report-json",
        str(root / "report.json"),
        *extra,
    ]


def run_main(scenario: Scenario, *extra: str, runtime: diff.DiffRuntime | None = None) -> tuple[int, Report]:
    root = scenario.root
    return_code = diff.main(main_args(root, *extra), runtime=runtime or scenario.runtime(), root=root)
    report: Report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    return return_code, report


def mutation_argv(scenario: Scenario) -> tuple[str, ...]:
    return next(command.argv for command in scenario.commands if "mutate" in command.argv)


def python_executable() -> str:
    return sys.executable


type Section = dict[str, object]


def section(report: Report, name: str) -> Section:
    """A nested object of a JSON report, typed for assertions."""
    value = report[name]
    assert isinstance(value, dict)
    return cast("Section", value)


def findings(report: Report) -> list[Section]:
    value = report["findings"]
    assert isinstance(value, list)
    return [cast("Section", item) for item in cast("list[object]", value) if isinstance(item, dict)]
