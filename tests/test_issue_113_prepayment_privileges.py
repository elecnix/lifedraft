#!/usr/bin/env python3
"""Issue #113: prepayment privileges and the excess-prepayment penalty.

A closed-term Canadian mortgage grants two penalty-free privilege
mechanisms per tranche — an annual lump-sum allowance (a percent of
ORIGINAL principal) and a per-payment increase allowance (a multiplier on
the regular payment) — and charges ``penalty_months_interest`` months of
interest at the lender's STANDARD VARIABLE rate on any overshoot.

Every figure below is a fabricated round number (a $500,000 tranche,
10% = $50,000/year free, a $60,000/year prepayment stream whose overshoot
is penalized): no real borrower, no real amounts (DP#15).

The unit tests drive the pure functions; the schedule tests drive
``rate_model.amortization_schedule`` — the ONE place the extra-payment
machinery lives — never a reimplementation of its loop (DP#11).
"""
import pytest

from countries.canada.prepayment_privileges import (
    PrepaymentPrivileges,
    excess_penalty,
    price_monthly_extra,
    split_extra_payment,
)
from countries.canada.rate_model import (
    amortization_schedule,
    annual_summary,
    build_rate_path,
    monthly_payment,
)


def _priv(original_principal=500_000.0, pct=0.10, multiplier=1.0,
          months=3, standard_rate=0.06):
    """Fabricated round-number privileges on a $500k tranche."""
    return PrepaymentPrivileges(
        original_principal=original_principal,
        annual_lump_sum_pct=pct,
        payment_increase_multiplier=multiplier,
        penalty_months_interest=months,
        standard_variable_rate=standard_rate,
    )


def _flat_path(rate=0.05):
    return build_rate_path("Flat", rate, 10, 'variable', [rate])


# =============================================================================
# Declared terms validate loudly (DP#32)
# =============================================================================

class TestPrepaymentPrivilegesValidation:

    def test_zero_principal_refused(self):
        with pytest.raises(ValueError, match="original_principal"):
            _priv(original_principal=0)

    def test_pct_above_one_refused(self):
        with pytest.raises(ValueError, match="annual_lump_sum_pct"):
            _priv(pct=1.5)

    def test_negative_multiplier_refused(self):
        with pytest.raises(ValueError, match="payment_increase_multiplier"):
            _priv(multiplier=-1.0)

    def test_negative_months_refused(self):
        with pytest.raises(ValueError, match="penalty_months_interest"):
            _priv(months=-3)

    def test_standard_variable_rate_required_not_derived(self):
        # A missing standard variable rate must be DECLARED: defaulting it
        # to the discounted contract rate would understate every penalty.
        priv = _priv(standard_rate=0.0)
        # A declared 0% standard rate is a real (penalty-free) term...
        assert excess_penalty(priv, 10_000.0) == 0.0
        # ...but a negative one is nonsense.
        with pytest.raises(ValueError, match="standard_variable_rate"):
            _priv(standard_rate=-0.06)

    def test_annual_allowance_is_pct_of_original_principal(self):
        assert _priv().annual_lump_sum_allowance == pytest.approx(50_000.0)


# =============================================================================
# The free/penalized split
# =============================================================================

