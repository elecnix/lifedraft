#!/usr/bin/env python3
"""Tests for issue #691: per-account MER (management-expense-ratio) fee reduces
the account's compounded return in the engine.

Before this fix the two dead MER fields (``product_registry.Product.mer`` and
``asset_location.PortfolioHolding.mer_pct``) were consumed by nothing: two
otherwise-identical accounts differing only in fee projected to a byte-identical
balance. This wires ONE canonical spelling -- a per-account ``mer`` on the
contract account schema -- into the existing #823 per-account growth seam
(``_blended_pot_rate``), subtracting each fee-flagged account's balance-weighted
MER from the pot's gross rate before compounding (net = gross - Σ(balance·mer)/
pot_total). The gross rate is the global ``return_model`` rate (or its #823
expected_return blend); a declared MER now moves the ending balance.

Absence is a strict no-op: a household that declares no ``mer`` gets today's
behaviour (the golden invariant is unchanged -- the golden household declares no
MER). All test data uses fabricated round numbers (DP#13/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import contract_schema
import input_contract as ic
from simulation_config import SimulationConfig
from rule_registry import RULES, RuleContext, YearWorkingState
from rules_contributions import _blended_pot_rate


def _load_example_doc():
    """The shipped contract example, trimmed to the subset the adapter maps
    (same helper tests/test_issue_823_ftq.py uses)."""
    import copy
    import json

    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = json.load(f)
    doc = copy.deepcopy(doc)
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]

    def _owner_id(acc):
        o = acc.get("owner")
        if isinstance(o, str):
            return o
        if isinstance(o, dict):
            return o.get("person")
        return None

    doc["accounts"] = [a for a in doc["accounts"] if _owner_id(a) in keep]
    return doc


def _config(**overrides):
    """A minimal SimulationConfig with fabricated round numbers (DP#13/DP#15)."""
    defaults = dict(
        projection_years=5,
        investment_return=0.07,
        mortgage_balance=0,
        mortgage_rate=0.05,
        margin_available=0,
        family_members=[
            {'role': 'primary', 'gross_income': 130000, 'birth_year': 1990,
             'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 20000},
        ],
        children=[],
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def _ctx(config, *, investment_return=0.07):
    return RuleContext(
        year=0, calendar_year=2026, allocations={}, config=config,
        investment_return=investment_return, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=0.40, spouse_marginal_rate=0.0,
        resp_data=None, fhsa_contribution=0.0, rrsp_annual_limit=None,
        tfsa_annual_limit=None, fhsa_annual_limit=None,
        non_reg_after_tax_return=None, cpp_income=0.0, oas_income=0.0,
        pension_income=0.0, drawdown_order=None, rrif_min_rate_primary=0.0,
        rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0, retiree_marginal_rate=0.0,
        drawdown_bracket_target=None, drawdown_other_taxable_income=0.0,
        living_costs=0.0, after_tax_income=0.0,
    )


# ── The fee subtracts from the pot rate ─────────────────────────────────────

class TestMerSubtractsFromPotRate(unittest.TestCase):
    def test_no_mer_returns_global_rate(self):
        """Absence no-op: no MER declared -> the pot grows at the global rate
        (golden-invariant-shaped)."""
        cfg = _config()  # no account_mer_drag
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)

    def test_mer_subtracted_balance_weighted(self):
        # A $40k slice of a $100k RRSP pot carries a 1.00% MER.
        # net = 0.07 - (40000*0.01)/100000 = 0.07 - 0.004 = 0.066
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_balance': 40_000, 'weighted_mer_sum': 40_000 * 0.01,
                             'fee_share': 0.4, 'fee_rate': 0.01},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.066)

    def test_whole_pot_mer_subtracts_full_fee(self):
        # The whole $100k pot carries a 1.16% MER -> net = 0.07 - 0.0116.
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_balance': 100_000, 'weighted_mer_sum': 100_000 * 0.0116,
                             'fee_share': 1.0, 'fee_rate': 0.0116},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07 - 0.0116)

    def test_mer_for_different_kind_does_not_affect_this_pot(self):
        cfg = _config(account_mer_drag={
            'tfsa': {'mer_balance': 50_000, 'weighted_mer_sum': 50_000 * 0.01,
                             'fee_share': 1.0, 'fee_rate': 0.01},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)

    def test_zero_pot_returns_global(self):
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_balance': 40_000, 'weighted_mer_sum': 40_000 * 0.01,
                             'fee_share': 0.4, 'fee_rate': 0.01},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 0.0), 0.07)

    def test_zero_mer_is_no_op(self):
        """DP#32: an explicit 0.0 MER is fee-free, identical to no MER -- not a
        source of divergence."""
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_balance': 100_000, 'weighted_mer_sum': 0.0,
                             'fee_share': 1.0, 'fee_rate': 0.0},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)

    def test_mer_composes_with_expected_return_override(self):
        # An account with BOTH a 7.3% expected_return and a 1.0% MER: its
        # contribution nets to (0.073 - 0.01). $25k slice of a $100k pot.
        # gross blend = (25000*0.073 + 75000*0.07)/100000 = 0.07075
        # net = 0.07075 - (25000*0.01)/100000 = 0.07075 - 0.0025 = 0.06825
        cfg = _config(
            account_return_overrides={
                'rrsp': {'override_balance': 25_000,
                         'weighted_rate_sum': 25_000 * 0.073},
            },
            account_mer_drag={
                'rrsp': {'mer_balance': 25_000, 'weighted_mer_sum': 25_000 * 0.01,
                             'fee_share': 0.25, 'fee_rate': 0.01},
            },
        )
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.06825)


