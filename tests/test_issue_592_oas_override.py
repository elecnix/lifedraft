#!/usr/bin/env python3
"""Tests for issue #592: OAS override falls through `or`, and freezes indexation.

`simulation.py`'s retirement transition used to read the OAS override as
    ret.get('oas_annual_max') or get_oas_annual_max(sim_year)

Two defects in that one line:
  1. `0` is falsy, so `retirement.oas_annual_max: 0` ("this household gets no
     OAS" — e.g. insufficient Canadian residency years) silently reverted to
     the year-versioned code table instead of producing zero OAS.
  2. A non-zero override is a scalar, so it froze OAS at one number across
     every projection year, defeating the year-versioning in
     `countries/canada/retirement.py` (`CPP_OAS_BY_YEAR`).

DP#13: defaults are fallbacks for absent input, not opinions that override
a genuine value. `None` means "not specified, use the table"; `0` means zero.

Lean, single-responsibility, relational (DP#3). Round numbers, no PII.

Run: uv run pytest tests/test_issue_592_oas_override.py -q
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from simulation import FamilySimulation, SimulationConfig
from countries.canada.retirement import CPP_OAS_BY_YEAR

START_YEAR = 2026


def _base_dict(retirement_overrides=None, start_year=START_YEAR,
                primary_birth_year=None, retirement_age=65, inflation=0.02):
    """A single retired primary member, low income so OAS clawback never bites."""
    if primary_birth_year is None:
        # Already past retirement_age at start_year, so the member is retired
        # from year 0 on — every sim_year in the test is a "post-retirement" year.
        primary_birth_year = start_year - retirement_age - 5
    ret = {
        'rrif_conversion_age': 71,
        'drawdown_order': ['tfsa', 'non_reg', 'rrsp'],
    }
    if retirement_overrides:
        ret.update(retirement_overrides)
    return {
        'assumptions': {
            'projection_years': 5,
            'investment_return': 0.05,
            'salary_growth': 0.0,
            'start_year': start_year,
            'frozen_brackets': True,
            'time_step': 'yearly',
            'inflation': inflation,
        },
        'savings': {'rate': 0.10},
        'property': {
            'house_value': 600000, 'mortgage_balance': 200000,
            'mortgage_rate': 0.045, 'ltv_max': 0.80,
            'current_payment_monthly': 1200, 'amortization_years': 25,
            'margin_available': 0,
        },
        'family': {
            'members': [
                # Low CPP estimate and no pension keeps net income far below
                # any OAS clawback threshold, so oas == oas_annual_max exactly.
                {'role': 'primary', 'gross_income': 0,
                 'birth_year': primary_birth_year, 'retirement_age': retirement_age,
                 'cpp_monthly_estimated': 100, 'cpp_start_age': 65,
                 'oas_start_age': 65, 'oas_defer_months': 0,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            'children': [],
        },
        'accounts': {
            'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33810,
            'tfsa_annual_room_per_person': 7000, 'resp_current_balance': 0,
        },
        'retirement': ret,
        'cash_flows': [],
    }


def _oas_for_year(cfg_dict, sim_year):
    """Isolated call into the OAS/CPP computation for one calendar year.

    Epic #795 bite 1: the transition used to be `sim._retirement_transition`;
    it is now the registered `retirement_income` rule. We build a minimal
    RuleContext/YearWorkingState (the primary is already retired in every
    fixture here -- birth_year = start_year - retirement_age - 5) and call
    the rule directly, reading `ws.oas_income`.
    """
    from rule_registry import RuleContext, YearWorkingState, RULES
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg)
    start_year = sim.start_year
    ws = YearWorkingState(year=sim_year - start_year)
    ctx = RuleContext(
        year=sim_year - start_year,
        calendar_year=sim_year,
        allocations={},
        config=cfg,
        investment_return=0.0,
        mortgage_rate=0.0,
        heloc_rate=0.0,
        mortgage_data=None,
        use_readvanceable=False,
        deduct_later=False,
        primary_marginal_rate=0.0,
        spouse_marginal_rate=0.0,
        resp_data=None,
        fhsa_contribution=0.0,
        rrsp_annual_limit=None,
        tfsa_annual_limit=None,
        fhsa_annual_limit=None,
        non_reg_after_tax_return=None,
        cpp_income=0.0,
        oas_income=0.0,
        pension_income=0.0,
        drawdown_order=None,
        rrif_min_rate_primary=0.0,
        rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0,
        retiree_marginal_rate=0.0,
        drawdown_bracket_target=None,
        drawdown_other_taxable_income=0.0,
        primary_income_pre=0.0,
        spouse_income_pre=0.0,
        primary_retired=True,
        spouse_retired=False,
        base_primary_income=sim._primary_income,
        base_spouse_income=sim._spouse_income,
        year_brackets=sim.brackets,
        tax_indexation_rate=sim.tax_provider.indexation_rate,
    )
    RULES['retirement_income'](ws, ctx)
    return ws.oas_income


class TestZeroOASOverrideIsRespected(unittest.TestCase):
    """DP#13: `0` is a value, not an absent key."""

    def test_zero_override_produces_zero_oas(self):
        oas = _oas_for_year(
            _base_dict({'oas_annual_max': 0, 'oas_clawback_threshold': 95323}),
            START_YEAR)
        self.assertEqual(oas, 0.0,
                          "retirement.oas_annual_max: 0 must produce zero OAS, "
                          "not silently fall back to the code table")

    def test_nonzero_override_is_still_honoured(self):
        # The other side of the threshold (DP#17): a genuine non-zero override
        # must keep working exactly as before.
        oas = _oas_for_year(
            _base_dict({'oas_annual_max': 8908, 'oas_clawback_threshold': 95323}),
            START_YEAR)
        self.assertAlmostEqual(oas, 8908, places=2)


