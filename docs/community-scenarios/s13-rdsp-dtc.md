# S13 — RDSP + Disability Tax Credit

- **Community source:** r/PersonalFinanceCanada RDSP threads; canada.ca RDSP page
  <https://www.canada.ca/en/employment-social-development/programs/disability/savings.html>
  and Disability Tax Credit
  <https://www.canada.ca/en/revenue-agency/services/tax/individuals/segments/tax-credits-deductions-persons-disabilities/disability-tax-credit.html>.
- **Program / maneuver:** Registered Disability Savings Plan — DTC eligibility unlocks
  the Canada Disability Savings Grant (up to 300% match, $3,500/yr, $70,000 lifetime)
  and the Canada Disability Savings Bond (up to $1,000/yr, $20,000 lifetime, no
  contribution required for low income).
- **Situation:** A DTC-eligible person (or their family) contributes to an RDSP to
  harvest the maximum grant match and, if low-income, the bond; withdrawals after the
  10-year assistance-holdback period are partly taxable to the beneficiary.
- **Why it is interesting (complex, under-served):** The grant/bond match rates, carry-
  forward of unused entitlements, DTC gating, and the 10-year holdback make this one of
  the highest-return programs in Canada — yet it is rarely modeled by planning tools.
- **Engine coverage — GAP.** No RDSP module exists (`grep -ri rdsp countries/canada/`
  returns nothing) and the Disability Tax Credit is only present as provincial DTC
  *rate constants* (`countries/canada/provinces` DTC rates), not as an RDSP/grant engine.
  **Roadmap candidate.**
