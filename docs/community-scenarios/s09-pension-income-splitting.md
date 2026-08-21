# S09 — Pension income splitting at 65

- **Community source:** Sun Life "Income splitting opportunities for couples in
  retirement"
  <https://www.sunlifeglobalinvestments.com/en/insights/investor-education/tax-and-estate-planning/Income-splitting-opportunities-for-couples-in-retirement/>;
  r/fican retirement-tax threads.
- **Program / maneuver:** Pension income splitting — from age 65, up to 50% of eligible
  pension income (including RRIF withdrawals) can be allocated to a spouse on the tax
  return.
- **Situation:** A retiree converts part of the RRSP to a RRIF at 65 to create eligible
  pension income, then splits up to 50% with a lower-income spouse, also unlocking two
  pension-income tax credits and reducing OAS clawback.
- **Why it is interesting (moderately complex):** A pure paper allocation each year
  with a clean 0-50% optimization; interacts with OAS clawback and each spouse's
  bracket, and pairs with (or substitutes for) spousal RRSPs.
- **Engine coverage — MODELED.** `countries/canada/pension_split_optimizer.py` searches
  the optimal split fraction (tests `tests/test_pension_split_optimizer.py`).
