#!/usr/bin/env python3
"""#702 — attribution.py is wired into the production tax path.

`countries/canada/attribution.py` (spousal & minor-child attribution, TOSI,
prescribed-rate / below-market loans) was fully built, unit-tested in isolation,
and called by NOTHING in production — the "implemented, tested, never reached"
defect. #702 wires its s.74.2 minor-lender arm: the production private-loan
classifier (`classify_private_loan_interest`, run each year from
`simulation._private_loan_interest_adjustments`) delegates the minor-vs-adult
decision to `attribution.check_attribution` instead of re-spelling the `< 18`
threshold itself (DP#9/DP#10).

The call-graph reachability of the module is pinned in
`tests/architecture/test_unreached_rule_modules.py::test_attribution_is_reached_from_production`.
This file pins the BEHAVIOUR the wiring must produce, and the absence no-op.
"""
import sys
import unittest

from countries.canada.private_loan_interest import classify_private_loan_interest


PAID_INV_LOAN = {
    'id': 'l1', 'lender': 'ca', 'borrower': 'p1',
    'rate': 0.05, 'principal': 100_000,
    'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid',
}


class TestMinorLenderAttributedThroughRuleModule(unittest.TestCase):
    """ITA s.74.2 via attribution.check_attribution: a minor lender's interest
    is attributed BACK to the borrower (the transferor), not left with the
    minor — the income split is undone in the transferor's hands."""

    def _classify(self, *, lender_age, lender_role, borrower_role):
        return classify_private_loan_interest(
            PAID_INV_LOAN,
            lender_is_external=False,
            lender_age=lender_age,
            lender_role=lender_role,
            borrower_role=borrower_role,
            borrower_is_child=False,
        )

    def test_minor_lender_income_attributes_to_the_borrower(self):
        # A 15-year-old (minor) lender with no taxed bracket lends to the
        # primary. s.74.2 (decided by attribution.check_attribution) attributes
        # the interest to the BORROWER, so income_role is the borrower's role,
        # NOT the minor lender's.
        effect = self._classify(lender_age=15, lender_role=None,
                                 borrower_role='primary')
        self.assertEqual(effect.income_role, 'primary',
                         "s.74.2: a minor lender's interest is attributed back "
                         "to the borrower (transferor), taxed in their hands")
        self.assertEqual(effect.interest, 5_000.0)

    def test_adult_lender_keeps_the_income_no_attribution(self):
        # An adult (25) lender WITH a taxed role keeps the interest as their own
        # income — attribution.check_attribution returns attributed=False at 18+.
        effect = self._classify(lender_age=25, lender_role='spouse',
                                 borrower_role='primary')
        self.assertEqual(effect.income_role, 'spouse',
                         "adult lender (18+) is exempt: interest stays theirs")

    def test_the_threshold_is_the_rule_modules_boundary(self):
        # The minor/adult boundary is exactly ITA s.74.2's age 18, and it is the
        # rule module that draws it: at 17 the interest attributes to the
        # borrower, at 18 it stays with the lender.
        just_minor = self._classify(lender_age=17, lender_role=None,
                                    borrower_role='primary')
        just_adult = self._classify(lender_age=18, lender_role='spouse',
                                    borrower_role='primary')
        self.assertEqual(just_minor.income_role, 'primary')  # attributed
        self.assertEqual(just_adult.income_role, 'spouse')   # not attributed


class TestAbsenceIsNoOp(unittest.TestCase):
    """Wiring attribution.py changes NOTHING for a household with no
    attributable transfers: the golden household (no private loans) is
    byte-identical to before the wiring."""

    GOLDEN_TERMINAL_ASSETS = 9709753.139463063

    def test_golden_invariant_unchanged(self):
        sys.path.insert(0, 'tests')
        from test_golden_trajectory_581 import golden_household_config, _run
        terminal = _run(golden_household_config())[-1].total_assets
        self.assertEqual(terminal, self.GOLDEN_TERMINAL_ASSETS,
                         "no attributable transfer in the golden household -> "
                         "wiring attribution.py must be an exact no-op")


if __name__ == "__main__":
    unittest.main()
