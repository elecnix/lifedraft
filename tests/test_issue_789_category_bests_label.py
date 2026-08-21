#!/usr/bin/env python3
"""Issue #789: category_bests labels the MAX-refinance winner as "No Refinance".

The engine's math is right (the winner's net_benefit is the max-refinance
scenario's), but its one-word headline label states the INVERSE of the
finding. This test pins the DP#9 single-source-of-truth property: the
``cash_out`` label on a ``category_bests`` entry must come from the SAME
result row whose ``net_benefit``/``year_by_year`` it reports -- never a
separately-computed grouping key or a default that diverges from the data.

Fabricated round numbers / role-based names only (DP#4/DP#15).
"""

import json
import os
import tempfile
import unittest

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers
from optimize import run_income_scenario_exploration, run_ltv_exploration
from output_plugins import _category_bests

try:
    # Added by the #789 fix -- a pure function of cash-out dollars.
    from output_plugins import _cash_out_label
except ImportError:  # pre-#789 main: the helper does not exist yet
    def _cash_out_label(cash_out: float) -> str:
        if cash_out <= 0:
            return "No Refinance"
        if cash_out <= 400_000:
            return "Fill Registered Room"
        return "Maximum Refinance (80%)"


def _make_cfg(house_value=700_000, mortgage_balance=100_000):
    """A fabricated household with property data so LTV exploration runs.

    house_value=700k, mortgage_balance=100k, ltv_max=0.80 -> max cash-out =
    700_000 * 0.80 - 100_000 = 460_000 > 400_000, so the max-refinance row
    labels "Maximum Refinance (80%)" -- the level the winner actually is.
    """
    return {
        'assumptions': {
            'projection_years': 10, 'investment_return': 0.07,
            'salary_growth': 0.02,
        },
        'savings': {'rate': 0.20},
        'property': {
            'house_value': house_value, 'mortgage_balance': mortgage_balance,
            'mortgage_rate': 0.05, 'margin_available': 200_000, 'ltv_max': 0.80,
            'current_payment_monthly': 1000, 'amortization_years': 25,
        },
        'family': {'members': [
            {'role': 'primary', 'gross_income': 120_000,
             'rrsp_room_accumulated': 100_000, 'tfsa_room_accumulated': 30_000,
             'pension_adjustment': 4000},
            {'role': 'spouse', 'gross_income': 50_000,
             'rrsp_room_accumulated': 50_000, 'tfsa_room_accumulated': 30_000,
             'pension_adjustment': 4000},
        ], 'children': [{'name': 'child_a', 'age': 10, 'gross_income': 0}]},
        'accounts': {
            'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33_000,
            'tfsa_annual_room_per_person': 7_000, 'resp_current_balance': 0,
        },
    }


def _write(cfg):
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(cfg, f)
    return path


