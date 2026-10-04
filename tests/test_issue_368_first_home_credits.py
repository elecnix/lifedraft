"""#368: first-home buyers' tax credits.

Until this, a declared first-home purchase moved only the FHSA qualifying
withdrawal and the Home Buyers' Plan -- **no tax credit was ever booked** -- so
every first-time buyer paid too much tax in the purchase year. For a Quebec
household in 2024 that is up to **$2,652.50** of overstated cash, and from
2026 a further up to **$5,875** of refundable relief.

Every figure below is checked against a primary source, not against the
issue:

* CRA line 31270 / ITA s.118.05(3) -- $10,000 from 2022 ($5,000 before),
  claimed at the year's lowest federal rate, **not splittable unless both
  spouses qualify**.
* TP-1 line 396 (TP-752.HA-V) -- Quebec $1,400, non-refundable, limited to
  Quebec tax otherwise payable.
* Ministère des Finances bulletin 2026-2 -- from 2026, refundable:
  100% of the first $5,000 of transfer duties plus 25% of the next $3,500
  (max $5,875), reduced by 2.35% of the basis above $750,000.

NOTE: these tests were written during a hold on executing test commands and
have NOT yet been run. The first thing to do on resuming is run them.
"""
import pytest

from countries.canada.first_home_credits import (
    federal_home_buyers_amount,
    home_buyers_amount_for_year,
    is_first_home_buyer,
    lowest_federal_rate,
    quebec_home_buyers_credit,
    quebec_homeownership_refundable_credit,
)

QC_ABATEMENT = 0.165


# ── The federal RATE is read from year-versioned data, never hardcoded ───────

def test_the_lowest_federal_rate_is_read_not_assumed():
    """It moves: 15% (2023/2024), 14.5% blended (2025), 14% (2026).

    A literal here would be a plausible wrong number the year it changed --
    the exact failure this repo is built to prevent.
    """
    assert lowest_federal_rate(2024) == pytest.approx(0.15, abs=1e-9)
    assert lowest_federal_rate(2025) == pytest.approx(0.145, abs=1e-9)
    assert lowest_federal_rate(2026) == pytest.approx(0.14, abs=1e-9)


def test_the_american_dollar_amount_is_data():
    """$10,000 from 2022; $5,000 before (CRA 'Amounts for prior years')."""
    for year in (2023, 2024, 2025, 2026):
        assert home_buyers_amount_for_year(year) == 10_000
    assert home_buyers_amount_for_year(2021) == 5_000


# ── Federal credit, by province ──────────────────────────────────────────────

@pytest.mark.parametrize("year,rate", [(2024, 0.15), (2025, 0.145), (2026, 0.14)])
def test_a_non_quebec_buyer_gets_the_full_face_credit(year, rate):
    """Ontario: 10,000 x the year's lowest rate, unabated."""
    assert federal_home_buyers_amount(year, province="ontario") == pytest.approx(
        10_000 * rate, abs=0.01
    )


def test_the_issue_figures_reproduce_exactly():
    """The issue's stated numbers, to the cent."""
    # 2024 Quebec: 10,000 x 15% x (1 - 0.165) = 1,252.50
    assert federal_home_buyers_amount(2024, province="quebec") == pytest.approx(
        1_252.50, abs=0.01
    )
    # 2025 Quebec: 10,000 x 14.5% x 0.835 = 1,210.75
    assert federal_home_buyers_amount(2025, province="quebec") == pytest.approx(
        1_210.75, abs=0.01
    )
    # 2026 Quebec: 10,000 x 14% x 0.835 = 1,169.00
    assert federal_home_buyers_amount(2026, province="quebec") == pytest.approx(
        1_169.00, abs=0.01
    )
    # 2026 Ontario: the full $1,400
    assert federal_home_buyers_amount(2026, province="ontario") == pytest.approx(
        1_400.00, abs=0.01
    )


def test_the_quebec_reduction_is_the_abatement_not_a_guess():
    """The credit is worth rate x (1 - 0.165), per ITA s.120(4).

    The CRA computes the refundable Quebec abatement on line 42900 -- *basic*
    federal tax, i.e. AFTER the federal credits. So a federal credit shrinks
    the abatement by 16.5% of itself.
    """
    full = federal_home_buyers_amount(2024, province="ontario")
    quebec = federal_home_buyers_amount(2024, province="quebec")
    assert quebec == pytest.approx(full * (1 - QC_ABATEMENT), abs=0.01)


def test_a_split_claim_never_exceeds_the_year_maximum():
    """A claimant cannot claim more than the year's amount."""
    from countries.canada.first_home_credits import home_buyers_amount_for_year as _max
    overshoot = federal_home_buyers_amount(
        2024, province="ontario", claimed_amount=_max(2024) + 5_000)
    assert overshoot == pytest.approx(federal_home_buyers_amount(
        2024, province="ontario"), abs=0.01)


