"""Tests for issue #306: TFSA overcontribution penalty accuracy and RESP specified plan rules.

P0 correctness concerns:
1. TFSA overcontribution penalty must NOT apply an RRSP-style $2,000 grace buffer.
   CRA does NOT give a $2,000 buffer for TFSA — the 1%/month penalty applies to
   ANY excess. (ITA s.207.06)
2. RESP specified plan rules: 31st/35th/40th anniversary limits on how long an RESP
   can stay open. These are not yet implemented but need at least documentation
   and test scaffolding.

This module locks in correct TFSA penalty behavior with regression tests and
adds scaffolding for RESP specified plan time limits.
"""
import unittest
from countries.canada.account_models import TSFAccount, RRSPAccount, RESPAccount


class TestTFSAOvercontributionPenalty(unittest.TestCase):
    """Regression tests for TFSA overcontribution penalty (ITA s.207.06).

    Critical: TFSA has NO grace amount. The 1%/month penalty applies to the
    FULL excess, not just the amount above a $2,000 buffer. This is different
    from RRSP (ITA s.204.1) which has a $2,000 grace.
    """

    def test_tfsa_penalty_on_any_excess(self):
        """TFSA penalty applies to any excess, even $1. No grace buffer."""
        tfsa = TSFAccount(contribution_room=0)
        # $1 excess should incur 1%/month penalty
        penalty = tfsa.overcontribution_penalty(excess_amount=1.0, months=1)
        self.assertAlmostEqual(penalty, 0.01)  # $1 * 1% * 1 month

    def test_tfsa_penalty_no_grace_buffer(self):
        """TFSA penalty has NO $2,000 grace buffer like RRSP.

        This is the critical difference: RRSP allows $2,000 excess before
        penalty kicks in, but TFSA does NOT.
        """
        tfsa = TSFAccount(contribution_room=0)
        rrsp = RRSPAccount(contribution_room=0)

        # $2,000 excess: RRSP should be $0 (within grace), TFSA should be $20
        tfsa_penalty = tfsa.overcontribution_penalty(excess_amount=2000.0, months=1)
        rrsp_penalty = rrsp.overcontribution_penalty(excess_amount=2000.0, months=1)

        # TFSA penalty applies to FULL $2,000
        self.assertAlmostEqual(tfsa_penalty, 20.0)  # $2,000 * 1% * 1 month

        # RRSP penalty is $0 because $2,000 is within the grace amount
        self.assertAlmostEqual(rrsp_penalty, 0.0)

        # TFSA penalty must be GREATER than RRSP penalty for any amount <= $2,000
        self.assertGreater(tfsa_penalty, rrsp_penalty,
                           "TFSA penalty must exceed RRSP penalty for amounts within the RRSP grace buffer")

    def test_tfsa_penalty_scales_with_months(self):
        """TFSA penalty of 1%/month accumulates over multiple months."""
        tfsa = TSFAccount(contribution_room=0)

        # $1,000 excess for 3 months
        penalty_3_months = tfsa.overcontribution_penalty(excess_amount=1000.0, months=3)
        self.assertAlmostEqual(penalty_3_months, 30.0)  # $1,000 * 1% * 3 months

        # Verify it scales linearly
        penalty_1_month = tfsa.overcontribution_penalty(excess_amount=1000.0, months=1)
        self.assertAlmostEqual(penalty_3_months, penalty_1_month * 3)

    def test_tfsa_penalty_zero_for_no_excess(self):
        """No penalty when there's no excess."""
        tfsa = TSFAccount(contribution_room=0)
        self.assertAlmostEqual(tfsa.overcontribution_penalty(excess_amount=0.0, months=1), 0.0)
        self.assertAlmostEqual(tfsa.overcontribution_penalty(excess_amount=-100.0, months=1), 0.0)

    def test_tfsa_penalty_large_excess(self):
        """TFSA penalty on a large excess (spouse with substantial contribution room)."""
        tfsa = TSFAccount(contribution_room=0)
        # If the spouse accidentally over-contributes $10,000
        penalty = tfsa.overcontribution_penalty(excess_amount=10000.0, months=1)
        self.assertAlmostEqual(penalty, 100.0)  # $10,000 * 1% * 1 month

    def test_tfsa_vs_rrsp_penalty_at_grace_boundary(self):
        """At exactly $2,000 excess, RRSP has $0 penalty but TFSA has $20."""
        tfsa = TSFAccount(contribution_room=0)
        rrsp = RRSPAccount(contribution_room=0)

        tfsa_penalty = tfsa.overcontribution_penalty(excess_amount=2000.0, months=1)
        rrsp_penalty = rrsp.overcontribution_penalty(excess_amount=2000.0, months=1)

        # RRSP: $2,000 - $2,000 grace = $0 penalty base → $0 penalty
        self.assertAlmostEqual(rrsp_penalty, 0.0)
        # TFSA: $2,000 full excess → $20 penalty
        self.assertAlmostEqual(tfsa_penalty, 20.0)

    def test_tfsa_penalty_above_rrsp_grace(self):
        """At $5,000 excess, RRSP penalty is on $3,000 (after $2,000 grace);
        TFSA penalty is on full $5,000."""
        tfsa = TSFAccount(contribution_room=0)
        rrsp = RRSPAccount(contribution_room=0)

        tfsa_penalty = tfsa.overcontribution_penalty(excess_amount=5000.0, months=1)
        rrsp_penalty = rrsp.overcontribution_penalty(excess_amount=5000.0, months=1)

        # TFSA: $5,000 * 1% = $50
        self.assertAlmostEqual(tfsa_penalty, 50.0)
        # RRSP: ($5,000 - $2,000) * 1% = $30
        self.assertAlmostEqual(rrsp_penalty, 30.0)

    def test_tfsa_penalty_rate_is_1_percent_monthly(self):
        """Verify the penalty rate is exactly 1% per month."""
        tfsa = TSFAccount(contribution_room=0)
        self.assertAlmostEqual(tfsa.PENALTY_RATE_MONTHLY, 0.01)

    def test_tfsa_no_grace_constant(self):
        """Verify TSFAccount has no OVERCONTRIBUTION_GRACE attribute or it's 0."""
        tfsa = TSFAccount(contribution_room=0)
        # TSFAccount should NOT have an OVERCONTRIBUTION_GRACE attribute
        # (or it should be 0 if inherited)
        if hasattr(tfsa, 'OVERCONTRIBUTION_GRACE'):
            self.assertEqual(tfsa.OVERCONTRIBUTION_GRACE, 0,
                             "TFSA must not have a grace amount for over-contribution penalty")


