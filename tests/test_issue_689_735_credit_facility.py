#!/usr/bin/env python3
"""A revolving line of credit: representable (#689) and undrawn by default
(#735) -- one hole, two sides.

## The bug this closes

#689: the schema conflated two axes -- revolving vs. amortizing, and secured
vs. unsecured -- into one `kind` enum. `personal_loan` forbade a `limit` and
required an `amortization` (a term loan); the only revolving kind, `heloc`,
required `collateral` (secured by definition). A real, signed, unsecured,
interest-only, undrawn line of credit could not be written into the input
contract at all.

#735: even a representable line was drawn in full and invested at year 0
(`lump_sum = config.margin_available + config.cash_out`, unconditional) --
charging its cost (interest on a fully-drawn balance) while denying its
benefit (standby liquidity in a #679 shortfall, which can only ever find
room there if the room was never spent).

Together: the #679 liquidation waterfall's second rung (emergency reserve ->
revolving credit facility -> non-registered -> TFSA -> registered) was
permanently empty -- partly because the facility couldn't be declared,
partly because any facility that WAS declared was already spent.

## What's tested here (DP#4/DP#15: fabricated round numbers, role-based
## names -- no real household's figures appear anywhere in this file)

1. Schema representability: the exact reproduction from #689 (personal_loan
   + limit fails; a revolving, unsecured `line_of_credit` now validates).
2. The facility reaches the engine: `SimulationConfig.credit_facility_*`
   populated from a contract, secured vs. unsecured.
3. A declared, undrawn line produces $0 interest and $0 invested capital at
   year 0 -- asserted on the trajectory (YearResult), not the config.
4. In a #679 shortfall year, the facility is drawn BEFORE any asset is
   liquidated, and the resulting ruin year is strictly later than with no
   facility at all.
5. An unsecured line does NOT count against the property's charge limit; a
   secured one does -- both sides -- and the #681 invariant
   (total_secured_debt <= charge_limit) still holds every year.
6. #735: the year-0 draw of `margin_available` defaults to zero, and the
   optimizer can sweep the draw fraction.
"""

import copy
import unittest

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

import input_contract as ic
from simulation_config import SimulationConfig
from simulation_state import SimState, simulate_year_pure
from trajectory_invariants import assert_invariant, run_invariant


# ============================================================================
# Fixture helpers (DP#4/DP#15: fabricated, round numbers, role-based names)
# ============================================================================

def _mort_data(balance, payment=18_000, principal=6_000):
    return {
        'end_balance': max(0.0, balance - principal),
        'total_payment': payment,
        'total_interest': payment - principal,
        'total_principal': principal,
    }


def _base_config(**overrides):
    defaults = dict(
        projection_years=6,
        investment_return=0.05,
        mortgage_balance=300_000,
        mortgage_rate=0.05,
        margin_available=0,
        family_members=[
            {'role': 'primary', 'gross_income': 95_000, 'birth_year': 1985,
             'rrsp_room_accumulated': 40_000, 'tfsa_room_accumulated': 20_000},
        ],
        children=[],
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def _contract_liability(**overrides):
    """A minimal, valid ``line_of_credit`` liability dict (fabricated)."""
    base = {
        "id": "personal_loc",
        "owner": "p1",
        "kind": "line_of_credit",
        "balance": {"amount": 0, "as_of": "2026-06-30"},
        "limit": 30_000,
        "rate": 0.0725,
        "rate_type": "variable",
        "collateral": None,
    }
    base.update(overrides)
    return base


def _minimal_liability(kind, **overrides):
    base = {
        "id": f"{kind}_test",
        "owner": "p1",
        "kind": kind,
        "balance": {"amount": 5_000, "as_of": "2026-06-30"},
        "rate": 0.07,
        "rate_type": "fixed",
        "collateral": None,
        "amortization": {"years": 5, "payment_monthly": 100},
    }
    base.update(overrides)
    return base


# ============================================================================
# 1. Schema representability (#689) -- the exact reproduction from the issue.
# ============================================================================

class TestSchemaRepresentability(unittest.TestCase):

    def _minimal_doc(self):
        doc = copy.deepcopy(ic._default_example())
        # Trim to the two-generation subset the legacy adapter can map, same
        # technique as tests/test_input_contract.py -- irrelevant to schema
        # VALIDATION (this class never calls to_internal_config), but keeps
        # every liability's owner inside a set validate_contract accepts.
        return doc

    def test_personal_loan_cannot_carry_a_limit(self):
        """The #689 reproduction: a personal_loan (amortizing, closed-end)
        forbids a revolving `limit` -- this is NOT a bug, it is the OTHER
        half of the fix (the axes are separated, not merged into one
        permissive shape)."""
        doc = self._minimal_doc()
        doc["liabilities"].append(_minimal_liability(
            "personal_loan", limit=30_000))
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_unsecured_revolving_line_of_credit_validates(self):
        """The actual #689 fix: revolving (has a `limit`, no
        `amortization`) AND unsecured (`collateral: null`) together --
        the (revolving, unsecured) quadrant that used to not exist."""
        doc = self._minimal_doc()
        doc["liabilities"].append(_contract_liability())
        ic.validate_contract(doc)  # must not raise

    def test_secured_revolving_line_of_credit_also_validates(self):
        """A line_of_credit MAY be secured too (collateral set) -- `heloc`
        is not the only way to express a secured revolving facility;
        `collateral`, not `kind`, is what decides secured-vs-not (#689)."""
        doc = self._minimal_doc()
        doc["liabilities"].append(
            _contract_liability(collateral="principal_residence"))
        ic.validate_contract(doc)  # must not raise

    def test_line_of_credit_still_forbids_amortization(self):
        """Still a revolving kind: an amortization schedule on it is
        incoherent and remains rejected."""
        doc = self._minimal_doc()
        doc["liabilities"].append(_contract_liability(
            amortization={"years": 5, "payment_monthly": 500}))
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)


