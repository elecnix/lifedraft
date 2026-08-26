#!/usr/bin/env python3
"""Tests for issue #691/#136: per-account MER (management-expense-ratio) fee
reduces the account's compounded return in the engine — dynamically.

Before #691 the two dead MER fields (``product_registry.Product.mer`` and
``asset_location.PortfolioHolding.mer_pct``) were consumed by nothing: two
otherwise-identical accounts differing only in fee projected to a byte-identical
balance. #691 wired ONE canonical spelling — a per-account ``mer`` on the
contract account schema — into the growth rule, subtracting each fee-flagged
account's balance-weighted MER from the pot's gross rate before compounding.

#136 fixes two defects in #691's implementation:
  1. **Silent zero (DP#32):** an account that opens at $0 with a declared MER
     paid ZERO fee forever, because the fee numerator (``weighted_mer_sum``)
     was ``0 * mer = 0``. Now ``mer_rate`` is the MER rate (not a frozen
     weighted sum), so once contributions fund the account it pays
     ``mer_rate * pot_total`` each year.
  2. **Fee decay:** for funded accounts the fee numerator was frozen at load
     time while the pot total grew, so the effective fee rate decayed toward
     zero over the horizon. Now ``mer_rate`` is a constant rate subtracted from
     the gross rate (``net = gross - mer_rate``), so the fee grows
     proportionally with the pot — no decay.

The MER rate (``mer_rate``) is computed at contract-load time as the
balance-weighted average of declared MERs across ALL accounts of a kind (not
just MER-flagged ones), so the fee is correct in year 1 (the rate times the
opening pot equals the old frozen ``weighted_mer_sum``) and constant in year 2+
(the rate does not decay). When every MER-flagged account opens at $0,
``mer_rate`` falls back to the max declared MER for a SINGLE-flagged pot
(every account of the kind declares a MER — the pot IS the flagged money),
or 0.0 for a MIXED pot (non-flagged money coexists — charging it would tax
money that never declared a fee).

Absence is a strict no-op: a household that declares no ``mer`` gets today's
behaviour (the golden invariant is unchanged — the golden household declares no
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

    def test_mer_subtracted_as_constant_rate(self):
        # Issue #136: the MER is a constant RATE (mer_rate), not a frozen
        # weighted sum. A $40k slice of a $100k RRSP pot carries a 1.00% MER.
        # mer_rate = weighted_mer_sum / kind_total = 400 / 100000 = 0.004
        # net = 0.07 - 0.004 = 0.066 (same as the old frozen approach in year 1,
        # but the rate stays 0.004 in year 2+ — no decay).
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_rate': 0.004},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.066)

    def test_whole_pot_mer_subtracts_full_fee(self):
        # The whole $100k pot carries a 1.16% MER -> net = 0.07 - 0.0116.
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_rate': 0.0116},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07 - 0.0116)

    def test_mer_for_different_kind_does_not_affect_this_pot(self):
        cfg = _config(account_mer_drag={
            'tfsa': {'mer_rate': 0.01},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)

    def test_zero_pot_returns_global(self):
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_rate': 0.004},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 0.0), 0.07)

    def test_zero_mer_is_no_op(self):
        """DP#32: an explicit 0.0 MER is fee-free, identical to no MER -- not a
        source of divergence."""
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_rate': 0.0},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)

    def test_mer_composes_with_expected_return_override(self):
        # An account with BOTH a 7.3% expected_return and a 1.0% MER on a
        # $25k slice of a $100k pot. The MER rate (mer_rate) is the weighted
        # average across all accounts: 25000*0.01 / 100000 = 0.0025.
        # gross blend = (25000*0.073 + 75000*0.07)/100000 = 0.07075
        # net = 0.07075 - 0.0025 = 0.06825
        cfg = _config(
            account_return_overrides={
                'rrsp': {'override_balance': 25_000,
                         'weighted_rate_sum': 25_000 * 0.073},
            },
            account_mer_drag={
                'rrsp': {'mer_rate': 0.0025},
            },
        )
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.06825)

    def test_mer_rate_does_not_decay_as_pot_grows(self):
        """Issue #136 defect #2: the fee rate must NOT decay as the pot grows.
        The old frozen approach charged a fixed dollar amount (weighted_mer_sum)
        spread across the pot, so the effective rate decayed. Now mer_rate is a
        constant rate: the net rate is the same whether the pot is $100k or
        $200k."""
        cfg = _config(account_mer_drag={
            'rrsp': {'mer_rate': 0.004},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        rate_at_100k = _blended_pot_rate(ctx, 'rrsp', 100_000)
        rate_at_200k = _blended_pot_rate(ctx, 'rrsp', 200_000)
        # The rate is constant (0.066) regardless of pot size — no decay.
        self.assertAlmostEqual(rate_at_100k, 0.066)
        self.assertAlmostEqual(rate_at_200k, 0.066)


# ── Behaviour: two households diverge on fee alone (the acceptance criterion) ─

class TestRegisteredGrowthAppliesMer(unittest.TestCase):
    def test_higher_mer_grows_slower(self):
        """The issue's acceptance test: two RRSP pots, same $100k balance, same
        gross rate, differ ONLY in MER -> their post-growth balances diverge by
        the compounded fee. Today (before the fix) they were byte-identical."""
        cfg_cheap = _config(account_mer_drag={
            'rrsp': {'mer_rate': 0.0020},
        })
        cfg_dear = _config(account_mer_drag={
            'rrsp': {'mer_rate': 0.0116},
        })
        ws_cheap = YearWorkingState(year=0)
        ws_cheap.new_rrsp_bal = 100_000
        ws_dear = YearWorkingState(year=0)
        ws_dear.new_rrsp_bal = 100_000
        RULES['registered_growth'](ws_cheap, _ctx(cfg_cheap, investment_return=0.07))
        RULES['registered_growth'](ws_dear, _ctx(cfg_dear, investment_return=0.07))
        # Cheap fund: 100000 * (1 + 0.07 - 0.002) = 100000 * 1.068
        # Dear fund:  100000 * (1 + 0.07 - 0.0116) = 100000 * 1.0584
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
            'rrsp': {'mer_rate': 0.004},
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
        # mer_rate is the balance-weighted average of declared MERs across ALL
        # RRSP accounts: p1_rrsp ($210k, mer=0.0116) + p2_rrsp ($95k, no mer).
        #   mer_rate = (210000 * 0.0116) / (210000 + 95000)
        #           = 2436 / 305000
        #           = 0.007983606557377049...
        self.assertIn("mer_rate", m)
        self.assertAlmostEqual(m["mer_rate"],
                               210000 * 0.0116 / (210000 + 95000))

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
        recorded with mer_rate = 0.0 -- it reaches the config but does not
        move the rate (fee-free, not unknown)."""
        doc = _load_example_doc()
        rrsp = next(a for a in doc["accounts"]
                    if a["kind"] == "rrsp" and a["owner"] == "p1")
        rrsp["mer"] = 0.0
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIn("rrsp", cfg.account_mer_drag)
        self.assertAlmostEqual(cfg.account_mer_drag["rrsp"]["mer_rate"], 0.0)

    def test_zero_opening_balance_single_flagged_pot_records_mer_rate(self):
        """Issue #136 defect #1: an account that opens at $0 with a declared MER
        must record a non-zero mer_rate (not silently zero) WHEN the pot is
        single-flagged (every account of the kind declares a MER — the pot IS
        the flagged money). The mer_rate falls back to the max declared MER."""
        doc = _load_example_doc()
        # Make BOTH RRSP accounts declare a MER (single-flagged pot) and set
        # both to $0 so the $0-fallback fires.
        for a in doc["accounts"]:
            if a["kind"] == "rrsp":
                a["mer"] = 0.0116
                a["balance"]["amount"] = 0.0
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIn("rrsp", cfg.account_mer_drag)
        # mer_rate is 0.0116 (the declared MER), NOT 0.0 (which would be the
        # silent-zero DP#32 violation the old frozen-weight approach produced).
        self.assertAlmostEqual(cfg.account_mer_drag["rrsp"]["mer_rate"], 0.0116)

    def test_zero_opening_balance_mixed_pot_records_zero_mer_rate(self):
        """Issue #136 review finding 1: when a $0 MER-flagged account coexists
        with a non-flagged account (MIXED pot), mer_rate MUST be 0.0 — not the
        max declared MER. Charging the non-flagged money a fee it never
        declared is the bug this fixes. The $0 flagged account pays $0 because
        B=0 (correct, not a silent zero); the non-flagged money is fee-free."""
        doc = _load_example_doc()
        rrsp = next(a for a in doc["accounts"]
                    if a["kind"] == "rrsp" and a["owner"] == "p1")
        rrsp["mer"] = 0.0116
        rrsp["balance"]["amount"] = 0.0
        # p2_rrsp ($95k) has no MER — the pot is mixed.
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIn("rrsp", cfg.account_mer_drag)
        # mer_rate is 0.0: the non-flagged $95k must NOT be charged 0.0116.
        # The $0 flagged account pays $0 because B=0 (correct, not a silent
        # zero — the engine's per-owner-pot model cannot route contributions
        # to a specific $0 sub-account, so the flagged share stays $0).
        self.assertAlmostEqual(cfg.account_mer_drag["rrsp"]["mer_rate"], 0.0)


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
        # The golden household's RRSP pot has $300k (primary) + $150k (spouse).
        # This test injects mer_rate = 0.0116 directly into the config (bypassing
        # the adapter) to apply a 1.16% MER to the ENTIRE RRSP pot — the scenario
        # where the whole pot is fee-flagged. The fee is mer_rate * pot_total
        # each year (dynamic, no decay). Note: 450_000 * 0.0116 / 450_000 simplifies
        # to 0.0116 (the expression is written explicitly to show the formula;
        # it is NOT the adapter's weighted average, which would be
        # 300k*0.0116/450k ≈ 0.00773 if only the primary's RRSP declared MER).
        variant['accounts']['mer_drag'] = {
            'rrsp': {'mer_rate': 0.0116},
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
