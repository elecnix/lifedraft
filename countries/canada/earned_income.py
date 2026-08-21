#!/usr/bin/env python3
"""
Earned income (ITA s.146(1)) — which income accrues RRSP contribution room.

Epic #795 bite 3 (DP#10/DP#25): this classification is Canadian tax law, so it
lives in the Canada jurisdiction module, not in the generic fold
(`simulation.py`). The fold's dated income-segment blending
(`_income_components_for_year`) stays generic — it derives a year's income from
stored `[from, to)` windows by day count, the way `age_in(year)` derives age
from a birth date (DP#1) — and simply routes each segment's ``kind`` through the
classifier here to decide whether it accrues RRSP room.

ITA s.146(1) defines "earned income" for RRSP contribution-room purposes.
Employment income and net self-employment income count; Employment Insurance
benefits do not (verified against the Income Tax Act's s.146(1) "earned income"
definition, https://laws-lois.justice.gc.ca/eng/acts/I-3.3/section-146.html --
a Supplementary Unemployment Benefit Plan top-up an EMPLOYER pays on top of EI
is earned income; the EI benefit itself, paid by Service Canada, is not).
Investment and rental income are not earned income either. An income override's
``kind`` decides which bucket an amount falls in; there is no default (DP#32) --
see $defs/income_kind's schema description and input_contract.py's mapping,
which requires it.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — RRSP earned income
    ITA s.146(1) "earned income" definition
"""

EARNED_INCOME_KINDS = frozenset({"employment", "self_employment"})

# The complement -- kinds this engine has consciously classified as NOT earned
# income. Together these two sets must PARTITION $defs/income_kind's enum, and
# tests/test_issue_674_income_shocks.py asserts exactly that against the schema.
#
# The partition is what makes the classification total, and totality is the
# point: `if kind in EARNED_INCOME_KINDS` alone silently answers "no" for a
# kind nobody classified -- a typo in a hand-built config, or (the real risk) a
# kind added to the schema enum later whose ITA s.146(1) treatment nobody
# stopped to decide. That silence understates RRSP room, which is the SAME
# defect #674 exists to fix, merely pointing the other way, and it is the
# "unknown key defaulting to a convenient value" trap listed in AGENTS.md.
# An unclassified kind must therefore raise (DP#32) -- never be assumed.
NON_EARNED_INCOME_KINDS = frozenset({"rental", "investment", "ei", "other"})


def is_earned_income(kind: str) -> bool:
    """ITA s.146(1): does an amount of this ``kind`` accrue RRSP room?

    Total by construction -- an unclassified kind raises rather than
    silently returning False (see NON_EARNED_INCOME_KINDS' comment).
    """
    if kind in EARNED_INCOME_KINDS:
        return True
    if kind in NON_EARNED_INCOME_KINDS:
        return False
    raise ValueError(
        f"income kind {kind!r} is not classified as earned income or not "
        f"(ITA s.146(1)). Known kinds: "
        f"{sorted(EARNED_INCOME_KINDS | NON_EARNED_INCOME_KINDS)}. A kind the "
        f"engine has not classified must not be silently treated as unearned "
        f"-- that understates RRSP contribution room (DP#32). Classify it in "
        f"countries/canada/earned_income.py's EARNED_INCOME_KINDS / "
        f"NON_EARNED_INCOME_KINDS."
    )
