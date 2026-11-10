# PyMutBench harness

The code and data behind [docs/benchmark.md](../docs/benchmark.md). The harness itself is stdlib-only
Python 3.14; everything that executes third-party code (subject projects, their test suites and the mutation
tools) runs in Docker images built from a digest-pinned `python:3.14` base, with the network disabled except
for image builds.

## Layout

| path | what it is |
|---|---|
| `Dockerfile`, `Dockerfile.tools` | the base image (CPython 3.14, git, uv) and the generation image (one virtual environment per tool) |
| `pymutbench/subjects.py` | the subject projects: repository, packages, test environment, test roots |
| `pymutbench/validate.py` | mining and validation of real-fault candidates (runs in a subject image) |
| `pymutbench/review.py` | the adjudication rubric, the two blind raters and their agreement |
| `pymutbench/scope.py` | the mutation scope of a bug: the definitions its fix touched |
| `pymutbench/adapters/` | one generation adapter per tool: the tool's own generator, every native mutant as a mutated file |
| `pymutbench/astmat.py` | verified source materialization of AST-level mutants |
| `pymutbench/oracle.py` | the reference oracle (bug mode and suite mode) |
| `pymutbench/plugin_path/pmb_pytest_plugin.py` | the pytest plugin of every reference run |
| `pymutbench/native.py` | native campaigns: each tool's own engine, timed, its verdicts mapped to artifacts |
| `pymutbench/metrics.py`, `stats.py`, `report.py`, `report_engine.py`, `svg.py` | measures, statistics, tables and figures |
| `conformance/` | small projects with one execution trap each |
| `results/` | the published data of the current release |

## Reproducing a release

```console
$ export PMB_WORK=~/pymutbench        # clones, logs and intermediate outputs (outside the repository)
$ cd bench
$ python3 -m pymutbench.orchestrate build-base
$ python3 -m pymutbench.orchestrate build-subject attrs click ...     # one image per subject
$ uv build --wheel -o .wheels .. && docker build -f Dockerfile.tools -t pymutbench-tools:1 .
$ python3 -m pymutbench.orchestrate validate-all --list                # every subject's candidates, mainline order
$ python3 -m pymutbench.orchestrate validate-all --containers 4 --jobs 3                  # screening
$ python3 -m pymutbench.review build --per-subject 25 --batch-size 5   # raters B and C, see below
$ python3 -m pymutbench.review build-tiebreak --batch-size 5
$ python3 -m pymutbench.review rate-codex --effort medium --inline --inputs inputs_tiebreak --rater tiebreak
$ python3 -m pymutbench.review finalize                                # final.jsonl, agreement.json
$ python3 -m pymutbench.orchestrate validate-all --stage validate3 --recheck validate     # final protocol
$ python3 -m pymutbench.pipeline select                                # bugs/<subject>.jsonl, bugs/selection.json
$ python3 -m pymutbench.review build-purity                            # purity packets of the selected bugs
$ python3 -m pymutbench.pipeline generate --parallel 3
$ export PMB_MEMORY=2500m                # every judging container's memory limit (unset: no limit)
$ python3 -m pymutbench.pipeline judge --parallel 3 --jobs 4
$ python3 -m pymutbench.pipeline confirm --parallel 3 --jobs 4   # every witness re-judged from fresh trees
$ python3 -m pymutbench.pipeline sensitivity --sample 1000   # stage 0/1 kills re-judged by the full suite alone
$ python3 -m pymutbench.report bugs --results results --images ../docs/images
```

Raters B and C are Claude agents (Opus and Sonnet) given `review/rubric.md`, `review/schema.json` and the batch
files, each blind to the other rater and to every tool result; they write `review/claude/` and
`review/claude_c/` in that schema. The purity labels come the same way: one Claude agent per batch of
`review/purity_inputs/`, given `review/purity_rubric.md` and `review/purity_schema.json`, writes
`review/purity/`; a selected bug without a purity label blocks the report.

`select` reports, per subject, every candidate ahead of the last selected bug
(every candidate, while the cap is not reached) that is not yet validated, adjudicated or re-validated:
`validate-all --only`, `review build --subjects` and `validate-all --recheck validate --only` process exactly
those. Every stage is resumable: a bug or a campaign whose output exists is skipped, and a partly judged bug
continues from its verdict file. Run `judge` again until every bug is complete; a bug whose container hit the
memory limit stays incomplete, so resume it alone with `--jobs 1`. Each driver process copies
`pymutbench/` once into `$PMB_WORK/snapshots/<digest>` and mounts that copy as `/bench`, so editing the working
tree never changes a running stage. Generation ledgers carry a code fingerprint per tool, oracle results the
oracle's, both with the image identity; a changed adapter means `generate --tools <tool> --force` and a changed
oracle a new judging run. `report` publishes only a complete corpus: a final selection (or subjects named with
`--accept-incomplete`), a purity label for every selected bug, every tool artifact judged, every witness
re-judged, the oracle-shortcut sample judged, one fingerprint per tool and one for the oracle; `--preview` lists
the gaps instead.

