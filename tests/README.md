# Test suite — invariant & property layer

Most tests in this directory verify behaviour the way the application is used:
they run a pipeline and assert it (a) doesn't crash and (b) produces specific
output numbers for fixed inputs. That style is necessary but insufficient: it
let three real correctness bugs ship despite a 2,400-test suite (issue #258):

- **#249** — sensitivity sweep was inert (the swept return rate was silently
  discarded).
- **#250** — `optimize.py` headline double-counted a refinance cash-out as free
  money (~$0.5M phantom benefit).
- **#251** — the deduct-later header contradicted the simulation.
- **#257** — a latent cash-out double-count (recorded as debt twice).

All four are semantic/accounting errors. "It runs" tests can't see them, and
"output == $X" tests actively *lock in* the wrong number.

## The lower-layer approach

To make this whole class regression-proof, the `*_258.py` modules assert
**invariants and relationships**, not symptoms or magic numbers:

| Module | What it guarantees |
|---|---|
| `test_overlay_propagation_258.py` | **Config-transformation propagation.** Each `ScenarioOverlay` field lands *where the engine actually reads it* — `investment_return` → the effective `return_model` rate used in compounding (`sim.return_model.return_for_year(0)`); `mortgage_rate` → `sim.rate_path.get_rate(0)`; `cash_out` → recorded debt; `salary_growth` → `config.salary_growth`; `resp_cash_out` → zeroed RESP + `free_cash`. This catches the DP#21 dual-field family (writing the deprecated source of truth while the engine reads the preferred one). |
| `test_engine_invariants_258.py` | **Monotonicity** (net worth strictly ↑ with `investment_return`, strictly ↓ with mortgage rate), **money conservation** (Δ initial `total_debt` == `cash_out`), and **determinism** (identical inputs ⇒ identical `YearResult`). |
| `test_cross_engine_258.py` | **Cross-engine consistency.** `optimize.py` and `simulate.py` must agree on recorded debt and net benefit for one canonical refinance scenario, pinned to the same named strategy. |

### Rules of thumb for new modeling code

1. **Assert relations, not dollars.** Prefer `a < b`, `Δdebt == cash_out`,
   "strictly monotone over a grid" over a hardcoded `assertAlmostEqual(x, 123456)`.
   Relational assertions survive legitimate model refinements; pinned numbers
   silently lock in whatever the math happens to produce, right or wrong.
2. **Assert at the point of consumption.** A propagation test must read the value
   from where the *engine* reads it, not from where the transform wrote it.
   (`apply_overlay` writing `assumptions.investment_return` is worthless if the
   engine compounds off `return_model_data`.)
3. **Conservation is a law.** Every borrowed dollar maps to exactly one liability;
   every invested dollar has a matching source. Test the balance, not the balance
   sheet line.
4. **Determinism is contractual** (DP#3). Pure functions get a same-inputs ⇒
   same-output test.

## xfail tracking (issue #258)

This suite branched from `main` while the fixes lived in unmerged PRs, so some
invariants are honestly violated on `main`. Those tests are marked
`@pytest.mark.xfail(strict=False)` so CI stays green and they auto-flip to XPASS
when the fix lands. Do **not** weaken an assertion to force green — use `xfail`.

| Test | Tracks | Flips when merged |
|---|---|---|
| `test_investment_return_reaches_engine_with_return_model_block` (×3) | #249 | sweep propagates into `return_model_data` |
| `test_net_worth_increases_with_investment_return` | #249 | sweep is no longer inert |
| `test_cash_out_conserves_debt_exactly` (×3) | #257 | cash-out recorded as debt once |
| `test_cross_engine_refinance_debt_matches` | #250 | optimize.py records cash-out as debt |
| `test_cross_engine_refinance_net_benefit_matches` | #250 | net benefit no longer inflated |

All other tests in these modules hold on `main` today (determinism,
`mortgage_rate` propagation, mortgage-rate monotonicity, cash-out monotonicity,
matched future_value) and are not xfailed.
