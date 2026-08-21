#!/usr/bin/env python3
"""Issue #783: Quebec provincial tuition tax credit (TP-1 Schedule T, 8%).

The federal tuition credit (ITA s.118.5) was modelled in #764; the Quebec
PROVINCIAL tuition credit was not -- its rate was absent from the tax-data
files, so a Quebec student's tuition yielded only the federal credit and the
provincial portion was silently $0 (DP#32/DP#12: a missing rate is data added
to the year-versioned tax-data module, not hardcoded).

This tests (DP#4/DP#15: fabricated round numbers):
1. The QC tuition credit rate (8%) is in the year-versioned Quebec tax-data,
   sourced (NOT the 14% general non-refundable rate), and projects forward.
2. `tuition_tax_credit` computes federal(15%→data) + QC(8%) for a Quebec
   resident, federal-only for a non-QC resident, and $0 for $0 tuition
   (DP#32: absence handled, never a guessed rate).
3. The QC credit is APPLIED end-to-end: a QC-resident student's after-tax
   income rises by federal+QC, an Ontario student's by federal only.
4. The rate tracks the DATA, not a literal (monkeypatch the data field).
"""

import unittest

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers
from countries.canada.tax_calc import tuition_tax_credit
from tax_data import TaxDataProvider


def _provider():
    """A TaxDataProvider with the canonical (registered) year data."""
    return TaxDataProvider()


class TestQCTuitionCreditRateInYearVersionedData(unittest.TestCase):
    """DP#12/DP#20: the QC tuition rate is year-versioned data, sourced -- not
    a hardcoded constant, and NOT the 14% general non-refundable rate."""

    def test_qc_2026_rate_is_8_percent(self):
        qc = _provider()._load_year(2026, 'canada', 'quebec')
        self.assertEqual(qc.qc_tuition_credit_rate, 0.08)

    def test_qc_rate_is_not_the_general_non_refundable_rate(self):
        qc = _provider()._load_year(2026, 'canada', 'quebec')
        # The 8% tuition-specific rate is distinct from the 14% general rate.
        self.assertNotEqual(qc.qc_tuition_credit_rate,
                            qc.qc_non_refundable_credit_rate)

    def test_federal_record_carries_zero_qc_tuition_rate(self):
        fed = _provider()._load_year(2026, 'canada', 'federal')
        self.assertEqual(fed.qc_tuition_credit_rate, 0.0,
                         "the QC tuition rate is Quebec-specific; federal is 0")

    def test_qc_rate_projects_forward_to_untabled_years(self):
        # DP#20: a future year inherits the rate from the nearest tabled year.
        qc2030 = _provider()._load_year(2030, 'canada', 'quebec')
        self.assertEqual(qc2030.qc_tuition_credit_rate, 0.08)

    def test_qc_rate_present_for_all_tabled_years(self):
        p = _provider()
        for year in (2023, 2024, 2025, 2026):
            self.assertEqual(p._load_year(year, 'canada', 'quebec').qc_tuition_credit_rate,
                             0.08, f"QC tuition rate for {year} must be 8%")


class TestTuitionCreditComputesFederalPlusQC(unittest.TestCase):
    """`tuition_tax_credit` parameterized by province (DP#3/DP#8): a Quebec
    resident gets federal + QC; a non-QC resident gets federal only; $0 tuition
    -> $0 credit (DP#32)."""

    def test_quebec_resident_gets_federal_plus_qc(self):
        p = _provider()
        fed_lowest = p._load_year(2026, 'canada', 'federal').federal_brackets[0].rate
        credit = tuition_tax_credit(10_000, 2026, p, province='quebec')
        self.assertAlmostEqual(credit, 10_000 * (fed_lowest + 0.08))
        # With the 2026 data (federal lowest 14%), that is 1400 + 800 = 2200.
        self.assertAlmostEqual(credit, 2200.0)

    def test_qc_alias_accepted(self):
        p = _provider()
        self.assertAlmostEqual(tuition_tax_credit(10_000, 2026, p, province='qc'),
                               tuition_tax_credit(10_000, 2026, p, province='quebec'))

    def test_non_quebec_resident_gets_federal_only(self):
        p = _provider()
        fed_lowest = p._load_year(2026, 'canada', 'federal').federal_brackets[0].rate
        for prov in (None, 'ontario', 'british_columbia'):
            self.assertAlmostEqual(tuition_tax_credit(10_000, 2026, p, province=prov),
                                   10_000 * fed_lowest,
                                   msg=f"province={prov!r} must yield federal credit only")

    def test_zero_tuition_is_zero_credit(self):
        p = _provider()
        self.assertEqual(tuition_tax_credit(0, 2026, p, province='quebec'), 0.0)
        self.assertEqual(tuition_tax_credit(0, 2026, p, province='ontario'), 0.0)

    def test_credit_tracks_the_data_not_a_literal(self):
        """DP#12: monkeypatch the QC rate in the data and the credit follows --
        the 8% is not baked into the computation."""
        p = _provider()
        p._fallbacks['canada:quebec:2026'].qc_tuition_credit_rate = 0.20
        self.assertAlmostEqual(tuition_tax_credit(10_000, 2026, p, province='quebec'),
                               10_000 * (0.14 + 0.20))  # fed lowest 14% + patched 20%

    def test_no_qc_data_yields_zero_provincial_credit(self):
        """DP#32: a provider with no Quebec tax data must yield a $0 QC
        provincial credit (a loud absence), never a guessed 8%. The federal
        portion falls back to its own round placeholder; the provincial
        portion is exactly $0 because the rate is not in the data."""
        p = TaxDataProvider(auto_register=False)  # no data registered
        credit = tuition_tax_credit(10_000, 2026, p, province='quebec')
        # Federal falls back to the 0.15 placeholder; QC has no data -> 0.
        self.assertAlmostEqual(credit, 10_000 * 0.15)
        # And the QC portion alone is 0 (the federal-only call equals it):
        self.assertAlmostEqual(credit,
                               tuition_tax_credit(10_000, 2026, p, province='ontario'))


