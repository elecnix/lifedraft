#!/usr/bin/env python3
"""Issues #711 (CPP/QPP sharing) + #712 (pension income splitting): the two
retirement income-splitting rules, wired into the live drawdown.

Both were "dead-on-dead" (pension_split_optimizer's only caller was cpp_sharing,
which nothing called) and BLOCKED on the retirement tax model lacking per-spouse
marginal rates. #363 PR 4 lifted that blocker — the drawdown now prices each
spouse's RRSP/RRIF slice on that spouse's OWN progressive bracket set and claws
back OAS per person. This PR wires the two rules in:

    simulation_rules.apply_retirement_income now calls
      * cpp_sharing.share_cpp_amounts       — equalize CPP toward 50/50, and
      * pension_split_optimizer.split_pension_amounts — move up to 50% of the
        higher-bracket spouse's eligible pension to the lower-bracket spouse,
    as PURE income transfers between two retired spouses, driven by the
    decisions.cpp_share / decisions.pension_split_pct ELECTIONS the optimizer
    sweeps (DP#22/#30). Both CONSERVE the household total, so the drawdown net
    target is unchanged (money conservation) — only the per-spouse split moves,
    so each spouse's own progressive drawdown stack + OAS-clawback base shift,
    and the household's retirement tax with them. No parallel tax model is built:
    the per-spouse marginal rates are the ones the drawdown already prices.

DP#15/#4: every figure is FABRICATED, ROUND, and role-based ("primary"/"spouse").
Nothing here is a real person's finances.

A NOTE ON THE ORACLE FORM. The textbook benefit of income splitting is
``transfer × (high_MTR − low_MTR)`` — but that is the saving on the SPLIT
INCOME ITSELF, and this engine models CPP/OAS/pension as received NET of tax
(``covered_net`` in apply_retirement_income sums them at face value). So the
transferred pension is not itself re-taxed; the saving instead materializes on
the two channels the engine DOES price per spouse: (a) the progressive tax on
the RRSP/RRIF DRAW, which is cheaper when the drawing spouse's base income is
lowered, and (b) the per-person OAS recovery tax. Both are hand-recomputed from
the pure ``tax_on_income`` / ``oas_clawback`` primitives below — a CRA-style
oracle the engine must match, never copied from engine output.

Run: uv run pytest tests/test_issue_711_712_income_splitting.py -q
"""

from tax_data import default_tax_provider
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tax_calculator import tax_on_income
from countries.canada.cpp_sharing import share_cpp_amounts
from countries.canada.pension_split_optimizer import (
    split_pension_amounts, MAX_PENSION_SPLIT_PCT,
)
from simulation import FamilySimulation, SimulationConfig
from simulation_rules import RuleContext, YearWorkingState, RULES

START_YEAR = 2026
BRACKETS = default_tax_provider().get_combined_brackets(START_YEAR, "quebec")


# ============================================================================
# Class A — the pure mechanism functions (hand math, DP#3/#15)
# ============================================================================

class ShareCppAmounts(unittest.TestCase):
    """cpp_sharing.share_cpp_amounts — a pure transfer toward equalization."""

    def test_full_share_equalizes_and_conserves(self):
        p, s = share_cpp_amounts(20_000.0, 8_000.0, 1.0)
        self.assertEqual((p, s), (14_000.0, 14_000.0))   # (20k+8k)/2 each
        self.assertEqual(p + s, 28_000.0)                 # total conserved

    def test_partial_share_moves_halfway(self):
        # share=0.5 moves each halfway to the 14k mean.
        self.assertEqual(share_cpp_amounts(20_000.0, 8_000.0, 0.5), (17_000.0, 11_000.0))

    def test_zero_share_is_a_value_not_a_default(self):
        # DP#32: an elected 0 leaves the split untouched, never coerced.
        self.assertEqual(share_cpp_amounts(20_000.0, 8_000.0, 0.0), (20_000.0, 8_000.0))

    def test_symmetric_couple_gets_no_change(self):
        # Equal CPP already => equalizing is a no-op (the ~zero-benefit case).
        self.assertEqual(share_cpp_amounts(12_000.0, 12_000.0, 1.0), (12_000.0, 12_000.0))

    def test_share_outside_unit_interval_fails_loudly(self):
        with self.assertRaises(ValueError):
            share_cpp_amounts(20_000.0, 8_000.0, 1.5)


