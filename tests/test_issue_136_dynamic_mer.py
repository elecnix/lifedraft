#!/usr/bin/env python3
"""Tests for issue #136: per-account MER drag must follow the money, not the
load-time snapshot.

Before this fix the MER drag was a frozen dollar figure computed once from the
DECLARED opening balances (``weighted_mer_sum``) and divided by the CURRENT pot
total each year (``_blended_pot_rate``). Two consequences:

1. *Zero-opening-balance account.* An account declared ``balance: 0, mer: 0.005``
   contributed ``0 * 0.005 = 0`` to the frozen sum forever -> its declared fee
   NEVER applied, even after the account was funded by later contributions
   (the issue's primary bug -- a silent zero).
2. *Dilution.* For a funded account the frozen dollar numerator stayed put while
   the pot total grew, so the EFFECTIVE fee rate decayed toward zero over the
   horizon.

This PR stores the drag as a FIXED RATE (``fee_rate`` x ``fee_share``, both from
the declared contract) so the fee prices the pot every year: no freezing at
opening, no dilution. A pot-kind whose fee accounts all open zero-balance gets
``fee_share = 1`` (the declared fee account is the whole pot once funded).

Golden no-op: a household declaring no ``mer`` produces byte-identical output
(the golden invariant 9709753.139463063 is unchanged -- the golden household
declares no MER). DP#32: an explicit 0.0 fee is a declared fact that moves no
rate (recorded, not assumed). All test data uses fabricated round numbers
(DP#13/DP#15).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contract_schema
import input_contract as ic
from simulation_config import SimulationConfig
from rule_registry import RULES, RuleContext, YearWorkingState
from rules_contributions import _blended_pot_rate


def _load_example_doc():
    """The shipped contract example, trimmed to the subset the adapter maps
    (same helper tests/test_issue_691_mer.py uses)."""
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


# ── The drag is a RATE, so it neither freezes at opening nor dilutes ─────────

class TestFeeDragIsARate(unittest.TestCase):
    def test_fee_rate_does_not_depend_on_pot_dollars(self):
        """Issue #136, dilution: the same fee entry must yield the SAME net pot
        rate whether the pot holds $100k or $1,000,000. The old spelling
        (weighted_mer_sum / pot_total) thinned the fee as the pot grew; the
        new one is a rate (fee_share x fee_rate)."""
        fee = {
            'mer_balance': 40_000, 'weighted_mer_sum': 40_000 * 0.01,
            'fee_share': 0.4, 'fee_rate': 0.01,
        }
        small = _ctx(_config(account_mer_drag={'rrsp': fee}), investment_return=0.07)
        big = _ctx(_config(account_mer_drag={'rrsp': fee}), investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(small, 'rrsp', 100_000), 0.066)
        self.assertAlmostEqual(_blended_pot_rate(big, 'rrsp', 2_000_000), 0.066)
        # And the growth rule compounds at that rate for a tiny pot and a huge
        # pot identically (no dollar-scale dependence).
        ws = YearWorkingState(year=0); ws.new_rrsp_bal = 2_000_000
        RULES['registered_growth'](ws, _ctx(_config(account_mer_drag={'rrsp': fee})))
        self.assertAlmostEqual(ws.new_rrsp_bal, 2_000_000 * 1.066)

    def test_zero_opening_fee_account_is_the_pot(self):
        """Issue #136, B1: a kind whose fee accounts open at zero gets
        fee_share = 1 (the fee account is the whole pot once funded). The rate
        is the DECLARED fee even though the opening weighted sum is $0 -- the
        old frozen-dollar spelling recorded a permanent $0 and never applied
        the fee."""
        fee = {
            'mer_balance': 0.0, 'weighted_mer_sum': 0.0,
            'fee_share': 1.0, 'fee_rate': 0.005,
        }
        cfg = _config(account_mer_drag={'tfsa': fee})
        ctx = _ctx(cfg, investment_return=0.07)
        # A $0 balance pays nothing on the pot of zero...
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'tfsa', 0.0), 0.07)
        # ... but a subsequently-funded pot pays the fee in FULL: year 1 money
        # pays 0.5%, exactly what a household holding $10k in the account pays.
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'tfsa', 10_000), 0.065)
        # And the fee stays on the same RATE as the pot swells to six figures.
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'tfsa', 150_000), 0.065)

    def test_absent_fee_is_still_a_no_op(self):
        cfg = _config()  # no account_mer_drag
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)

    def test_explicit_zero_fee_moves_no_rate(self):
        """DP#32: an explicit 0.0 fee is a declared fact (distinct from null)
        and is recorded with fee_rate 0 -- it reaches the config but does not
        move the rate (fee-free, not unknown)."""
        fee = {'mer_balance': 100_000, 'weighted_mer_sum': 0.0,
               'fee_share': 1.0, 'fee_rate': 0.0}
        ctx = _ctx(_config(account_mer_drag={'rrsp': fee}), investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)


# ── Contract mapping: fee_share/fee_rate are derived from DECLARED balances ──

class TestContractMappingDerivesRateAndShare(unittest.TestCase):
    def test_zero_opening_account_maps_to_whole_pot_share(self):
        """B1 at the adapter: a brand-new zero-balance account declaring a fee
        becomes fee_share=1 with the DECLARED fee as the rate -- so once the
        pot is funded the whole pot pays it (not $0 forever)."""
        doc = _load_example_doc()
        # EVERY rrsp-kind account becomes a zero-opening fee account: a
        # brand-new, not-yet-funded portfolio whose declared fee must survive
        # once contributions arrive (issue #136).
        for a in doc["accounts"]:
            if a["kind"] == "rrsp":
                a["balance"] = {"amount": 0.0, "as_of": "2026-06-30"}
                a["mer"] = 0.005
        contract_schema.validate_contract(doc)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        m = cfg.account_mer_drag["rrsp"]
        self.assertAlmostEqual(m["mer_balance"], 0.0)
        self.assertAlmostEqual(m["fee_rate"], 0.005)    # declared fee kept
        self.assertAlmostEqual(m["fee_share"], 1.0)     # sole account -> whole pot

    def test_partial_fee_account_gets_declared_share(self):
        """A $30k fee account inside a $100k pot -> fee_share = 0.3, fee_rate
        = its 1% -- the drag is 30bp of the pot's return, constant as the pot
        moves (no dilution, no freeze)."""
        doc = _load_example_doc()
        # The household's rrsp pot is exactly $100k: a $30k fee-bearing slice
        # ($30k at 1% = 0.01) + a $70k plain one (no fee). The declared share
        # must come out 0.3 regardless of the pot's future size.
        plain = dict(doc["accounts"][0])
        plain["id"] = "p1_rrsp_plain"
        plain["balance"] = {"amount": 70_000.0, "as_of": "2026-06-30"}
        plain["mer"] = None
        fee = dict(plain)
        fee["id"] = "p1_rrsp_fee"
        fee["balance"] = {"amount": 30_000.0, "as_of": "2026-06-30"}
        fee["mer"] = 0.01
        doc["accounts"] = [a for a in doc["accounts"] if a["kind"] != "rrsp"]
        doc["accounts"].extend([plain, fee])
        contract_schema.validate_contract(doc)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        m = cfg.account_mer_drag["rrsp"]
        self.assertAlmostEqual(m["fee_rate"], 0.01)
        self.assertAlmostEqual(m["fee_share"], 30_000 / 100_000)

    def test_new_fee_keys_survive_config_round_trip(self):
        """DP#24: the internal config round-trips fee_share/fee_rate -- the
        growth rule's read spelling survives to_dict/from_dict (config_serde
        passes the dict through untouched)."""
        doc = _load_example_doc()
        for a in doc["accounts"]:
            if a["kind"] == "rrsp":
                a["mer"] = 0.0116
        contract_schema.validate_contract(doc)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        m = cfg.account_mer_drag["rrsp"]
        self.assertIn("fee_share", m)
        self.assertIn("fee_rate", m)
        cfg2 = SimulationConfig.from_dict(cfg.to_dict())
        self.assertEqual(cfg2.account_mer_drag, cfg.account_mer_drag)

    def test_zero_balance_account_with_explicit_free_fee_is_no_op(self):
        """DP#32: a brand-new account that DECLARES fee-free (mer: 0.0) with a
        $0 opening balance is a declared fact, not a trap: it maps to
        fee_rate=0 and fee_share=0, so once funded the pot stays at the global
        rate (fee-free, never a silent fee)."""
        doc = _load_example_doc()
        for a in doc["accounts"]:
            if a["kind"] == "rrsp":
                a["balance"] = {"amount": 0.0, "as_of": "2026-06-30"}
                a["mer"] = 0.0
        contract_schema.validate_contract(doc)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        m = cfg.account_mer_drag["rrsp"]
        self.assertAlmostEqual(m["fee_rate"], 0.0)
        self.assertAlmostEqual(m["fee_share"], 0.0)
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 50_000), 0.07)

    def test_no_mer_declared_keeps_empty_drag(self):
        """Golden: no account declares a fee -> mer_drag stays empty -> the
        growth pipeline is byte-identical (DP#32)."""
        doc = _load_example_doc()
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        self.assertEqual(cfg.account_mer_drag, {})


# ── Engine integration (DP#11): the fee follows into terminal assets ─────────

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from test_golden_trajectory_581 import golden_household_config


def _run(cfg: dict):
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()


class TestZeroFeeAccountReachesEngineOutput(unittest.TestCase):
    def test_zero_balance_fee_account_drags_once_funded(self):
        """The issue's acceptance case: a TFSA that opens at $0 with a declared
        0.5% fee and is funded by the run must PAY the fee. Since the account
        is the pot (fee_share = 1), the whole TFSA pot now grows at gross-0.5%
        and the household ends poorer than the identical fee-free household.
        Under the OLD frozen-dollar spelling (weighted_mer_sum = 0) these two
        terminal totals were IDENTICAL."""
        base_cfg = golden_household_config()
        fee_free = _run(base_cfg)[-1].total_assets
        variant = copy.deepcopy(base_cfg)
        # The mapping would derive exactly this for a zero-opening fee account.
        variant['accounts']['mer_drag'] = {
            'tfsa': {'mer_balance': 0.0, 'weighted_mer_sum': 0.0,
                     'fee_share': 1.0, 'fee_rate': 0.005},
        }
        fee_drag = _run(variant)[-1].total_assets
        # Fee applies even though the account opened empty and every contribution
        # arrived later => terminal is strictly lower, never equal.
        self.assertLess(fee_drag, fee_free)

    def test_golden_no_op_unchanged(self):
        """DP#32: the golden household declares no MER -> adding an empty
        mer_drag changes nothing (byte-identical terminal)."""
        base_cfg = golden_household_config()
        base_terminal = _run(base_cfg)[-1].total_assets
        variant = copy.deepcopy(base_cfg)
        variant['accounts']['mer_drag'] = {}
        self.assertEqual(base_terminal, _run(variant)[-1].total_assets)
