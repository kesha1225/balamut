# Onboarding: adding balamut to a project

A walkthrough of wiring balamut into an existing pytest project, using **attrs** as the example.
Every command and number below comes from a real run against `attrs 26.1.0` (1,382 tests, a ~5s
suite), so the timings are representative of a mid-sized pure-Python library.

## 0. Prerequisites

* Python 3.14+ (the bytecode fast path and `sys.monitoring` events are version-specific).
* A green test suite on the original code — balamut measures the suite first and treats tests
  that fail on the original as baseline failures, never as kills.
* POSIX (daemons talk over Unix sockets); git for the diff gate.

## 1. Install and scaffold

```console
$ pip install balamut            # or: uv add --dev balamut
$ balamut new
wrote .../pyproject.toml and .../balamut_conf.py (source=('src',), base='main')
```

`balamut new` detects the layout (attrs is `src/` + `tests/`), finds the live branch as the diff
base, and writes two things:

```toml
# pyproject.toml — the only wiring the project needs
[tool.balamut]
config = "balamut_conf.py"
```

```python
# balamut_conf.py — the whole profile in typed Python
from balamut.config import init

init(
    source=("src",),
    import_roots=("src",),
    base="main",
)
```

Everything else is optional and lives in this one file: preflight commands (`preflight=(("make",
"db-ready"),)`), database isolation, your own operator kinds via `init(register={...})`. See
[docs/configuration.md](configuration.md).

## 2. First run: the diff gate

Run the gate over the current branch; on a fresh checkout the diff is empty, which is exactly the
"does the pipeline work at all" smoke test:

```console
$ balamut diff
balamut diff: scope pinned (0 files in the diff); an edit of any of them before the verdict stops the run as SCOPE_CHANGED
balamut diff: full coverage substrate build (reason: missing_coverage)
cache: /.../.balamut/cache/attrs_main_72fc24.sqlite
baseline coverage -> store: N (test, line) rows
nothing to mutate (an empty diff or no paths)
balamut diff: CLEAN
```

What happened: the first run measured the whole suite once (~seconds for attrs), stored
**per-test, per-line coverage** in the cache, and reported CLEAN because an empty diff has nothing
to judge. From now on every run reuses that substrate; changed files re-measure only the tests
that import them.

## 3. A full campaign

`mutate` judges every covered site of a path, not just the diff:

```console
$ balamut mutate src/attr --progress none
mechanisms: ... code / ... cold (module or class level)
mutants: 1062, daemons: auto x 1 workers
parallelism: daemons auto -> 3 x 1 workers = 3 (... uncached mutants, shared environment)
campaign final: done=1062 killed=888 survived=82 timeout=0 no_coverage=83
═══ SCORE 92% | 65 actionable / 37 clusters | 16 uncovered | ... cold advisory ═══
```

The numbers above are the real first cold run (≈150s including the substrate build). The score is
`killed / (killed + survived)`; the tail of the output groups the survivors into clusters with the
exact test files to strengthen:

```
▼ ACTIONABLE: ... survivors in ... clusters (largest first):
  4x src  False → True
       ↳ src/attr/_make.py:... · ...
       ↳ strengthen: tests/test_make.py, tests/test_next_gen.py
```

`--agent-json` emits the same clusters as JSON for a coding agent: survivors grouped by mutation,
the covering tests to strengthen, the uncovered lines to cover.

## 4. The find → fix → confirm loop

Take one survivor, write the test that pins the contract, and re-judge only that path:

```console
$ balamut confirm src/attr/_make.py --tests tests/test_make.py
```

`confirm` re-measures just those tests and re-runs just those mutants — the inner loop is seconds,
not the whole campaign. Once the clusters close, re-run the gate for the verdict.

## 5. CI: gate every pull request

```yaml
# .github/workflows/mutation.yml
name: mutation
on: [pull_request]
jobs:
  balamut:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # the diff gate needs the base ref
      - uses: actions/setup-python@v5
        with:
          python-version: "3.14"
      - run: pip install -e . balamut
      - run: balamut diff --report-json mutation-report.json
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: mutation-report
          path: mutation-report.json
```

The gate exits 0 for CLEAN, 1 for findings, 2 for infrastructure problems, 3 when the scope
changed mid-run. `--report-json` moves the machine-readable report out of the gitignored
`.balamut/` into the artifact.

## 6. State and hygiene

* Everything balamut writes lives in `.balamut/` (gitignored by the scaffold): the verdict cache,
  the coverage substrate, daemon logs. Delete it freely — the next run rebuilds it.
* Keep the suite green: balamut never counts a baseline failure as a kill, and a red suite makes
  the substrate build fail closed.
* `balamut estimate src/attr` predicts the campaign duration before you pay for it;
  `balamut analyze` reads the cached verdicts afterwards without re-running anything.