def test_a_zero_claim_is_zero():
    assert federal_home_buyers_amount(2024, province="ontario", claimed_amount=0) == 0.0


# ── Quebec line 396, non-refundable ─────────────────────────────────────────

def test_the_quebec_credit_is_its_1400_maximum():
    assert quebec_home_buyers_credit(2024, quebec_tax_payable=50_000) == pytest.approx(
        1_400.00, abs=0.01)


def test_the_quebec_credit_is_capped_by_tax_otherwise_payable():
    """Non-refundable: a $500 Quebec liability yields $500 of credit."""
    assert quebec_home_buyers_credit(2024, quebec_tax_payable=500) == pytest.approx(
        500.00, abs=0.01)
    assert quebec_home_buyers_credit(2024, quebec_tax_payable=0.0) == 0.0


def test_the_quebec_credit_can_be_split_among_eligible_claimants():
    """Split three ways, the total still cannot exceed the maximum."""
    shares = [1_400.0 / 3, 1_400.0 / 3, 1_400.0 / 3]
    total = sum(quebec_home_buyers_credit(2024, quebec_tax_payable=50_000,
                                         claimed_amount=s)
                for s in shares)
    assert total == pytest.approx(1_400.00, abs=0.01)


# ── Quebec refundable credit, 2026+ (bulletin 2026-2) ───────────────────────

def test_the_refundable_credit_is_nil_before_2026():
    """The program does not exist for 2025, so zero is a GENUINE zero."""
    assert quebec_homeownership_refundable_credit(
        2025, transfer_duties=5_000, duty_basis=500_000) == 0.0


def test_the_issue_acceptance_case_at_500000_duties():
    """$5,000 of duties: the whole 100% band, no 25% tail."""
    credit = quebec_homeownership_refundable_credit(
        2026, transfer_duties=5_000, duty_basis=500_000)
    assert credit == pytest.approx(5_000.00, abs=0.01)


def test_the_partial_band_adds_25_percent():
    """$8,500 of duties: 5,000 at 100% + 3,500 at 25% = 5,875 -- the max."""
    credit = quebec_homeownership_refundable_credit(
        2026, transfer_duties=8_500, duty_basis=500_000)
    assert credit == pytest.approx(5_875.00, abs=0.01)


def test_the_partial_band_is_capped_at_3500():
    """Duties beyond $8,500 add nothing -- the credit maximum is $5,875."""
    for duties in (20_000, 100_000):
        assert quebec_homeownership_refundable_credit(
            2026, transfer_duties=duties, duty_basis=500_000) == pytest.approx(
            5_875.00, abs=0.01)


def test_the_credit_never_exceeds_the_duties_paid():
    """Cannot refund more transfer duty than was actually paid."""
    assert quebec_homeownership_refundable_credit(
        2026, transfer_duties=1_000, duty_basis=500_000) == pytest.approx(
            1_000.00, abs=0.01)


@pytest.mark.parametrize("basis,expected", [
    (750_000, 5_875.00),          # at the threshold: no reduction yet
    (800_000, 5_639.50),          # 50,000 x 2.35% = 1,175 off
    (1_000_000, 0.00),            # the issue's explicit nil point
    (1_200_000, 0.00),            # never negative
])
def test_the_reduction_is_2_35_percent_of_the_basis_above_750000(basis, expected):
    credit = quebec_homeownership_refundable_credit(
        2026, transfer_duties=8_500, duty_basis=basis)
    assert credit == pytest.approx(expected, abs=0.01)


def test_the_refundable_credit_is_paid_even_with_zero_quebec_tax():
    """It is REFUNDABLE, so unlike line 396 it does not need tax owing."""
    # The function takes no tax at all, which is the point: nothing bounds it
    # by a liability.
    assert quebec_homeownership_refundable_credit(
        2026, transfer_duties=5_000, duty_basis=500_000) > 0


# ── Eligibility: the spouse test the FHSA predicate lacks ───────────────────

def test_an_undeclared_history_qualifies():
    """Absent is not evidence against; the household is asserting first-time."""
    assert is_first_home_buyer(1990, 2026) is True


def test_a_prior_home_in_the_four_year_window_disqualifies():
    """2026 acquisition: 2025, 2024, 2023, 2022 and 2026 are all in scope."""
    for prior in (2026, 2025, 2024, 2023, 2022):
        assert is_first_home_buyer(1990, 2026, prior_home_years={prior}) is False, prior


def test_a_prior_home_outside_the_window_does_not_disqualify():
    """The window is four PRECEDING years plus the year of acquisition."""
    assert is_first_home_buyer(1990, 2026, prior_home_years={2021}) is True
    assert is_first_home_buyer(1990, 2026, prior_home_years={1990}) is True