# ============================================================================
# 2. The facility reaches the engine (#689's "and then consume it").
# ============================================================================

class TestReachesTheEngine(unittest.TestCase):

    def _mapped_config(self, liability_overrides=None):
        import sys
        sys.path.insert(0, "tests")
        from test_input_contract import _two_generation_subset, _load_example
        doc = _two_generation_subset(_load_example())
        doc["liabilities"] = [
            l for l in doc["liabilities"] if l["kind"] != "line_of_credit"]
        doc["liabilities"].append(
            _contract_liability(**(liability_overrides or {})))
        cfg = ic.to_internal_config(doc)
        return SimulationConfig.from_dict(cfg)

    def test_unsecured_facility_reaches_config(self):
        config = self._mapped_config()
        self.assertEqual(config.credit_facility_limit, 30_000)
        self.assertAlmostEqual(config.credit_facility_rate, 0.0725)
        self.assertEqual(config.credit_facility_rate_type, "variable")
        self.assertFalse(config.credit_facility_secured)

    def test_secured_facility_reaches_config_as_secured(self):
        config = self._mapped_config({"collateral": "principal_residence"})
        self.assertTrue(config.credit_facility_secured)

    def test_undeclared_facility_defaults_to_no_room(self):
        """DP#32: a household that never declares this liability gets a
        real, honest $0 -- not a fabricated default room."""
        import sys
        sys.path.insert(0, "tests")
        from test_input_contract import _two_generation_subset, _load_example
        doc = _two_generation_subset(_load_example())
        doc["liabilities"] = [
            l for l in doc["liabilities"] if l["kind"] != "line_of_credit"]
        cfg = ic.to_internal_config(doc)
        config = SimulationConfig.from_dict(cfg)
        self.assertEqual(config.credit_facility_limit, 0.0)
        self.assertFalse(config.credit_facility_secured)


# ============================================================================
# 3. An undrawn line costs nothing at year 0 -- on the TRAJECTORY.
# ============================================================================

class TestUndrawnAtYearZero(unittest.TestCase):

    def test_declared_undrawn_line_is_zero_interest_zero_invested(self):
        cfg = _base_config(credit_facility_limit=30_000, credit_facility_rate=0.0725)
        state = SimState.initial(cfg)
        result, new_state = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 95_000, '_annual_savings': 0},
            config=cfg, investment_return=0.05, primary_marginal_rate=0.30,
            mortgage_data=_mort_data(cfg.mortgage_balance),
        )
        self.assertEqual(result.credit_facility_balance, 0.0)
        self.assertEqual(new_state.credit_facility_balance, 0.0)
        # Never invested: this facility funds only #679 shortfalls, never a
        # year-0 lump sum (that is margin_available's own, separate #735
        # question -- see TestDrawFractionDefaultsToZero below).
        self.assertNotIn('credit_facility', str(result.contributions))

    def test_stays_undrawn_across_several_solvent_years(self):
        cfg = _base_config(credit_facility_limit=30_000, credit_facility_rate=0.0725)
        state = SimState.initial(cfg)
        for year in range(4):
            result, state = simulate_year_pure(
                state=state, year=year,
                allocations={'_primary_income': 95_000, '_annual_savings': 0},
                config=cfg, investment_return=0.05, primary_marginal_rate=0.30,
                mortgage_data=_mort_data(cfg.mortgage_balance),
            )
            self.assertEqual(result.credit_facility_balance, 0.0,
                              f"year {year}: an untouched facility must stay at $0")


