#!/usr/bin/env python3
"""Issue #727 (DP#21): a stress path is a PER-YEAR ReturnModel, not a single
averaged rate.

Pre-#727 `run_stress_test` collapsed the whole stress return path to one
average (`stress_path.average_return`) and set a flat `fixed` return_model at
that average -- which averaged away the very shape a stress test exists to
capture: a -40% year-1 crash then recovery was smeared across the whole
horizon, so year 1 never bore the crash. DP#21 says the engine consumes a
pluggable ReturnModel applied PER YEAR.

This tests (DP#4/DP#15: fabricated round numbers):
1. The overlay lands in `return_model` as a `variable` model whose per-year
   `rates` ARE the stress path (the #591 invariant holds; the shape is per-year).
2. Each year's stress return is applied IN THAT YEAR: a -40% year-1 return
   produces a year-1 balance drop of ~40% (start x 0.6), NOT the averaged
   rate's balance -- the per-year shape is preserved, not smeared.
3. The base path (no stress) is unchanged -- a flat path behaves as before.
4. The `StressPath` input shape (List[float] per-year) is preserved.
"""

import os
import tempfile
import unittest

from stress_scenarios import (
    StressPath, STRESS_BASELINE, STRESS_2008_CRASH, run_stress_test,
    apply_stress_overlay,
)
from return_model import ReturnModel, ReturnEngine


def _make_cfg(projection_years=2, rrsp_balance=100_000):
    """A fabricated single-earner household with a known starting RRSP balance
    and NO savings/debt -- so the only thing that moves the balance is the
    per-year investment return applied to it (DP#15: round numbers)."""
    return {
        'assumptions': {'start_year': 2026, 'projection_years': projection_years,
                        'investment_return': 0.07, 'salary_growth': 0.0,
                        'inflation': 0.0, 'frozen_brackets': True,
                        'savings_rate': 0.0, 'horizon_age': 95},
        'savings': {'rate': 0.0},
        'family': {'members': [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 100_000,
             'retirement_age': 95, 'rrsp_balance': rrsp_balance,
             'tfsa_balance': 0, 'rrsp_room_accumulated': 0,
             'tfsa_room_accumulated': 0}], 'children': []},
        'accounts': {},
        'portfolio': {'accounts': {}},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'mortgage_rate': 0.05, 'amortization_years': 25,
                     'margin_available': 0, 'ltv_max': 0.80,
                     'heloc_readvance': False},
        'retirement': {'spending_target': 0, 'rrif_conversion_age': 71},
        'tax': {'province': 'qc'},
        'household_budget': {'living_costs': 50_000},
    }


def _write(cfg):
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        import json
        json.dump(cfg, f)
    return path


def _best_year_by_year(cfg, stress_path):
    """Apply the stress overlay, run the optimizer, return the winner's
    year_by_year series (so we can inspect the per-year balance drop)."""
    from optimize import run_optimization
    mod_cfg = apply_stress_overlay(cfg, stress_path)
    path = _write(mod_cfg)
    try:
        results = run_optimization(mod_cfg, path)
    finally:
        os.unlink(path)
    best = max(results, key=lambda r: r.get('net_benefit', 0))
    return best.get('year_by_year', [])


class TestStressOverlayIsPerYearReturnModel(unittest.TestCase):
    """DP#21: the stress path lands as a per-year `variable` ReturnModel."""

    def test_overlay_lands_as_variable_model_with_per_year_rates(self):
        cfg = _make_cfg(projection_years=3)
        path = StressPath("crash", [-0.40, 0.20, 0.07], [0.05, 0.05, 0.05])
        mod = apply_stress_overlay(cfg, path, projection_years=3)
        self.assertEqual(mod['return_model']['type'], 'variable')
        self.assertEqual(mod['return_model']['rates'], [-0.40, 0.20, 0.07])
        # The averaged scalar is NOT what lands (it would be a fixed `rate`).
        self.assertNotIn('rate', mod['return_model'])

    def test_engine_reads_per_year_rate_for_each_year(self):
        """The variable model the overlay builds returns each year's stress
        rate via the engine's ReturnEngine.return_for_year seam."""
        cfg = _make_cfg(projection_years=3)
        path = StressPath("crash", [-0.40, 0.20, 0.07], [0.05, 0.05, 0.05])
        mod = apply_stress_overlay(cfg, path, projection_years=3)
        from return_model import build_return_model_from_config
        model = build_return_model_from_config(mod['return_model'])
        self.assertAlmostEqual(model.return_for_year(0), -0.40)
        self.assertAlmostEqual(model.return_for_year(1), 0.20)
        self.assertAlmostEqual(model.return_for_year(2), 0.07)

    def test_stresspath_input_shape_preserved(self):
        """The StressPath input is still a List[float] per-year; the fix only
        changes the ROUTING (averaged scalar -> per-year ReturnModel)."""
        self.assertEqual(STRESS_2008_CRASH.investment_return_path[0], -0.40)
        self.assertEqual(len(STRESS_2008_CRASH.investment_return_path), 10)


