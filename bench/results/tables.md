### Detection

Operational rate: a bug the tool's generator failed on counts as not detected. Conditional rate: over the bugs the tool's generation succeeded on. Intervals: 95%, bootstrap over projects, pointwise.

| tool | bugs detected | rate (95% CI) | project mean | leave one project out | without arrow | conditional rate (bugs) | unavailable |
|---|---|---|---|---|---|---|---|
| mutation | 201/325 | 62% (53%–71%) | 60% | 60%–64% | 60% | 62% (325) | 0 |
| universalmutator | 184/325 | 57% (49%–65%) | 56% | 55%–58% | 58% | 57% (325) | 0 |
| poodle | 178/325 | 55% (47%–63%) | 54% | 53%–56% | 53% | 55% (325) | 0 |
| deployproof | 143/325 | 44% (37%–51%) | 44% | 42%–45% | 42% | 44% (325) | 0 |
| mutmut | 142/325 | 44% (35%–53%) | 45% | 41%–45% | 45% | 44% (325) | 0 |
| cosmic-ray | 139/325 | 43% (36%–50%) | 43% | 41%–44% | 43% | 43% (325) | 0 |
| irradiate | 139/325 | 43% (34%–51%) | 43% | 41%–45% | 43% | 43% (325) | 0 |
| fest | 134/325 | 41% (34%–49%) | 41% | 40%–42% | 40% | 41% (325) | 0 |
| balamut | 131/325 | 40% (33%–48%) | 41% | 38%–42% | 41% | 40% (325) | 0 |
| pytest-leela | 93/325 | 29% (22%–36%) | 28% | 26%–30% | 29% | 29% (325) | 0 |
| pytest-gremlins | 81/325 | 25% (19%–32%) | 26% | 23%–26% | 25% | 25% (325) | 0 |

### Budgets

R(q): q of the tool's in-scope emissions drawn without replacement (unmaterializable ones included). S(k): k of the unique artifacts of the tool that the reference oracle judges survivors (the tool's own engine may report others). Both use min(budget, population) draws and are 0 for an empty population.

| tool | R(1) | R(10) (95% CI) | R(100) | S(1) | S(3) (95% CI) | S(10) | in-scope emissions / bug (median) | survivors / bug (median) |
|---|---|---|---|---|---|---|---|---|
| mutation | 5% | 22% (16%–28%) | 50% | 19% | 34% (27%–41%) | 48% | 133 | 18 |
| universalmutator | 6% | 26% (20%–32%) | 49% | 18% | 33% (27%–38%) | 47% | 56 | 11 |
| poodle | 8% | 30% (24%–37%) | 51% | 20% | 35% (29%–41%) | 48% | 38 | 8 |
| deployproof | 8% | 29% (23%–35%) | 42% | 20% | 32% (27%–38%) | 40% | 22 | 4 |
| mutmut | 6% | 22% (17%–28%) | 40% | 16% | 27% (21%–33%) | 37% | 35 | 6 |
| cosmic-ray | 7% | 22% (17%–27%) | 39% | 15% | 26% (21%–31%) | 35% | 40 | 8 |
| irradiate | 6% | 22% (16%–28%) | 39% | 16% | 27% (21%–34%) | 37% | 39 | 6 |
| fest | 7% | 26% (20%–32%) | 40% | 21% | 32% (26%–39%) | 38% | 24 | 3 |
| balamut | 7% | 23% (18%–29%) | 39% | 18% | 30% (24%–36%) | 38% | 25 | 3 |
| pytest-leela | 7% | 21% (16%–27%) | 28% | 16% | 24% (18%–29%) | 28% | 9 | 1 |
| pytest-gremlins | 7% | 20% (14%–26%) | 25% | 14% | 20% (15%–26%) | 24% | 8 | 1 |

### Variants (outside the ranking and the comparisons)

balamut's ranking row uses the source kinds `balamut mutate` runs by default (src, concurrency, fault; its default oracle kind mutates tests, outside the scope), the file-level default every tool gets. The variant adds the extreme kind, which `balamut diff` also runs by default on changed code: it measures that kind under the benchmark's scope and oracle, not the `balamut diff` workflow.

| tool | bugs detected | rate (95% CI) | project mean | leave one project out | without arrow | conditional rate (bugs) | unavailable |
|---|---|---|---|---|---|---|---|
| balamut + extreme | 161/325 | 50% (42%–57%) | 50% | 48%–51% | 50% | 50% (325) | 0 |

| tool | R(1) | R(10) (95% CI) | R(100) | S(1) | S(3) (95% CI) | S(10) | in-scope emissions / bug (median) | survivors / bug (median) |
|---|---|---|---|---|---|---|---|---|
| balamut + extreme | 7% | 27% (21%–32%) | 47% | 21% | 35% (30%–42%) | 45% | 33 | 5 |