class TestSplitExtraPayment:

    def test_negative_requested_extra_refuses_loudly(self):
        # DP#32: a prepayment is money paid TO the lender; a negative one is
        # nonsense and must refuse rather than be silently treated as zero.
        with pytest.raises(ValueError, match="requested_extra cannot be negative"):
            split_extra_payment(_priv(), -1.0, 3_000.0, 0.0)

    def test_within_annual_allowance_all_free(self):
        # $30k of a $50k allowance: entirely free.
        split = split_extra_payment(_priv(), 30_000.0, 3_000.0, 0.0)
        assert split == {'lump_sum': 30_000.0, 'payment_increase': 0.0,
                         'excess': 0.0}

    def test_excess_over_annual_allowance_penalized(self):
        # $60k requested against a $50k allowance -> the increase privilege
        # absorbs one regular payment's worth ($3k), the $7k remainder is
        # the penalized overshoot (the two privileges stack).
        split = split_extra_payment(_priv(), 60_000.0, 3_000.0, 0.0)
        assert split['lump_sum'] == pytest.approx(50_000.0)
        assert split['payment_increase'] == pytest.approx(3_000.0)
        assert split['excess'] == pytest.approx(7_000.0)
        # With no increase privilege declared, the full $10k overshoot is
        # penalized.
        split = split_extra_payment(_priv(multiplier=0.0),
                                    60_000.0, 3_000.0, 0.0)
        assert split['excess'] == pytest.approx(10_000.0)

    def test_used_allowance_shifts_room_to_the_increase_privilege(self):
        # $48k already prepaid this year: $2k lump room left. A $5k extra
        # rides the remaining lump room, then the 1x payment-increase
        # privilege ($3k regular payment), leaving NOTHING penalized.
        split = split_extra_payment(_priv(), 5_000.0, 3_000.0, 48_000.0)
        assert split['lump_sum'] == pytest.approx(2_000.0)
        assert split['payment_increase'] == pytest.approx(3_000.0)
        assert split['excess'] == 0.0

    def test_over_both_privileges_penalized_on_the_overshoot_only(self):
        # Same $48k used; a $7k extra exhausts both buckets -> $2k excess.
        split = split_extra_payment(_priv(), 7_000.0, 3_000.0, 48_000.0)
        assert split['lump_sum'] == pytest.approx(2_000.0)
        assert split['payment_increase'] == pytest.approx(3_000.0)
        assert split['excess'] == pytest.approx(2_000.0)

    def test_no_increase_privilege_means_full_penalty_sooner(self):
        # multiplier 0: after the $2k lump room, everything is excess.
        split = split_extra_payment(_priv(multiplier=0.0),
                                    5_000.0, 3_000.0, 48_000.0)
        assert split['payment_increase'] == 0.0
        assert split['excess'] == pytest.approx(3_000.0)

    def test_overuse_floors_remaining_allowance_at_zero(self):
        # $55k used against $50k: no negative lump room is invented; the
        # extra rides the increase privilege alone.
        split = split_extra_payment(_priv(), 2_000.0, 3_000.0, 55_000.0)
        assert split['lump_sum'] == 0.0
        assert split['payment_increase'] == pytest.approx(2_000.0)
        assert split['excess'] == 0.0


class TestPenaltyPricedAtStandardVariableRate:

    def test_three_months_interest_on_the_overshoot(self):
        # $10k overshoot at a 6% standard variable rate:
        # 10_000 * 0.06 * 3/12 = $150.
        assert excess_penalty(_priv(), 10_000.0) == pytest.approx(150.0)

    def test_not_the_borrowers_contract_rate(self):
        # The contract rate (4.8%) is NOT what prices the charge: the
        # standard variable rate (6%) is — the whole point of issue #113.
        # At the contract rate the same overshoot would cost $120.
        assert excess_penalty(_priv(), 10_000.0) > 10_000.0 * 0.048 * 0.25

    def test_declared_zero_penalty_months_is_a_real_zero(self):
        assert excess_penalty(_priv(months=0), 10_000.0) == 0.0


class TestPriceMonthlyExtra:

    def test_partial_prepayment_never_free_and_penalized_mix_reported(self):
        priced = price_monthly_extra(_priv(), 6_000.0, 3_000.0, 48_000.0,
                                     closes_loan=False)
        assert priced['free'] == pytest.approx(5_000.0)   # 2k lump + 3k incr
        assert priced['excess'] == pytest.approx(1_000.0)
        assert priced['penalty'] == pytest.approx(1_000.0 * 0.06 * 0.25)

    def test_full_repayment_gets_no_privilege_protection(self):
        # Repaying the tranche ENTIRELY: the whole amount is penalized even
        # though the annual allowance had room left (clause 1: the lump-sum
        # privilege "does not apply when repaying the tranche in full").
        priced = price_monthly_extra(_priv(), 20_000.0, 3_000.0, 0.0,
                                     closes_loan=True)
        assert priced['free'] == 0.0
        assert priced['excess'] == pytest.approx(20_000.0)
        assert priced['penalty'] == pytest.approx(20_000.0 * 0.06 * 0.25)