class TestStressCrashAppliedInYear1(unittest.TestCase):
    """The headline: a -40% year-1 return produces a year-1 balance drop of
    ~40% (start x 0.6), NOT the averaged rate's balance -- the crash hits
    year 1, not an averaged smear across the horizon."""

    def test_year1_balance_drops_by_40_percent_not_averaged(self):
        cfg = _make_cfg(projection_years=2, rrsp_balance=100_000)
        path = StressPath("y1_crash", [-0.40, 0.20], [0.05, 0.05])
        yby = _best_year_by_year(cfg, path)
        self.assertTrue(yby, "must produce a year_by_year series")
        # Year 1 (projection year 0): the RRSP balance is start x (1 + -0.40)
        # = 100,000 x 0.6 = 60,000. The averaged rate would be (-0.40+0.20)/2
        # = -0.10 -> 90,000. The -40% crash hits year 1, not the averaged -10%.
        self.assertAlmostEqual(yby[0]['primary_rrsp'], 60_000.0, places=2,
                               msg="year-1 balance must reflect the -40% crash "
                                   "applied in year 1 (100k x 0.6), not the "
                                   "averaged -10% (90k)")
        # Year 2 (projection year 1): 60,000 x (1 + 0.20) = 72,000.
        self.assertAlmostEqual(yby[1]['primary_rrsp'], 72_000.0, places=2)

    def test_averaged_rate_would_have_given_a_different_year1_balance(self):
        """Document the pre-#727 smear: the averaged rate (-10%) would have
        left year 1 at 90,000, not 60,000. This asserts the per-year path
        does NOT equal the averaged flat-rate outcome -- the shape now
        matters, which is the whole point of the fix."""
        cfg = _make_cfg(projection_years=2, rrsp_balance=100_000)
        path = StressPath("y1_crash", [-0.40, 0.20], [0.05, 0.05])
        yby = _best_year_by_year(cfg, path)
        averaged_year1 = 100_000 * (1 + path.average_return(2))  # 90,000
        self.assertNotAlmostEqual(yby[0]['primary_rrsp'], averaged_year1,
                                   places=2,
                                   msg="the per-year stress must NOT equal the "
                                       "averaged-flat-rate outcome (that was the "
                                       "pre-#727 smear bug)")

    def test_2008_crash_year1_is_negative_return(self):
        """The shipped 2008 crash path applies its -40% year-1 return in year
        1 (the balance drops), rather than the +4% averaged rate."""
        cfg = _make_cfg(projection_years=2, rrsp_balance=100_000)
        yby = _best_year_by_year(cfg, STRESS_2008_CRASH)
        # Year 1 balance < start (the -40% crash applied), not > start (the
        # averaged +4% would have grown it).
        self.assertLess(yby[0]['primary_rrsp'], 100_000.0,
                         "the -40% year-1 crash must drop the year-1 balance, "
                         "not grow it as the averaged +4% rate would have")


class TestBaselineUnchanged(unittest.TestCase):
    """A flat stress path (constant return) is unchanged by the per-year
    routing -- a constant path and its average are the same number, so the
    base/no-stress behaviour is preserved."""

    def test_flat_path_equals_its_average_per_year(self):
        # A constant path: per-year routing and the averaged scalar coincide.
        flat = StressPath("flat", [0.07] * 10, [0.05] * 10)
        self.assertAlmostEqual(flat.average_return(10), 0.07)
        self.assertEqual(flat.fill_returns(10), [0.07] * 10)

    def test_baseline_runs_and_returns_per_year_path(self):
        cfg = _make_cfg(projection_years=3)
        result = run_stress_test(cfg, STRESS_BASELINE)
        self.assertEqual(result['stress_name'], "Baseline (no stress)")
        self.assertEqual(result['return_path'][:3], [0.07, 0.07, 0.07])


if __name__ == '__main__':
    unittest.main()