class SplitPensionAmounts(unittest.TestCase):
    """pension_split_optimizer.split_pension_amounts — a pure T1032 transfer."""

    def test_half_split_from_higher_spouse_conserves(self):
        # Primary is higher: 50% of 40k (=20k) moves to the spouse.
        p, s = split_pension_amounts(40_000.0, 10_000.0, 0.5, primary_is_higher=True)
        self.assertEqual((p, s), (20_000.0, 30_000.0))
        self.assertEqual(p + s, 50_000.0)                 # total conserved

    def test_split_direction_follows_the_higher_spouse(self):
        # Spouse is higher: 50% of its 40k moves to the primary.
        self.assertEqual(
            split_pension_amounts(10_000.0, 40_000.0, 0.5, primary_is_higher=False),
            (30_000.0, 20_000.0))

    def test_zero_split_is_a_value_not_a_default(self):
        self.assertEqual(
            split_pension_amounts(40_000.0, 10_000.0, 0.0, primary_is_higher=True),
            (40_000.0, 10_000.0))

    def test_split_above_the_statutory_cap_fails_loudly(self):
        with self.assertRaises(ValueError):
            split_pension_amounts(40_000.0, 10_000.0, MAX_PENSION_SPLIT_PCT + 0.01,
                                  primary_is_higher=True)


# ============================================================================
# Class B — end-to-end wiring through the live retirement fold
# ============================================================================
#
# A HIGH-pension primary and a LOW-pension spouse, both retired. The whole
# discretionary draw lands on the primary's RRSP (drawn first in the default
# order), so the household's retirement tax is exactly the progressive tax on
# the primary's draw, stacked on the primary's OWN base — which pension
# splitting lowers. Both spouses have equal, tiny CPP so CPP sharing is a
# no-op here (isolated in Class C). Round numbers, role-based names (DP#4/#15).

_PRIMARY = {'role': 'primary', 'gross_income': 0, 'birth_year': START_YEAR - 72,
            'retirement_age': 65, 'cpp_monthly_estimated': 100,
            'pension_income_annual': 60_000, 'rrsp_balance': 800_000}
_SPOUSE = {'role': 'spouse', 'gross_income': 0, 'birth_year': START_YEAR - 70,
           'retirement_age': 65, 'cpp_monthly_estimated': 100,
           'pension_income_annual': 5_000, 'rrsp_balance': 800_000}


def _run_year(retirement, members=(_PRIMARY, _SPOUSE)):
    """Drive retirement_income + retirement_drawdown for one year and return the
    populated working state. This is the live fold path: the sizing rule writes
    the per-spouse bases, the drawdown rule prices the draw against them."""
    cfg = SimulationConfig.from_dict({
        'assumptions': {'start_year': START_YEAR, 'investment_return': 0.05,
                        'inflation': 0.02, 'horizon_age': 95},
        'property': {'house_value': 500_000, 'mortgage_balance': 0,
                     'margin_available': 0, 'ltv_max': 0.80,
                     'amortization_years': 25, 'mortgage_rate': 0.045},
        'family': {'members': list(members), 'children': []},
        'accounts': {'rrsp_annual_max': 31_000},
        'retirement': retirement, 'tax': {'province': 'qc'},
    })
    sim = FamilySimulation(cfg)
    ws = YearWorkingState(year=0)
    ws.new_rrsp_bal = 800_000.0
    ws.new_spouse_rrsp_bal = 800_000.0
    ctx = RuleContext(
        year=0, calendar_year=START_YEAR, allocations={}, config=cfg,
        investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=0.0, spouse_marginal_rate=0.0, resp_data=None,
        fhsa_contribution=0.0, rrsp_annual_limit=None, tfsa_annual_limit=None,
        fhsa_annual_limit=None, non_reg_after_tax_return=None,
        cpp_income=0.0, oas_income=0.0, pension_income=0.0, drawdown_order=None,
        rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0,
        retiree_marginal_rate=0.0, drawdown_bracket_target=None,
        drawdown_other_taxable_income=0.0, primary_income_pre=0.0,
        spouse_income_pre=0.0, primary_retired=True, spouse_retired=True,
        base_primary_income=sim._primary_income,
        base_spouse_income=sim._spouse_income,
        year_brackets=sim.brackets,
        tax_indexation_rate=sim.tax_provider.indexation_rate,
    )
    RULES['retirement_income'](ws, ctx)
    RULES['retirement_drawdown'](ws, ctx)
    return ws