# ============================================================================
# 4. Drawn BEFORE any asset is liquidated; ruin year strictly later (#689's
#    and #735's shared enforcement).
# ============================================================================

LIVING_COSTS = 54_000
MORTGAGE_PAYMENT = 18_000
AFTER_TAX_WORKING = 90_000
AFTER_TAX_EI = 28_000


def _run_income_collapse(collapse_year, n_years=4, credit_facility_limit=0.0,
                          after_tax_ei=AFTER_TAX_EI, reserve=2_000):
    cfg = _base_config(
        credit_facility_limit=credit_facility_limit,
        credit_facility_rate=0.06 if credit_facility_limit else None,
        emergency_reserve_target_months=6,
        emergency_reserve_rate=0.0,
        emergency_reserve_instrument='cash',
        emergency_reserve_held_in=None,
    )
    state = SimState(
        emergency_reserve_balance=reserve,
        mortgage_balance=cfg.mortgage_balance,
        non_reg_balance=5_000, non_reg_acb=5_000,
        jurisdiction_state={'canada': {  # #700: per-adult stores
            'adult_tfsa': {'primary': {'balance': 3_000, 'room': 0.0}},
            'adult_rrsp': {'primary': {'own': 2_000, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}}},
    )
    results = []
    for year in range(n_years):
        working = year < collapse_year
        after_tax_income = AFTER_TAX_WORKING if working else after_tax_ei
        allocations = {'_primary_income': 95_000, '_annual_savings': 0}
        mort = _mort_data(state.mortgage_balance, payment=MORTGAGE_PAYMENT)
        result, state = simulate_year_pure(
            state=state, year=year,
            allocations=allocations, config=cfg, investment_return=0.05,
            primary_marginal_rate=0.30, mortgage_data=mort,
            living_costs=LIVING_COSTS, after_tax_income=after_tax_income,
        )
        results.append(result)
    return results


class TestFacilityDrawnBeforeLiquidation(unittest.TestCase):

    def test_facility_drawn_before_non_reg_in_the_waterfall_order(self):
        results = _run_income_collapse(collapse_year=1, n_years=2, credit_facility_limit=30_000)
        shortfall_year = results[1]
        self.assertGreater(shortfall_year.solvency_shortfall, 0.0)
        touched = [e['source'] for e in shortfall_year.forced_liquidation_events]
        self.assertIn('revolving_credit', touched)
        self.assertLess(touched.index('revolving_credit'), touched.index('non_reg'),
                         "the facility must be drawn BEFORE non-reg is liquidated")
        self.assertFalse(shortfall_year.credit_facility_unrepresentable)
        self.assertGreater(shortfall_year.credit_facility_balance, 0.0)

    def test_ruin_year_is_strictly_later_with_a_facility(self):
        """If the facility does not change the ruin outcome, it is not
        being modelled (#689/#735's own enforcement bar).

        A moderate, SUSTAINED shortfall (~$10k/year -- after_tax_ei=$62k
        against $72k required) rather than the module-default ($44k/year):
        the facility must actually be able to bridge one or more years, or
        this comparison degenerates into "everything ruins immediately
        either way" and proves nothing."""
        without_facility = _run_income_collapse(
            collapse_year=1, n_years=6, credit_facility_limit=0.0, after_tax_ei=62_000)
        with_facility = _run_income_collapse(
            collapse_year=1, n_years=6, credit_facility_limit=30_000, after_tax_ei=62_000)

        def _first_ruin_year(results):
            for i, r in enumerate(results):
                if r.ruined:
                    return i
            return None

        ruin_without = _first_ruin_year(without_facility)
        ruin_with = _first_ruin_year(with_facility)
        self.assertIsNotNone(ruin_without, "the household must actually be at risk of ruin "
                                            "without a facility, for this comparison to mean anything")
        if ruin_with is not None:
            self.assertGreater(ruin_with, ruin_without,
                                "a real, undrawn facility must delay ruin, not leave it unchanged")
        # else: the facility covered every shortfall in the horizon -- ruin
        # never happens at all, which is an even stronger form of "later".

    def test_credit_facility_unrepresentable_true_only_when_undeclared(self):
        with_facility = _run_income_collapse(collapse_year=1, n_years=2, credit_facility_limit=30_000)
        without_facility = _run_income_collapse(collapse_year=1, n_years=2, credit_facility_limit=0.0)
        self.assertFalse(with_facility[1].credit_facility_unrepresentable)
        self.assertTrue(without_facility[1].credit_facility_unrepresentable)


# ============================================================================
# 5. Unsecured doesn't count against the charge; secured does -- both sides
#    (#689's enforcement (d)), and #681's invariant still holds.
# ============================================================================

def _year_with_drawn_facility(limit=100_000, shortfall=60_000):
    """A single, mortgage-free year that draws the credit facility to
    exactly cover ``shortfall`` -- isolates the facility's OWN contribution
    to the charge check from the mortgage's (no other liability muddies the
    number)."""
    cfg = _base_config(mortgage_balance=0, credit_facility_limit=limit,
                        credit_facility_rate=0.06, emergency_reserve_target_months=0)
    state = SimState(non_reg_balance=0, non_reg_acb=0)
    mort = {'end_balance': 0.0, 'total_payment': 0.0, 'total_interest': 0.0, 'total_principal': 0.0}
    result, _ = simulate_year_pure(
        state=state, year=0,
        allocations={'_primary_income': 0, '_annual_savings': 0},
        config=cfg, investment_return=0.0, primary_marginal_rate=0.30,
        mortgage_data=mort, living_costs=shortfall, after_tax_income=0.0,
    )
    return [result]


class TestChargeLimitBothSides(unittest.TestCase):

    def test_unsecured_drawn_balance_excluded_from_charge_check(self):
        results = _year_with_drawn_facility(limit=100_000, shortfall=60_000)
        self.assertEqual(results[0].credit_facility_balance, 60_000.0)
        # A tiny house: the drawn facility ALONE ($60k) blows past 80% LTV
        # of a $50k house ($40k) -- but the mortgage is $0, so this can only
        # trip if the (wrongly) UNSECURED facility were folded in.
        ctx = {'house_value': 50_000, 'credit_facility_secured': False}
        violations = run_invariant('total_secured_debt_within_charge_limit', results, ctx)
        self.assertEqual(violations, [],
                          "an UNSECURED facility must not count against the property's charge")

    def test_secured_drawn_balance_counted_in_charge_check(self):
        results = _year_with_drawn_facility(limit=100_000, shortfall=60_000)
        # Same $50k house, same $60k drawn balance -- only `credit_facility_
        # secured` differs. Now it MUST trip.
        ctx = {'house_value': 50_000, 'credit_facility_secured': True}
        violations = run_invariant('total_secured_debt_within_charge_limit', results, ctx)
        self.assertNotEqual(violations, [],
                             "a SECURED facility drawn past the charge must be caught")

    def test_681_invariant_holds_for_an_ordinary_secured_household(self):
        """DP#17's other side: a normal, in-bounds household with a secured
        facility must NOT trip the invariant."""
        results = _run_income_collapse(collapse_year=5, n_years=3, credit_facility_limit=20_000)
        ctx = {'house_value': 900_000, 'credit_facility_secured': True}
        assert_invariant('total_secured_debt_within_charge_limit', results, ctx)

    def test_contract_load_time_charge_check_folds_in_secured_facility(self):
        """input_contract.py's OWN combined-charge refusal (#664/#689):
        mortgage + HELOC limit + a SECURED line of credit limit must fit the
        registered charge, same as mortgage + HELOC alone did before."""
        import sys
        sys.path.insert(0, "tests")
        from test_input_contract import _two_generation_subset, _load_example
        doc = _two_generation_subset(_load_example())
        principal = next(p for p in doc["properties"] if p["kind"] == "principal")
        doc["liabilities"] = [l for l in doc["liabilities"] if l["kind"] != "line_of_credit"]
        # A secured facility whose limit alone, stacked on the existing
        # mortgage+HELOC, blows through 80% LTV.
        huge_limit = principal["value"]["amount"] * 2
        doc["liabilities"].append(_contract_liability(
            collateral=principal["id"], limit=huge_limit))
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_contract_load_time_unsecured_facility_never_refused_by_charge(self):
        """The same oversized limit, UNSECURED, must never trip the charge
        refusal -- it is not registered against the property at all."""
        import sys
        sys.path.insert(0, "tests")
        from test_input_contract import _two_generation_subset, _load_example
        doc = _two_generation_subset(_load_example())
        principal = next(p for p in doc["properties"] if p["kind"] == "principal")
        doc["liabilities"] = [l for l in doc["liabilities"] if l["kind"] != "line_of_credit"]
        huge_limit = principal["value"]["amount"] * 2
        doc["liabilities"].append(_contract_liability(limit=huge_limit, collateral=None))
        ic.to_internal_config(doc)  # must not raise


# ============================================================================
# 6. #735: the margin_available draw fraction defaults to zero, and is
#    sweepable.
# ============================================================================

class TestDrawFractionDefaultsToZero(unittest.TestCase):

    def test_scenario_overlay_default_is_undrawn(self):
        """ScenarioOverlay.draw_fraction (simulate.py's real, consumed
        field -- unlike a bare SimulationConfig, which a household never
        declares this on, DP#9) defaults to 0.0."""
        from simulation_config import ScenarioOverlay
        overlay = ScenarioOverlay(label="test")
        self.assertEqual(overlay.draw_fraction, 0.0)

    def test_zero_draw_fraction_means_margin_never_becomes_debt(self):
        """The #735 fix, exercised the way the engine actually computes a
        year-0 lump sum: margin_available * draw_fraction + cash_out."""
        from simulation_state import margin_draw_for_lump_sum
        cfg = _base_config(margin_available=150_000, cash_out=0.0)
        draw_fraction = 0.0
        lump_sum = cfg.margin_available * draw_fraction + cfg.cash_out
        self.assertEqual(lump_sum, 0.0)
        self.assertEqual(margin_draw_for_lump_sum(lump_sum, cfg.margin_available), 0.0)

    def test_partial_draw_fraction_draws_only_that_share(self):
        from simulation_state import margin_draw_for_lump_sum
        cfg = _base_config(margin_available=100_000, cash_out=0.0)
        draw_fraction = 0.25
        lump_sum = cfg.margin_available * draw_fraction + cfg.cash_out
        self.assertEqual(lump_sum, 25_000.0)
        self.assertEqual(margin_draw_for_lump_sum(lump_sum, cfg.margin_available), 25_000.0)

    def test_grid_optimizer_default_never_draws_margin(self):
        """GridOptimizer.optimize() called with NO draw_fraction_options
        must not silently reintroduce the #735 bug -- the default is [0.0],
        not the old unconditional-full-draw behaviour."""
        from optimizer import GridOptimizer
        cfg = _base_config(margin_available=100_000, mortgage_rate=0.05, heloc_rate=0.05)
        opt = GridOptimizer(cfg)
        ranked = opt.optimize(
            use_readvanceable_options=[False], deduct_later_options=[False],
            income_overrides=[None],
        )
        self.assertTrue(ranked)
        for r in ranked:
            self.assertEqual(r.config_overrides.get('draw_fraction', 0.0), 0.0)

    def test_grid_optimizer_can_sweep_draw_fraction(self):
        from optimizer import GridOptimizer
        cfg = _base_config(margin_available=100_000, mortgage_rate=0.05, heloc_rate=0.05)
        opt = GridOptimizer(cfg)
        ranked = opt.optimize(
            use_readvanceable_options=[False], deduct_later_options=[False],
            income_overrides=[None], draw_fraction_options=[0.0, 0.25, 0.5, 1.0],
        )
        seen_fractions = {r.config_overrides['draw_fraction'] for r in ranked}
        self.assertEqual(seen_fractions, {0.0, 0.25, 0.5, 1.0})

    def test_discover_draw_fraction_options_gated_on_a_declared_facility(self):
        from scenario_discovery import _discover_draw_fraction_options
        self.assertEqual(_discover_draw_fraction_options({'property': {}}), [0.0])
        self.assertEqual(
            _discover_draw_fraction_options({'property': {'margin_available': 100_000}}),
            [0.0, 0.25, 0.5, 1.0],
        )


if __name__ == '__main__':
    unittest.main()
