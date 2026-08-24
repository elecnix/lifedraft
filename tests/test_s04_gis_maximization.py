#!/usr/bin/env python3
"""Issue #1020 (S04 Step 1): GIS is now PAID by the simulation fold.

Before #1020, ``countries.canada.retirement.gis_benefit`` was a year-versioned
pure helper with its own unit tests (``test_retirement_issue_53``,
``test_dp12_dp20_issue_330``) but was NEVER called from the fold -- dead code.
``YearResult`` had no GIS field, the ``retirement_income`` rule never invoked
the helper, and the optimizer's objective (``net_benefit`` / objective_score)
was blind to GIS entirely. So no drawdown-order candidate could make the
optimizer prefer GIS-preserving behaviour, and a modest-asset GIS-eligible
household received $0 GIS in its trajectory.

The fix (Step 1 only -- the pre-65 preservation MANEUVER is a separate follow-up):
the ``retirement_income`` rule now calls ``gis_benefit`` (DP#9: reused, not
re-spelled) from the PRIOR year's GIS-countable income (CRA's prior-year income
test -- OAS excluded per the helper's ``net_income`` contract; employment
income in a still-working prior year IS countable, which is exactly why the
preservation maneuver must be set up in the 50s). The GIS amount is folded
into the drawdown ``covered_net`` (GIS is non-taxable cash that covers spending,
reducing the discretionary drawdown shortfall) and into ``retirement_income`` /
``total_family_income``, and surfaced on a new ``YearResult.gis_income`` field.

This test asserts (fabricated households, DP#4/DP#15 -- round numbers, role
names, no real data):

  1. A MODEST-asset GIS-eligible single retiree receives NONZERO ``gis_income``
     in retirement years 2+ (year 1 of retirement's prior year is a working
     year -> GIS=0 that year, CRA-faithful; from year 2 the prior year is a
     low retirement year -> GIS kicks in). The GIS amount is the helper's own
     output on the prior-year countable base, so the fold and the helper agree.
  2. A HIGH-income household receives ``gis_income == 0`` every year (GIS-
     ineligible: countable income far above the single elimination threshold
     of ~$7,452 = $4,000 exemption + $1,726 max / 0.50).
  3. The GOLDEN household (``golden_household_config``) is GIS-ineligible and
     its terminal ``total_assets`` is byte-exact ``9709753.139463063`` -- GIS
     wiring is a no-op there (DP#32).

The pre-65 RRSP->TFSA preservation MANEUVER is deliberately NOT implemented here
(Step 2 follow-up): this test only verifies that GIS is now PAID and visible.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import unittest

from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from countries.canada.retirement import gis_benefit

from test_golden_trajectory_581 import golden_household_config, _run


# ── A modest-asset GIS-ELIGIBLE single retiree-to-be ──────────────────────
# Set up in the 50s (birth 1971 -> age 55 in 2026). Low CPP base ($250/mo),
# a SMALL RRSP ($25k), a modest TFSA ($45k, funds spending tax-free so the
# taxable drawdown stays low), low spending target ($16k), modest non-reg.
# The taxable retirement income (CPP + small RRSP drawdown) lands under the
# single GIS elimination threshold (~$7,452), so GIS is preserved.
def _modest_gis_household() -> dict:
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1971,
                 'gross_income': 32_000, 'retirement_age': 65,
                 'cpp_monthly_estimated': 250,
                 'rrsp_room_accumulated': 5_000,
                 'tfsa_room_accumulated': 40_000,
                 'rrsp_balance': 25_000,
                 'tfsa_balance': 45_000},
            ],
        },
        'accounts': {'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': 2026, 'horizon_age': 85,
            'investment_return': 0.04, 'salary_growth': 0.02,
            'inflation': 0.02, 'frozen_brackets': True,
        },
        'portfolio': {
            'accounts': {
                'non_reg': {
                    'balance': 10_000, 'cost_basis': 10_000,
                    'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                    'yield': {'eligible_dividends': 0.015, 'interest': 0.01},
                },
            },
        },
        'property': {
            'house_value': 250_000, 'mortgage_balance': 0,
            'mortgage_rate': 0.05, 'amortization_years': 25,
            'margin_available': 0, 'ltv_max': 0.80, 'heloc_readvance': False,
        },
        'savings': {'rate': 0.10},
        'retirement': {'spending_target': 16_000, 'rrif_conversion_age': 71},
        'tax': {'province': 'qc'},
    }


# ── A HIGH-income GIS-INELIGIBLE household ────────────────────────────────
# High salary, large RRSP -> retirement taxable income far above the GIS
# elimination threshold -> gis_income == 0 every year.
def _high_income_household() -> dict:
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1971,
                 'gross_income': 180_000, 'retirement_age': 65,
                 'cpp_monthly_estimated': 1_300,
                 'rrsp_room_accumulated': 40_000,
                 'tfsa_room_accumulated': 20_000,
                 'rrsp_balance': 400_000,
                 'tfsa_balance': 50_000},
            ],
        },
        'accounts': {'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': 2026, 'horizon_age': 85,
            'investment_return': 0.06, 'salary_growth': 0.02,
            'inflation': 0.02, 'frozen_brackets': True,
        },
        'portfolio': {
            'accounts': {
                'non_reg': {
                    'balance': 100_000, 'cost_basis': 100_000,
                    'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                    'yield': {'eligible_dividends': 0.015, 'interest': 0.01},
                },
            },
        },
        'property': {
            'house_value': 800_000, 'mortgage_balance': 0,
            'mortgage_rate': 0.05, 'amortization_years': 25,
            'margin_available': 0, 'ltv_max': 0.80, 'heloc_readvance': False,
        },
        'savings': {'rate': 0.20},
        'retirement': {'spending_target': 90_000, 'rrif_conversion_age': 71},
        'tax': {'province': 'qc'},
    }


def _run_cfg(cfg: dict):
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                            use_readvanceable=False, deduct_later=False)
    return sim.run()


class TestGISWiredIntoFold(unittest.TestCase):
    """Issue #1020 Step 1: GIS is computed in the fold and surfaced on YearResult."""

    def test_yearresult_has_gis_income_field(self):
        """YearResult now carries a gis_income field (was absent before #1020)."""
        from year_result import YearResult
        r = YearResult()
        self.assertTrue(hasattr(r, 'gis_income'))
        self.assertEqual(r.gis_income, 0.0)

    def test_modest_household_receives_nonzero_gis(self):
        """The modest GIS-eligible household receives nonzero gis_income.

        GIS uses the PRIOR-year income test, so the FIRST retirement year (age
        65) has GIS=0 (prior year was a working year with salary -> countable
        income above the threshold). From age 66+ the prior year is a low
        retirement year (CPP + small RRSP drawdown under the threshold) and GIS
        kicks in. This is the CRA-faithful pattern the preservation maneuver
        targets.
        """
        results = _run_cfg(_modest_gis_household())
        gis_years = [r for r in results if r.gis_income > 0]
        self.assertGreater(len(gis_years), 0,
                           "modest GIS-eligible household must receive nonzero GIS")
        # First retirement year (age 65): prior year was working -> GIS == 0.
        # primary birth 1971, start 2026 -> age 65 at year offset 11 (cal 2036).
        r65 = results[10]  # year offset 11 is index 10 (0-based results)
        cal65 = 2026 + r65.year - 1
        self.assertEqual(cal65 - 1971, 65)
        self.assertEqual(r65.gis_income, 0.0,
                         "age-65 year's prior year was a working year -> GIS=0")
        # Age 66+ : GIS > 0 in at least one year.
        r66 = results[11]
        self.assertGreater(r66.gis_income, 0.0,
                            "age-66 year's prior year is a low retirement year -> GIS>0")

    def test_gis_amount_matches_helper_on_prior_year_base(self):
        """The fold's GIS agrees with gis_benefit() on the prior-year base.

        DP#9: the rule reuses the helper; the amount the fold pays must equal
        what the helper computes on the prior year's countable income (CPP +
        pension + drawdown + LIF + employment, EXCLUDING OAS and GIS).
        """
        cfg = _modest_gis_household()
        results = _run_cfg(cfg)
        # Walk year-by-year; for each retirement year with a prior year, the
        # GIS paid should equal gis_benefit(prior_countable, is_coupled, year).
        is_coupled = len([m for m in cfg['family']['members']
                          if m.get('role') == 'spouse']) > 0
        for i in range(1, len(results)):
            r = results[i]
            prior = results[i - 1]
            if not r.any_retired:
                continue
            prior_countable = (prior.retirement_income + prior.employment_income
                               - prior.oas_income - prior.gis_income)
            cal = 2026 + r.year - 1
            expected = gis_benefit(prior_countable, is_coupled=is_coupled,
                                   year=cal)['gis_amount']
            self.assertAlmostEqual(r.gis_income, expected, places=2,
                                   msg=f"year {r.year} GIS mismatch")

    def test_gis_folded_into_retirement_income(self):
        """gis_income is part of retirement_income / total_family_income."""
        results = _run_cfg(_modest_gis_household())
        for r in results:
            if r.gis_income > 0:
                # retirement_income includes the GIS slice.
                self.assertGreaterEqual(r.retirement_income, r.gis_income)
                # total_family_income includes it too.
                self.assertGreaterEqual(r.total_family_income, r.gis_income)

    def test_high_income_household_gets_zero_gis(self):
        """A high-income household is GIS-ineligible -> gis_income == 0 always."""
        results = _run_cfg(_high_income_household())
        gis_total = sum(r.gis_income for r in results)
        self.assertEqual(gis_total, 0.0,
                         "high-income household must receive $0 GIS (ineligible)")
        for r in results:
            self.assertEqual(r.gis_income, 0.0)

    def test_golden_invariant_unchanged(self):
        """The golden household is GIS-ineligible -> GIS wiring is a no-op.

        Terminal total_assets must stay byte-exact 9709753.139463063 (DP#32:
        the golden household's prior-year countable income is always far above
        the GIS threshold, so gis_income == 0 across all 46 years).
        """
        results = _run(golden_household_config())
        # GIS is $0 every year for the golden (high-income) household.
        gis_total = sum(getattr(r, 'gis_income', 0.0) for r in results)
        self.assertEqual(gis_total, 0.0,
                         "golden household must receive $0 GIS (ineligible)")
        self.assertEqual(results[-1].total_assets, 9709753.139463063)


if __name__ == '__main__':
    unittest.main()