# ── Behaviour: two households diverge on fee alone (the acceptance criterion) ─

class TestRegisteredGrowthAppliesMer(unittest.TestCase):
    def test_higher_mer_grows_slower(self):
        """The issue's acceptance test: two RRSP pots, same $100k balance, same
        gross rate, differ ONLY in MER -> their post-growth balances diverge by
        the compounded fee. Today (before the fix) they were byte-identical."""
        cfg_cheap = _config(account_mer_drag={
            'rrsp': {'mer_balance': 100_000, 'weighted_mer_sum': 100_000 * 0.0020,
                             'fee_share': 1.0, 'fee_rate': 0.0020},
        })
        cfg_dear = _config(account_mer_drag={
            'rrsp': {'mer_balance': 100_000, 'weighted_mer_sum': 100_000 * 0.0116,
                             'fee_share': 1.0, 'fee_rate': 0.0116},
        })
        ws_cheap = YearWorkingState(year=0)
        ws_cheap.new_rrsp_bal = 100_000
        ws_dear = YearWorkingState(year=0)
        ws_dear.new_rrsp_bal = 100_000
        RULES['registered_growth'](ws_cheap, _ctx(cfg_cheap, investment_return=0.07))
        RULES['registered_growth'](ws_dear, _ctx(cfg_dear, investment_return=0.07))
        # Cheap fund: 100000 * 1.068 ; dear fund: 100000 * 1.0584.
        self.assertAlmostEqual(ws_cheap.new_rrsp_bal, 100_000 * 1.068)
        self.assertAlmostEqual(ws_dear.new_rrsp_bal, 100_000 * 1.0584)
        self.assertGreater(ws_cheap.new_rrsp_bal, ws_dear.new_rrsp_bal)

    def test_no_mer_growth_unchanged(self):
        """Without any MER the growth is exactly the global rate (no-op)."""
        cfg = _config()
        ws = YearWorkingState(year=0)
        ws.new_rrsp_bal = 100_000
        ws.new_tfsa_p_bal = 50_000
        RULES['registered_growth'](ws, _ctx(cfg, investment_return=0.07))
        self.assertAlmostEqual(ws.new_rrsp_bal, 107_000)
        self.assertAlmostEqual(ws.new_tfsa_p_bal, 53_500)


# ── Config round-trip (DP#24) ───────────────────────────────────────────────

class TestConfigRoundTrip(unittest.TestCase):
    def test_mer_drag_round_trips(self):
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_balance': 40_000, 'weighted_mer_sum': 40_000 * 0.01,
                             'fee_share': 0.4, 'fee_rate': 0.01},
        })
        d = cfg.to_dict()
        self.assertIn('mer_drag', d['accounts'])
        cfg2 = SimulationConfig.from_dict(d)
        self.assertEqual(cfg2.account_mer_drag, cfg.account_mer_drag)

    def test_no_mer_does_not_emit(self):
        cfg = _config()
        d = cfg.to_dict()
        self.assertNotIn('mer_drag', d['accounts'])
        cfg2 = SimulationConfig.from_dict(d)
        self.assertEqual(cfg2.account_mer_drag, {})


# ── Contract mapping: account.mer flows through to_internal_config ──────────