### Paired with balamut

Discordant bugs over all bugs (operational); the difference is balamut's rate minus the tool's, with a 95% interval from the project bootstrap. McNemar mid-p and Holm are descriptive: they ignore the clustering by project.

| tool | balamut only | tool only | difference (95% CI) | mid-p | Holm | both available (bugs / projects) |
|---|---|---|---|---|---|---|
| mutation | 14 | 84 | -21.5 pp (-29.6 to -13.8) | 1.43e-13 | 1.29e-12 | 325 / 26 |
| universalmutator | 12 | 65 | -16.3 pp (-23.0 to -9.8) | 3.49e-10 | 2.79e-09 | 325 / 26 |
| poodle | 9 | 56 | -14.5 pp (-20.9 to -8.8) | 1.18e-09 | 8.28e-09 | 325 / 26 |
| deployproof | 22 | 34 | -3.7 pp (-11.1 to +2.6) | 0.111 | 0.556 | 325 / 26 |
| mutmut | 26 | 37 | -3.4 pp (-10.5 to +2.9) | 0.169 | 0.675 | 325 / 26 |
| cosmic-ray | 27 | 35 | -2.5 pp (-8.1 to +2.8) | 0.314 | 0.941 | 325 / 26 |
| irradiate | 38 | 46 | -2.5 pp (-10.4 to +6.0) | 0.386 | 0.941 | 325 / 26 |
| fest | 26 | 29 | -0.9 pp (-8.4 to +6.0) | 0.689 | 0.941 | 325 / 26 |
| pytest-leela | 43 | 5 | +11.7 pp (+7.5 to +16.0) | 7.6e-09 | 4.56e-08 | 325 / 26 |
| pytest-gremlins | 52 | 2 | +15.4 pp (+10.7 to +19.7) | 8.55e-14 | 8.55e-13 | 325 / 26 |

### Mutant accounting (unique in-scope mutants, bugs the tool's generation succeeded on)

| tool | in-scope emissions | unique | killed | survived | of which witnesses | invalid | equivalent (TCE) | no-op | unresolved |
|---|---|---|---|---|---|---|---|---|---|
| mutation | 110169 | 105383 | 90744 | 14286 | 1558 | 77 | 200 | 0 | 76 |
| universalmutator | 48313 | 48313 | 39808 | 8418 | 1347 | 0 | 5 | 0 | 82 |
| cosmic-ray | 36826 | 36825 | 28166 | 8230 | 1207 | 372 | 22 | 0 | 35 |
| irradiate | 38432 | 36076 | 29538 | 6463 | 935 | 0 | 36 | 1 | 38 |
| mutmut | 30363 | 30353 | 24272 | 6037 | 836 | 0 | 12 | 0 | 32 |
| poodle | 29467 | 29046 | 20956 | 7080 | 981 | 989 | 2 | 0 | 19 |
| balamut | 20795 | 20522 | 17406 | 3098 | 531 | 2 | 0 | 0 | 16 |
| fest | 17638 | 17587 | 13025 | 4228 | 610 | 98 | 215 | 10 | 11 |
| deployproof | 17565 | 17562 | 12599 | 4925 | 721 | 0 | 20 | 0 | 18 |
| pytest-leela | 7736 | 7552 | 6437 | 1105 | 250 | 0 | 0 | 0 | 10 |
| pytest-gremlins | 6515 | 6513 | 5196 | 1305 | 237 | 0 | 0 | 0 | 12 |

### Witness flow (unique artifacts)

A first-judgement witness counts when the re-judgement from fresh trees reproduces it (re-judgements without an outcome are unknown, the others not reproduced), the same fail-to-pass test is red with an outcome of its own in both judgements (red only through a collection or startup failure of the whole run is synthetic) and that test checks the fault (otherwise feature only).

| tool | first-judgement witnesses | re-judgement unknown | not reproduced | synthetic | feature only | witnesses |
|---|---|---|---|---|---|---|
| mutation | 1635 | 0 | 14 | 0 | 63 | 1558 |
| universalmutator | 1388 | 0 | 4 | 0 | 37 | 1347 |
| poodle | 997 | 0 | 1 | 0 | 15 | 981 |
| deployproof | 742 | 0 | 0 | 0 | 21 | 721 |
| mutmut | 849 | 0 | 1 | 0 | 12 | 836 |
| cosmic-ray | 1234 | 0 | 9 | 0 | 18 | 1207 |
| irradiate | 959 | 0 | 2 | 0 | 22 | 935 |
| fest | 634 | 0 | 2 | 0 | 22 | 610 |
| balamut | 559 | 0 | 1 | 0 | 27 | 531 |
| pytest-leela | 260 | 0 | 1 | 0 | 9 | 250 |
| pytest-gremlins | 247 | 0 | 0 | 0 | 10 | 237 |