# =============================================================================
# The schedule applies the split month by month
# =============================================================================

class TestAmortizationScheduleWithPrivileges:

    def test_excess_charged_in_month_it_occurs(self):
        # $500k, 5%, $5,000/month extra: the $50k annual allowance is
        # exhausted 10 months in; month 11's extra rides the 1x increase
        # privilege (~$2,922 for a $2,922 regular payment); month 12's
        # exceeds BOTH and pays the penalty.
        priv = _priv()
        sched = amortization_schedule(
            500_000.0, _flat_path(0.05), amortization_years=25,
            projection_months=24, extra_payment=5_000.0,
            prepayment_privileges=priv)
        year1 = [m for m in sched if m['year'] == 1]
        assert len(year1) == 12
        free_running = 0.0
        for m in year1[:10]:
            # First ten months: fully inside the lump-sum allowance.
            assert m['prepayment_free'] == pytest.approx(5_000.0)
            assert m['prepayment_penalty'] == 0.0
            free_running += 5_000.0
        assert free_running == pytest.approx(50_000.0)
        # Month 11: allowance gone; the 1x-increase privilege absorbs the
        # regular payment's worth; the remainder is penalized.
        m11 = year1[10]
        regular = monthly_payment(500_000.0, 0.05, 25)
        assert m11['prepayment_free'] == pytest.approx(regular)
        expected_penalty = (5_000.0 - regular) * 0.06 * 3 / 12
        assert m11['prepayment_penalty'] == pytest.approx(expected_penalty)
        # Month 12: still past both buckets -> same penalty shape.
        m12 = year1[11]
        assert m12['prepayment_excess'] == pytest.approx(
            5_000.0 - regular)
        assert m12['prepayment_penalty'] == pytest.approx(
            (5_000.0 - regular) * 0.06 * 3 / 12)

    def test_allowance_resets_each_schedule_year(self):
        priv = _priv()
        sched = amortization_schedule(
            500_000.0, _flat_path(0.05), amortization_years=25,
            projection_months=16, extra_payment=5_000.0,
            prepayment_privileges=priv)
        year2_start = [m for m in sched if m['year'] == 2][:10]
        for m in year2_start:
            # Fresh $50k: ten more fully-free months.
            assert m['prepayment_free'] == pytest.approx(5_000.0)
            assert m['prepayment_penalty'] == 0.0

    def test_extra_principal_still_reduces_balance_in_full(self):
        # Privileges price the overshoot; they do NOT claw back principal:
        # the balance follows the FULL extra either way.
        priv = _priv()
        priced = amortization_schedule(
            500_000.0, _flat_path(0.05), amortization_years=25,
            projection_months=12, extra_payment=5_000.0,
            prepayment_privileges=priv)
        unpriced = amortization_schedule(
            500_000.0, _flat_path(0.05), amortization_years=25,
            projection_months=12, extra_payment=5_000.0)
        for mp, mu in zip(priced, unpriced):
            assert mp['balance'] == pytest.approx(mu['balance'])

    def test_full_repayment_in_schedule_penalizes_whole_amount(self):
        # A $20k tranche with a $25k/month extra closes in month 1: every
        # dollar of that closing prepayment is penalized.
        priv = _priv(original_principal=20_000.0)
        sched = amortization_schedule(
            20_000.0, _flat_path(0.05), amortization_years=5,
            projection_months=6, extra_payment=25_000.0,
            prepayment_privileges=priv)
        first = sched[0]
        assert first['balance'] == pytest.approx(0.0)
        assert first['prepayment_free'] == 0.0
        closing_extra = first['principal'] - (
            first['payment'] - first['interest'])
        assert first['prepayment_excess'] == pytest.approx(closing_extra)
        assert first['prepayment_penalty'] == pytest.approx(
            closing_extra * 0.06 * 3 / 12)
        assert len(sched) == 1

    def test_money_conserved_outflow_matches_principal_and_penalty(self):
        # The year's household outflow (payments + extras + penalties)
        # covers the interest accrued plus the principal retired plus the
        # penalties paid — nothing conjured, nothing lost.
        priv = _priv()
        sched = amortization_schedule(
            500_000.0, _flat_path(0.05), amortization_years=25,
            projection_months=24, extra_payment=5_000.0,
            prepayment_privileges=priv)
        outflow = sum(m['payment'] + m['prepayment_extra']
                      + m['prepayment_penalty'] for m in sched)
        interest = sum(m['interest'] for m in sched)
        principal_retired = 500_000.0 - sched[-1]['balance']
        penalties = sum(m['prepayment_penalty'] for m in sched)
        assert outflow == pytest.approx(interest + principal_retired
                                        + penalties)

    def test_legacy_shape_without_privileges_unchanged(self):
        sched = amortization_schedule(
            200_000.0, _flat_path(0.05), amortization_years=25,
            projection_months=12, extra_payment=500.0)
        assert all('prepayment_penalty' not in m for m in sched)
        # And the no-extra schedule too.
        plain = amortization_schedule(
            200_000.0, _flat_path(0.05), amortization_years=25,
            projection_months=12)
        assert all('prepayment_extra' not in m for m in plain)


