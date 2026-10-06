#!/usr/bin/env python3
"""Issue #341: the Quebec deduction for workers (TP-1 line 201, TA s.358.0.3).

Quebec lets every resident deduct 6% of eligible work income from QUEBEC
taxable income, up to an annually indexed maximum. The engine did not model it,
so Quebec taxable income always equalled federal taxable income -- and because
the golden household is a Quebec employee, that over-taxed its Quebec bill every
working year.

The caps are year-versioned data, sourced from Finances Québec "Dépenses
fiscales", fiche 110906, Tableau C.42 (the measure cites *Loi sur les impôts*
art. 358.0.3):

    2023     2024     2025     2026
    1 315    1 380    1 420    1 450

and Revenu Québec's line 201 page states the rate and the current cap directly:
"La déduction pour travailleur ... est égale à 6 % de votre revenu de travail
admissible. Le maximum est de 1 420 $."

DP#15: every household here is fabricated, round-numbered and role-named.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from countries.canada.provinces.quebec.quebec_credits import (
    quebec_worker_deduction)
from countries.canada.tax_calc import compute_total_tax

PUBLISHED_CAP = {2023: 1_315.0, 2024: 1_380.0, 2025: 1_420.0, 2026: 1_450.0}
RATE = 0.06


class TestTheLaw:
    @pytest.mark.parametrize("year,cap", sorted(PUBLISHED_CAP.items()))
    def test_the_cap_is_the_published_maximum(self, year, cap):
        """Work income high enough that the cap binds."""
        assert quebec_worker_deduction(1_000_000.0, year=year) == cap

    def test_the_deduction_is_six_percent_below_the_cap(self):
        """$10,000 of 2024 work income: 6% is $600, well under the $1,380 cap."""
        assert quebec_worker_deduction(10_000.0, year=2024) == pytest.approx(600.0)

    def test_it_caps_at_the_boundary(self):
        """$23,000 x 6% = $1,380 exactly -- the year the cap starts to bind."""
        assert quebec_worker_deduction(23_000.0, year=2024) == pytest.approx(1_380.0)
        assert quebec_worker_deduction(23_001.0, year=2024) == pytest.approx(1_380.0)

    def test_no_work_income_means_no_deduction(self):
        assert quebec_worker_deduction(0.0, year=2024) == 0.0
        assert quebec_worker_deduction(-5.0, year=2024) == 0.0

    def test_a_year_whose_record_carries_no_parameters_raises(self):
        """A silent 0.0 here would over-tax every working Quebec household in
        that year, so the function refuses instead.

        Registered directly rather than by asking for an old year: the provider
        PROJECTS a year with no record of its own (backwards as well as forwards),
        so "a year with no data" is not reachable that way -- the only way to see
        a record that carries no parameters is to put one there, which is exactly
        the state a newly added year would be in.
        """
        from tax_data import TaxYearData, TaxBracket, default_tax_provider
        provider = default_tax_provider()
        key = "canada:quebec:2031"
        saved = provider._fallbacks.get(key)
        provider.register_year(TaxYearData(
            year=2031, country="canada", province="quebec",
            federal_brackets=[TaxBracket(0, 60_000, 0.14)],
            # Deliberately left at their 0.0 defaults: a year whose
            # worker-deduction parameters were never sourced.
            qc_worker_deduction_rate=0.0, qc_worker_deduction_max=0.0))
        try:
            with pytest.raises(ValueError, match="worker-deduction parameters"):
                quebec_worker_deduction(30_000.0, year=2031, provider=provider)
        finally:
            if saved is not None:
                provider._fallbacks[key] = saved
            else:
                provider._fallbacks.pop(key, None)


class TestTheQuebecSlice:
    """The deduction must move QUEBEC taxable income and nothing federal."""

    def _tax(self, income, province, year=2025):
        """2025, not 2024: Ontario's earliest tax-data row is 2025, so this is the
        only year in which the same household can be priced in both provinces."""
        return compute_total_tax(
            taxable_income=income, employment_income=income,
            year=year, province=province)

    def test_quebec_taxable_income_is_the_cap_below_the_federal_base(self):
        """$27,500 of employment income leaves Quebec taxable income the year's
        full cap below federal taxable income -- the cap binds at this income."""
        result = self._tax(27_500.0, "quebec")
        assert result['breakdown']['qc_worker_deduction'] == pytest.approx(1_420.0)
        assert result['breakdown']['qc_taxable_income'] == pytest.approx(
            27_500.0 - 1_420.0)

    def test_ontario_is_untouched(self):
        """The same household in Ontario gets no deduction and no split base --
        the measure is Quebec's alone."""
        result = self._tax(27_500.0, "ontario")
        assert result['breakdown']['qc_worker_deduction'] == 0.0
        assert result['breakdown']['qc_taxable_income'] == pytest.approx(27_500.0)

    def test_federal_tax_does_not_move(self):
        """Federal tax is computed on the federal base, so the deduction must
        not touch it: the federal-after-abatement figure for the same input is
        identical in Quebec and in Ontario."""
        qc = self._tax(27_500.0, "quebec")
        on = self._tax(27_500.0, "ontario")
        assert qc['breakdown']['federal_before_abatement'] == pytest.approx(
            on['breakdown']['federal_before_abatement']), (
            "the GROSS federal tax is the figure the deduction must not touch")
        # The Quebec ABATEMENT legitimately differs between the two runs: it is
        # Quebec's own 16.5% reduction of federal tax, so Ontario has none of it.
        # That difference is not the deduction's doing, and asserting the
        # after-abatement figures equal would be asserting the abatement away.
        assert on['breakdown']['quebec_abatement'] == 0.0
        assert qc['breakdown']['quebec_abatement'] > 0.0

    def test_two_thousand_twenty_five_uses_the_published_cap(self):
        result = self._tax(30_000.0, "quebec", year=2025)
        assert result['breakdown']['qc_worker_deduction'] == pytest.approx(1_420.0)

    def test_the_quebec_tax_falls_by_the_cap_times_the_marginal_rate(self):
        """The issue's arithmetic: $1,380 x 14% = $193.20 of pre-credit Quebec
        tax. Asserted against the pure Quebec tax function, so the comparison is
        between two figures the engine itself computes."""
        from countries.canada.tax_calc import quebec_tax
        with_deduction = quebec_tax(27_500.0 - 1_380.0, 2024)
        without = quebec_tax(27_500.0, 2024)
        assert without - with_deduction == pytest.approx(193.20, abs=0.01)
