# AGENTS.md — working on this repo

**[README.md](README.md) explains what this project is and how to use it. Read it first; this file
does not repeat it.** What follows is what you need in order to *change* the code without breaking
the property the whole codebase exists to protect.

---

## The one thing to understand before you touch anything

This codebase was rebuilt because **the engine silently substituted zero** — a missing input became
`0`, an unimplemented rule became a no-op, and the run completed, green, printing a confident wrong
number. Roughly 3,900 tests caught none of it.

So the bar here is not "does it work." It is:

> **When this is wrong, will anyone find out?**

Code that produces a plausible answer from absent data is **worse** than code that crashes. Prefer
the loud failure. Always.

---

## Workflow (non-negotiable)

- **Never work on `main`. Never push to `main`.** Branch, open a PR, merge the PR.
- **Worktrees go in `~/Source/lifedraft/<name>`:**
  ```sh
  git -C ~/Source/lifedraft/main worktree add ~/Source/lifedraft/fix-NNN -b fix/NNN-slug origin/main
  cd ~/Source/lifedraft/fix-NNN
  VIRTUAL_ENV=$PWD/.venv uv venv && VIRTUAL_ENV=$PWD/.venv uv pip install -q -e ".[dev]"
  ```
- **Never `--no-verify`.** The pre-commit hooks are guardrails, not friction.
- **Refreshing `origin/main` needs the explicit refspec.** A plain `git fetch origin main` silently
  does nothing in this bare-repo setup:
  ```sh
  git fetch origin '+refs/heads/main:refs/remotes/origin/main'
  ```
- Open a **draft** PR. Monitor CI. Fix what it tells you.

## Running the tests

```sh
VIRTUAL_ENV=$PWD/.venv .venv/bin/python -m pytest -q        # ~8 min, 4144 tests
```

Run it in the **foreground and read the output yourself.** Do not background it and then report a
success you never saw.

**The CI runners live on the maintainer's own workstation.** A full local suite run competes with
them (and with him) for the same 16 cores — load has hit 24, and a CI job has been SIGTERM-killed by
the contention. Prefer a **targeted** run of the tests you touched, and let CI run the full suite.

### Coverage (`tools/coverage_gate.py`)

CI measures coverage on the 3.12 leg and enforces, per file: **zero-coverage** files fail, and a
file's **uncovered-line count may not increase** against `tools/coverage_baseline.json`. When
coverage legitimately changes, regenerate the baseline **in the same PR** and commit it:

```sh
pytest -q --cov --cov-report=json:coverage.json     # COVERAGE_CORE=sysmon is ~free on 3.12
python tools/coverage_gate.py --update              # then commit tools/coverage_baseline.json
python tools/coverage_gate.py --report              # zero-coverage files + worst offenders
```