### Undecided outcomes

Artifacts that could still be witnesses: an unresolved historical run, a survivor whose fail-to-pass run gave no outcome, or a witness whose re-judgement gave none. The optimistic rate counts every bug with one of them as detected.

| tool | undecided artifacts | bugs undetected with an undecided artifact | rate | optimistic rate |
|---|---|---|---|---|
| mutation | 77 | 10 | 62% | 65% |
| universalmutator | 82 | 7 | 57% | 59% |
| poodle | 19 | 6 | 55% | 57% |
| deployproof | 18 | 7 | 44% | 46% |
| mutmut | 32 | 6 | 44% | 46% |
| cosmic-ray | 37 | 7 | 43% | 45% |
| irradiate | 38 | 5 | 43% | 44% |
| fest | 11 | 5 | 41% | 43% |
| balamut | 16 | 6 | 40% | 42% |
| pytest-leela | 10 | 6 | 29% | 30% |
| pytest-gremlins | 12 | 6 | 25% | 27% |

### By stratum

Where the fix is: inside functions (220 bugs), in class-body or module-level statements (38), both (67). Coverage, retrospective (the fail-to-pass run includes feature tests): the fail-to-pass run executes at least one line the fix changed and the historical suite executed every such line (covered, 222 bugs) or not every one (gap, 97), or it executes none of them (6).

| tool | fix in functions | fix in class/module statements | fix in both | changed lines covered | coverage gap | no changed line run | witness by any fail-to-pass test |
|---|---|---|---|---|---|---|---|
| mutation | 60% | 58% | 70% | 52% | 88% | 17% | 62% |
| universalmutator | 59% | 29% | 64% | 43% | 90% | 33% | 57% |
| poodle | 53% | 47% | 66% | 39% | 92% | 33% | 55% |
| deployproof | 40% | 50% | 55% | 28% | 82% | 17% | 44% |
| mutmut | 46% | 11% | 54% | 30% | 77% | 17% | 44% |
| cosmic-ray | 45% | 16% | 52% | 26% | 82% | 17% | 43% |
| irradiate | 47% | 11% | 48% | 31% | 72% | 17% | 43% |
| fest | 38% | 39% | 52% | 23% | 84% | 17% | 42% |
| balamut | 40% | 13% | 55% | 23% | 82% | 0% | 41% |
| pytest-leela | 29% | 13% | 37% | 12% | 69% | 0% | 29% |
| pytest-gremlins | 25% | 11% | 33% | 9% | 63% | 0% | 26% |

### Secondary measures

Only this tool: bugs no other ranked tool witnesses. Re-creates the fault: single-file fixes where a mutant whose edit touches a line the fix changed turns the fixed file into the buggy one (equal ASTs).

| tool | only this tool | re-creates the fault (applicable bugs) |
|---|---|---|
| mutation | 3 | 2 (296) |
| universalmutator | 2 | 2 (296) |
| poodle | 1 | 2 (296) |
| deployproof | 1 | 0 (296) |
| mutmut | 1 | 3 (296) |
| cosmic-ray | 1 | 2 (296) |
| irradiate | 0 | 0 (296) |
| fest | 0 | 1 (296) |
| balamut | 0 | 3 (296) |
| pytest-leela | 0 | 1 (296) |
| pytest-gremlins | 0 | 1 (296) |

Bugs: 325 in 26 projects; witnessed by at least one tool: 70%; coverage-guided (the fail-to-pass tests run a line of the repair scope that the historical suite never ran; retrospective): 31%.

### Oracle shortcuts

1000 kills by a single test or the covering subset, drawn uniformly without replacement (seed 20261105) from 157058 over 207 bugs, judged again by the whole historical suite in canonical order alone: killed 998, survived 0, other 2 ({"killed": 998, "unresolved": 2}). Killed of the decided outcomes: 100.0% (998/998); of the sample: 99.8% (998/1000). By the first judgement's stage: {"killer": {"killed": 879, "unresolved": 2}, "subset": {"killed": 119}}.

| bug | artifact | first stage | first killed by | whole suite | fault tests red | tools |
|---|---|---|---|---|---|---|
| cattrs@02c3ead21d62 | a6624c9b0207 | killer | tests/preconf/test_pyyaml.py::test_pyyaml | unresolved (<external-kill>) | – | mutation |
| portion@fc83f86848ea | a4a7a7060a48 | killer | tests/test_doc.py::test_readme | unresolved (<external-kill>) | – | universalmutator |
