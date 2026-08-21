# S08 — Spousal RRSP equalization

- **Community source:** r/PersonalFinanceCanada, "Gifted wife $29k for TFSA, now want to
  move it to a spousal RRSP"
  <https://www.reddit.com/r/PersonalFinanceCanada/comments/1snz3wu/gifted_wife_29k_for_tfsa_now_want_to_move_it_to_a>
  and r/fican "Spousal RRSP Withdrawal Rule"
  <https://www.reddit.com/r/fican/comments/1hkpxxn/spousal_rrsp_withdrawal_rule>; PlanEasy
  "What Is A Spousal RRSP?" <https://www.planeasy.ca/what-is-a-spousal-rrsp/>.
- **Program / maneuver:** Spousal RRSP — the higher-income spouse contributes (and
  deducts), the lower-income spouse owns and later withdraws, subject to the 3-year
  attribution rule.
- **Situation:** A couple with lopsided incomes equalizes their registered balances
  before retirement so that RRIF income lands roughly 50/50, cutting the household's
  lifetime tax and reducing OAS-clawback exposure for the higher earner.
- **Why it is interesting (moderately complex):** It is a pre-65 income-splitting lever
  (versus pension splitting after 65), gated by the attribution timing and by the
  contributor's own RRSP room — a nice couple-level optimization.
- **Engine coverage — MODELED.** Spousal RRSP and the attribution timing are handled in
  `countries/canada/attribution.py`; the `spousal_rrsp` account kind is in the schema and
  `spousal_rrsp_balance` is tracked in `simulation_state.py`, so drawdown attributes
  spousal withdrawals correctly (tests `tests/test_attribution.py`).
