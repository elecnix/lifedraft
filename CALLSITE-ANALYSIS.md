# Call-site analysis for issue #231 — frozen `YearInputs`

**Status: WORK IN PROGRESS — analysis insurance file. NOT a PR deliverable.
Delete before merge.**

Worktree: `/home/nicolas/Source/lifedraft/fix-231`, branch `fix/231-yearinputs`,
off `origin/main` @ `ffa6fc0` (both predecessors #236 `d23d9c1` and #237
`ffa6fc0` MERGED). Venv installed with `uv`, `-e ".[dev]"`.

## Measured facts (re-measured on this tree, 2025-…)

| fact | value | how |
|---|---|---|
| `simulate_year_pure` params | **58** | `ast` walk of FunctionDef args (simulation_state.py:1827) |
| `RuleContext` annotated fields | **52** | `ast` walk of ClassDef AnnAssign (rule_registry.py:34) |
| production call sites | **3** | simulation.py:1709, 2501, 2907 (ast walk, exhaustive over non-test `.py`) |
| test call sites | **127 real calls in tests** | ast walk: `sym.Call(fn.Name=='simulate_year_pure')` → 127; grep line count 128 (one extra is a docstring mention `simulate_year_pure(...)` in test_issue_627_engine_collapse.py:65) |
| test files with calls | **29** | `grep -rln "simulate_year_pure(" tests/` |
| golden invariant | `9709753.139463063` | ran `_run(golden_household_config())[-1].total_assets` — CONFIRMED on this tree |

Per-file call-site distribution (grep, exact):

```
36 tests/test_simulation_state.py
17 tests/test_issue_584_rules_registry.py
12 tests/test_lira_wiring.py
10 tests/test_issue_679_solvency.py
 8 tests/test_dp20_year_versioned_data.py
 4 tests/test_issue_97_income_type_growth.py
 4 tests/test_issue_758_runway.py
 4 tests/test_issue_689_735_credit_facility.py
 3 tests/test_issue_761_discretionary_living_costs.py
 3 tests/test_issue_575_576_taxable_investing.py
 2 each: test_issue_823_ftq, test_issue_747_amt_parts, test_issue_710_wire_amt,
        test_issue_289_nonreg_warning, test_issue_25_simstate_no_backcompat,
        test_issue_141_superficial_loss, test_issue_1082_amt_unfunded,
        test_bugfixes_43_44_45
 1 each: test_jurisdiction_agnostic, test_issue_936_deposit_products,
        test_issue_912_fhsa_lira_lif_composition, test_issue_763_consumer_loans,
        test_issue_760_finite_term_living_costs, test_issue_759_installment_obligation,
        test_issue_708_lira_early_conversion, test_issue_641_per_account_composition,
        test_issue_627_engine_collapse(docstring), test_issue_546_deduct_later_bracket_fill,
        test_issue_140_capital_loss_carryforward
```

## Call-site shape (exhaustive ast probe — THIS is the reusable classifier)

Saved as `callsite_classifier.py` in this tree. Findings:

- **All 127 real calls pass `state`/`year` and `allocations`/`config` as the
  first arguments** — either `(state, 0, allocs, cfg, ...)` positionally or
  `(state=..., year=..., allocations=..., config=..., ...)` as keywords. NOT
  ONE site omits them. A classifier that assumes "args 0-1 = state/year,
  then everything else is YearInputs material" is SAFE.
- **0 problem sites** on the clean probe (the earlier "fewer-than-4" flagging
  was a bug in my throwaway classifier — it double-counted the glob and mis-
  checked the condition; the fixed probe, saved to a file per operator
  instruction, reports 0).
- No call uses `**kwargs`-style splat; no duplicate kwargs; no keyword-only
  `*` splat.

### Decision on the 128 sites — Option A: keep them in `tests/`, convert
mechanically, NO test-only shim… (final choice pending Slice 1 implementation)

I considered all three:

1. **Convert all 128 mechanically (script + review).** For each site wrap the
   arguments after `(state, year,` in `_build_year_inputs(...)`. The old
   kwarg names ARE the YearInputs field names (minus state/year), so the
   wrap is purely syntactic: `simulate_year_pure(state, 0, allocs, cfg,
   investment_return=0.07, mortgage_data=mort)` →
   `simulate_year_pure(state, 0, _build_year_inputs(allocations=allocs,
   config=cfg, investment_return=0.07, mortgage_data=mort))`. Keyword-form
   sites keep `state=`/`year=` in the outer call and move everything else
   inside. This is the "no interim state" option: largest diff, but every
   test then exercises the REAL new signature — the most valuable outcome,
   because the tests ARE the enforcement surface that will catch a future
   signature regression.

