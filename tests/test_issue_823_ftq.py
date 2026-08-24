#!/usr/bin/env python3
"""Tests for issue #823: per-account expected_return override + illiquidity
(locked_until) for Fonds FTQ modelling.

Two behaviours:
  (a) An account with ``expected_return`` grows at its own rate (blended
      balance-weighted into its pot) while unflagged accounts in the same pot
      use the global ``investment_return``.
  (b) An account with ``locked_until`` (an unlock age) is EXCLUDED from the
      solvency liquidation waterfall (and thus from runway) before the owner
      reaches the unlock age, and INCLUDED after.

The LSIF 30% credit itself is already modelled (countries/canada/lsif_credit.py)
and is NOT re-tested here. These tests cover only the two gaps #823 closes.

All test data uses fabricated round numbers per DP#13/DP#15.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import contract_accounts
import contract_errors
import contract_schema
import input_contract as ic
from simulation_config import SimulationConfig
from rule_registry import RULES, RuleContext, YearWorkingState
from rules_contributions import _blended_pot_rate
from rules_solvency import _still_locked


def _load_example_doc():
    """The shipped contract example, trimmed to the two-generation subset the
    adapter maps (same helper tests/test_input_contract.py uses)."""
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

# ── Helpers ─────────────────────────────────────────────────────────────────

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


def _ctx(config, *, investment_return=0.07, calendar_year=2026,
         primary_marginal_rate=0.40, living_costs=0.0, after_tax_income=0.0):
    """A RuleContext seeded for direct rule invocation (the pattern in
    test_issue_584_rules_registry.py)."""
    return RuleContext(
        year=0, calendar_year=calendar_year, allocations={}, config=config,
        investment_return=investment_return, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=primary_marginal_rate, spouse_marginal_rate=0.0,
        resp_data=None, fhsa_contribution=0.0, rrsp_annual_limit=None,
        tfsa_annual_limit=None, fhsa_annual_limit=None,
        non_reg_after_tax_return=None, cpp_income=0.0, oas_income=0.0,
        pension_income=0.0, drawdown_order=None, rrif_min_rate_primary=0.0,
        rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0, retiree_marginal_rate=0.0,
        drawdown_bracket_target=None, drawdown_other_taxable_income=0.0,
        living_costs=living_costs, after_tax_income=after_tax_income,
    )


# ── (a) per-account expected_return override ────────────────────────────────

class TestBlendedPotRate(unittest.TestCase):
    def test_no_override_returns_global_rate(self):
        cfg = _config()  # no account_return_overrides
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)

    def test_override_blended_balance_weighted(self):
        # An FTQ account: $25k of a $100k RRSP pot declares 7.3%.
        # Blend = (25000*0.073 + 75000*0.07) / 100000 = (1825 + 5250) / 100000
        #       = 7075 / 100000 = 0.07075
        cfg = _config(account_return_overrides={
            'rrsp': {'override_balance': 25_000, 'weighted_rate_sum': 25_000 * 0.073},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07075)

    def test_override_smaller_than_pot_blends_rest_at_global(self):
        cfg = _config(account_return_overrides={
            'rrsp': {'override_balance': 10_000, 'weighted_rate_sum': 10_000 * 0.073},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        # (10000*0.073 + 90000*0.07) / 100000 = (730 + 6300)/100000 = 0.0703
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.0703)

    def test_override_for_different_kind_does_not_affect_this_pot(self):
        cfg = _config(account_return_overrides={
            'tfsa': {'override_balance': 50_000, 'weighted_rate_sum': 50_000 * 0.09},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        # RRSP pot has no override -> global rate.
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)

    def test_zero_pot_returns_global(self):
        cfg = _config(account_return_overrides={
            'rrsp': {'override_balance': 25_000, 'weighted_rate_sum': 25_000 * 0.073},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 0.0), 0.07)

    def test_nonpositive_override_balance_returns_global(self):
        # A zero/negative override balance (e.g. an empty flagged account)
        # does not move the pot rate -- guard against div-by-zero / a
        # nonsensical blend.
        cfg = _config(account_return_overrides={
            'rrsp': {'override_balance': 0.0, 'weighted_rate_sum': 0.0},
        })
        ctx = _ctx(cfg, investment_return=0.07)
        self.assertAlmostEqual(_blended_pot_rate(ctx, 'rrsp', 100_000), 0.07)


class TestRegisteredGrowthAppliesOverride(unittest.TestCase):
    def test_ftq_account_grows_at_its_own_rate(self):
        """A pot with an FTQ override grows at the blended rate, not the global
        rate -- so the post-growth balance is higher than it would be at 7%."""
        cfg_override = _config(account_return_overrides={
            'rrsp': {'override_balance': 25_000, 'weighted_rate_sum': 25_000 * 0.073},
        })
        cfg_plain = _config()
        # Seed an RRSP pot of $100k in both, grow one year.
        ws_over = YearWorkingState(year=0)
        ws_over.new_rrsp_bal = 100_000
        ws_plain = YearWorkingState(year=0)
        ws_plain.new_rrsp_bal = 100_000
        RULES['registered_growth'](ws_over, _ctx(cfg_override, investment_return=0.07))
        RULES['registered_growth'](ws_plain, _ctx(cfg_plain, investment_return=0.07))
        # Plain grows at exactly 7%: 107000.
        self.assertAlmostEqual(ws_plain.new_rrsp_bal, 107_000)
        # Override grows at the blend 0.07075: 100000 * 1.07075 = 107075.
        self.assertAlmostEqual(ws_over.new_rrsp_bal, 100_000 * 1.07075)
        # The override balance is strictly higher (FTQ's 7.3% lifts the pot).
        self.assertGreater(ws_over.new_rrsp_bal, ws_plain.new_rrsp_bal)

    def test_no_override_growth_unchanged(self):
        """Without an override the growth is exactly the global rate (golden-
        invariant-shaped: no override -> today's behaviour)."""
        cfg = _config()
        ws = YearWorkingState(year=0)
        ws.new_rrsp_bal = 100_000
        ws.new_tfsa_p_bal = 50_000
        RULES['registered_growth'](ws, _ctx(cfg, investment_return=0.07))
        self.assertAlmostEqual(ws.new_rrsp_bal, 107_000)
        self.assertAlmostEqual(ws.new_tfsa_p_bal, 53_500)


# ── (b) illiquidity: locked_until excluded from solvency before unlock ──────

class TestStillLocked(unittest.TestCase):
    def test_no_locked_returns_zero(self):
        cfg = _config()  # no account_locked
        ctx = _ctx(cfg, calendar_year=2026)
        self.assertEqual(_still_locked(ctx, 'rrsp'), 0.0)

    def test_locked_before_unlock_age(self):
        # Owner born 1990, unlock age 65 -> locked through 2054 (age 64),
        # liquid from 2055 (age 65). In 2026 the owner is 36 -> locked.
        cfg = _config(account_locked={
            'rrsp': [{'balance': 20_000, 'unlock_age': 65,
                      'owner_birth_year': 1990}],
        })
        ctx = _ctx(cfg, calendar_year=2026)
        self.assertEqual(_still_locked(ctx, 'rrsp'), 20_000)

    def test_liquid_after_unlock_age(self):
        cfg = _config(account_locked={
            'rrsp': [{'balance': 20_000, 'unlock_age': 65,
                      'owner_birth_year': 1990}],
        })
        # 2055: owner turns 65 -> balance is liquid (not locked).
        ctx = _ctx(cfg, calendar_year=2055)
        self.assertEqual(_still_locked(ctx, 'rrsp'), 0.0)

    def test_unlock_age_boundary_is_liquid_at_that_age(self):
        cfg = _config(account_locked={
            'rrsp': [{'balance': 20_000, 'unlock_age': 65,
                      'owner_birth_year': 1990}],
        })
        # Age exactly 65 (2055) -> liquid. Age 64 (2054) -> locked.
        self.assertEqual(_still_locked(_ctx(cfg, calendar_year=2054), 'rrsp'),
                         20_000)
        self.assertEqual(_still_locked(_ctx(cfg, calendar_year=2055), 'rrsp'),
                         0.0)

    def test_locked_for_different_kind_does_not_affect_this_pot(self):
        cfg = _config(account_locked={
            'tfsa': [{'balance': 30_000, 'unlock_age': 65,
                      'owner_birth_year': 1990}],
        })
        ctx = _ctx(cfg, calendar_year=2026)
        # RRSP has no locked accounts -> 0.
        self.assertEqual(_still_locked(ctx, 'rrsp'), 0.0)


class TestSolvencyExcludesLockedBalance(unittest.TestCase):
    """Issue #823: a locked balance is excluded from the solvency liquidation
    waterfall before the unlock age (so runway is shorter), and included after.

    apply_solvency runs the waterfall only when there is a shortfall
    (required - available > 0). We manufacture a shortfall and inspect the
    registered-source draw: before unlock the locked balance cannot be drawn;
    after unlock it can.
    """

    def _run_solvency(self, *, locked, calendar_year, rrsp_balance,
                      living_costs=200_000, after_tax_income=0.0):
        cfg = _config(account_locked=locked)
        ctx = _ctx(cfg, calendar_year=calendar_year,
                   living_costs=living_costs, after_tax_income=after_tax_income,
                   primary_marginal_rate=0.40)
        ws = YearWorkingState(year=0)
        ws.new_rrsp_bal = rrsp_balance
        # A shortfall of `living_costs` (no income, no debt service, no
        # contributions) forces the waterfall to liquidate registered.
        ws.solvency_after_tax_income = after_tax_income
        fired = RULES['solvency'](ws, ctx)
        return ws, fired

    def test_locked_balance_not_drawn_before_unlock(self):
        # $30k RRSP, ALL of it locked until 65 (owner born 1990, year 2026).
        locked = {'rrsp': [{'balance': 30_000, 'unlock_age': 65,
                            'owner_birth_year': 1990}]}
        ws, fired = self._run_solvency(
            locked=locked, calendar_year=2026, rrsp_balance=30_000,
            living_costs=200_000)
        # The waterfall fired (there is a shortfall) but could NOT draw the
        # locked registered balance -> the registered source contributed 0
        # and the shortfall remains uncovered.
        self.assertTrue(fired)
        self.assertAlmostEqual(ws.new_rrsp_bal, 30_000)  # untouched
        self.assertGreater(ws.solvency_shortfall, 0.0)
        self.assertEqual(ws.solvency_covered, 0.0)

    def test_liquid_balance_drawn_after_unlock(self):
        # Same $30k RRSP, but in 2055 (owner is 65) -> liquid.
        locked = {'rrsp': [{'balance': 30_000, 'unlock_age': 65,
                            'owner_birth_year': 1990}]}
        ws, fired = self._run_solvency(
            locked=locked, calendar_year=2055, rrsp_balance=30_000,
            living_costs=200_000)
        self.assertTrue(fired)
        # After unlock the registered balance IS drawn toward the shortfall.
        self.assertLess(ws.new_rrsp_bal, 30_000)
        self.assertGreater(ws.solvency_covered, 0.0)

    def test_no_locked_balance_drawn_normally(self):
        # No illiquidity declared -> the full registered balance is liquid
        # (today's behaviour; the solvency waterfall draws it).
        ws, fired = self._run_solvency(
            locked={}, calendar_year=2026, rrsp_balance=30_000,
            living_costs=200_000)
        self.assertTrue(fired)
        self.assertLess(ws.new_rrsp_bal, 30_000)
        self.assertGreater(ws.solvency_covered, 0.0)


# ── Config round-trip (DP#24) ───────────────────────────────────────────────

class TestConfigRoundTrip(unittest.TestCase):
    def test_overrides_round_trip(self):
        cfg = _config(
            account_return_overrides={
                'rrsp': {'override_balance': 25_000,
                         'weighted_rate_sum': 25_000 * 0.073},
            },
            account_locked={
                'rrsp': [{'balance': 20_000, 'unlock_age': 65,
                          'owner_birth_year': 1990}],
            },
        )
        d = cfg.to_dict()
        self.assertIn('return_overrides', d['accounts'])
        self.assertIn('locked', d['accounts'])
        # Reload and verify the maps survive.
        cfg2 = SimulationConfig.from_dict(d)
        self.assertEqual(cfg2.account_return_overrides,
                         cfg.account_return_overrides)
        self.assertEqual(cfg2.account_locked, cfg.account_locked)

    def test_no_overrides_do_not_emit(self):
        cfg = _config()
        d = cfg.to_dict()
        # Absence-safe: an empty override/locked map round-trips to 'absent'
        # (no key emitted), the same convention as lira / equity_grants.
        self.assertNotIn('return_overrides', d['accounts'])
        self.assertNotIn('locked', d['accounts'])
        cfg2 = SimulationConfig.from_dict(d)
        self.assertEqual(cfg2.account_return_overrides, {})
        self.assertEqual(cfg2.account_locked, {})


# ── Contract mapping: _map_account_overrides via to_internal_config ─────────

class TestContractMapping(unittest.TestCase):
    """Issue #823: per-account expected_return / locked_until on the contract
    account schema flow through to_internal_config into the config's override
    maps (and are absent when no account declares them -- golden)."""

    def test_override_and_lock_flow_through_to_internal_config(self):
        doc = _load_example_doc()
        # Find p1's first rrsp account and flag it as FTQ: 7.3% return, locked
        # until age 65. Fabricated round numbers (DP#15); the example doc's
        # own balances are already fabricated.
        rrsp = next(a for a in doc["accounts"]
                    if a["kind"] == "rrsp" and a["owner"] == "p1")
        rrsp["expected_return"] = 0.073
        rrsp["locked_until"] = {"age": 65}
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        # The rrsp override pot is populated.
        self.assertIn("rrsp", cfg.account_return_overrides)
        ov = cfg.account_return_overrides["rrsp"]
        self.assertAlmostEqual(ov["override_balance"], rrsp["balance"]["amount"])
        self.assertAlmostEqual(ov["weighted_rate_sum"],
                               rrsp["balance"]["amount"] * 0.073)
        # The locked map carries the balance + unlock age + owner birth year.
        self.assertIn("rrsp", cfg.account_locked)
        entry = cfg.account_locked["rrsp"][0]
        self.assertEqual(entry["unlock_age"], 65)
        self.assertEqual(entry["balance"], rrsp["balance"]["amount"])
        # Owner birth year resolved from p1's birth_date (DP#1).
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        self.assertEqual(entry["owner_birth_year"], int(p1["birth_date"][:4]))

    def test_no_override_no_lock_is_absent(self):
        """The unmodified example declares no per-account override / lock --
        the config's override maps are empty (golden: today's global-rate,
        fully-liquid behaviour, DP#32)."""
        doc = _load_example_doc()
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(cfg.account_return_overrides, {})
        self.assertEqual(cfg.account_locked, {})

    def test_locked_until_without_owner_birth_date_is_rejected(self):
        """DP#1/DP#32: a locked_until with no owner birth_date to compute the
        unlock AGE from must fail loudly, not default to a plausible person."""
        doc = _load_example_doc()
        rrsp = next(a for a in doc["accounts"]
                    if a["kind"] == "rrsp" and a["owner"] == "p1")
        rrsp["locked_until"] = {"age": 65}
        # birth_date is schema-required on people, so we cannot drop it and
        # still pass validate_contract; instead exercise the helper directly
        # with a synthetic doc whose owner is absent from people.
        with self.assertRaises(contract_errors.ContractAdaptationError):
            contract_accounts._map_account_overrides(
                {"accounts": [{"id": "x", "kind": "rrsp", "owner": "ghost",
                               "balance": {"amount": 1000},
                               "locked_until": {"age": 65}}],
                 "people": []})

    def test_locked_until_date_form_converts_to_age(self):
        """``locked_until: {date: '2055-01-01'}`` resolves to the owner's age at
        that date (DP#1: a date condition and an age condition are the same
        fact once the owner's birth_year is known)."""
        doc = _load_example_doc()
        rrsp = next(a for a in doc["accounts"]
                    if a["kind"] == "rrsp" and a["owner"] == "p1")
        rrsp["locked_until"] = {"date": "2055-06-01"}
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        expected_age = 2055 - int(p1["birth_date"][:4])
        self.assertEqual(cfg.account_locked["rrsp"][0]["unlock_age"],
                         expected_age)

    def test_locked_until_neither_age_nor_date_is_rejected(self):
        with self.assertRaises(contract_errors.ContractAdaptationError):
            contract_accounts._map_account_overrides(
                {"accounts": [{"id": "x", "kind": "rrsp", "owner": "p1",
                               "balance": {"amount": 1000},
                               "locked_until": {}}],
                 "people": [{"id": "p1", "birth_date": "1990-01-01"}]})

    def test_locked_until_owner_without_birth_date_is_rejected(self):
        """The owner IS in people but has no birth_date -- the unlock AGE
        still cannot be computed (DP#1/DP#32)."""
        with self.assertRaises(contract_errors.ContractAdaptationError):
            contract_accounts._map_account_overrides(
                {"accounts": [{"id": "x", "kind": "rrsp", "owner": "p1",
                               "balance": {"amount": 1000},
                               "locked_until": {"age": 65}}],
                 "people": [{"id": "p1", "birth_date": None}]})

    def test_expected_return_null_is_absent(self):
        """An explicit null expected_return is absent (not a zero override) --
        DP#32: null means 'use the global rate', not 'grow at 0%'."""
        doc = _load_example_doc()
        rrsp = next(a for a in doc["accounts"]
                    if a["kind"] == "rrsp" and a["owner"] == "p1")
        rrsp["expected_return"] = None
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        # No override recorded for rrsp -- null is absent, not zero.
        self.assertNotIn("rrsp", cfg.account_return_overrides)


# ── Integration tests: override + lock reach engine output (DP#11/DP#18) ──
#
# The unit tests above call RULES['registered_growth'](ws, ctx) and
# RULES['solvency'](ws, ctx) directly on hand-built state, which verifies each
# rule's contract in isolation. DP#11 says unit tests verify a module's
# contract; integration tests verify composition. DP#18 says an overlay (here:
# a declared expected_return override or locked_until on an account) must
# change the engine's observable output, not merely populate a config field
# that nothing reads. These tests drive FamilySimulation.run() end-to-end and
# assert that each feature measurably changes the result versus the baseline.

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


class TestOverrideReachesEngineOutput(unittest.TestCase):
    """DP#18: a declared expected_return override on a registered account must
    change the household's terminal assets versus the global-rate baseline —
    not merely blend into a hand-built pot rate in a unit test."""

    def test_override_raises_terminal_assets_versus_baseline(self):
        """A household declaring a 7.3% expected_return on part of its RRSP ends
        strictly richer than the same household at the 7% global rate. The
        override lifts the blended pot rate, compounding more every year."""
        base_cfg = golden_household_config()
        variant = copy.deepcopy(base_cfg)
        # The golden household's primary RRSP is $300k. Override part of it
        # at 7.3% while the global rate is 7%.
        variant['accounts']['return_overrides'] = {
            'rrsp': {'override_balance': 300_000,
                     'weighted_rate_sum': 300_000 * 0.073},
        }
        base_terminal = _run(base_cfg)[-1].total_assets
        variant_terminal = _run(variant)[-1].total_assets
        # The override lifts the RRSP pot rate, compounding more every year:
        # the variant must end strictly richer.
        self.assertGreater(variant_terminal, base_terminal)

    def test_absence_is_no_op_golden_unchanged(self):
        """DP#32: the golden household declares no override, so adding an empty
        return_overrides dict does not change its terminal total_assets."""
        base_cfg = golden_household_config()
        variant = copy.deepcopy(base_cfg)
        variant['accounts']['return_overrides'] = {}  # explicit empty
        base_terminal = _run(base_cfg)[-1].total_assets
        variant_terminal = _run(variant)[-1].total_assets
        self.assertEqual(base_terminal, variant_terminal)


class TestLockedBalanceReachesEngineOutput(unittest.TestCase):
    """DP#18: a declared locked_until on a registered account must exclude that
    balance from the solvency waterfall before the unlock age — not merely set
    a config field that the waterfall ignores. These tests drive simulate_year_pure
    (the fold's core, per DP#11) to verify the lock changes the solvency outcome."""

    def test_locked_rrsp_skipped_by_solvency_before_unlock(self):
        """A household with an RRSP locked until age 71 and a solvency shortfall
        must NOT draw the locked balance when the owner is under 71. Compare
        two runs of simulate_year_pure: one with the lock, one without. The
        locked run must cover less of the shortfall (solvency cannot draw
        the locked RRSP).

        DP#18: state comes from SimState.initial(config), not a hand-built
        dict — the RRSP balance is declared on the family member (the config
        key the engine reads), and SimState.initial wires it into the
        per-adult store that YearWorkingState.from_state reads back."""
        from simulation_state import SimState, simulate_year_pure

        # A retiree born in 1955, age 70 in calendar year 2025.
        # With unlock_age=71 the RRSP is still locked in 2025 (age 70 < 71).
        # The $100k RRSP balance is declared on the primary member (the config
        # path the engine reads) — SimState.initial wires it into the per-adult
        # store that the fold's prologue reads back (DP#18: config→state→engine
        # output, not a hand-seeded dict).
        config_unlocked = SimulationConfig(
            investment_return=0.05,
            family_members=[
                {'role': 'primary', 'gross_income': 0, 'birth_year': 1955,
                 'retirement_age': 65, 'rrsp_room_accumulated': 0,
                 'tfsa_room_accumulated': 0, 'rrsp_balance': 100_000},
            ],
            projection_years=1,
            province='qc',
        )
        config_locked = SimulationConfig(
            investment_return=0.05,
            family_members=[
                {'role': 'primary', 'gross_income': 0, 'birth_year': 1955,
                 'retirement_age': 65, 'rrsp_room_accumulated': 0,
                 'tfsa_room_accumulated': 0, 'rrsp_balance': 100_000},
            ],
            projection_years=1,
            province='qc',
            account_locked={
                'rrsp': [{'balance': 100_000, 'unlock_age': 71,
                          'owner_birth_year': 1955}],
            },
        )
        # Build state from config (DP#18): SimState.initial reads
        # rrsp_balance off the family member and wires it into the per-adult
        # store. Both configs produce the same initial state (account_locked
        # is a rule input, not a state initializer).
        base_state = SimState.initial(config_unlocked)
        # Create a solvency shortfall: $0 income, $50k living costs.
        allocations = {'_primary_income': 0, '_annual_savings': 0}
        unlocked_result, _ = simulate_year_pure(
            state=base_state, year=0, allocations=allocations,
            config=config_unlocked, investment_return=0.05,
            primary_marginal_rate=0.40, calendar_year=2025,
            living_costs=50_000, after_tax_income=0,
        )
        locked_result, _ = simulate_year_pure(
            state=base_state, year=0, allocations=allocations,
            config=config_locked, investment_return=0.05,
            primary_marginal_rate=0.40, calendar_year=2025,
            living_costs=50_000, after_tax_income=0,
        )
        # Before unlock (age 70 < 71), the locked RRSP cannot be drawn by
        # solvency. The unlocked run CAN draw it, so it covers more shortfall.
        self.assertGreater(unlocked_result.solvency_covered,
                           locked_result.solvency_covered,
                           "Unlocked RRSP must cover more shortfall than locked")

    def test_absence_is_no_op_golden_unchanged(self):
        """DP#32: the golden household declares no lock, so adding an empty
        locked dict does not change its terminal total_assets."""
        base_cfg = golden_household_config()
        variant = copy.deepcopy(base_cfg)
        variant['accounts']['locked'] = {}  # explicit empty
        base_terminal = _run(base_cfg)[-1].total_assets
        variant_terminal = _run(variant)[-1].total_assets
        self.assertEqual(base_terminal, variant_terminal)


if __name__ == '__main__':
    unittest.main()