class TestAnnualSummaryAndMortgageData:

    def test_debt_service_includes_extra_and_penalty(self):
        from simulation import _mortgage_data_for
        priv = _priv()
        sched = amortization_schedule(
            500_000.0, _flat_path(0.05), amortization_years=25,
            projection_months=24, extra_payment=5_000.0,
            prepayment_privileges=priv)
        summary = annual_summary(sched)
        y1 = summary[0]
        expected_total = sum(
            m['payment'] + m['prepayment_extra'] + m['prepayment_penalty']
            for m in sched if m['year'] == 1)
        assert y1['total_payment'] == pytest.approx(expected_total)
        assert y1['total_principal'] == pytest.approx(sum(
            m['principal'] for m in sched if m['year'] == 1))
        expected_penalty = sum(
            m['prepayment_penalty'] for m in sched if m['year'] == 1)
        assert y1['prepayment_penalty'] == pytest.approx(expected_penalty)

        # The engine reads mortgage data through _mortgage_data_for: its
        # fallback aggregation must agree with annual_summary's fold (DP#9:
        # one spelling of the year's debt service).
        via_summary = _mortgage_data_for(
            0, amort_annual=summary, amort=sched)
        via_fallback = _mortgage_data_for(0, amort_annual=[], amort=sched)
        assert via_summary['total_payment'] == pytest.approx(expected_total)
        assert via_fallback['total_payment'] == pytest.approx(expected_total)
        assert via_fallback['prepayment_penalty'] == pytest.approx(
            expected_penalty)

    def test_legacy_annual_rows_byte_identical(self):
        sched = amortization_schedule(
            200_000.0, _flat_path(0.05), amortization_years=25,
            projection_months=12, extra_payment=500.0)
        summary = annual_summary(sched)
        # No prepayment keys anywhere — the pre-#113 row shape survives.
        assert all('prepayment_penalty' not in row for row in summary)
        assert all('prepayment_extra' not in row for row in summary)
        # And the fold is exactly payments (no phantom additions).
        assert summary[0]['total_payment'] == pytest.approx(
            sum(m['payment'] for m in sched))