2. **Test-only helper.** e.g. `syp(state, year, allocations, config,
   **kwargs)` in a tests helper module that builds YearInputs and calls the
   real function. This is NOT a production shim (DP#9 bans shims in the
   API; test-only helpers are not the API), and it keeps the 128 sites
   byte-identical. BUT it duplicates the year-step input contract in a
   place that has no production reader — the 128 sites would then exercise
   a private copy of the argument list, and a future param rename would
   break exactly as loudly BUT the tests would be asserting against a
   test-side mirror, not the real signature. The repo's own architecture
   guards (test_issue_627_engine_collapse, test_issue_583_pure_fold) are
   built on the precise opposite principle: tests must drive the REAL
   seam. A test-side mirror recreates by hand the "second spelling" the
   repo kills on sight.

3. **Slice by file.** The signature change is atomic; you cannot land
   half the tests converted and CI green. This only applies within a
   single PR as commit sequencing, which I will do (one commit per
   logical slice), not as standing intermediate states.

**Verdict:** Option 1 — convert all mechanically, script + review. Rejected
2 because a test-side mirror is a second spelling of the input contract in
the exact place DP#11/DP#18 guards patrol hardest; rejected 3 as a standing
state. Justification (for the PR): converting the call sites is the loud
way — a future change to the fold's signature must break tests, and the
teal estate of 128 converted sites is the price of that loudness.

## Slice plan (from the issue brief)

- **Slice 1**: frozen `YearInputs` dataclass + private `_build_year_inputs(...)`;
  `simulate_year_pure(state, year, inputs)`. Convert the 3 production sites
  and the 127 test sites. Update syntactic guards (test_issue_96
  signature introspection; test_coupling_guard numbers if they move).
- **Slice 2**: derive `RuleContext` from `YearInputs` (it is already
  `@dataclass(frozen=True)` — keep frozen; 50 of its 52 fields overlap the
  YearInputs surface; `amt_credit_opening`/`qc_imr_credit_opening` are
  state-derived, not inputs).
- **Slice 3**: move prior-year GIS (`prior_gis_countable_income`) out of a
  caller keyword into the fold's own state (currently threaded by
  `simulation.py._prior_gis_countable(results)` → kwarg → RuleContext).
- **Slice 4**: relocate `simulate_year_pure` + `SimState` into their own
  module. **Required before Slice 4: `from simulation_state import ...`
  appears in 55 test files; 52 import SimState, 35 import simulate_year_pure
  (non-exclusive union). The move must update every import site + __init__.py
  exports + any `simulation_state.` attribute references.**

## Guards / frozen numbers this reshape touches

- `test_issue_96_marginal_rate_defaults.py` — **WILL BREAK**: it does
  `inspect.signature(simulate_year_pure)` and asserts
  `parameters['primary_marginal_rate'].default == 0.40` and spouse 0.20.
  When the signature becomes `(state, year, inputs)`, these params vanish.
  The round-defaults intent must move INTO the YearInputs dataclass
  (or the builder) and the guard updated to assert `YearInputs` field
  defaults — say so in the PR.
- `tests/architecture/test_coupling_guard.py` — pins ws-field inventories
  (261 fields / 26 multi-writers / 54 seams). It scans RULE modules'
  `ws.<field>` accesses — unaffected by the fold's own signature.
- `tests/architecture/test_dp32_zero_fallback.py` / `test_dp18_*.py`,
  `repo_scan.py` — scan for `x or DEFAULT`/dead writes. **The builder must
  not use `or`** — a supplied 0/None must survive (DP#32/#13). Use explicit
  defaults/None-pass-through.
- `tests/architecture/test_dp_income_scenario_reaches_engine.py`,
  `test_issue_627_engine_collapse.py`, `test_issue_583_pure_fold.py`,
  `test_issue_96_marginal_rate_defaults.py` — read the SHAPE of code
  (source/AST/signature). Expect to touch 96 and 583 at minimum.
- Coverage gate (`tools/coverage_gate.py` + baseline): **a NEW production
  file (Slice 4 module) enters with ZERO uncovered-line budget**, and if
  uncovered lines legitimately move, regenerate + commit the baseline in
  the SAME PR (`--update`).

## What I could NOT verify / risks

- `test_coupling_guard.py` / `repo_scan.py` may enumerate files by glob;
  a Slice-4 new module could shift its inventory if it scans
  simulation_state.py directly — must re-check before Slice 4.
- `test_issue_583_pure_fold.py` imports `simulation_state.simulate_year_pure`
  and inspects `_simulate_year_step`'s SOURCE for
  `'prior_gis_countable_income=prior_gis_countable_income'` — Slice 3 (GIS
  into fold state) will break that source-shape assertion; update intent,
  don't delete.
- sim_state `RuleContext` construction in `simulate_year_pure` currently
  passes `amt_credit_opening`/`qc_imr_credit_opening` from
  `jurisdiction_state['canada']` — these stay state-derived in Slice 2, so
  RuleContext from YearInputs needs those two wired from state, not inputs.