class TestQCTuitionCreditIsAppliedEndToEnd(unittest.TestCase):
    """The QC provincial credit is APPLIED in the fold (reusing #764's path,
    no new rule): a QC-resident student's after-tax income rises by federal+QC,
    an Ontario student's by federal only."""

    def _cfg(self, tuition, province):
        from simulation_config import SimulationConfig
        return SimulationConfig(
            projection_years=2, house_value=800_000, mortgage_balance=300_000,
            mortgage_rate=0.05, amortization_years=25, margin_available=0,
            savings_rate=0.0, living_costs=40_000, start_year=2026,
            province=province, investment_return=0.05,
            family_members=[{'role': 'primary', 'birth_year': 1985,
                             'gross_income': 95_000,
                             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
                             'tuition_by_year': {2026: tuition} if tuition else {}}])

    def _run(self, cfg):
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        return FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()

    def _fed_lowest(self):
        return _provider()._load_year(2026, 'canada', 'federal').federal_brackets[0].rate

    def test_quebec_student_after_tax_rises_by_federal_plus_qc(self):
        no_tuition = self._run(self._cfg(0, 'quebec'))[0]
        with_tuition = self._run(self._cfg(10_000, 'quebec'))[0]
        delta = with_tuition.after_tax_income - no_tuition.after_tax_income
        self.assertAlmostEqual(delta, 10_000 * (self._fed_lowest() + 0.08),
                               places=2,
                               msg="a QC student's after-tax income must rise by "
                                   "federal + QC (8%) provincial tuition credit")

    def test_ontario_student_after_tax_rises_by_federal_only(self):
        no_tuition = self._run(self._cfg(0, 'ontario'))[0]
        with_tuition = self._run(self._cfg(10_000, 'ontario'))[0]
        delta = with_tuition.after_tax_income - no_tuition.after_tax_income
        self.assertAlmostEqual(delta, 10_000 * self._fed_lowest(), places=2,
                               msg="an ON student gets the federal credit only")

    def test_quebec_credit_is_larger_than_ontario_by_the_qc_portion(self):
        """The headline: a QC student is credited MORE than an ON student by
        exactly the QC provincial portion (8% of tuition)."""
        qc_delta = (self._run(self._cfg(10_000, 'quebec'))[0].after_tax_income
                    - self._run(self._cfg(0, 'quebec'))[0].after_tax_income)
        on_delta = (self._run(self._cfg(10_000, 'ontario'))[0].after_tax_income
                    - self._run(self._cfg(0, 'ontario'))[0].after_tax_income)
        self.assertGreater(qc_delta, on_delta)
        self.assertAlmostEqual(qc_delta - on_delta, 10_000 * 0.08, places=2,
                               msg="the QC-vs-ON difference is the 8% QC provincial credit")

    def test_zero_tuition_means_zero_applied_credit(self):
        # DP#32: no tuition declared -> no credit applied (absence, not a guess).
        no_tuition = self._run(self._cfg(0, 'quebec'))[0]
        # after_tax_income is the same as a run with no tuition key at all.
        cfg_no_key = self._cfg(0, 'quebec')
        cfg_no_key.family_members[0] = {
            k: v for k, v in cfg_no_key.family_members[0].items() if k != 'tuition_by_year'}
        no_key = self._run(cfg_no_key)[0]
        self.assertAlmostEqual(no_tuition.after_tax_income, no_key.after_tax_income,
                               places=2)


if __name__ == '__main__':
    unittest.main()