class TestOASIndexationWithoutOverride(unittest.TestCase):
    """With no config override, OAS must track CPP_OAS_BY_YEAR year over year."""

    def test_oas_differs_across_years_per_table(self):
        oas_2023 = _oas_for_year(
            _base_dict(start_year=2023, retirement_overrides={}), 2023)
        oas_2026 = _oas_for_year(
            _base_dict(start_year=2023, retirement_overrides={}), 2026)
        self.assertAlmostEqual(oas_2023, CPP_OAS_BY_YEAR[2023]['oas_annual_max'], places=2)
        self.assertAlmostEqual(oas_2026, CPP_OAS_BY_YEAR[2026]['oas_annual_max'], places=2)
        self.assertNotAlmostEqual(oas_2023, oas_2026, places=2)


class TestOASOverrideDoesNotFreezeIndexation(unittest.TestCase):
    """A non-zero override must index forward, not freeze at a scalar."""

    def test_override_grows_over_a_five_year_horizon(self):
        cfg_dict = _base_dict(
            {'oas_annual_max': 8908, 'oas_clawback_threshold': 95323},
            start_year=START_YEAR, inflation=0.02)
        oas_year0 = _oas_for_year(cfg_dict, START_YEAR)
        oas_year5 = _oas_for_year(cfg_dict, START_YEAR + 5)
        self.assertNotAlmostEqual(
            oas_year0, oas_year5, places=2,
            msg="A configured oas_annual_max must not freeze OAS across the "
                "projection horizon")
        self.assertGreater(oas_year5, oas_year0)
        # Indexed at the configured inflation rate from start_year (DP#20).
        expected_year5 = 8908 * (1.02 ** 5)
        self.assertAlmostEqual(oas_year5, expected_year5, places=2)

    def test_zero_override_stays_zero_across_years(self):
        # Zero indexed forward is still zero — not a divide-by-zero or NaN trap.
        cfg_dict = _base_dict(
            {'oas_annual_max': 0, 'oas_clawback_threshold': 95323},
            start_year=START_YEAR, inflation=0.02)
        oas_year0 = _oas_for_year(cfg_dict, START_YEAR)
        oas_year5 = _oas_for_year(cfg_dict, START_YEAR + 5)
        self.assertEqual(oas_year0, 0.0)
        self.assertEqual(oas_year5, 0.0)


if __name__ == '__main__':
    unittest.main()