The engine and conformance tracks time each tool's own campaign, one container at a time, in images built per
project (with the test dependencies a project's suite needs at HEAD, `orchestrate.NATIVE_TEST_EXTRAS`) and for
the trap fixtures. `PMB_ENGINE_CPUS=0-3` puts each campaign container on those cores and `PMB_ENGINE_MEMORY=3g`
caps it, so a campaign that exhausts memory fails inside it; a project's reference oracle runs after its
campaigns, on the test roots they run and on `PMB_CPUS`, and judges the union of their mutants in one run.
Other benchmark work ran on two other cores beside two campaigns only: pytest-leela on sqlparse, which failed at
its 2-hour limit, and, for the first 4 of its 10 minutes, mutation on cachetools.
A campaign that leaves no report is recorded as failed
(`<tool>.r1.failed.json`), and a rerun skips it as it skips a report: to run a campaign again, delete its report
or failure record. The reference run has no resume, so a union with an artifact it has not judged is judged
again whole. A reference run that does not finish prints its exit status; 137 means the out-of-memory killer
took the oracle process itself, and the `engine` command below with the project's name and `--jobs 1` judges
that union again one mutant at a time (this release's cachetools union was judged so). `report_engine` refuses a planned campaign or oracle run that is
missing and an artifact without a reference verdict; `--preview` lists the gaps instead:

```console
$ python3 -m pymutbench.orchestrate build-native python-semver cachetools humanize toolz sqlparse attrs
$ python3 -m pymutbench.orchestrate build-conformance
$ PMB_ENGINE_CPUS=0-3 PMB_ENGINE_MEMORY=3g PMB_CPUS=0-5 python3 -m pymutbench.pipeline engine --reps 1
$ PMB_ENGINE_CPUS=0-3 PMB_ENGINE_MEMORY=3g PMB_CPUS=0-5 python3 -m pymutbench.pipeline conformance
$ python3 -m pymutbench.report_engine engine --results results --images ../docs/images
$ python3 -m pymutbench.report_engine conformance --results results --images ../docs/images
```

## Adding a tool

1. Pin the release in `Dockerfile.tools` and in `NATIVE_TOOLS` of `pymutbench/orchestrate.py`.
2. Write `pymutbench/adapters/ad_<tool>.py`: call the tool's own generator on `--files` under `--root` and emit
   one JSON line per native mutant with the complete mutated text (`_common.emit`). A mutant that has no
   faithful plain-source form is emitted with a `status` explaining why, never dropped.
3. Add the campaign to `pymutbench/native.py`: the tool's documented quick start, its report parsed into the
   normalized verdicts (killed, survived, timeout, no_coverage, error, skipped).
4. Register the tool in `generate.TOOLS`, `metrics.TOOLS` and the campaign list `pipeline.NATIVE_TOOLS`,
   rebuild the images and run the pipeline.

A new tool changes the union of judged mutants, so it is published as a new result release; earlier releases
stay as they were.

## A new release of a benchmarked tool

A tool's new release is measured in the images of the release it replaces, so only the tool's own environment
changes. For balamut: move the old wheel out of `.wheels` (`Dockerfile.tools` installs `balamut-*.whl`), name the
new one in `NATIVE_TOOLS`, then

```console
$ uv build --wheel -o .wheels ..
$ python3 -m pymutbench.orchestrate refresh-tool balamut --measured balamut-0.1.2 --unpin balamut pytest-fast
$ rm -f $PMB_WORK/out/engine/*/balamut.r1*.json $PMB_WORK/out/conformance/*/balamut.r1*.json
$ python3 -m pymutbench.pipeline generate --tools balamut --force --parallel 3
$ python3 -m pymutbench.pipeline judge --parallel 3 --jobs 4
$ python3 -m pymutbench.pipeline confirm --parallel 3 --jobs 4
$ python3 -m pymutbench.pipeline sensitivity --sample 1000   # when judge added verdicts
$ PMB_ENGINE_CPUS=0-3 PMB_ENGINE_MEMORY=3g PMB_CPUS=0-5 python3 -m pymutbench.pipeline engine --reps 1
$ PMB_ENGINE_CPUS=0-3 PMB_ENGINE_MEMORY=3g PMB_CPUS=0-5 python3 -m pymutbench.pipeline conformance
```

and the report commands above. `refresh-tool` tags every image that holds an environment of the tool (the
generation image, the trap image and the six project images) with `--measured`, unless that tag exists, and
rebuilds the image from it: the release named in `NATIVE_TOOLS` goes into the tool's existing environment, every
other installed distribution pinned to its version except those in `--unpin` (the tool's own and the
dependencies the release moves), and the command prints each environment's change. The other environments, the
reference one and the project trees stay as measured; a full rebuild would reinstall them all, with unpinned
pytest. `judge` and `confirm` judge only the artifacts a union did not have; `judge` also judges again an
artifact the oracle gated out once a new origin lifts the gate. Every verdict `judge` adds can change the stage 0/1
kills the oracle-shortcut sample is drawn from, and `report` checks only that the recorded sample is judged:
`sensitivity` draws it again from the artifacts of the current unions, and a sampled mutant that already has a
full-suite verdict keeps it. A project or trap union that gains artifacts is judged again whole, which also renews
the verdicts the other tools are compared with there.