The ratchet only clicks one way: if coverage *improves*, the gate **fails** until you regenerate, so
the slack cannot be left behind for coverage to drift back down into. See `tools/README.md` for why
it counts uncovered **lines** and not a percentage — and for the one thing it structurally cannot
catch (a dead module that has unit tests, e.g. #710/#711/#712/#702).

**Do not iterate this gate through CI — that's a ~24 min feedback loop.** When the gate names
specific uncovered lines (`Uncovered: 1501, 1502, ...`), verify a candidate test covers them
*locally in seconds* with the `coverage` module directly, no full suite:

```python
import coverage
c = coverage.Coverage(source=['simulation_rules']); c.start()
# ... run ONLY the scenario that should hit those lines (a targeted _run / simulate_year_pure) ...
c.stop()
missing = set(c.analysis2('simulation_rules.py')[3])   # [3] = missing line numbers
for L in (1501, 1502, 1503, 1504):
    print(L, 'COVERED' if L not in missing else 'missing')
```

Iterate the *test* against this until the target lines read COVERED, then run the file normally and
push. A `+N` on a file usually means pre-existing thinly-covered branches whose line numbers *shifted*
under your diff (not new code you wrote) — the fix is still a real test that exercises them, never an
allowlist entry. (Caveat: local `ctrace` and CI `sysmon` can disagree by a line or two; if the module
says COVERED locally but CI still flags it, the true count is usually still under baseline — check the
count, not just the named lines.)

### The golden invariant

The 46-year golden household's terminal `total_assets` is **`9709753.139463063`** (moved by #1046,
which wires the RESP annual allocation's per-child contribution cap (`resp_annual_match_cap`) and
advances each RESPChild's lifetime state (total_cesg_received, total_before_age_15, etc.) after
each year's CESG/QESI computation — the golden household's two RESP children now accumulate
annual contributions and CESG grants correctly, changing the RESP balance trajectory and
wind-down. It previously read `9816435.13530067`, moved by #1001,
which nets the forced RRIF-minimum withdrawal's after-tax proceeds into the discretionary drawdown
sizing — the mandatory minimum now funds the net spending shortfall FIRST, so the discretionary draw
stops pulling tax-free TFSA it did not need while the already-taxed RRIF surplus was reinvested into
taxable non-reg for 25 years; preserving TFSA compounding at 6% instead of swapping it for ~5.74%
after-tax non-reg raised terminal assets. It previously read `9766299.424395865`, moved by #825,
which routed the forced RRIF-minimum withdrawal's tax through the same progressive re-bracketing +
per-spouse OAS-clawback machinery as the discretionary drawdown, replacing a flat placeholder rate
applied to the whole forced slice; the golden household's forced RRIF minimum, split per spouse,
re-brackets from each spouse's low CPP/pension base — much of it below the old flat rate and below
the per-person OAS threshold — so its effective tax fell and terminal assets rose. It previously read
`9325808.371211344`, moved by #751's `tfsa_pct` fix).

It is **computed, not stored.** `grep` cannot find it — and a `grep` that comes back empty proves
*nothing*. To check it, run the fixture:

```sh
python -c "import sys; sys.path.insert(0,'tests')
from test_golden_trajectory_581 import golden_household_config, _run
print(repr(_run(golden_household_config())[-1].total_assets))"
```

**Stronger than re-running it:** if your diff touches **zero existing files** — i.e.
`git diff origin/main --diff-filter=MDR --name-only` is empty — then no pre-existing invariant can
have moved, *by construction* rather than by measurement. Prefer that argument whenever it is
available to you.

---

## The input contract at a glance

One document describes the household: **a dated balance sheet of owned entities.**
When wiring adapters, tests, or schema fields, this is the anatomy every contract
follows:

```jsonc
{
  "as_of": "2026-06-30", "currency": "CAD", "dollars": "nominal",
  "jurisdiction": { "country": "canada", "province": "quebec" },

  "people":      [ /* birth_date, residency, relationships, incomes, contribution room */ ],
  "accounts":    [ /* OWNED: kind, owner, balance{amount,as_of}, acb, beneficiary, successor_holder */ ],
  "liabilities": [ /* mortgage | heloc: collateral, rate, rate_type, limit, deductibility */ ],
  "properties":  [ /* value, acb, designated_principal_residence_years */ ],
  "estate":      { /* spousal rollover election, life_insurance[] */ },
  "assumptions": { /* return_model, inflation, mortality — BELIEFS, kept separate from facts */ },
  "decisions":   { /* what to SWEEP: retirement_age, contribution_strategy, mortgage, income */ },
  "provenance":  { /* JSON-Pointer → how each value is known */ }
}
```

Schema: `schema/input_schema.json` plus the jurisdiction overlay
(`schema/countries/canada/input_schema.json`); synthetic example at
`schema/example.json`. Validate any document with
`python -c "import input_contract; input_contract.load_and_map('my.json')"` —
it refuses loudly rather than silently dropping what it cannot model.
**A refusal is a feature.**

## Reporting discipline

This is load-bearing, because a false green is load-bearing: someone merges on it.

- **State your method beside your result.** `ran <command>, got <output>` — never a bare
  "verified X." A verification whose provenance the reader cannot check is precisely the thing this
  project treats as a defect.
- **If a verification needed an inference to perform, the inference goes in the report.**
- **If you cannot verify something you were asked to verify, say so** — then assert the strongest
  thing you actually *can* defend. An honest "I could not verify X, and here is why" is worth more
  than a green tick you cannot support.
- **If an instruction looks impossible or wrong, push back.** Instructions in this repo have been
  wrong before — including from the orchestrator. Saying so is the job.

---

## The guards will fight you. They are meant to.

| Guard | Fires when you… |
|---|---|
| `tests/architecture/test_dp32_zero_fallback.py` | write `x or DEFAULT`, `cfg.get(k) or D`, `node.get(k, []) or []`, or `if balance <= 0: return 0` |
| `tests/architecture/test_dp18_dead_write.py` | make an overlay write a key the engine never reads |
| `tests/architecture/test_next_action_absence.py` | let a missing input **delete** an obligation rather than merely un-price it |
| `tests/test_schema_coverage.py` | add a schema leaf nothing consumes (it is leaf-*and-kind* aware) |
| `tests/trajectory_invariants.py` | break money conservation, `ACB <= FMV`, or the RRIF minimum at 71 — checked **every year** |
| `tools/coverage_gate.py` (CI) | add a production file no test touches, **increase** any file's uncovered-line count, or add a `# pragma: no cover` |
| `.github/workflows/clone-detection.yml` | duplicate logic that *this PR* introduces (dupdelta, warn-only) |

**When a guard fires, fix the code — do not add an allowlist entry.** The allowlists exist for
already-triaged exceptions carrying a citation and a mechanism. Growing one to make your build go
green is exactly how the original bugs got in.

---

## Traps this codebase has actually fallen into

Every one of these shipped, passed CI, and printed a confident wrong number.

- **`or` as a default.** `config.oas_max or TABLE[year]` makes `0` unrepresentable — a user who means
  *zero* silently gets the table.
- **Returning the first match.** `for x in xs: if match: return x` silently drops the rest. A
  household with two mortgage sub-accounts lost 40% of its debt this way.
- **A date window that skips.** `if income["from"] > as_of: continue` turned a $250k salary into `$0`,
  and the optimizer cheerfully ranked strategies against it.
- **Parsed, mapped, then never passed.** `decisions.income[]` reached the config and stopped there,
  because the caller never handed it to the optimizer. `test_schema_coverage` passed — because the
  *adapter* read the leaf. **A leaf being read by the adapter is not the same as the block reaching
  the engine.**
- **Reimplementing the engine in the test.** A test that copies a production formula or builds
  internal engine state (`YearWorkingState`, `resp_data`, …) by hand instead of driving
  `FamilySimulation.run()` / `simulate_year_pure` passes while the engine is broken — it verifies a
  scenario the engine cannot produce. **A unit test of a pure function calling that function is
  correct; a test that claims to validate an engine behaviour but skips the engine is a shortcut.**
  Drive the fold; assert the engine's output, not an intermediate the test constructed (DP#11/DP#18).
- **An unknown key defaulting to the favourable value.** `_PURPOSE_MAP.get(p, DebtPurpose.INVESTMENT)`
  invented a tax deduction that statute expressly forbids.
- **A missing input deleting an obligation.** `if room is None: continue` removed a dated, expiring
  grant — so the user never learned to go and look up the room.
- **Swallowing exceptions.** `except Exception: score = -inf` makes a *crashing* strategy
  indistinguishable from a *bad* one, and silently ranks it last.

If your change has any of these shapes, it is probably wrong — even if the tests are green.

---

## Personal data (DP#15) — hard rule

**No real figure, name, account number, or file path from anyone's actual finances may enter this
repo.** Not in code, not in tests, not in comments, not in commit messages, not in PR bodies.

Real household data lives in a **separate private repo**, whose CI validates it against this engine's
`main`. You may read and run against it to sanity-check your work. **Nothing from it comes back.**

Fixtures use **fabricated round numbers** and **role-based names** (`primary`, `spouse`, `child_a`) —
DP#4.

---

## Design principles — index

A one-liner is enough to *recognise relevance*, never to substitute for reading the principle. Read
the full text of the matching row in **[DESIGN_PRINCIPLES.md](DESIGN_PRINCIPLES.md)** before touching
the code it governs.

| # | One-liner | Read in full when… |
|---|-----------|--------------------|
| 1 | Store dates, not derived values — the actual date, never a coarser year or age | adding any age, eligibility, or date-driven rule |
| 2 | Configuration belongs in input, not in code | hardcoding a rate, bracket, or limit |
| 3 | Pure functions, no hidden state | writing tax/grant/amortization math |
| 4 | Role-based names, not person names | adding fields or test data |
| 5 | Anchor decisions, overlay sensitivities | adding a new scenario dimension |
| 6 | Strategies are discovered from rules, not named by convention | adding a named strategy (e.g. "Smith Manoeuvre") |
| 7 | Model the mechanism, not the branded product | adding a bank- or product-specific feature |
| 8 | Compose through data, not through inheritance | adding a strategy, rate path, or engine variant |
| 9 | No backward compatibility — no shims, no deprecation cycles | changing any API |
| 10 | One module per government program, one per jurisdiction | adding a rule or a new country/province |
| 11 | Unit tests verify each module; integration tests verify composition | adding any test |
| 12 | Real data is fetched, cached, and segregated from library code | adding a hardcoded rate/bracket constant |
| 13 | Defaults are fallbacks for absent input, not opinions — never a way to coerce a value that was supplied | adding a default value, or writing `x or DEFAULT` |
| 14 | Scripts read a common config schema; each script uses the parts it needs | adding an input field or a new script |
| 15 | Personal data never enters version control | writing tests or fixtures |
| 16 | Modules auto-include when their trigger data is present | wiring a new module into the simulation |
| 17 | Tests exercise every rule path, both sides of every threshold | adding a rule |
| 18 | Scenarios overlay a base; an overlay must modify a key the engine reads, not evaporate | touching cash-out/HELOC/overlay/sweep money flow |
| 19 | Track cost basis from day one; tax at withdrawal | touching non-reg/ACB or RRSP deduction timing |
| 20 | Data is year-versioned; simulate across tax years | adding or reading tax/contribution-limit data |
| 21 | Return models are pluggable data, not hardcoded assumptions | touching investment return assumptions |
| 22 | Optimization objectives are data; the optimizer ranks, it doesn't choose | adding an optimizer objective |
| 23 | Randomness must be reproducible | adding any stochastic/Monte Carlo code |
| 24 | Config round-trips: load, modify, save | modifying a config programmatically |
| 25 | Four layers — data → scenario → simulation → optimization — dependencies point inward | adding an import between layers |
| 26 | The simulation step is a pure function over explicit state; `run` is a fold | touching the engine or `SimState` |
| 27 | Investment income has distinct tax treatments, modeled by type | touching investment return or tax code |
| 28 | Eligibility is date-computed; programs enter **and exit** on a schedule; a gate built from missing data isn't a legitimate zero | adding a program, or touching per-member/family overlay merging |
| 29 | The optimizer reports risk measures alongside expected value | adding an optimizer or scenario comparison |
| 30 | The simulator models tax consequences; it does not make financial decisions | deciding whether a feature is in scope |
| 31 | The optimizer mode is pluggable data; search method and objective are separate choices | adding an optimizer mode |
| 32 | **Zero is a value, not a fallback — absence must fail loudly, never default to zero** | writing `cfg.get(k) or DEFAULT`, `if balance <= 0: return 0`, or any path that can silently no-op on missing/zero input |
| 33 | **A declaration is a lens, not a blindfold — declared candidates annotate the sweep, never replace it** | adding or consuming a declared candidate list (`decisions.*_options[]`, `scenarios.*`), or writing `if declared: … else: auto-discover` |

---

## Definition of done

1. The behaviour is fixed **and something fails if it regresses.** A detector not wired into CI is
   not done — land the enforcement in the *same* PR.
2. The full suite is green, and **you read the output yourself.**
3. No guard was silenced with an allowlist entry to make it pass.
4. No personal data anywhere in the diff.
5. Your report states the method beside the result, and names what you could not verify.

---

## Pi subagents & chains

See [`.pi/README.md`](.pi/README.md).

---

## PR policy (project override of the global `~/.claude/CLAUDE.md`)

`CLAUDE.md` is a symlink to this file. Merging PRs is **permitted in this repo.** You may mark PRs
ready for review and **squash-merge** them here. Still:

- create PRs as **drafts** first;
- do all work in **git worktrees** — never commit directly on the default branch;
- never bypass pre-commit hooks with `--no-verify`;
- wait for required CI checks to pass before merging.
