# S12 — Fonds FTQ / LSIF 30% labour-sponsored credit

- **Community source:** Fonds de solidarité FTQ "RRSP+: 30% in additional tax savings"
  <https://www.fondsftq.com/en/personal/savings-vehicles-products/rrsp-plus>; Revenu
  Québec line 424
  <https://www.revenuquebec.ca/en/citizens/income-tax-return/completing-your-income-tax-return/how-to-complete-your-income-tax-return/line-by-line-help/400-to-447-income-tax-and-contributions/line-424/>.
- **Program / maneuver:** Labour-Sponsored Investment Fund credit — the Fonds FTQ (and
  Fondaction) give a 30% credit (15% Quebec + 15% federal) on up to $5,000/yr
  ($1,500 credit), *stacked on top of the RRSP deduction* when held in an RRSP+.
- **Situation:** A Quebec employee contributes $5,000/yr to a Fonds FTQ RRSP+ and
  captures both the RRSP deduction and the $1,500 labour-sponsored credit, for combined
  tax relief that can reach 57-83% of the contribution — subject to an income cap that
  can disqualify the federal half.
- **Why it is interesting (province-specific, high-value):** A rare stacked credit with
  eligibility gates, an 8-year hold, and a phase-out for higher incomes — attractive
  but hemmed by rules.
- **Engine coverage — MODELED.** `countries/canada/fonds_ftq.py` and
  `countries/canada/lsif_credit.py` model the credit (tests `tests/test_lsif_credit.py`).
