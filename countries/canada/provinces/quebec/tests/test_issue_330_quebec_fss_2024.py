"""#330: the Quebec Health Services Fund (Schedule F) figures for 2024.

A Quebec resident owes an individual contribution to the FSS (TP-1 line 446,
Schedule F) on income OTHER THAN employment income -- so nearly every retiree
above the exemption pays it every year of retirement.

``quebec_health_services_fund_individual`` reads its five parameters from the
Quebec ``TaxYearData`` record. For 2024 the record set NONE of them, so every
field defaulted to 0 and the function returned ``0.0`` for every base -- a
loud absence that read as "nobody owes this". 2025 and 2026 were populated
and always returned the right figure.

Measured on `main` at c949015:

    year 2023: 46000 -> 0.0     (no data)
    year 2024: 46000 -> 0.0     (no data)   <- the bug
    year 2024: 26163 -> 0.0     (no data)   <- the bug
    year 2025: 46000 -> 150.0   (correct)
    year 2026: 46000 -> 150.0   (correct)

The 2024 figures are sourced from Revenu Quebec's own Schedule F for 2024
(TP-1.D.F-V(2024-12)): exemption $17,630, first cap $150, second threshold
$61,315, maximum $1,000, rate 1%.
"""
import pytest

from countries.canada.provinces.quebec import quebec_health_services_fund_individual

EXEMPTION_2024 = 17630.0
FIRST_CAP_2024 = 150.0
SECOND_THRESHOLD_2024 = 61315.0
MAX_2024 = 1000.0
RATE_2024 = 0.01


def test_a_retiree_sized_base_hits_the_first_cap():
    """The issue's first acceptance case: $46,000 owes the $150 cap.

    (46,000 - 17,630) x 1% = $283.70, capped at $150.
    """
    assert quebec_health_services_fund_individual(46000, year=2024) == 150.0


def test_the_cffp_condo_case_reproduces_exactly():
    """The issue's second acceptance case: base $26,163 owes $85.33.

    Cross-checked against the CFFP (Universite de Sherbrooke) worked case
    "Deménagement dans un immeuble détenu en copropriété", which adds
    "Cotisation au FSS 85 $" to the Quebec balance.
    """
    assert quebec_health_services_fund_individual(26163, year=2024) == pytest.approx(
        85.33, abs=0.01
    )


def test_below_the_exemption_is_nothing():
    """Nothing is owed on a base at or under the exemption."""
    assert quebec_health_services_fund_individual(0, year=2024) == 0.0
    assert quebec_health_services_fund_individual(EXEMPTION_2024, year=2024) == 0.0


def test_the_first_band_is_the_rate_until_the_cap_binds():
    """$150 is the MAXIMUM of the first band, not its floor.

    The cap only starts binding once (base - 17,630) x 1% reaches $150, i.e.
    at a base of $32,630. Below that the contribution is the rate times the
    excess, so $20,000 owes $23.70 and not $150.
    """
    cap_binds_at = EXEMPTION_2024 + FIRST_CAP_2024 / RATE_2024  # 32,630
    assert cap_binds_at == pytest.approx(32630.0)

    # Below the cap: rate x excess.
    for base in (20000.0, 30000.0):
        assert quebec_health_services_fund_individual(
            base, year=2024) == pytest.approx(
            (base - EXEMPTION_2024) * RATE_2024, abs=0.01), base

    # From there to the second threshold: exactly the cap.
    for base in (33000.0, 46000.0, SECOND_THRESHOLD_2024):
        assert quebec_health_services_fund_individual(
            base, year=2024) == FIRST_CAP_2024, base


def test_the_second_band_is_the_cap_plus_the_excess():
    """Above $61,315 it is $150 plus 1% of the excess, capped at $1,000."""
    base = 100000.0
    expected = FIRST_CAP_2024 + (base - SECOND_THRESHOLD_2024) * RATE_2024
    assert quebec_health_services_fund_individual(base, year=2024) == pytest.approx(
        expected, abs=0.01
    )


def test_the_maximum_caps_the_second_band():
    """The $1,000 maximum binds above $146,315 and never lifts."""
    assert quebec_health_services_fund_individual(200000, year=2024) == MAX_2024
    assert quebec_health_services_fund_individual(1_000_000, year=2024) == MAX_2024


def test_the_2024_figures_come_from_the_record_not_the_call_site():
    """The parameters are data, read by the function (DP#2/DP#20).

    Hardcoding them at the call site would pass every case above and then be
    wrong the moment the province revises a threshold.
    """
    from tax_data import TaxDataProvider

    record = TaxDataProvider()._load_year(2024, "canada", "quebec")
    assert record.qc_fss_individual_exemption == EXEMPTION_2024
    assert record.qc_fss_individual_first_cap == FIRST_CAP_2024
    assert record.qc_fss_individual_second_threshold == SECOND_THRESHOLD_2024
    assert record.qc_fss_individual_max == MAX_2024
    assert record.qc_fss_individual_rate == RATE_2024


def test_years_that_were_already_correct_are_untouched():
    """2025 and 2026 were populated before this fix and must not move."""
    assert quebec_health_services_fund_individual(46000, year=2025) == 150.0
    assert quebec_health_services_fund_individual(46000, year=2026) == 150.0


def test_2023_still_reports_absence_rather_than_an_invented_figure():
    """No 2023 figures were published for this, so none are invented.

    2023 keeps returning 0.0 because its record carries no parameters. That is
    the DP#32 shape the whole repo is built on -- an absent datum must not be
    filled with a plausible number -- so this asserts the absence is still
    visible rather than papering over it. Sourcing 2023 is follow-up work.
    """
    from tax_data import TaxDataProvider

    record = TaxDataProvider()._load_year(2023, "canada", "quebec")
    assert record.qc_fss_individual_exemption == 0
    assert quebec_health_services_fund_individual(46000, year=2023) == 0.0
