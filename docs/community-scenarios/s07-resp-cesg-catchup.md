# S07 — RESP / CESG grant catch-up

- **Community source:** Money After Graduation / Embark "CESG Contributions: How to
  Catch Up and Maximize"
  <https://www.embark.ca/learning-centre/cesg-contributions-how-to-catch-up-maximize-the-benefits>;
  canada.ca CESG page
  <https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-education-savings-grant-cesg.html>.
- **Program / maneuver:** Canada Education Savings Grant — 20% match on the first
  $2,500/year, $500/year, $7,200 lifetime per child; unused room lets you claim up to
  $1,000 grant on $5,000 in one year.
- **Situation:** A parent who started late (or received a large one-time gift) spreads
  contributions to capture $5,000/year (grant on $2,500 current + $2,500 carry) rather
  than dumping a lump sum that forfeits grant. The plan targets exactly $36,000
  contributed to bank the full $7,200 CESG.
- **Why it is interesting (simple, high-value):** The rules are easy to state but the
  optimal contribution schedule (and the age-17 cutoff) is a classic
  constrained-optimization the engine can solve exactly.
- **Engine coverage — MODELED.** `countries/canada/resp_rules.py` models RESP
  contributions and CESG grant accrual/catch-up.