class TestCategoryBestsLabelMatchesItsData(unittest.TestCase):
    """DP#9: the label on a category_bests entry comes from the SAME result
    row whose net_benefit it reports. Pre-#789 the headline result rows
    carried no ``cash_out`` key, so _category_bests read
    ``r.get('cash_out', 0)`` -> 0 for every row -> every row bucketed as
    "No Refinance", and the winner (always the max-refinance row, because
    headline results run at ltv_max) was labelled "No Refinance" -- the
    inverse of the finding."""

    def setUp(self):
        self.cfg = _make_cfg()
        self.path = _write(self.cfg)
        self.addCleanup(os.unlink, self.path)

    def test_winner_label_is_max_refinance_not_no_refinance(self):
        """The headline winner's net_benefit equals the 80%-LTV max-refinance
        row (the data IS the max-refinance scenario), so its label must say
        "Maximum Refinance (80%)", not "No Refinance"."""
        results = run_income_scenario_exploration(self.cfg, self.path)
        cats = _category_bests(results)
        self.assertTrue(cats, "category_bests must not be empty")
        winner = cats[0]
        # The winner's data IS the max-refinance scenario: its net_benefit
        # equals the best 80%-LTV row of the LTV exploration.
        ltv_results = run_ltv_exploration(self.cfg, self.path)
        max_refi_rows = [r for r in ltv_results if r['ltv'] == 0.80]
        self.assertTrue(max_refi_rows, "LTV exploration must include 80% rows")
        max_refi_best = max(max_refi_rows, key=lambda r: r.get('net_benefit', 0))
        self.assertAlmostEqual(
            winner['net_benefit'], max_refi_best['net_benefit'], places=2,
            msg="the category_bests winner's net_benefit must equal the "
                "max-refinance (80%% LTV) row's -- pre-#789 the data was already "
                "the max-refi scenario, only the label was wrong.")
        # The headline: the label must NOT invert the finding.
        self.assertNotEqual(
            winner['cash_out'], "No Refinance",
            "the winner IS the max-refinance scenario (its net_benefit matches "
            "the 80%%-LTV row) -- labelling it 'No Refinance' states the inverse "
            "of the finding (issue #789).")
        self.assertEqual(
            winner['cash_out'], "Maximum Refinance (80%)",
            "with cash_out > $400,000 the winner's label must be "
            "'Maximum Refinance (80%)'.")

    def test_winner_cash_out_amount_matches_ltv_exploration_row(self):
        """The winner's declared cash_out (dollars) equals the LTV-exploration
        row whose net_benefit matches -- the label and the data come from one
        source (DP#9)."""
        results = run_income_scenario_exploration(self.cfg, self.path)
        cats = _category_bests(results)
        winner = cats[0]
        # The entry carries the cash-out DOLLARS it was derived from
        # (post-#789). Pre-#789 this field is absent -- the label had no
        # dollar source of truth behind it.
        self.assertIn(
            'cash_out_amount', winner,
            "a category_bests entry must carry the cash-out DOLLARS its "
            "label was derived from, so the label and the data share one "
            "source (DP#9 / #789).")
        ltv_results = run_ltv_exploration(self.cfg, self.path)
        max_refi_best = max(
            [r for r in ltv_results if r['ltv'] == 0.80],
            key=lambda r: r.get('net_benefit', 0))
        self.assertAlmostEqual(
            winner['cash_out_amount'], max_refi_best['cashout'], places=2,
            msg="the winner's cash_out_amount must equal the matching "
                "LTV-exploration row's cashout dollars.")
        # The label is a pure function of those dollars -- it can never
        # diverge from the data it decorates.
        self.assertEqual(winner['cash_out'],
                         _cash_out_label(winner['cash_out_amount']))

    def test_winner_net_benefit_is_max_over_ltv_exploration(self):
        """Invariant: the category_bests winner's net_benefit equals the max
        net_benefit over the LTV exploration's 80%-LTV rows (the headline runs
        at ltv_max, so its best IS the max-refi best)."""
        results = run_income_scenario_exploration(self.cfg, self.path)
        cats = _category_bests(results)
        ltv_results = run_ltv_exploration(self.cfg, self.path)
        max_over_ltv = max(
            (r.get('net_benefit', 0) for r in ltv_results if r['ltv'] == 0.80),
            default=0)
        self.assertAlmostEqual(cats[0]['net_benefit'], max_over_ltv, places=2)

    def test_use_readvanceable_label_comes_from_the_row(self):
        """The use_readvanceable label on a category_bests entry comes from the
        SAME row's readvanceable_mortgage flag (the canonical engine output
        key), not a default that always reads False.

        Built from hand-authored result rows (the way test_output_plugins'
        fixtures are built) so it is deterministic and does not depend on the
        engine discovering a readvanceable strategy for a particular config.
        Pre-#789 _category_bests read r.get('use_readvanceable', False) -> False
        because the row carries readvanceable_mortgage, not use_readvanceable.
        """
        rows = [
            {'strategy': 'no_readvance', 'cash_out': 460_000,
             'readvanceable_mortgage': False, 'deduct_later': False,
             'resp_cash_out': 0, 'ltv': 0.80,
             'net_benefit': 700_000, 'future_value': 2_000_000,
             'total_debt': 500_000},
            {'strategy': 'smith_manoeuvre', 'cash_out': 460_000,
             'readvanceable_mortgage': True, 'deduct_later': False,
             'resp_cash_out': 0, 'ltv': 0.80,
             'net_benefit': 800_000, 'future_value': 2_500_000,
             'total_debt': 600_000},
        ]
        cats = _category_bests(rows)
        # The readvanceable winner (smith_manoeuvre, higher net_benefit) must
        # surface use_readvanceable=True on its category_bests entry.
        winner = cats[0]
        self.assertAlmostEqual(winner['net_benefit'], 800_000)
        self.assertTrue(
            winner['use_readvanceable'],
            "a readvanceable winning strategy must surface "
            "use_readvanceable=True on its category_bests entry, not silently "
            "read False from a key the row never carried (#789).")
        self.assertEqual(winner['cash_out'], "Maximum Refinance (80%)")


class TestCashOutLabelIsPureFunctionOfDollars(unittest.TestCase):
    """_cash_out_label is a pure function of the cash-out dollars -- the single
    source of truth -- so the label on a category_bests entry can never
    diverge from the data (DP#9 / #789). All three bands."""

    def test_zero_is_no_refinance(self):
        self.assertEqual(_cash_out_label(0), "No Refinance")
        self.assertEqual(_cash_out_label(-1), "No Refinance")

    def test_registered_room_fill_band(self):
        self.assertEqual(_cash_out_label(1), "Fill Registered Room")
        self.assertEqual(_cash_out_label(400_000), "Fill Registered Room")

    def test_maximum_refinance_band(self):
        self.assertEqual(_cash_out_label(400_001), "Maximum Refinance (80%)")
        self.assertEqual(_cash_out_label(460_000), "Maximum Refinance (80%)")


if __name__ == '__main__':
    unittest.main()