def _household_tax(ws):
    """The tax the retirement draw actually cost = gross withdrawn − net kept."""
    return ws.drawdown_total - ws.drawdown_net_delivered


class PensionSplitThroughTheFold(unittest.TestCase):

    def test_split_delivers_the_same_net_but_pays_strictly_less_tax(self):
        """Money conservation + a real saving: splitting delivers the identical
        net drawdown target (the household total is conserved, so the target is
        byte-unchanged) yet grosses up LESS, because the primary's draw is now
        priced on a lower base."""
        base = _run_year({'spending_target': 90_000})
        split = _run_year({'spending_target': 90_000, 'pension_split_pct': 0.5})

        # The elected split moved 50% of the primary's $60k pension to the spouse.
        self.assertAlmostEqual(base.drawdown_other_taxable_income_primary
                               - split.drawdown_other_taxable_income_primary,
                               30_000.0, places=6)
        # Money conservation: same net target, and both deliver it to the dollar.
        self.assertAlmostEqual(base.drawdown_net_target, split.drawdown_net_target,
                               places=6)
        self.assertAlmostEqual(split.drawdown_net_delivered,
                               split.drawdown_net_target, places=2)
        # A strictly cheaper draw for the identical net.
        self.assertLess(_household_tax(split), _household_tax(base))
        self.assertLess(split.drawdown_total, base.drawdown_total)

    def test_engine_tax_matches_the_hand_recomputed_progressive_oracle(self):
        """CRA oracle: with the whole draw on the primary, the household tax must
        equal the progressive tax on the primary's own taxable draw stacked on
        the primary's own (post-split) base — recomputed from tax_on_income, not
        read from engine internals."""
        for ret in ({'spending_target': 90_000},
                    {'spending_target': 90_000, 'pension_split_pct': 0.5}):
            ws = _run_year(ret)
            base_p = ws.drawdown_other_taxable_income_primary
            taxable = ws.drawdown_taxable          # all of it on the primary
            oracle = (tax_on_income(base_p + taxable, BRACKETS)
                      - tax_on_income(base_p, BRACKETS))
            self.assertAlmostEqual(_household_tax(ws), oracle, places=2)

    def test_symmetric_couple_gets_no_benefit(self):
        """Two spouses with EQUAL pension: whoever is 'higher' is a tie, the
        transfer is symmetric and the per-spouse bases are unchanged, so the
        draw and its tax are byte-identical with or without the election."""
        sym_p = dict(_PRIMARY, pension_income_annual=30_000)
        sym_s = dict(_SPOUSE, pension_income_annual=30_000)
        base = _run_year({'spending_target': 90_000}, members=(sym_p, sym_s))
        split = _run_year({'spending_target': 90_000, 'pension_split_pct': 0.5},
                          members=(sym_p, sym_s))
        self.assertAlmostEqual(_household_tax(base), _household_tax(split), places=6)
        self.assertAlmostEqual(base.drawdown_total, split.drawdown_total, places=6)

    def test_absent_election_is_identical_to_an_explicit_zero(self):
        """DP#13/#32: omitting the election reproduces today's behaviour exactly;
        an explicit 0 is the same no-op, never a different path."""
        absent = _run_year({'spending_target': 90_000})
        zero = _run_year({'spending_target': 90_000, 'pension_split_pct': 0.0,
                          'cpp_share': 0.0})
        self.assertAlmostEqual(absent.drawdown_total, zero.drawdown_total, places=9)
        self.assertAlmostEqual(_household_tax(absent), _household_tax(zero), places=9)


# ============================================================================
# Class C — CPP/QPP sharing through the fold
# ============================================================================
#
# A HIGH-CPP primary and a LOW-CPP spouse (asymmetric), both retired. Equalizing
# CPP lowers the primary's base and cuts the tax on the primary's draw, exactly
# as the pension split does. Fabricated round CPP via cpp_monthly_estimated.

