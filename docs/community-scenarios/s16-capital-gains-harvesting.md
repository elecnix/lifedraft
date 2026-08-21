# S16 — Capital-gains harvesting and tax-loss selling

- **Community source:** r/PersonalFinanceCanada / r/fican year-end tax-loss and
  gains-harvesting threads; canada.ca superficial-loss rules
  <https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/personal-income/line-12700-capital-gains/capital-losses-deductions/what-a-superficial-loss.html>.
- **Program / maneuver:** Deliberate realization in a non-registered account —
  *harvesting gains* to fill a low bracket and bump up the adjusted cost base, and
  *harvesting losses* to offset gains, while respecting the 30-day superficial-loss rule
  (loss denied if you or an affiliated person rebuys within 30 days).
- **Situation:** In a low-income year a retiree or sabbatical-taker realizes just enough
  gains to stay under a bracket/OAS threshold (resetting ACB tax-free), and in a down
  market sells losers to bank losses against past/future gains without triggering the
  superficial-loss trap by swapping to a similar-but-not-identical ETF.
- **Why it is interesting (moderately complex):** The 2024 inclusion-rate change (50% up
  to $250k, 66.7% above) and the superficial-loss rule make timing and affiliated-person
  tracking genuinely tricky.
- **Engine coverage — PARTIAL.** Gain realization and the $250k inclusion boundary are
  modeled (`countries/canada/portfolio.py`, `tests/test_non_reg_capital_gains_realization.py`)
  and a `rrsp_bracket_fill` drawdown exists, but there is **no deliberate tax-loss
  harvesting engine and no superficial-loss (30-day) rule**. Candidate enhancement.
