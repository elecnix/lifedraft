"""Issue #356 (partial): the Quebec work premium's missing and zeroed parameters.

WHAT THIS FILE PINS, AND WHAT IT DOES NOT
-----------------------------------------
This is the DATA half of #356. Two defects are fixed and tested here:

  1. the 2024 and 2023 blocks carried NO ``qc_work_premium_*`` values, so every
     field fell back to the 0.0 dataclass default and ``quebec_work_premium``
     silently returned nothing for those years -- a wrong number, not an error;
  2. ``TaxYearData`` had no maximum for a SINGLE-PARENT family or for a COUPLE
     WITH CHILDREN, so the two household types the program pays the most could
     not be represented at all.

The credit is STILL NOT WIRED into household cash -- no production path calls
``quebec_work_premium``. That is the larger half of #356 and is NOT claimed
here; this file does not assert it.

Every figure is cited from a primary source, never inferred (#273):
  * 2023 and 2024 -- Ministere des Finances du Quebec, "Parametres du regime
    d'imposition des particuliers pour l'annee d'imposition 2024"
    (decembre 2023), Tableau 3, under "Prime au travail generale".
  * 2025 and 2026 -- Revenu Quebec, "Montant des credits d'impot relatifs a la
    prime au travail", which tabulates the maximum by household type.

DP#15: no household data appears here; the figures are statutory.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.provinces.quebec import quebec_work_premium
from countries.canada.provinces.quebec.quebec_credits import _get_quebec_data


# Statutory maxima, per household type, from the sources named above.
MAXIMA = {
    2023: {'single': 1095.27, 'couple': 1709.61,
           'single_parent': 2832.60, 'couple_children': 3684.50},
    2024: {'single': 1152.34, 'couple': 1797.07,
           'single_parent': 2980.20, 'couple_children': 3873.00},
    2025: {'single': 1185.52, 'couple': 1848.34,
           'single_parent': 3066.00, 'couple_children': 3983.50},
    2026: {'single': 1207.33, 'couple': 1882.45,
           'single_parent': 3122.40, 'couple_children': 4057.00},
}

# Reduction thresholds: one adult, and a couple.
THRESHOLDS = {
    2023: (11842, 18338),
    2024: (12334, 19092),
}


class TestTheParametersExistForEveryYear:
    """The silent-zero defect: 2023/2024 returned 0.0 for everyone because the
    fields were never set. A missing parameter must never be a zero benefit."""

    @pytest.mark.parametrize('year', [2023, 2024, 2025, 2026])
    def test_all_four_household_maxima_are_present(self, year):
        d = _get_quebec_data(year)
        expected = MAXIMA[year]
        assert d.qc_work_premium_max_single == pytest.approx(expected['single'], abs=0.01)
        assert d.qc_work_premium_max_couple == pytest.approx(expected['couple'], abs=0.01)
        assert d.qc_work_premium_max_single_parent == pytest.approx(
            expected['single_parent'], abs=0.01), (
            "a single-parent family's maximum must be its own value -- this field "
            "did not exist before #356, so the type could not be represented")
        assert d.qc_work_premium_max_couple_children == pytest.approx(
            expected['couple_children'], abs=0.01)

    @pytest.mark.parametrize('year', [2023, 2024])
    def test_the_reduction_thresholds_and_exclusions_are_present(self, year):
        d = _get_quebec_data(year)
        one_adult, couple = THRESHOLDS[year]
        assert d.qc_work_premium_reduction_threshold_single == pytest.approx(one_adult, abs=0.01)
        assert d.qc_work_premium_reduction_threshold_couple == pytest.approx(couple, abs=0.01)
        # The excluded first dollars are NOT indexed and do not move by year.
        assert d.qc_work_premium_excluded_single == 2400
        assert d.qc_work_premium_excluded_couple == 3600
        assert d.qc_work_premium_growth_rate == pytest.approx(0.116)
        assert d.qc_work_premium_reduction_rate == pytest.approx(0.10)


class TestTheCreditActuallyComputesForThoseYears:
    """The consequence: the years that returned 0.0 for any income now return a
    figure. A test that only read the parameters would miss the actual defect."""

    @pytest.mark.parametrize('year', [2023, 2024, 2025, 2026])
    def test_a_low_income_worker_gets_a_positive_premium(self, year):
        got = quebec_work_premium(25_000, 20_905, year=year)
        assert got > 0.0, (
            f"{year}: an eligible person alone must receive a work premium, got "
            f"{got}. A zero here is the silent-miss defect, not a legitimate result.")

    def test_2024_reproduces_the_published_figure(self):
        """The CFFP's 2024 reference case: a person alone with 25,000 of
        employment income and a Quebec net income of 20,905 receives 295.

        This is the case that pins the ORDER and the rates: the premium is the
        maximum (1,152.34, reached because 11.6% of the 22,600 of work income
        above the 2,400 exclusion exceeds it) less 10% of the net income above
        the one-adult threshold 12,334 -> 1,152.34 - 857.10 = 295.24.
        """
        got = quebec_work_premium(25_000, 20_905, year=2024)
        assert got == pytest.approx(295.0, abs=1.0), (
            f"expected the published 295 (+/- the source's rounding), got {got:.2f}")

    def test_2023_differs_from_2024(self):
        """Sanity that the two newly-filled years are not the same numbers: the
        2024 parameters are the 2023 ones indexed at 5.08%, so an unindexed pair
        of identical values would mean one block was copied into the other."""
        y23 = quebec_work_premium(25_000, 20_905, year=2023)
        y24 = quebec_work_premium(25_000, 20_905, year=2024)
        assert y23 != y24
        assert y24 > y23, "2024 is the indexed year and must be the larger"

    def test_the_maximum_is_reached_not_exceeded(self):
        """The cap still binds: a high earner never exceeds the maximum."""
        for year, maxima in MAXIMA.items():
            got = quebec_work_premium(200_000, 5_000, year=year)
            assert got <= maxima['single'] + 0.01, (
                f"{year}: the premium {got:.2f} exceeds the person-alone maximum "
                f"{maxima['single']}: the cap is not binding")

    def test_no_work_income_earns_nothing(self):
        for year in (2023, 2024, 2025, 2026):
            assert quebec_work_premium(0, 0, year=year) == 0.0
            assert quebec_work_premium(2_400, 5_000, year=year) == 0.0, (
                "work income at the 2,400 exclusion earns nothing")