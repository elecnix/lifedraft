# S20 — CPP claiming-age timing (delay to 70)

- **Community source:** r/fican and r/PersonalFinanceCanada "when to take CPP" threads;
  canada.ca CPP retirement-pension timing
  <https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-benefit/amount.html>.
- **Program / maneuver:** CPP start-age election — taking CPP as early as 60 (0.6%/month
  reduction) or as late as 70 (0.7%/month enhancement, ~42% more than at 65), plus
  optional CPP pension sharing between spouses.
- **Situation:** A retiree with other assets bridges spending from an RRSP meltdown in
  the early years and delays CPP to 70 to lock in the largest inflation-indexed,
  longevity-protected pension; a couple may also elect CPP sharing to level income.
- **Why it is interesting (simple lever, deep consequences):** A single date choice with
  large, longevity- and breakeven-dependent effects; it couples tightly with the RRSP
  meltdown, OAS timing, and GIS/OAS clawback.
- **Engine coverage — MODELED.** `countries/canada/claiming_age_optimizer.py` searches
  the optimal CPP/OAS start age, `cpp_estimator.py` projects the entitlement, and
  `cpp_sharing.py` models spousal sharing (tests `tests/test_cpp_estimator.py`,
  `tests/test_cpp_sharing.py`).
