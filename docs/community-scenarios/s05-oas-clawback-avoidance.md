# S05 — OAS clawback avoidance

- **Community source:** PlanEasy "What Are OAS Clawbacks? How Can You Avoid Them?"
  <https://www.planeasy.ca/what-are-oas-clawbacks-how-can-you-avoid-them/>; r/fican
  clawback threads.
- **Program / maneuver:** Old Age Security recovery tax — OAS is clawed back at 15% of
  net income above the threshold (~$93k in 2026), fully eliminated near ~$152k.
- **Situation:** A higher-income retiree keeps net income under the threshold using
  pension income splitting, TFSA withdrawals (non-taxable), RRSP-meltdown smoothing,
  and timing of capital-gain realizations, so as not to lose 15 cents of OAS on each
  extra dollar.
- **Why it is interesting (complex):** The clawback is a hidden marginal-rate spike;
  optimizing it couples with drawdown order, CPP/OAS start age, and splitting — several
  levers at once.
- **Engine coverage — MODELED.** OAS and its clawback are handled in
  `countries/canada/retirement.py`, and splitting to reduce a spouse's net income is in
  `countries/canada/pension_split_optimizer.py`.
