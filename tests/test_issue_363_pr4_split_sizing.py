#!/usr/bin/env python3
"""Issue #363 PR 4: the retirement-income sizing rule computes the PER-SPOUSE
drawdown bases that drive the two-bracket-set split.

``simulation_rules.apply_retirement_income`` no longer only sums CPP/OAS/pension
to household scalars — when both spouses are retired it also writes each spouse's
OWN progressive base, OAS-inclusive bracket-fill headroom base, gross OAS and
bracket ceiling, and sets the ``drawdown_two_member_split`` gate that
``apply_retirement_drawdown`` reads to pass ``per_member`` into
``plan_drawdown_net``.

These are lean, single-responsibility checks on THAT wiring (the tax arithmetic
of the split itself is pinned by ``DrawdownTwoSpouseSplit`` in
``test_drawdown_oracle.py``). Round numbers, role-based names, no PII (DP#4/#15).

Run: uv run pytest tests/test_issue_363_pr4_split_sizing.py -q
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from simulation import FamilySimulation, SimulationConfig
from simulation_rules import RuleContext, YearWorkingState, RULES

START_YEAR = 2026


def _run_sizing_rule(members, retirement, sim_year=START_YEAR,
                     primary_retired=True, spouse_retired=True):
    """Call the ``retirement_income`` rule directly for one year and return the
    populated ``YearWorkingState`` (epic #795 bite 1 made the transition a
    registered rule; we drive it in isolation, as test_issue_592 does)."""
    cfg_dict = {
        'assumptions': {'start_year': START_YEAR, 'investment_return': 0.05,
                        'inflation': 0.02, 'horizon_age': 95},
        'property': {'house_value': 500000, 'mortgage_balance': 0,
                     'margin_available': 0, 'ltv_max': 0.80,
                     'amortization_years': 25, 'mortgage_rate': 0.045},
        'family': {'members': members, 'children': []},
        'accounts': {'rrsp_annual_max': 31000},
        'retirement': retirement,
        'tax': {'province': 'qc'},
    }
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg)
    ws = YearWorkingState(year=sim_year - sim.start_year)
    ctx = RuleContext(
        year=sim_year - sim.start_year, calendar_year=sim_year, allocations={},
        config=cfg, investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=0.0, spouse_marginal_rate=0.0, resp_data=None,
        fhsa_contribution=0.0, rrsp_annual_limit=None, tfsa_annual_limit=None,
        fhsa_annual_limit=None, non_reg_after_tax_return=None,
        cpp_income=0.0, oas_income=0.0, pension_income=0.0, drawdown_order=None,
        rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0,
        retiree_marginal_rate=0.0, drawdown_bracket_target=None,
        drawdown_other_taxable_income=0.0,
        primary_income_pre=0.0, spouse_income_pre=0.0,
        primary_retired=primary_retired, spouse_retired=spouse_retired,
        base_primary_income=sim._primary_income,
        base_spouse_income=sim._spouse_income,
        year_brackets=sim.brackets,
        tax_indexation_rate=sim.tax_provider.indexation_rate,
    )
    fired = RULES['retirement_income'](ws, ctx)
    return ws, fired


# Two retired spouses: a HIGH-pension primary and a LOW-pension spouse — the
# asymmetry the per-spouse split exists to exploit. Low CPP keeps both below the
# OAS clawback threshold so oas == full annual OAS (irrelevant to these wiring
# checks, but it keeps the numbers clean).
_HIGH = {'role': 'primary', 'gross_income': 0, 'birth_year': START_YEAR - 70,
         'retirement_age': 65, 'cpp_monthly_estimated': 100,
         'pension_income_annual': 50000, 'rrsp_balance': 600000}
_LOW = {'role': 'spouse', 'gross_income': 0, 'birth_year': START_YEAR - 68,
        'retirement_age': 65, 'cpp_monthly_estimated': 100,
        'pension_income_annual': 8000, 'rrsp_balance': 400000}


class TwoSpouseSizing(unittest.TestCase):

    def test_both_retired_sets_the_split_gate_and_distinct_per_spouse_bases(self):
        """Both retired => split gate on, and each spouse's own base reflects
        their OWN pension (not the household sum)."""
        ws, fired = _run_sizing_rule([_HIGH, _LOW], {'spending_target': 60000})
        self.assertTrue(fired)
        self.assertTrue(ws.drawdown_two_member_split)
        # Each spouse's progressive base is their OWN cpp + pension, so the high
        # spouse's base is strictly larger than the low spouse's.
        self.assertGreater(ws.drawdown_other_taxable_income_primary,
                           ws.drawdown_other_taxable_income_spouse)
        # The two per-spouse bases sum to the household base the single schedule
        # would have used (no income invented or lost by the split).
        self.assertAlmostEqual(
            ws.drawdown_other_taxable_income_primary
            + ws.drawdown_other_taxable_income_spouse,
            ws.drawdown_other_taxable_income, places=6)
        # The OAS-inclusive bracket-fill base adds each spouse's own OAS on top.
        self.assertAlmostEqual(
            ws.drawdown_bracket_fill_base_primary,
            ws.drawdown_other_taxable_income_primary + ws.drawdown_oas_gross_primary,
            places=6)

    def test_one_retiree_leaves_the_split_off(self):
        """Only the primary retired => no split (the household schedule prices
        the draw exactly as pre-PR-4)."""
        ws, _ = _run_sizing_rule([_HIGH, _LOW], {'spending_target': 60000},
                                 primary_retired=True, spouse_retired=False)
        self.assertFalse(ws.drawdown_two_member_split)

    def test_explicit_bracket_fill_target_override_applies_to_each_spouse(self):
        """DP#13: an explicit ``retirement.bracket_fill_target`` wins per spouse,
        exactly as it does for the single household ceiling — the auto-detected
        year-versioned ceiling is NOT used when the override is present."""
        override = 55000.0
        ws, _ = _run_sizing_rule(
            [_HIGH, _LOW],
            {'spending_target': 60000, 'bracket_fill_target': override})
        self.assertEqual(ws.drawdown_bracket_target_primary, override)
        self.assertEqual(ws.drawdown_bracket_target_spouse, override)
        # And the household ceiling honours the same override (unchanged path).
        self.assertEqual(ws.drawdown_bracket_target, override)


if __name__ == '__main__':
    unittest.main()
