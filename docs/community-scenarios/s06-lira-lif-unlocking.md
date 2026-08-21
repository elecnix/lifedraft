# S06 — LIRA / LIF 50% unlocking

- **Community source:** Financial Wisdom Forum threads "LIRA to LIF and unlocking"
  <https://www.financialwisdomforum.org/forum/viewtopic.php?t=127168> and "Ontario LIRA
  50% Unlocking Restrictions"
  <https://www.financialwisdomforum.org/forum/viewtopic.php?t=121767>.
- **Program / maneuver:** Provincial/federal one-time 50% unlocking of a locked-in
  retirement account. In Ontario, convert LIRA to a LIF, then within 60 days file
  Form 5.2 to move up to 50% to an RRSP/RRIF (no LIF max-withdrawal cap).
- **Situation:** A retiree with a locked-in pension transfer converts the LIRA to a
  LIF and unlocks 50% into an RRSP for withdrawal flexibility. Small balances (below
  the 40%-of-YMPE small-balance rule) can be fully unlocked in two steps.
- **Why it is interesting (complex, jurisdiction-specific):** Rules differ by pension
  jurisdiction (federal vs each province), the 60-day form window, LIF min/max bands,
  and Quebec's distinct regime — a rich rules-engine problem.
- **Engine coverage — MODELED.** `countries/canada/locked_in_account.py` models LIRA/LIF
  and unlocking; Quebec's variant is in
  `countries/canada/provinces/quebec/quebec_lif.py` with tests
  `tests/test_locked_in_account.py` and `tests/test_quebec_lif.py`.
