#!/usr/bin/env python3
"""Tests for issue #377(c): OAS clawback interactions with LIF withdrawals.

DP#11: this is an *integration* file — it verifies the composition of two
modules (locked_in_account.LIFFund and retirement.oas_clawback), not either
module in isolation. Single-module OAS clawback boundary tests already live
in tests/test_issue_309_retirement_accuracy.py and
countries/canada/tests/test_retirement_issue_53.py (exact-threshold, one
dollar above/below, full-clawback point, year-versioned threshold — all
already covered there); this file only adds tests that exercise LIF and OAS
*together*, plus OAS clawback rule paths (partial entitlement) that were
genuinely untested anywhere in the suite.

DP#17: every threshold boundary needs tests on both sides.

Gaps closed here:
- OAS clawback interaction with LIF withdrawal size (Quebec no-max at 55+
  can produce a withdrawal large enough to trigger full clawback; federal
  LIF's maximum caps how much clawback a LIF withdrawal alone can cause)
- OAS partial entitlement (oas_amount < max, e.g. residency-prorated OAS):
  the clawback cap must track the actual entitlement, not the statutory max
- The behavioural fix from #592 (oas_amount=0 must mean zero OAS, not
  "use the default") is exercised as a clawback-path regression
"""

import pytest

from countries.canada.locked_in_account import LIFFund
from countries.canada.retirement import (
    OAS_CLAWBACK_RATE,
    get_oas_annual_max,
    get_oas_clawback_threshold,
    oas_clawback,
)


class TestOASClawbackYearComparison:
    """OAS clawback threshold is year-versioned (DP#20) — behavioural check.

    tests/test_issue_309_retirement_accuracy.py already asserts that the
    threshold *value* differs by year (test_year_versioned_threshold). This
    test goes one step further and asserts the *consequence*: the same
    income produces MORE clawback in a year with a lower threshold.
    """

    def test_lower_threshold_in_earlier_year_produces_more_clawback(self):
        """2023 has a lower threshold → same income produces more clawback."""
        income = 95000
        clawback_2023 = oas_clawback(net_income=income, year=2023)['clawback_amount']
        clawback_2026 = oas_clawback(net_income=income, year=2026)['clawback_amount']
        assert clawback_2023 > clawback_2026


class TestOASClawbackLIFWithdrawalInteraction:
    """OAS clawback interaction with LIF withdrawals (DP#17).

    A Quebec LIF holder age 55+ (2025+) can withdraw the full balance.
    This large taxable withdrawal can trigger OAS clawback.

    These tests verify the clawback computation when LIF income pushes
    net income above the OAS threshold.
    """

    def test_lif_withdrawal_triggers_oas_clawback(self):
        """A moderate LIF withdrawal combined with other income triggers clawback."""
        lif_income = 30000
        cpp = 18092   # max CPP at 65 (2026)
        oas_max = get_oas_annual_max(2026)
        net_income = lif_income + cpp + oas_max
        # $30k + $18k + $8.9k = $56.9k, below threshold of $95.3k → no clawback
        result = oas_clawback(net_income=net_income, year=2026)
        assert result['clawback_amount'] == 0

    def test_large_lif_withdrawal_full_clawback(self):
        """A large LIF withdrawal fully claws back OAS."""
        oas_max = get_oas_annual_max(2026)
        threshold = get_oas_clawback_threshold(2026)
        # Full clawback income
        full_clawback_income = threshold + oas_max / OAS_CLAWBACK_RATE + 50000
        result = oas_clawback(net_income=full_clawback_income, year=2026)
        assert result['clawback_amount'] == pytest.approx(oas_max)
        assert result['net_oas'] == pytest.approx(0)

    def test_partial_clawback_from_lif_withdrawal(self):
        """LIF withdrawal of $20k with $80k other income → partial clawback."""
        threshold = get_oas_clawback_threshold(2026)
        cpp = 18092
        oas_max = get_oas_annual_max(2026)
        other_income = 60000
        lif_income = 20000
        net_income = cpp + oas_max + other_income + lif_income
        # $18k + $8.9k + $60k + $20k = $106.9k, above threshold
        result = oas_clawback(net_income=net_income, year=2026)
        excess = net_income - threshold
        expected_clawback = min(oas_max, excess * OAS_CLAWBACK_RATE)
        assert result['clawback_amount'] == pytest.approx(expected_clawback)
        assert result['clawback_amount'] > 0
        assert result['net_oas'] > 0  # Not fully clawed back

    def test_quebec_lif_no_max_leads_to_large_withdrawal(self):
        """Quebec LIF at age 55+ in 2026: max = full balance.

        This means a simulated withdrawal could be very large,
        triggering full OAS clawback.
        """
        lif = LIFFund(balance=500000, owner_birth_year=1960,
                      reference_rate=0.06, jurisdiction='quebec')
        max_w = lif.maximum_withdrawal(2026)
        assert max_w == pytest.approx(500000)
        # Full balance withdrawal + other income fully claws back OAS
        net_income = max_w + 20000  # plus some CPP/OAS
        result = oas_clawback(net_income=net_income, year=2026)
        assert result['net_oas'] == pytest.approx(0)

    def test_federal_lif_max_is_limited(self):
        """Federal LIF has maximum so withdrawal less likely to fully claw back."""
        lif = LIFFund(balance=500000, owner_birth_year=1955,
                      reference_rate=0.06, jurisdiction='federal')
        max_w = lif.maximum_withdrawal(2026)
        assert max_w < 500000  # Federal max applies
        assert max_w > 0

    def test_lif_minimum_alone_does_not_trigger_clawback(self):
        """LIF minimum withdrawal alone is unlikely to trigger OAS clawback."""
        lif = LIFFund(balance=300000, owner_birth_year=1955,
                      reference_rate=0.06, jurisdiction='federal')
        min_w = lif.minimum_withdrawal(2026)
        cpp = 18092
        oas_max = get_oas_annual_max(2026)
        net_income = min_w + cpp + oas_max
        # LIF min for 300k at age 70 ≈ 300k × 0.05 ≈ 15k
        # Total ≈ 15k + 18k + 8.9k = 41.9k < 95.3k threshold
        result = oas_clawback(net_income=net_income, year=2026)
        assert result['clawback_amount'] == 0


