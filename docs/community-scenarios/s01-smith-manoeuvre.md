# S01 — Smith Manoeuvre (readvanceable mortgage)

- **Community source:** r/PersonalFinanceCanada, "Would I be dumb for not using Smith
  Manoeuvre?"
  <https://www.reddit.com/r/PersonalFinanceCanada/comments/1tjkuic/would_i_be_dumb_for_not_using_smith_manoeuvre_in/>
  and r/fican "Smith manoeuvre for FIRE"
  <https://www.reddit.com/r/fican/comments/1ol56x4/smith_manoeuvre_for_fire>; Loonie
  Doctor home-equity income-splitting series
  <https://www.looniedoctor.ca/2018/02/23/home-equity-income-splitting-investing/>.
- **Program / maneuver:** Converting a non-deductible mortgage into a tax-deductible
  investment loan using a readvanceable mortgage (HELOC that grows as principal is paid).
- **Situation:** A homeowner with a readvanceable mortgage re-borrows each principal
  payment and invests it in income-producing securities, making the interest
  tax-deductible. Refunds and dividends are recycled back into the mortgage to
  accelerate the "good debt for bad debt" swap.
- **Why it is interesting (complex):** Leverage, interest deductibility (CRA IT-533
  tracing), attribution, and mortgage mechanics all interact; the payoff depends on
  rate spreads and the investor's discipline over 15-25 years.
- **Engine coverage — MODELED.** `countries/canada/strategies.py` defines
  `STRATEGY_READVANCE_PRIORITY` (aliases `smith_priority`); `account_models.py`
  implements `contribute(is_smith=True)` and `add_smith_interest()`;
  `simulation_rules.py` applies `apply_sm_readvance` / `apply_sm_interest` /
  `apply_sm_investment_growth` with a charge guard that refuses an unbounded readvance
  past the registered charge (#681). The Quebec deductible-interest portion and
  carry-forward are in `provinces/quebec/quebec_deduction.py`. A "poor-man's" variant
  with the readvance turned off is handled (issue #713).
