# Design Principles

Principles inferred from the current codebase and review feedback.

---

## Enforcement status

"A principle that isn't a test is documentation, and documentation rots" (#586). A
principle in this file is binding to the degree a machine checks it, not to the
degree it is eloquently worded — 32 principles and ~4,000 tests coexisted with an
entire class of bug (the engine silently substituting zero for a missing or
unimplemented input) because none of the principles that would have caught it were
load-bearing. This table says, for every principle, whether a violation today is a
**build failure** or a **code-review opinion** — and it is honest in both
directions: an entry claiming "Enforced" that isn't would be worse than one that
admits "Advisory."

- **Enforced** — a test exists that would fail on a *new* violation of the general
  pattern, not just a regression pin for one historical bug.
- **Partially enforced** — a real mechanical check exists but is narrower than the
  principle's full claim (scoped to specific files/functions, or covers one clause
  of a multi-clause principle).
- **Advisory** — no mechanical check exists today. Either it requires domain
  judgment an AST/behavioural rule can't supply (DP#2's "is this a hardcoded rate
  or a legitimate placeholder?"), or it depends on a schema convention that doesn't
  exist yet (DP#1 needs #596/#597), or it's out of this PR's scope and explicitly
  left to a named companion issue (DP#13/#14 overlap #582; DP#9 is flagged but not
  gated — see below).

| # | Principle | Status | Mechanism |
|---|-----------|--------|-----------|
| 1 | Store dates, not derived values | Advisory | No schema convention yet marks which fields *should* be dates vs. legitimately-coarser data (e.g. `projection_years`); needs #596/#597 first. |
| 2 | Configuration belongs in input, not code | Advisory | "Hardcoded rate" vs. "documented placeholder" requires domain judgment; no generic AST rule can tell them apart. |
| 3 | Pure functions, no hidden state | Partially enforced | `tests/test_issue_25_simstate_no_backcompat.py`, `tests/test_monthly_pure_fold.py` structurally check the known engine (`SimState`, `simulate_year_pure`); not a sweep of every function claiming purity. |
| 4 | Role-based names, not person names | Advisory | Naming convention; no scanner (high false-positive/negative risk for a name heuristic). |
| 5 | Anchor decisions, overlay sensitivities | Advisory | No check classifies a *new* variable as anchor-vs-overlay; DP#18's tests verify overlays reach the engine once so classified, not the classification itself. |
| 6 | Strategies discovered from rules | Advisory | A design stance ("not a label to hardcode"), not a syntactic pattern. |
| 7 | Model the mechanism, not the branded product | Advisory | Judgment call. |
| 8 | Compose through data, not inheritance | Partially enforced | `tests/test_jurisdiction_agnostic.py` is an AST import-boundary check (no `countries.canada` imports in core files); doesn't sweep for inheritance hierarchies generally. |
| 9 | No backward compatibility | Advisory (mechanically checkable, not gated) | A grep for `deprecated`/shim markers is trivial to build, but ~150 pre-existing instances are under active remediation on other branches right now (`dp9-remove-*` worktrees, PR #560); hard-gating today would break CI under everyone else's feet. Left as a documented follow-up, out of #586's scope by design (see #586's own text). |
| 10 | One module per government program / jurisdiction | Advisory | No test asserts the module-per-program mapping; DP#25's import-boundary test is a different (narrower) claim. |
| 11 | Unit tests verify a module's contract; integration tests drive the fold — never reimplement production logic in a test | Advisory | Test-organization convention; "a test file exists" doesn't prove the layering is honored. No static check yet enforces that a test claiming an engine behaviour actually drives the fold and asserts engine output (a targeted mutation check would — see AGENTS.md traps). |
| 12 | Real data fetched, cached, segregated | Advisory | No check that a rate/bracket literal in library code came from a data provider vs. was hardcoded. |
| 13 | Defaults are fallbacks for absent input, never a coercion | Partially enforced | This PR's `tests/architecture/test_dp32_zero_fallback.py` enforces the `x = cfg.get(k) or DEFAULT` half (DP#32 is this principle's sharper, mechanically-checkable restatement). The "one typed load, reject unknown keys" half is **owned by #582** (in progress) — not duplicated here. |
| 14 | One config schema, scripts use parts they need | Advisory (owned by #582) | Schema-coverage validation is #582's deliverable; not duplicated in this PR. |
| 15 | Personal data never enters version control | Advisory | No PII/name scanner built; relies on `.gitignore` + review discipline today. |
| 16 | Modules auto-include on trigger data | Advisory | No check that a module's activation condition matches its documented trigger fields. |
| 17 | Tests exercise every rule path | Advisory | Coverage-of-rule-paths is a measurement problem (which branches did a suite exercise), not something a static rule proves. |
| 18 | Overlays must land on a key the engine reads | **Enforced** | This PR's `tests/architecture/test_dp18_dead_write.py` — an AST check for mutated-default dead writes, plus a table-driven behavioural check (apply every registered overlay function with non-trivial args, assert the *engine's output* differs) covering `apply_sensitivity_overlay`, `apply_preset`, `apply_anchor_preset`, `compose_preset`, `run_stress_test`, and the `ScenarioOverlay` path. |
| 19 | Track cost basis from day one | Advisory | No generic check that every disposition-triggering account has ACB tracking wired. |
| 20 | Data is year-versioned | Advisory | No sweep asserts every tax/limit lookup is parameterized by year. |
| 21 | Return models are pluggable data | Advisory | Exercised incidentally by DP#18's overlay tests; no dedicated check that every return-rate source is pluggable. |
| 22 | Optimizer ranks, doesn't choose | Advisory | Judgment call about API shape. |
| 23 | Randomness must be reproducible | Partially enforced | `tests/test_dp23_reproducible_rng.py` checks known stochastic entry points (`monte_carlo`, `run_monte_carlo`) require and honor a `seed`; doesn't sweep for a *new* stochastic function added without one. |
| 24 | Config round-trips: load, modify, save | Advisory | No generic round-trip fuzz test across the whole schema. |
| 25 | Four layers, dependencies point inward | **Enforced** (pre-existing) | `tests/test_jurisdiction_agnostic.py` — AST check that `simulation.py`/`simulation_state.py` have zero module-level `countries.canada` imports. Scoped to those two files, not the full data→scenario→simulation→optimization graph. |
| 26 | Simulation step is a pure fold | Partially enforced | `tests/test_monthly_pure_fold.py` / `tests/test_issue_288_run_is_fold.py`-style tests check the known engine threads `SimState` correctly; not a purity sweep of arbitrary functions. |
| 27 | Investment income taxed by type | Advisory | No check that every income-type distinction claimed in the principle is actually modeled. |
| 28 | Eligibility is date-computed; programs enter **and** exit; per-member data must be complete | Advisory | `tests/architecture/test_dp28_member_completeness.py` (which enforced the completeness half via `module_registry._deep_merge`'s per-member overlay-default mechanism) was deleted in epic #603 Track C Phase 2b along with that mechanism: the input contract now requires every field explicit (JSON Schema `required`, validated once at the one loading boundary) instead of deep-merging jurisdiction defaults into an unvalidated dict at runtime, so there is no longer an overlay-completeness question to guard for the input document itself. The lifecycle entry/exit half still relies on scattered per-program DP#17 rule-path tests, not a generic sweep. |
| 29 | Optimizer reports risk alongside expected value | Advisory | No check that every optimizer path surfaces a downside measure. |
| 30 | Simulator models tax consequences, doesn't make decisions | Advisory | A scope boundary; a judgment call, not a pattern. |
| 31 | Optimizer mode is pluggable, independent of objective | Advisory | No check that a new optimizer mode reuses `simulate_year_pure` / doesn't import another mode. |
| 32 | Zero is a value, not a fallback | **Enforced** | This PR's `tests/architecture/test_dp32_zero_fallback.py` — an AST sweep for `X.get(...) or DEFAULT` / `getattr(...) or DEFAULT` across all first-party source, with an itemised, issue-cited allowlist for confirmed violations (#606, #621) and reviewed-harmless sites. A new, untriaged site fails the build. |

**How the allowlists stay honest.** DP#18 and DP#32's tests key their allowlists on
`(file, exact matched source snippet)`, not line number, so unrelated edits
elsewhere in a file don't cause false failures — but any change to the *flagged
expression itself* does: fixing a violation without updating the allowlist fails
the build (a stale entry), and a brand-new site matching the pattern without a
triage entry also fails the build (an unlisted finding). The allowlist cannot grow,
or shrink, silently.

---

## 1. Store dates, not derived values — and store the actual date, not a coarser value that approximates one

> `birth_date` instead of `birth_year` instead of `age`; `StudyPeriod(start, end)` instead of `is_student`; `retirement_date` instead of `retirement_age`.

Derived values go stale the moment the simulation clock ticks. Store the source of truth (a date, a date range) and compute derived properties (`age_in(year)`, `is_student_in(year)`) on demand. This makes multi-year simulation correct by construction.

This principle is not satisfied by trading one derived value for a coarser one. `birth_year` is itself a derived approximation of `birth_date`, accurate to within a year — and several binding rules need better than a year: RRIF conversion triggers at the **end of the year you turn 71**, with the first minimum withdrawal based on age at January 1; CPP and OAS start age, and OAS deferral, are counted in **months**; the FHSA's 15-year participation window runs from the account's **opening date**, not a birth year. A field named `_year` or `_age` where the rule actually needs a `_date` is DP#1 violated at the root, even though it looks like the principle is being followed (#597). Store `birth_date`; derive age, CPP/OAS eligibility months, and the RRIF-conversion year from it, on demand, the same way `age_in(year)` is derived today.

## 2. Configuration belongs in input, not in code

> `build_broker_scenarios(broker_offers=cfg['...'])` instead of hardcoded rates.

Hardcoded rates, brackets, and limits are personal data that doesn't belong in library code. Functions accept config dicts or structured parameters; defaults use round numbers for documentation, not real values.

## 3. Pure functions, no hidden state

> `marginal_rate(income, brackets)` returns the same result every time.

Tax calculations, grant formulas, and amortization math are pure functions. Same input → same output. No globals, no caches, no side effects. This makes them trivially testable and safe to compose.

Test functions follow the same pattern: core test logic is a plain function parameterized by jurisdiction fixtures. `test_marginal_rate(brackets, income, expected)` takes brackets as an argument; the Canadian test module calls it with Canadian brackets. A new jurisdiction adds fixtures, not test logic. One test instance per jurisdiction; no inheritance hierarchies.

## 4. Role-based names, not person names

> `primary_rrsp_room` not `alex_rrsp_room`.

Library code uses role-based identifiers (`primary`, `spouse`, `child`). Person names, real incomes, and account balances belong in the input config, not in class fields, defaults, or test data.

## 5. Anchor decisions, overlay sensitivities

> Scenarios are real choices (stay vs. switch jobs); sensitivity overlays (inflation ±1%) layer on top.

The top-level scenarios represent actual decisions being weighed. Uncertain variables like renewal rates and savings rates are *overlays* applied across anchors, not separate scenarios. This keeps the decision front and center.

## 6. Strategies are discovered from rules, not named by convention

> The readvanceable mortgage strategy isn't called "Smith Manoeuvre" in code — it's found when the rules hold.

A financial strategy is not a label to hardcode. It's a set of conditions that, when they all hold, make a course of action optimal. The optimizer discovers it from the rules: readvanceable mortgage → HELOC room created; interest deductible under CRA §20(1)(c); after-tax return exceeds after-tax HELOC cost. When any condition fails, the strategy disappears. Unit tests verify both directions — strategy found when possible, not applied when not.

## 7. Model the mechanism, not the branded product

> `ReadvanceableMortgage` not `ManulifeOne`; `build_broker_scenarios(broker_offers=cfg)` not hardcoded rates.

Library code models financial mechanics (readvancing, deductible interest, amortization). Brand names, specific bank products, and product-specific features belong in the input config — they're data about a particular offering, not rules of the system.

A bank deposit offer is the same story. A flat high-interest savings account, a promotional-rate teaser, and a term deposit/GIC are *the same mechanism* — a balance earning interest under a schedule of rate steps — distinguished only by field values. Model it once as a generic `decisions.deposit_products[]` entry carrying a `rate_schedule` of interest steps; do not model it as a `deposit_offers[]` or promo-shaped leaf named after the offer. The rule, stated crisply: **when a new contract leaf is named after a product or offer, or carries product-specific fields (e.g. `promo_rate` + `ongoing_rate`) instead of a generic mechanism, that is DP#7 violated — generalize to the mechanism** (a `rate_schedule`, a composition, a generic option). The branded offer is input data feeding the mechanism; the mechanism is the code.

## 8. Compose through data, not through inheritance

> `AllocationStrategy` is a dataclass; `StrategyEngine.allocate(state)` takes any strategy.

Strategies, rate paths, and study periods are *data* objects that get passed into engines. The engines are stateless functions that take `(data) → result`. No strategy subclasses, no override hierarchies.

Jurisdiction-specific state composes the same way. `SimConfig` carries a `jurisdiction_state` dict alongside universal fields (`income`, `mortgage_balance`). Canadian modules populate `jurisdiction_state['canada']`; core treats it as opaque data passed through each simulation year. Only jurisdiction modules read or write their own section. This keeps core jurisdiction-agnostic by construction — the engine carries jurisdiction state, but never interprets it.

## 9. No backward compatibility

> No `Child(age=10)` shim that maps to `birth_year`. Just `birth_year`.

Backward-compat shims bloat the codebase and rot over time. Every deprecated alias, legacy parameter, and re-export wrapper adds lines that must be maintained, tested, and navigated — for an API that is already superseded. The old form never dies gracefully: removal dates get postponed, callers accumulate on the deprecated path, and the shim becomes a permanent fixture that obscures the canonical API.

**Do not maintain backward compatibility.** When changing an API, make the change in one commit: update the API, update every internal caller, and delete the old form entirely. External callers get a clear error (`NameError`, `TypeError`, `AttributeError`) with a helpful message pointing to the new API. The error message is the migration guide. There are no transition periods, no deprecation cycles, and no compatibility shims.

## 10. One module per government program, one module per jurisdiction

> `resp_rules` owns RESP/CESG/QESI; `tax_data` provides brackets by year/country/province.

Each module corresponds to a government program or regulatory regime. Jurisdiction (country, province) is data, not code branches — `TaxDataProvider.get_brackets(year, country, province)` loads the right brackets from data modules. That program's module may:
- Compose lower-level modules (e.g., a retirement module uses `tax_calculator` + `account_models`)
- Receive inputs provided by other modules (e.g., `resp_rules` receives family income from `family`)
- Receive a higher-level module to delegate to (e.g., `simulation` receives a `strategy` engine)

Core defines interfaces — protocols, base dataclasses, or function signatures — and jurisdiction packages provide concrete implementations. `marginal_rate(income, brackets)` is core; Canadian brackets and dividend-credit rules are registered by the Canada package. Adding a new country means adding a `countries/<country>/` directory and a registration call, not modifying core code.

The directory structure mirrors political hierarchy: `countries/<country>/` for federal-level modules and `countries/<country>/provinces/<province>/` for provincial modules. Each level is a proper Python package. Package presence is also a trigger (DP#16): if `countries/canada/` is on disk, the Canada package auto-registers its modules with the core providers.

| Core module | Role (jurisdiction-agnostic) |
|-------------|----------------------------|
| `tax_data` | Tax bracket data provider (year + jurisdiction as parameters) |
| `tax_calculator` | Generic tax computation functions (receives brackets from tax_data) |
| `simulation` | Year-by-year projection engine (receives jurisdiction state as data) |
| `strategy` | Allocation framework (jurisdiction-specific strategies are data) |
| `optimizer` | Pluggable optimizer modes and objectives |
| `return_model` | Pluggable investment return models |
| `stress_scenarios` | Path-based market and rate stress tests |

| Canada module | Government program / regime |
|---------------|--------------------------|
| `countries.canada.account_models` | Registered account regimes (RRSP, TFSA, RESP contribution rules) |
| `countries.canada.resp_rules` | RESP: CESG, QESI, CLB grant programs |
| `countries.canada.rate_model` | Mortgage regulation (amortization, penalties, rate terms) |
| `countries.canada.family` | Spousal RRSP attribution (ITA s.146(8.3)), family taxation |
| `countries.canada.retirement` | OAS, CPP/QPP, RRIF, pension splitting |
| `countries.canada.provinces.quebec.quebec_deduction` | Quebec interest deduction limit (TP-1 Schedule L) |

## 11. Unit tests verify each module's contract; integration tests verify composition by driving the fold

> Unit tests for `tax_calculator` use fabricated data; integration tests compose `tax_calculator` + `account_models` + `rate_model` by running the engine.

Unit tests verify a single module's *contract* — the input→output guarantee of one pure function — by calling that function with fabricated data and no cross-module dependencies. "In isolation" means **no dependency on other modules and no personal data**, not "you may avoid calling the engine." It does not licence reimplementing the module's downstream composition in the test.

Integration tests verify that modules compose correctly — they drive the fold (`FamilySimulation.run()` / `simulate_year_pure`) and assert the engine's observable output (`YearResult` / `SimState` fields), to catch interface mismatches and cross-module regressions. Neither layer uses personal data.

**A test must never reimplement production logic to avoid calling the engine.** If the behaviour under test is the composition — the fold, multi-year state, the wiring from allocation through a rule to the balance — drive it through the production entry point (`FamilySimulation.run()` / `simulate_year_pure`) and assert the engine's output. Copying a production formula into the test, or building internal engine state by hand instead of letting the engine build it, yields a test that passes while the engine is broken (see the "Reimplementing the engine in the test" trap in `AGENTS.md`). A unit test of a pure function calling that function directly is correct and encouraged; a test that *claims to validate an engine behaviour* but skips the engine is not a unit test, it is a shortcut.

Core test logic lives as plain functions parameterized by jurisdiction fixtures. `tests/core/` contains test functions; `countries/<country>/tests/` imports those functions and calls them with jurisdiction-specific data. One test instance per jurisdiction; no inheritance hierarchies. A jurisdiction provides fixtures and expected values, not duplicated test logic. Adding a new jurisdiction means adding a test directory with fixtures, not copying and modifying test files.

## 12. Real data is fetched, cached, and segregated from library code

> BoC prime rates come from `boc_data.py` (fetched + cached), not hardcoded in `rate_model.py`.

Government-published data (tax brackets, BoC rates, contribution limits) does not belong as hardcoded constants in library modules. It belongs in a data provider module that fetches it dynamically (with local caching and fallback defaults). Library code receives data as parameters. This keeps the library generic and the data fresh.

A consequence worth stating for anyone auditing reachability (#746): these data-provider and infrastructure modules are **unreachable from the simulation fold by design**. `optimize.py`/`simulate.py` and the year loop read the *cached output* of a fetch, never the fetcher — a provider the fold called directly would be the bug, not the absent call. So `boc_data`, `market_rates`, the `OntarioTaxData` container (`provinces/ontario.py`), and the reporting-side `product_registry` (DP#7: mechanisms, not branded products) show up as "unreached" in the #710 reach guard, and that is correct and permanent. Their rows in that guard's allowlist are a design fact, not the "implemented but not yet wired" debt the guard is otherwise tracking (see `tests/architecture/test_unreached_rule_modules.py`).

## 13. Defaults are fallbacks for absent input, not opinions — and never a way to coerce a value that was actually supplied

> `build_rate_path` uses `current_rate=0.05` only if the input config doesn't provide the actual rate. `ret.get('oas_annual_max') or get_oas_annual_max(sim_year)` is **not** this pattern: it cannot distinguish "the config supplied `0`" from "the config supplied nothing," so a genuine zero is silently coerced into the table default (#592, DP#32).

When a function has default parameter values, those defaults are fallbacks for when no input is provided — not opinions about what the correct value is, and not a device for coercing an input the caller *did* provide into something else. Test for absence explicitly (`param=None` and `if param is None`), not with `param or DEFAULT` — the `or` form fires on any falsy value the caller legitimately supplied (`0`, `''`, `[]`), which conflates "absent" with "present and zero" (DP#32). If the user provides data via config, *that data wins, including when that data is zero.*

The distinction is sharper for **data** than for **configuration**. A rate or a search parameter may reasonably default — round and clearly placeholder (e.g., `0.05` not `0.0495`; `year=2026`) — because the household simply didn't state an opinion on it. A **balance** may not: defaulting a missing $700k RRSP balance to `0` is not a fallback, it is an opinion that the money does not exist, and the run should error rather than silently answer with someone else's zero (#582, and the same failure shape in #575, #593). If a schema field is required, its absence is an error to surface, never an invitation to default it.

## 14. Scripts read a common config schema; each script uses the parts it needs

> All scenario scripts read `input.json`; `compare_scenarios.py` uses the `scenarios` section, `sensitivity.py` uses the `sensitivity_overlays`.

There is one config schema. Scripts share it, using only the sections relevant to their function. A script should be able to output a template config (zeroed or with defaults) so a new user can edit it before running. CLI flags are for overrides and debugging, not the primary interface.

The schema composes: universal fields (income, savings rate, mortgage, goals) are defined at the root. Country and province packages define their own schema extensions as additive sections — `countries/canada/input_schema.json` adds `rrsp_room_accumulated` and `fhsa_room`; a Quebec extension adds `qc_interest_carryforward`. The simulation engine reads only universal fields; jurisdiction modules read their own sections (DP#16). The full schema is the union of universal + jurisdiction extensions, not a flat file with every country's fields mixed in.

## 15. Personal data never enters version control

> `input.json` is `.gitignore`d; baseline files go to `~/.cache/`, not the repo.

Financial data — incomes, account balances, RRSP room, mortgage details, children's names and birth years — is personal. It belongs in `input.json` (which the user owns and `.gitignore`s) or in `~/.cache/project-name/` for transient outputs. It never goes in `git add`, never appears in commit messages, and never lands in test fixtures. Test data uses fabricated round numbers and role-based names (DP#4). If a baseline or regression check needs real output, store it outside the repo.

## 16. Modules auto-include when their trigger data is present or inferable

> Quebec deduction activates because the family is Quebec-resident; FHSA activates when `fhsa_room_accumulated` appears in `input.json`; LTV exploration runs when `house_value`, `mortgage_balance`, and `margin_available` are all non-zero.

A module participates in the simulation automatically if the input data contains its trigger fields — no flags, no configuration, no opt-in. The system detects what it can do from the data and does it. If `birth_year` exists, retirement drawdown is used for withdrawal tax; if `margin_available > 0` and `house_value > 0`, the cash-out optimizer and LTV explorer run; if spousal RRSP is contributed, attribution rules are checked. The absence of data is the only way to disable a module. CLI flags exist for debugging overrides, not as the primary interface (DP#14).

Package presence is also a trigger: if `countries/canada/` is on disk, the Canada package auto-registers its modules with the core providers. Jurisdiction modules read their own schema sections (DP#14); the absence of a schema section disables that jurisdiction, just as the absence of trigger data disables a module.

## 17. Tests exercise every rule path, not just every module

> The Quebec deduction carry-forward rule needs two tests: year 1 creates carry-forward, year 2 consumes it. The HELOC tracing "poisoning" rule needs a test where personal draws reduce the deductible proportion.

DP#11 says tests verify each module. This goes further: every government rule, every conditional branch, every edge case, and every carry-forward or carry-back mechanism gets at least one test. A rule with two outcomes (deductible vs. not deductible; carry-forward vs. consumed) needs two tests. A rule that depends on a threshold (MTR change at bracket boundary; RESP age limit) needs tests on both sides of the threshold. Coverage is measured in rule-paths exercised, not in line count or module count.

## 18. Scenarios compose from a base; overlays modify, they don't replace

> `scenario_b = deepcopy(scenario_a); scenario_b['property']['ltv_max'] = 0.30` instead of rebuilding from scratch.

A scenario is a base configuration plus a set of overlays (DP#5). Changing one variable — LTV, mortgage rate, income — produces a new scenario by overlaying a delta on the base, not by constructing an independent config. The `run_ltv_exploration` function already does this: it `deepcopy`s the base config and modifies `ltv_max`, `mortgage_balance`, and `cash_out` for each level. Every comparison script should follow this pattern. Overlays are small, auditable diffs; independent configs drift apart and hide what changed.

"Modify" is not satisfied by a write that lands anywhere in the config — it must land on the key the engine actually reads, or the overlay has neither modified the base nor replaced it; it has evaporated. `apply_sensitivity_overlay` writes the swept return rate to `assumptions.investment_return`, but the engine reads `return_model.rate`, and `SimulationConfig.from_dict` only materializes the deprecated scalar when no `return_model` block is present — which every schema-conformant config has. The result looks like a real sensitivity sweep and produces the base scenario's numbers three times over (#591). An overlay path is not verified by a test that asserts the *merged config* changed; it is verified by a test that runs the *engine* on the merged config and asserts the *output* changed, because a merge can succeed onto a dead key.

This is not an overlay-specific rule. **Any test that claims to verify an engine behaviour must run the engine and assert its observable output — not an intermediate the test constructed, and not a reimplemented copy of the production math.** A hand-built engine-state fixture that bypasses the fold is the same failure as a merge onto a dead key: the test's assertion holds while the engine's own path is broken, so a real defect hides behind a green test. The cure is identical — drive the fold, assert the output, and cross-check the magnitude against an independent source (a hand-calc, a published figure, or a golden value) so the test is not tautological.

**Cash-out money-flow model (issue #257).** A refinance `cash_out` is a *mortgage* increase whose proceeds are invested. It is recorded as debt exactly once, on `mortgage_balance`, and its proceeds are sourced into the invested lump sum via `property['cash_out']`. `margin_available` represents *pre-existing undrawn HELOC room* (the Smith Manoeuvre draw source) and must **not** be inflated by `cash_out` — doing so records the same borrowed dollar as debt twice (once as the mortgage refinance, once as a phantom HELOC draw). The conservation invariant: invested year-0 capital == HELOC margin draw + cash-out proceeds == total *new* debt beyond the pre-existing mortgage. Every invested dollar maps to exactly one liability (or one savings source). This invariant is required to hold in **every simulated year**, not merely at initialization or termination — a violation that is wrong in year 5 and self-corrects by year 30 passes a terminal-only check (#581). Cross-engine consistency checks alone cannot catch a violation when both engines share the same booking path; a money-conservation invariant must assert the relation directly, every year, in the fold (DP#26) — not only in a single bespoke regression test for one historical issue (see `tests/test_issue_257_cashout_conservation.py`).

## 19. Track cost basis from day one; compute tax at withdrawal

> `NonRegAccount(acb=50000)` records what you paid; `capital_gains_tax = (proceeds - acb) * inclusion * MTR` computes tax only when you sell.

Every account that triggers tax on disposition — non-reg capital gains, RRSP withdrawals, RESP EAP — needs its cost basis or contribution history recorded when money goes in, not estimated when money comes out. Without ACB tracking, the simulation cannot distinguish a $100k gain from a $10k gain on a $200k balance. The same principle applies to RRSP: the deduction timing (deduct now vs. deduct later) must be recorded per contribution, not assumed from a flat rate.

## 20. Data is year-versioned; simulate across tax years, not within a single year's brackets

> `tax_data.get_brackets(year=2026)` returns different brackets than `tax_data.get_brackets(year=2028)`; a 10-year simulation applies each year's brackets in that year.

Tax brackets, contribution limits, and CPP parameters change every year. A simulation that applies 2026 brackets in 2033 compounds a systematic error. The data layer (DP#12) must index every parameter by year, and the simulation engine must look up the correct year's data at each time step. Contribution limits (RRSP, TFSA, FHSA) grow with indexation; tax brackets shift with inflation. The simulation should not assume they're constant — but it should also allow a "frozen" mode where they're held fixed for sensitivity isolation (DP#5).

## 21. Return models are pluggable data, not hardcoded assumptions

> `build_rate_path(initial_rate=0.0495, rate_type='variable')` already swaps rate paths; the same mechanism should exist for investment returns: `ReturnModel.fixed(0.07)`, `ReturnModel.mean_reverting(0.07, 0.15)`, `ReturnModel.historical(sequence)`.

Mortgage rate paths are already pluggable via `rate_model.py` — `build_rate_path` accepts fixed, variable, and forecast paths. Investment returns should follow the same pattern: a `ReturnModel` object that the simulation receives as data (DP#8), not a hardcoded 7% float. Fixed, mean-reverting, and Monte Carlo return models all implement the same interface (`return_for_year(year, balance) -> float`), letting the user choose their uncertainty model without changing the engine.

## 22. Optimization objectives are data; the optimizer ranks, it doesn't choose

> `optimizer.rank(scenarios, objective=max_after_tax_terminal_value)` — the user picks the objective; the optimizer produces an ordered list.

The current optimizer ranks by a single `net_benefit` metric. But the Right Answer depends on the question: "what maximizes terminal wealth?" differs from "what maximizes probability of success?" or "what minimizes the retirement gap?" The optimizer should accept the objective as a parameter (DP#8: compose through data). Default to `net_benefit`, but allow `max_probability_success` (Monte Carlo), `min_retirement_gap` (uses `retirement.py`), or `max_after_tax_income` (considers drawdown). The optimizer produces a ranked list; the user makes the decision.

## 23. Randomness must be reproducible

> `monte_carlo(cfg, n=5000, seed=42)` produces the same 5000 paths on every run; change the seed, get different paths.

Any function that uses randomness — Monte Carlo simulations, stochastic return models, bootstrapped confidence intervals — must accept a `seed` parameter and use it consistently. Without a fixed seed, a Monte Carlo run cannot be reproduced, a bug cannot be isolated, and a sensitivity overlay (DP#5) cannot isolate the effect of one variable from noise. The seed defaults to a reproducible value (DP#13); the user overrides it only when they want different paths. The seed is not a creative choice — it is a controlled variable.

## 24. Config round-trips: load, modify, save

> `SimulationConfig.from_json('input.json')` loads; `config.to_dict()` exports; `json.dump(config.to_dict(), f)` saves the modified scenario.

The input schema loads from JSON (DP#14). It must also export back to JSON. Any config that the program modifies during a run — a new LTV level, an updated allocation, a stress-test overlay — should be serializable so the user can save it, inspect it, or re-run it. Without round-trip, every derived scenario is ephemeral and irreproducible. `to_dict()` is the inverse of `from_dict()`; together they form a complete load–modify–save cycle.

## 25. The four layers are data → scenario → simulation → optimization, and dependencies point inward

> `tax_data` has no imports; `simulation` imports `strategy`; `optimize` imports `simulation`. Nothing in a lower layer imports from a higher layer.

The project separates into four layers of increasing abstraction: (1) **Data** — tax brackets, limits, rates, fetched and cached (DP#12); (2) **Scenario** — portfolio composition, events, overlays built from a base config (DP#5, DP#18); (3) **Simulation** — year-by-year time stepping that composes government-program modules (DP#10); (4) **Optimization** — scenario comparison under a pluggable objective (DP#22). Dependencies only point inward: the simulation engine never imports the optimizer; the data layer never imports the scenario builder. This keeps each layer testable in isolation (DP#11) and extensible without breaking callers.

The root package exposes only jurisdiction-agnostic primitives — the simulation engine, the optimizer framework, return models, strategy frameworks, and the tax data provider. Canadian programs, account types, and convenience wrappers live in `countries.canada`. If code compiles with only the root package on `PYTHONPATH`, it is jurisdiction-agnostic by construction. Importing from a jurisdiction package is a deliberate act; core should never require it.

## 26. The simulation step is a pure function over explicit state; `run` is a fold over steps

> `simulate_year(state, year, action, config, return_model) → (YearResult, SimState)` — same inputs always produce the same outputs; `run` folds this over all years.

The simulation advances one year at a time. Each step reads the current state (account balances, contribution rooms, HELOC tracing totals, QC carry-forward), applies the year's actions (contributions, growth, mortgage payment), and returns both the year's result and the new state. The state is a data object — a `SimState` dataclass — not mutable `self` attributes. This makes the step a pure function (DP#3): same `(state, action, config, return_model)` always yields the same `(result, next_state)`. The `run` method is a fold: start from `SimState.initial(config)`, call `simulate_year` for each year, accumulate results. Grid search folds once per candidate; Monte Carlo folds N times with different `return_model` seeds (DP#23); `scipy.optimize` wraps the fold inside `f(x) → float`; dynamic programming explores the state tree by forking `SimState` at any year. None of these optimizer modes require changes to the simulation engine — they consume the same `simulate_year` function, differing only in how they traverse the state space.

Universal state (income, mortgage balance, year) is explicit on SimState. Jurisdiction-specific state (Quebec deduction carry-forward, RRSP room, FHSA balance) goes in `SimState.jurisdiction_state['canada']`, not as individual fields on SimState. Jurisdiction modules read and write their section; core passes it through untouched (DP#8). This keeps the simulation engine jurisdiction-agnostic — it folds opaque jurisdiction state without interpreting it.

## 27. Investment income has distinct tax treatments; the simulator models them by type, not by a flat rate

> A $10,000 eligible dividend is not the same as a $10,000 capital gain is not the same as a $10,000 interest payment. Each has its own gross-up, credit, inclusion rate, and withholding tax.

Tax law treats five income types differently: (1) **Interest** — fully taxable at marginal rate; (2) **Canadian eligible dividends** — 38% gross-up, federal + provincial dividend tax credits, ~25% effective rate at high MTR; (3) **Canadian non-eligible dividends** — 15% gross-up, lower credits, ~35% effective; (4) **Capital gains** — 50% inclusion, ~23% effective at high MTR, deferred until realized; (5) **Foreign income** — fully taxable plus potentially unrecoverable withholding tax that varies by account type (RRSP treaty exemption vs TFSA no exemption). A sixth category, **Return of Capital**, is not taxed when received but reduces ACB. The simulator must accept the portfolio's income-type composition as data (DP#8) and apply the correct tax treatment per type per jurisdiction (DP#10). A flat 7% return model is a fallback (DP#13), not the production path.

## 28. Eligibility is a date-computed gate; programs enter and exit on a schedule

> RESP CESG eligibility ends the year the child turns 17; FHSA closes December 31 of the year after the first qualifying withdrawal; spousal RRSP attribution lasts 3 calendar years after contribution; pension splitting activates at 65; CPP can start at 60 or defer to 70.

Every government program has an eligibility window defined by dates or age thresholds. The simulator must check eligibility at each time step using the stored dates (DP#1): `child.age_in(year) < 17` gates CESG; `years_since_contribution >= 3` gates spousal RRSP attribution release; `member.age_in(year) >= 65` gates pension splitting. Eligibility is computed, not stored — a boolean `is_eligible` field would go stale (DP#1 again). Programs that have not yet activated report zero benefit; programs that have expired report zero benefit. The simulation must handle the transition years correctly — partial-year eligibility, catch-up provisions, and deadline urgency are all rule paths that need tests (DP#17).

A program reporting "zero benefit because outside its window" is a *correct* answer computed from a present, well-formed date — and it must not be confusable with a zero produced because the eligibility data itself never reached the member. The Canada overlay merges `family.members` positionally against a one-element template, so every member but the primary silently receives none of their jurisdiction defaults: their CPP start age, OAS start age, RRSP/TFSA/FHSA room all fall back to `0`/`65`, and the simulator reports it exactly as it would report a spouse who is correctly, if unfavourably, ineligible (#590). A gate computed from *missing* eligibility inputs is not eligibility computed from a date — it is DP#32's zero-as-fallback wearing this principle's clothing, and it needs the same fix: missing per-member data must fail loudly, not read as a legitimately-gated zero. The gate itself also needs a date precise enough to gate on — CPP/OAS eligibility is monthly, RRIF conversion is end-of-year-you-turn-71 — and a `birth_year` cannot resolve either without silently assuming a birth month (#597, DP#1).

## 29. The optimizer reports risk measures alongside expected value

> A scenario with $200k expected net benefit and 15% probability of loss is not the same as a scenario with $200k expected benefit and 0% probability of loss. The optimizer reports both.

Expected value alone cannot distinguish a safe strategy from a risky one. The optimizer must report at least one downside measure for every scenario: probability of loss (Monte Carlo, DP#23), maximum drawdown (stress test, scenario 8.1), or years to recovery after a specified shock. These risk measures are objective functions (DP#22) — the user chooses which risk measure matters for their decision. A strategy that dominates on both expected value and downside is strictly preferable; a strategy that trades expected value for lower risk requires the user's judgment (DP#5: anchor with overlay). Risk measures are computed per simulation path, not estimated from a single deterministic run.

## 30. The simulator models the tax consequences of financial decisions; it does not make financial decisions

> "Given this portfolio allocation, what's the after-tax outcome?" is in scope. "What should I invest in?" is not.

The user provides their asset allocation, risk tolerance, and planned decisions as input data. The simulator computes the tax consequences — deduction amounts, grant eligibility, clawback thresholds, after-tax returns — and ranks candidate decisions by after-tax outcome. What the simulator does not do: choose investments, assess insurance needs, predict market returns, evaluate rental property cash flow, optimize corporate tax structures, or recommend specific financial products. When a scenario requires data that belongs to those domains — rental income for cash damming, margin call risk for leverage, investment return assumptions for break-even analysis — the user provides it as input; the simulator uses it to compute the tax impact. Risk measures (DP#29) report the tax impact of a user-specified stress scenario; they do not predict the probability of that scenario occurring. This boundary keeps `input.json` bounded (describe your situation, not your entire financial life), keeps the module list bounded (one module per government program that affects personal tax), and keeps the output bounded (after-tax numbers, not investment advice).

**In scope**: RRSP/TFSA/RESP/FHSA contribution and withdrawal timing, mortgage interest deductibility, HELOC tracing, OAS clawback, pension splitting, CPP/QPP deferral, dividend tax credits, capital gains inclusion, foreign withholding tax, spousal RRSP attribution, TOSI, Quebec interest deduction limits, registered account contribution room, CESG/QESI grant eligibility.

**Out of scope**: Investment selection, asset allocation advice, insurance needs analysis, real estate valuation, rental property management, corporate tax optimization, business accounting, estate planning (wills, trusts, probate), foreign exchange risk, crypto taxation specifics, product-specific features (DP#7).

## 31. The optimizer mode is pluggable data; the search method and the objective are separate choices

> `GridOptimizer`, `MonteCarloOptimizer`, `ScipyOptimizer` all consume the same `simulate_year_pure` — the user picks the search method and the objective independently.

The optimizer has two independent degrees of freedom: **what** to optimize (the objective, DP#22) and **how** to search (the mode). Both are data the user provides, not code the optimizer hardcodes. Grid search evaluates every candidate on a fixed grid — appropriate for discrete strategy choices like RRSP-first vs TFSA-first. Monte Carlo runs N stochastic paths per candidate — appropriate for tail-risk questions like "what's P(loss)?" Continuous optimization (scipy) finds optimal real-valued parameters like LTV % or pension split % — appropriate when the decision variable is smooth and unimodal. Dynamic programming explores the state tree by forking `SimState` at each year — appropriate for sequential decisions like deduct-later timing or drawdown order. All four modes consume the same `simulate_year_pure` function (DP#26) and the same `ReturnModel` (DP#21); they differ only in how they call it and how many times. No optimizer mode may require changes to the simulation engine, and no optimizer mode may import from another optimizer mode. The `Optimizer` base class defines the interface; each mode is a subclass that implements `optimize()`.

## 32. Zero is a value, not a fallback; absence must fail loudly

> `ret.get('oas_annual_max') or get_oas_annual_max(sim_year)` — a configured `0` means "this household has no OAS"; the `or` makes it mean "unset, refetch the table," and zero becomes unrepresentable (#592).

`None` (or a key missing entirely) means *unknown* — use the fallback. `0` means *zero* — use it. An expression of the shape `x = cfg.get(k) or DEFAULT` conflates the two whenever `0`, `''`, `[]`, or `False` is a value the input can legitimately hold, and it is a **reviewable smell**, not an idiom: it silently overwrites a value the user actually supplied. The fix is explicit absence-testing — `x = cfg.get(k); x = DEFAULT if x is None else x`, or `if k not in cfg` — never truthiness.

The same failure has other shapes, and all of them must be rejected the same way: loudly, not by producing a plausible number.

- **An unimplemented rule is not a no-op.** If a rule cannot be applied, the run must say so in the output, not silently behave as if the rule fired and did nothing. Five schema blocks — `heloc`, `rental_properties`, `life_events`, `rate_scenarios`, `employer_benefits` — used to be parsed by `SimulationConfig.from_dict` and then read by nothing; filling them in with real numbers changed zero output, and nothing warned that it would (#593). Fixed in epic #603 Track C Phase 2 (DP#9) by deleting the five blocks outright, rather than leaving them to silently no-op: a feature that has never run is not a feature, it's a liability.
- **An override written to a key nothing reads is not applied.** A sensitivity sweep that writes `assumptions.investment_return` while the engine reads `return_model.rate` is a silent no-op sweep, not a smaller one — three runs reported as three different assumptions were the same number (#591; DP#18).
- **A balance of zero is not a rate of zero.** `if self.balance <= 0: return 0.0` conflates "there is nothing here yet to compound" with "the rate this account earns is zero," and a rate must not depend on the balance in the first place (DP#3). Combined with a portfolio object snapshotted once at `__init__` and never refreshed, a non-registered account that starts at `$0` — the ordinary case — compounds at 0% for the life of the projection, even after it holds seven figures (#575).
- **A missing optional dependency is not silent degradation.** If `pandas` is absent, the ranked summary must report that it was skipped and why, not just quietly omit a section of output (#367).

Absent input is an error the run must surface. Absent rule coverage is a coverage failure the run must surface. A rule that is disabled must say so explicitly — in the output — rather than by producing a zero balance, a zero rate, or an unswept sweep that looks identical to a real one. A tool whose entire product is a number the user cannot independently verify does not get to guess quietly; every one of these bugs produced a confident, plausible, wrong number, and 4,000 green tests caught none of them because none asserted that the *absence itself* was reported.
---

## 33. A declaration is a lens, not a blindfold

> `if declared: result[dim] = _convert_(declared)` / `else: result[dim] = _discover_(cfg)` — declaring two refinance options replaced a ten-rung LTV ladder with exactly those two, and the output still rendered like an exploration (#846, #853).

When a household declares candidates for a dimension — `decisions.mortgage.refinance_options[]`, `structure_options[]`, a strategy list — it is saying *"these are the options I am choosing between."* It is **not** saying *"stop exploring."* A declaration says which answers the household wants **named**; it must never shrink the search that gives those answers their meaning.

Replacing the swept set with the declared set destroys exactly the context the declaration exists to be read against. Declare `{no_cash_out, cash_out_80}` and you learn that 80% beats 0% — and you never learn that 50/60/70% exist, still less that one of them might beat both. The household's own question silently deletes its answer's frame of reference, and nothing looks wrong: the ranking still prints, confident and plausible.

The rule: **run the auto-discovered exploration, then annotate it.** A declared candidate landing on a swept rung *marks* that rung. One falling between rungs is *inserted in situ* and ranked there. The declaration becomes a lens — it tells the reader where they are standing on the curve — instead of a blindfold that removes the curve.

- **A declared candidate is a question, not a constraint.** Four dimensions carry this pattern (mortgage / refinance / strategy / resp_action), in both CLIs; `simulate.py`'s grid was measured collapsing **240 → 48 overlays** from the same branch (#848).
- **Narrowing must be earned, and loud.** If a dimension genuinely must narrow — cost, or an incompatible declaration — the output must name what was dropped (#846, #848). The silence is the defect; the narrowing is only the mechanism.
- **More information is the safe direction.** Choosing a default between "explore more and annotate" and "explore only what was asked", take the former: a reader can ignore an extra rung, but cannot ignore one they were never shown.

This is DP#32's failure mode wearing a different hat — an unswept sweep that looks identical to a real one — and it shares DP#18's shape: a declaration that reaches the engine and then evaporates is not a smaller answer, it is a **different** one. Related: DP#5 (anchor decisions, overlay sensitivities), DP#22 (the optimizer ranks, it doesn't choose).