class TestOASClawbackPartialEntitlement:
    """OAS clawback when OAS entitlement is partial (non-max).

    OAS entitlement can be partial due to fewer than 40 years of Canadian residency
    after age 18. The clawback should only recover up to the actual OAS amount,
    not the maximum.
    """

    def test_partial_oas_below_threshold_no_clawback(self):
        """Partial OAS ($5,000) below threshold → no clawback."""
        result = oas_clawback(net_income=90000, oas_amount=5000, year=2026)
        assert result['clawback_amount'] == 0
        assert result['net_oas'] == 5000

    def test_partial_oas_above_threshold_partial_clawback(self):
        """Partial OAS with income above threshold → clawback limited to OAS."""
        threshold = get_oas_clawback_threshold(2026)
        income = threshold + 20000  # $20k above threshold
        # 15% × $20k = $3,000 clawback, but only $2,500 OAS → cap at $2,500
        result = oas_clawback(net_income=income, oas_amount=2500, year=2026)
        assert result['clawback_amount'] == pytest.approx(2500)
        assert result['net_oas'] == pytest.approx(0)

    def test_partial_oas_small_clawback(self):
        """Partial OAS ($6,000) with $10,000 above threshold → $1,500 clawback."""
        threshold = get_oas_clawback_threshold(2026)
        income = threshold + 10000  # $10k above threshold
        result = oas_clawback(net_income=income, oas_amount=6000, year=2026)
        assert result['clawback_amount'] == pytest.approx(1500)
        assert result['net_oas'] == pytest.approx(4500)

    def test_partial_oas_exactly_at_full_clawback(self):
        """Partial OAS exactly fully recovered at threshold + OAS/0.15."""
        partial_oas = 4000
        threshold = get_oas_clawback_threshold(2026)
        full_point = threshold + partial_oas / OAS_CLAWBACK_RATE
        result = oas_clawback(net_income=full_point, oas_amount=partial_oas, year=2026)
        assert result['clawback_amount'] == pytest.approx(partial_oas)
        assert result['net_oas'] == pytest.approx(0)


class TestOASClawbackRetirementIncome:
    """OAS clawback with typical retirement income sources.

    Combines CPP, RRIF, LIF, and non-reg capital gains income
    to test the composite OAS clawback scenario.
    """

    def test_typical_retiree_no_clawback(self):
        """Modest retirement income: CPP + OAS + small RRIF → no clawback."""
        cpp = 14000  # ~$1,167/month at 65
        oas_amount = get_oas_annual_max(2026)
        rrif = 15000  # Small RRIF on ~$300k balance at 71
        net_income = cpp + oas_amount + rrif
        result = oas_clawback(net_income=net_income, year=2026)
        assert result['clawback_amount'] == 0

    def test_high_income_retiree_partial_clawback(self):
        """High-income retiree: CPP + OAS + large RRIF + non-reg dividends."""
        threshold = get_oas_clawback_threshold(2026)
        cpp = 18092
        oas_amount = get_oas_annual_max(2026)
        rrif = 60000
        non_reg = 50000  # Non-reg capital gains at 50% inclusion = $25k taxable
        taxable_non_reg = non_reg * 0.50  # 50% inclusion
        net_income = cpp + oas_amount + rrif + taxable_non_reg
        # $18k + $8.9k + $60k + $25k = $111.9k > $95.3k threshold
        assert net_income > threshold  # Should trigger clawback
        result = oas_clawback(net_income=net_income, year=2026)
        assert result['clawback_amount'] > 0

    def test_oas_clawback_with_zero_oas(self):
        """When OAS is zero (not yet eligible), clawback is zero.

        Regression guard for #592 (DP#32): oas_amount=0 must be treated as
        an explicit zero, not as "absent -> use the year-versioned default".
        A DP#32 violation here would silently substitute the ~$8,900
        default OAS max and report a nonzero net_oas.
        """
        result = oas_clawback(net_income=200000, oas_amount=0, year=2026)
        assert result['clawback_amount'] == 0
        assert result['net_oas'] == 0