_HI_CPP = {'role': 'primary', 'gross_income': 0, 'birth_year': START_YEAR - 72,
           'retirement_age': 65, 'cpp_monthly_estimated': 1_400,
           'pension_income_annual': 45_000, 'rrsp_balance': 800_000}
_LO_CPP = {'role': 'spouse', 'gross_income': 0, 'birth_year': START_YEAR - 70,
           'retirement_age': 65, 'cpp_monthly_estimated': 200,
           'pension_income_annual': 5_000, 'rrsp_balance': 800_000}


class CppShareThroughTheFold(unittest.TestCase):

    def test_full_equalization_moves_cpp_and_cuts_the_draw_tax(self):
        # A spending target above the couple's combined CPP/OAS/pension, so a
        # taxable RRSP draw is required (and lands on the primary).
        base = _run_year({'spending_target': 110_000}, members=(_HI_CPP, _LO_CPP))
        shared = _run_year({'spending_target': 110_000, 'cpp_share': 1.0},
                           members=(_HI_CPP, _LO_CPP))
        # CPP sharing moved the primary's base DOWN (its high CPP was pooled).
        self.assertLess(shared.drawdown_other_taxable_income_primary,
                        base.drawdown_other_taxable_income_primary)
        # Household base is conserved (nothing invented or lost by the pooling).
        self.assertAlmostEqual(
            base.drawdown_other_taxable_income_primary
            + base.drawdown_other_taxable_income_spouse,
            shared.drawdown_other_taxable_income_primary
            + shared.drawdown_other_taxable_income_spouse, places=6)
        # Same net target, delivered to the dollar, at strictly less tax.
        self.assertAlmostEqual(shared.drawdown_net_delivered,
                               shared.drawdown_net_target, places=2)
        self.assertLess(_household_tax(shared), _household_tax(base))


# ============================================================================
# Class D — the elections are a swept CONTRACT dimension (DP#22/#24)
# ============================================================================

from simulation_config import ScenarioOverlay, apply_overlay


class SweepDimension(unittest.TestCase):

    def test_overlay_round_trips_the_two_elections(self):
        """DP#24: an overlay carrying the elections survives to_dict/from_dict."""
        ov = ScenarioOverlay(label="split", cpp_share=1.0, pension_split_pct=0.5)
        back = ScenarioOverlay.from_dict(ov.to_dict())
        self.assertEqual(back.cpp_share, 1.0)
        self.assertEqual(back.pension_split_pct, 0.5)

    def test_absent_elections_are_omitted_and_stay_none(self):
        """None means 'no change from base' — it is not serialized, and it does
        not become an explicit 0 on the way back (DP#18/#32)."""
        ov = ScenarioOverlay(label="base")
        d = ov.to_dict()
        self.assertNotIn('cpp_share', d)
        self.assertNotIn('pension_split_pct', d)
        self.assertIsNone(ScenarioOverlay.from_dict(d).pension_split_pct)

    def test_apply_overlay_lands_on_the_retirement_keys_the_engine_reads(self):
        """DP#18: the swept elections must land on cfg['retirement'], which
        SimulationConfig maps to retirement_data and apply_retirement_income
        reads as ret.get('cpp_share') / ret.get('pension_split_pct'). A None
        election leaves the base config untouched."""
        base = {'family': {'members': [], 'children': []},
                'property': {'house_value': 500_000, 'mortgage_balance': 0,
                             'margin_available': 0, 'ltv_max': 0.80,
                             'amortization_years': 25, 'mortgage_rate': 0.045},
                'retirement': {'spending_target': 60_000}}
        derived = apply_overlay(base, ScenarioOverlay(
            label="split", cpp_share=0.75, pension_split_pct=0.30))
        self.assertEqual(derived['retirement']['cpp_share'], 0.75)
        self.assertEqual(derived['retirement']['pension_split_pct'], 0.30)
        # A no-op overlay adds neither key.
        untouched = apply_overlay(base, ScenarioOverlay(label="base"))
        self.assertNotIn('cpp_share', untouched['retirement'])
        self.assertNotIn('pension_split_pct', untouched['retirement'])


if __name__ == '__main__':
    unittest.main()
