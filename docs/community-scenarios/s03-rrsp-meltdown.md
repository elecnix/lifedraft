# S03 — RRSP meltdown before age 71

- **Community source:** r/fican, "Aggressive RRSP meltdown to 0 by 64 (age 55 FIRE)"
  <https://www.reddit.com/r/fican/comments/1u46z69/aggressive_rrsp_meltdown_to_0_by_64_age_55_fire>
  and r/PersonalFinanceCanada "Retire at 60. RRSP meltdown plan"
  <https://www.reddit.com/r/PersonalFinanceCanada/comments/1ke33ta/retire_at_60_rrsp_meltdown_plan>;
  Cut The Crap Investing
  <https://cutthecrapinvesting.com/2025/09/07/the-rrsp-meltdown-a-canadian-retirees-greatest-hack/>.
- **Program / maneuver:** Voluntary RRSP/RRIF withdrawals in the low-income years
  (typically 60-70) to defuse the "tax bomb" of forced RRIF minimums after 71 stacked
  on CPP and OAS.
- **Situation:** A retiree with a large RRSP draws it down aggressively in early
  retirement — often while delaying CPP/OAS to 70 — filling low tax brackets so that
  later mandatory RRIF minimums do not spike income and trigger OAS clawback.
- **Why it is interesting (complex):** The optimal drawdown rate trades current tax
  against future forced income, OAS clawback, and estate tax on the terminal RRIF;
  the modern non-leveraged version is distinct from the older (discouraged) loan-based
  meltdown.
- **Engine coverage — MODELED.** `countries/canada/retirement_transition.py` defines the
  `rrsp_meltdown` drawdown order (registered first, TFSA preserved) and the
  bracket-filling drawdown (`drawdown_bracket_target`, `plan_drawdown_net`,
  `rrsp_bracket_fill`, issue #618) that draws up to a declared bracket ceiling and
  reports the first-shortfall year per sweep value (#771); drawdown orders are
  enumerated in `scenario_discovery.py::discover_drawdown_orders`.
