# S19 — Canada Workers Benefit & GST/HST credit

- **Community source:** r/PersonalFinanceCanada low-income and student threads; canada.ca
  Canada Workers Benefit
  <https://www.canada.ca/en/revenue-agency/services/child-family-benefits/canada-workers-benefit.html>
  and GST/HST credit
  <https://www.canada.ca/en/revenue-agency/services/child-family-benefits/goods-services-tax-harmonized-sales-tax-gst-hst-credit.html>.
- **Program / maneuver:** Refundable income-tested benefits — the Canada Workers Benefit
  (with a disability supplement) and the quarterly GST/HST credit, both of which phase
  out as family net income rises.
- **Situation:** A lower-income worker or student manages *net* income (e.g. by timing an
  RRSP/FHSA deduction) to stay within the CWB and GST/HST-credit phase-in/out bands,
  since an RRSP deduction can both cut tax and *increase* refundable benefits.
- **Why it is interesting (simple but high effective rate):** For modest incomes these
  clawbacks stack into very high effective marginal rates, so a small deduction can have
  an outsized after-benefit payoff — the low-income mirror of OAS-clawback planning.
- **Engine coverage — GAP.** Neither the CWB nor the GST/HST credit is modeled
  (`grep -ri "workers benefit\|CWB\|GST/HST credit" countries/canada/` finds nothing).
  The engine handles high-income clawbacks (OAS/GIS) but not these low-income refundable
  credits. **Roadmap candidate.**
