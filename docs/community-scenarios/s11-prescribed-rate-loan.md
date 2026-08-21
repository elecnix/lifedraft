# S11 — Prescribed-rate spousal loan for income splitting

- **Community source:** Loonie Doctor, "Playing The Banker With a Spousal Loan To Income
  Split" <https://www.looniedoctor.ca/2018/02/03/spousal-loans-income-splitting/>;
  CIBC prescribed-rate-loan primer
  <https://www.cibc.com/content/dam/personal_banking/advice_centre/tax-savings/prescribed-rate-loans-en.pdf>.
- **Program / maneuver:** Prescribed-rate loan — the high-income spouse lends investment
  capital to the low-income spouse at the CRA prescribed rate (locked in for the life of
  the loan) to sidestep the attribution rules, so investment income is taxed in the
  lower spouse's hands.
- **Situation:** With a promissory note and interest paid by January 30 each year, the
  couple shifts the return on a large non-registered portfolio to the lower earner. The
  rate is fixed at inception, so loans struck when the prescribed rate was 1% keep that
  rate indefinitely.
- **Why it is interesting (complex, timing-sensitive):** Value depends on the spread
  between the locked prescribed rate and the portfolio return, the interest-payment
  formality, and both spouses' brackets — a maneuver that can quietly fail if a January
  payment is missed.
- **Engine coverage — MODELED.** `countries/canada/attribution.py` handles the
  prescribed-rate exception to attribution (tests `tests/test_attribution.py`); the
  prescribed rate also appears in `debt.py` and `locked_in_account.py`.
