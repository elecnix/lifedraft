# Repo tooling

- [`coverage_gate.py`](#coverage_gatepy--the-per-file-coverage-gate) — zero-coverage
  detector + per-file uncovered-line ratchet.
- [`perf_gate.py`](#perf_gatepy--the-per-test-runtime-regression-gate) — always-on
  test-duration profiler + per-test runtime regression gate.
- [`mutation_guard.py`](#mutation_guardpy--curated-fast-mutation-guard-for-dp11dp18) — curated,
  fast mutation guard for DP#11/DP#18.

---

# Test contract (DP#11 — every library module has a unit test)

The `tools/` scripts are CI/build utilities, not simulation library code, so
they are **excluded from the per-file coverage gate the right way** — via
`omit = ["tools/*"]` in `[tool.coverage.run]` in `pyproject.toml` (with the
explanatory comment there) — not by inflating `coverage_baseline.json`.
Measuring them under pytest would brand them dead when they are not; they are
exercised by `tests.yml` instead.

"Excluded from the coverage gate" is not the same as "has no test contract."
DP#11 still asks that every module with real library logic be unit-tested, so
the invariant is made explicit per module:

| script | test file | what is covered |
|---|---|---|
| `coverage_gate.py` | `tests/test_coverage_gate.py` | the three gates (A/B/C), `run_gates`, `update_baseline`, the auto-tightening ratchet |
| `perf_gate.py` | `tests/test_perf_gate.py` | `is_regression` thresholds, `run_gate` flags/ignores regressions, `update_baseline` wholesale replace |

---

# `coverage_gate.py` — the per-file coverage gate

```bash
# measure
pytest -q --cov --cov-report=json:coverage.json --cov-report=term-missing:skip-covered

# enforce (what CI runs)
python tools/coverage_gate.py --check

# regenerate the committed baseline, then commit tools/coverage_baseline.json
python tools/coverage_gate.py --update

# findings: zero-coverage files, worst offenders
python tools/coverage_gate.py --report
```

## Why not a repo-wide percentage

Because this repo has already run that experiment, and it failed.

`pyproject.toml` carried `fail_under = 93`. Two things were wrong with it:

1. **It never ran.** `coverage` was not in the dev extras and no workflow invoked it.
   It was config that is parsed and never read — the exact defect class this project
   exists to eliminate. It had been sitting there looking like enforcement.
2. **Even if it had run, it would have been gamed — and demonstrably was.** The repo
   contains `tests/test_push_93_97.py`, whose docstring is literally *"Coverage push:
   strategy, attribution, optimizer, scipy, mc — 93→97% targets."* It raises the
   percentage by testing `countries/canada/attribution.py` — a module with **zero
   production callers** (issue #702). The number went up. The dead code stayed dead.

That is what a global percentage buys at this size: at ~40k statements, 200 brand-new
untested lines move it by roughly half a percent, so a threshold loose enough not to
fire on noise is loose enough to let an entire dead module through. It measures how
much code the tests *touch*, which is not the question. The question is whether any
file is unguarded, and whether this diff made any file worse.

## The metric: uncovered LINE COUNT, not percentage

Gate B ratchets on the number of uncovered lines per file. The alternative — ratcheting
each file's coverage *percentage* — was considered and rejected:

- **A percentage masks the bug we actually care about.** Take a file at 90/100
  statements (90%). It gains 20 well-tested lines and one untested guard clause:
  110/121 = **90.9%**. The percentage went *up*, so a percent-ratchet **passes** — and
  an untested guard clause, i.e. a rule that can silently do nothing, just landed. The
  uncovered-line ratchet goes 10 → 11, fails, and prints the line number.
  (`tests/test_coverage_gate.py::test_gate_b_catches_the_line_a_percentage_ratchet_would_mask`
  pins this.)
- **A percentage trips for reasons unrelated to test quality.** It is a ratio of two
  moving numbers. Deleting a covered line from a small file (3/4 → 2/3) drops it from
  75% to 67% and fails a ratchet for a change that *removed code*. Fixing that needs a
  rounding fudge and a minimum-file-size rule — two more tunables to argue about.
- **The line count needs none of that.** It is monotone and unambiguous: unmoved by
  adding *covered* code (which is what we want to encourage), it rises if and only if
  untested statements enter the file, and it is immediately actionable because it names
  the lines.

So the rule is just: **the number of uncovered lines in a file must not increase.**

## The three gates

| Gate | Fires when… |
|---|---|
| **A** — zero coverage | a tracked production file has statements but **zero** covered lines. Nothing imports it; nothing runs it. |
| **B** — the ratchet | a file's uncovered-line count **exceeds** its baseline. A file absent from the baseline is new, and its allowance is **0**. |
| **C** — the pragma ratchet | a file's `# pragma: no cover` count increases. A pragma is an allowlist entry that hides in the source instead of in a reviewable file. |

## The ratchet only clicks one way

If a file's uncovered count drops *below* its baseline, that is **also a failure** — the
baseline is stale-loose, permitting slack the code no longer uses, and coverage could
quietly drift back down into it. The gate tells you to run `--update`. So an improvement
must be locked in by the same PR that makes it, and coverage cannot silently regress to
an old high-water mark.

## When the gate fires, write a test — do not grow the allowlist

This is the failure mode that kills coverage gates in practice, so the tool is built to
resist it:

- `--update` **cannot invent a zero-coverage allowlist entry.** A newly-dead file keeps
  failing Gate A until a human adds an entry *with a reason and an issue link*. Debt is
  visible or it is not debt.
- `tests/test_coverage_gate.py::test_every_zero_coverage_allowlist_entry_carries_a_reason_and_an_issue`
  fails the build if an entry lacks either.
- An allowlist entry whose file *does* get covered later becomes a **failure**, so an
  exemption cannot outlive the debt it describes.
- Adding uncovered lines to a new file shows up in review as a number going **up** in a
  committed file, rather than as nothing at all.

## What this gate structurally CANNOT catch

**A dead module that has unit tests.** Issues #710 (AMT), #711 (CPP sharing), #712
(pension splitting) and #702 (attribution) are modules that are fully built, thoroughly
unit-tested, and called by **zero production code**. Because their unit tests import and
exercise them, they show *high* coverage in this report — Gate A sees covered lines and
stays quiet.

Coverage of the *test suite* answers "does a test touch this line," not "does production
reach this line." Those are different questions, and only the second one catches an
implemented-but-never-called rule. That is the dead-code detector's job (issues
#710/#711/#712/#702), not this gate's. **Do not read a green coverage gate as evidence
that the code is reachable.**

## Known caveat: code executed only inside `ProcessPoolExecutor` workers

`voi.py:566` fans simulations out across worker processes. `concurrency = multiprocessing`
is deliberately **not** enabled, so coverage.py does not record what those workers execute:
lines reached *only* inside a VOI worker read as uncovered here.

That is a conservative error (it over-counts uncovered lines, never under-counts them), so
it cannot cause the ratchet to miss a regression — the direction that would matter. It is
left off because enabling it requires per-process data files plus a combine step, which
costs runtime on a runner we are already rationing and adds a flakiness surface to a gate
whose whole value is that it is trustworthy. If a file ever appears in the zero-coverage
list *because* of this, fix it by enabling the setting rather than by allowlisting the file.

---

# `perf_gate.py` — the per-test runtime regression gate

```bash
# measure (the conftest.py plugin writes test_timings.json on every run — no flag needed)
pytest -q

# enforce (what CI runs)
python tools/perf_gate.py --check

# regenerate the baseline after a legitimate change, then commit it
python tools/perf_gate.py --update

# findings: slowest tests, regressions, improvements
python tools/perf_gate.py --report
```

## Why per-test, not a flat suite timeout

A single wall-clock target is the timing equivalent of `fail_under = 93` on
coverage: a single number that moves for reasons unrelated to the question. CI
load on the self-hosted runner swings the total by 20% between runs (327s vs
412s for the same commit). A flat threshold tight enough to catch a real
regression would fire on noise; one loose enough to survive noise lets real
regressions through.

Per-test durations are more stable than the total (a 150s VOI sweep does not
get 150s of scheduling jitter — its subprocesses are CPU-bound), and the gate
only flags a test that is BOTH 1.5x slower AND at least 1s slower in absolute
terms, which is beyond the noise band on this runner.

## The gate

A test is a regression when ALL of:
- its baseline duration is >= 10s (below that, CI load swings per-test
  timing by 2-3x — too noisy to gate on);
- its current duration exceeds baseline * 1.5;
- the absolute increase exceeds 2.0s.

New tests (absent from the baseline) are ALLOWED — they have nothing to regress
against. Removed tests are silently dropped on `--update`.

## Why no auto-tightening (unlike coverage)

The coverage ratchet clicks one way: if a file's uncovered count drops, the
gate FAILS until the baseline is tightened, so the slack cannot be left behind.
Timing is different: a fast CI run is noise, not improvement. Auto-tightening on
a lucky-fast run would set a baseline the next run could not meet, turning the
gate into a source of false positives. So improvements are SILENTLY FINE (not a
failure); the baseline is only updated by an explicit `--update`. The report
still names tests that got faster, so a real optimization can be locked in
deliberately.

## The always-on profiler (conftest.py)

The brief is "the profiler should always run." A flag is opt-in; an auto-loaded
conftest is not. There is no way to run the suite without producing the
timings file — which is the point: a regression that slips through because
someone forgot `--profile` is exactly the failure mode this exists to prevent.

The plugin hooks into pytest's `pytest_runtest_makereport` (hookwrapper) and
writes `test_timings.json` once at session end. Under `-n auto` (xdist), each
worker writes a fragment file that the controller merges. Overhead is below
noise (a dict insert per test, one JSON write at session end).
# `mutation_guard.py` — curated, fast mutation guard for DP#11/DP#18

```bash
# run locally (no full suite — targets RESP + compounding tests only)
VIRTUAL_ENV=$PWD/.venv python tools/mutation_guard.py
```

## What it does

DP#11: "Unit tests verify a module's contract; integration tests drive the fold."
DP#18: "Any test claiming an engine behaviour must run the engine and assert its
observable output — not an intermediate the test constructed."

A test that copies production logic, or builds engine state by hand instead of
driving the fold, passes while the engine is broken. The mutation guard catches
this by applying targeted wiring-critical mutations to production code and
checking that at least one test in a targeted subset changes outcome (goes
LOUD). This is NOT full mutation testing (no cosmic-ray/mutmut over the whole
repo — too slow). It is a small set of curated mutations, each marked
`expected_loud` (at least one test should catch it) or `expected_silent` (the
suite is green despite the hole — the guard documents the gap until a fix
lands).

Each mutation:

1. Applies a monkeypatch to production code in a fresh subprocess (no state
   leakage between mutations).
2. Runs a TARGETED subset of the suite (the RESP + compounding + engine-fold
   tests, NOT the full ~8min suite — ~3s per mutation).
3. Records whether any test changed outcome (LOUD) or not (SILENT).
4. Fails the guard if a mutation's actual state mismatches its expected state
   (an `expected_loud` mutation that's silent, or an `expected_silent` mutation
   that's loud).

## Current mutations

| ID | Mutation | Expected | Citation |
|---|---|---|---|
| COMPOUNDING | Double `investment_return` in `apply_resp` compounding | loud | #1046 fixed — `test_resp_balance_grows_and_cesg_cap_binds` catches it |
| GRANT_WIRING | Force `calculate_cesg` to return `total_cesg=0` | loud | #1046 fixed — unit tests + `test_cesg_lifetime_cap_7200_per_child` (fold) catch it |
| ALLOC_RESP | Remove `resp_annual_match_cap` from `allocate` min-term | loud | #1046 fixed — `test_resp_allocation_nonzero_when_children_present` catches it |
| STATE_ADVANCE | Zero `opening_resp_cesg` in `apply_resp` prologue (breaks CESG lifetime cap) | loud | #1046 fixed — `test_lifetime_state_advances_across_years` + `test_cesg_lifetime_cap_7200_per_child` catch it |

All four mutations are `expected_loud`: #1046 wired the RESP annual allocation's
per-child contribution cap (`resp_annual_match_cap`) and advances each
`RESPChild`'s lifetime state after each year's CESG/QESI computation, and the
integration tests in `tests/test_issue_1046_resp_allocation_wiring.py` now drive
`FamilySimulation.run()` and assert on engine output, so each mutation is caught.
Before #1046 was fixed, three were `expected_silent` (documenting the wiring
gaps); they flipped to `expected_loud` in the same PR that fixed #1046.
`GRANT_WIRING` was `expected_loud` even before #1046 because unit tests of
`RESPCalculator` verify the method's own contract (DP#11: unit tests verify each
module's contract); the fold-level integration test was added with the fix.

## How mutations are applied

Each mutation is a Python source snippet that, when executed at module level in
a fresh interpreter with the repo on `sys.path`, monkeypatches the target
module(s). The mutation is undone by the subprocess terminating — no permanent
change to production code, no files written, no commit needed.

For example, the COMPOUNDING mutation:

```python
import simulation_rules as _sr
_original = _sr.apply_resp
def _patched(ws, ctx):
    original_return = ctx.investment_return
    ctx.investment_return = original_return * 2
    try:
        return _original(ws, ctx)
    finally:
        ctx.investment_return = original_return
_sr.apply_resp = _patched
```

This doubles the investment return rate in the RESP compounding step, making
`(1 + r*2)` instead of `(1 + r)`. Any test asserting a grown RESP/RRSP/TFSA
balance through the fold must fail if the engine's compounding is correctly
wired.

## CI integration

`.github/workflows/mutation-guard.yml` runs the guard on every PR and push to
`main`. It is a fast targeted run (~15-20s for the current 4 mutations), not
a full mutation-testing framework over the whole repo. It runs in parallel
with the `Tests` workflow and does not lengthen the critical path.

## Adding a new mutation

1. Add the mutation's `apply_src` Python snippet to `MUTATIONS` in
   `tools/mutation_guard.py`. The snippet must be valid Python that, when run
   at module level in a fresh interpreter with the repo on `sys.path`,
   monkeypatches the target module(s).
2. Set `expected` to `"loud"` if at least one test catches the mutation, or
   `"silent"` if no test catches it (documenting a known gap).
3. Set `citation` to the issue/PR number documenting why the expected state is
   what it is.
4. Set `test_files` to the targeted list of test files that should catch the
   mutation. Keep it focused — RESP + compounding + engine-fold tests, not the
   full ~8min suite.
5. Run the guard locally to verify: `VIRTUAL_ENV=$PWD/.venv python tools/mutation_guard.py`
6. Commit the change to `tools/mutation_guard.py` and this README.

## What this guard structurally CANNOT catch

Like the coverage gate, a mutation guard can only assert what it is programmed
to assert. It catches wiring regressions in the specific production paths the
curated mutations target. It does NOT catch:

- **Untargeted wiring gaps.** A mutation that doubles `investment_return` in
  `apply_resp` tests that the compounding is wired through the fold; it does
  NOT test that every other rule's compounding is wired.
- **Logic errors the mutations don't reach.** A mutation that forces
  `calculate_cesg` to return 0 tests that CESG is wired to the RESP balance; it
  does NOT test that QESI, CLB, or any other grant is wired.
- **Dead code.** A module with zero production callers that has unit tests
  (the coverage gate's known gap — see its README section) is not caught by
  this guard either.

This guard is a complement to the coverage gate and the DP#18 dead-write test,
not a replacement. It addresses the specific failure mode DP#11/DP#18 flag:
a test that claims to verify an engine behaviour but skips the engine.

---