class TestRESPTimeLimits(unittest.TestCase):
    """Test RESP specified plan time limits (31st/35th/40th anniversary).

    Per ITA s.146.1:
    - Non-specified plan RESP: must terminate by end of the 35th year after opening
    - Specified plan RESP: can remain open until the end of the 40th year
      (for beneficiaries with severe and prolonged mental impairment)
    - Contributions must cease by the end of the 31st year after opening
      (for non-specified plans)

    These rules are not yet implemented in the codebase. These tests document
    the expected behavior for future implementation.
    """

    def test_resp_lifetime_contribution_limit(self):
        """RESP has a $50,000 lifetime contribution limit per beneficiary."""
        from countries.canada.resp_rules import RESPCalculator
        calc = RESPCalculator()
        self.assertEqual(calc.RESP_LIFETIME_CONTRIBUTION_LIMIT, 50000)

    def test_resp_specified_plan_max_eap(self):
        """Specified educational programs have a $4,000 EAP withdrawal limit."""
        from countries.canada.resp_rules import RESPCalculator
        calc = RESPCalculator()
        self.assertEqual(calc.EAP_SPECIFIED_PROGRAM_MAX, 4000)

    def test_resp_qualifying_program_max_eap(self):
        """Qualifying educational programs have an $8,000 EAP withdrawal limit."""
        from countries.canada.resp_rules import RESPCalculator
        calc = RESPCalculator()
        self.assertEqual(calc.EAP_QUALIFYING_PROGRAM_MAX, 8000)

    def test_resp_contribution_must_cease_by_31st_year(self):
        """RESP contributions must cease by end of 31st year after plan opening.

        This is a documented rule per ITA s.146.1(1)(b). The RESPCalculator
        should enforce this limit, but it is not yet implemented.
        """
        # This test documents the expected behavior once implemented
        # RESPCalculator does not yet track plan opening year or enforce
        # the 31-year contribution limit
        pass

    def test_resp_non_specified_plan_terminates_at_35th_year(self):
        """Non-specified RESP plans must terminate by end of 35th year.

        This is a documented rule per ITA s.146.1(1)(d). The RESPCalculator
        should enforce this limit, but it is not yet implemented.
        """
        # This test documents the expected behavior once implemented
        pass

    def test_resp_specified_plan_terminates_at_40th_year(self):
        """Specified RESP plans can remain open until end of 40th year.

        A 'specified plan' is one where the beneficiary has a severe and
        prolonged mental impairment. ITA s.146.1(1)(d).
        """
        # This test documents the expected behavior once implemented
        pass


if __name__ == '__main__':
    unittest.main()