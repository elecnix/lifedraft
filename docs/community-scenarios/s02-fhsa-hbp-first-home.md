# S02 — FHSA + Home Buyers' Plan stacking for a first home

- **Community source:** r/fican, "I (32M) have 100k cash, maxed TFSA — what next?"
  <https://www.reddit.com/r/fican/comments/1tz26dx/i_32m_have_100k_cash_lying_around_maxed_out_tfsa>
  and r/PersonalFinanceCanada "Would it be beneficial to keep FHSA maxed out for 15
  years"
  <https://www.reddit.com/r/PersonalFinanceCanada/comments/1utc4hf/would_it_be_beneficial_to_keep_fhsa_maxed_out_for>;
  CIBC comparison
  <https://www.cibc.com/en/personal-banking/smart-advice/buying-or-renting-a-home/fhsa-rrsp-tfsa-comparison.html>.
- **Program / maneuver:** First Home Savings Account (deduct on the way in, tax-free
  qualifying withdrawal) combined with the RRSP Home Buyers' Plan (interest-free loan
  from your own RRSP, repaid over 15 years).
- **Situation:** A first-time buyer maxes the FHSA ($8,000/yr, $40,000 lifetime) for
  the deduction and tax-free withdrawal, and *also* withdraws up to $60,000 under the
  HBP for the same purchase. Unused FHSA funds roll to the RRSP tax-free if no home is
  bought.
- **Why it is interesting (simple to set up, subtle to optimize):** Very common and
  relatable, but the deduction-timing (claim FHSA deduction in a high-income year),
  the HBP repayment schedule, and the 15-year FHSA account clock create real
  optimization choices.
- **Engine coverage — MODELED.** `countries/canada/fhsa.py` models the FHSA and
  `countries/canada/hbp_rules.py` models the Home Buyers' Plan; regression tests live
  in `tests/test_fhsa.py` and `tests/test_fhsa_issue_50.py`.