class TestContractMapping(unittest.TestCase):
    def test_mer_flows_through_to_internal_config(self):
        doc = _load_example_doc()
        rrsp = next(a for a in doc["accounts"]
                    if a["kind"] == "rrsp" and a["owner"] == "p1")
        rrsp["mer"] = 0.0116
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIn("rrsp", cfg.account_mer_drag)
        m = cfg.account_mer_drag["rrsp"]
        self.assertAlmostEqual(m["mer_balance"], rrsp["balance"]["amount"])
        self.assertAlmostEqual(m["weighted_mer_sum"],
                               rrsp["balance"]["amount"] * 0.0116)

    def test_no_mer_is_absent(self):
        """The unmodified example declares no per-account MER -> the config's
        mer_drag map is empty (golden: today's fee-free-rate behaviour, DP#32)."""
        doc = _load_example_doc()
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(cfg.account_mer_drag, {})

    def test_mer_null_is_absent(self):
        """An explicit null MER is absent (not a zero fee) -- DP#32: null means
        'no fee declared', which is the fee-free global-rate behaviour."""
        doc = _load_example_doc()
        rrsp = next(a for a in doc["accounts"]
                    if a["kind"] == "rrsp" and a["owner"] == "p1")
        rrsp["mer"] = None
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertNotIn("rrsp", cfg.account_mer_drag)

    def test_mer_zero_is_recorded_but_no_op(self):
        """DP#32: an explicit 0.0 MER is a declared fact (distinct from null),
        recorded as a fee-flagged balance with a zero weighted sum -- it reaches
        the config but does not move the rate (fee-free, not unknown)."""
        doc = _load_example_doc()
        rrsp = next(a for a in doc["accounts"]
                    if a["kind"] == "rrsp" and a["owner"] == "p1")
        rrsp["mer"] = 0.0
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIn("rrsp", cfg.account_mer_drag)
        self.assertAlmostEqual(cfg.account_mer_drag["rrsp"]["weighted_mer_sum"], 0.0)


# ── Integration tests: MER reaches the engine output (DP#11/DP#18) ──────
#
# The unit tests above call RULES['registered_growth'](ws, ctx) directly on
# hand-built state, which verifies the rule's contract in isolation. DP#11 says
# unit tests verify a module's contract; integration tests verify composition.
# DP#18 says an overlay (here: a declared MER on an account) must change the
# engine's observable output, not merely populate a config field that nothing
# reads. These tests drive FamilySimulation.run() end-to-end and assert that a
# declared MER measurably reduces terminal assets versus the no-MER baseline.

import copy

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from test_golden_trajectory_581 import golden_household_config


def _run(cfg: dict):
    """Drive the fold and return the full YearResult list."""
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                            use_readvanceable=False, deduct_later=False)
    return sim.run()


class TestMerReachesEngineOutput(unittest.TestCase):
    """DP#18: a declared MER on a registered account must lower the household's
    terminal assets versus the fee-free baseline — not merely subtract from a
    hand-built pot rate in a unit test."""

    def test_mer_lowers_terminal_assets_versus_baseline(self):
        """A household declaring a 1.16% MER on its RRSP ends strictly poorer
        than the same household without the fee. The MER reduces the compounding
        rate, the balance grows more slowly every year, and the gap compounds."""
        base_cfg = golden_household_config()
        variant = copy.deepcopy(base_cfg)
        # The golden household's RRSP has a balance of $300k (primary) +
        # $150k (spouse). Add a 1.16% MER on the RRSP pot.
        variant['accounts']['mer_drag'] = {
            'rrsp': {'mer_balance': 450_000, 'weighted_mer_sum': 450_000 * 0.0116,
                             'fee_share': 1.0, 'fee_rate': 0.0116},
        }
        base_terminal = _run(base_cfg)[-1].total_assets
        variant_terminal = _run(variant)[-1].total_assets
        # The MER drags down compounding over 46 years: the variant must end
        # strictly poorer.
        self.assertLess(variant_terminal, base_terminal)

    def test_absence_is_no_op_golden_unchanged(self):
        """DP#32: the golden household declares no MER, so adding an empty
        mer_drag dict does not change its terminal total_assets (absence is a
        strict no-op, not a silent default-to-zero that masks a wiring gap)."""
        base_cfg = golden_household_config()
        variant = copy.deepcopy(base_cfg)
        variant['accounts']['mer_drag'] = {}  # explicit empty — no effect
        base_terminal = _run(base_cfg)[-1].total_assets
        variant_terminal = _run(variant)[-1].total_assets
        self.assertEqual(base_terminal, variant_terminal)


if __name__ == '__main__':
    unittest.main()
