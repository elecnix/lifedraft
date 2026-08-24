#!/usr/bin/env python3
"""Issue #761: living costs cannot be split into non-discretionary vs
discretionary -- a single scalar forces all spending to be either fully
rigid or fully compressible, and both are wrong for the runway metric.

## What this tests (DP#4/DP#15: fabricated round numbers, role-based names)

1. **The over-conservative behaviour reproduced.** Under a dated income
   shock, a contract that declares NO discretionary split charges the WHOLE
   ``household_budget.annual_living_costs`` as rigid (today's behaviour,
   byte-for-byte) -- the discretionary portion does NOT compress. This is
   the baseline the fix improves on.
2. **The fix.** The same shock with ``discretionary_fraction`` declared
   compresses the discretionary portion to ZERO in the solvency identity
   (a labelled stress assumption), the shortfall shrinks, and the runway is
   LONGER than the no-split case.
3. **The no-op property (DP#32).** A no-shock scenario is UNCHANGED whether
   or not a split is declared (discretionary compression never fires without
   a dated income shock). A shock scenario with NO split declared is also
   unchanged from today. A contract that omits the field reproduces today's
   numbers exactly.
4. **Absence must not silently default (DP#32).** A contract that declares a
   discretionary fraction without a measured ``annual_living_costs`` fails
   loudly at the loading boundary -- never defaults the missing scalar to
   zero (which would silently make the split a no-op).
5. **The compression is visible in the output.** ``YearResult`` stamps the
   spending figure actually charged (``solvency_spending_outflow``) and the
   discretionary dollars compressed (``solvency_discretionary_compressed``),
   so the identity is transparent about which figure it used, and
   ``model_fidelity`` surfaces the labelled assumption on either branch
   (all-rigid when no split, compresses-to-zero when a split is declared).
6. **Integration via ``FamilySimulation.run()``.** A dated
   ``income_segments`` job-loss shock auto-detects ``income_shock_active``
   and compresses the discretionary portion only in the shock years -- the
   pre-shock years and the no-shock (``stay``) scenario are unchanged.
"""

import unittest
from datetime import date

# Sibling-import the #679 fixture helpers (the repo's no-`tests.`-prefix
# convention -- see test_issue_758_runway.py's import comment).
from test_issue_679_solvency import (
    RUIN_AFTER_TAX_EI,
    RUIN_AFTER_TAX_WORKING,
    RUIN_LIVING_COSTS,
    RUIN_MORTGAGE_PAYMENT,
    RUIN_RRSP_CONTRIBUTION,
    _mort_data,
    _reserve_config,
)

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers
from runway import compute_runway
from simulation_config import SimulationConfig
from simulation_state import SimState, simulate_year_pure
import contract_errors

# ─── A one-year pure-step helper that varies ONLY the discretionary split ──
# Isolates the solvency identity's spending-outflow term from everything else
# (same mortgage, same income, same reserve) so the test reads the
# compression directly off YearResult, not through a multi-year fold.

def _one_shock_year(*, discretionary_fraction, income_shock_active,
                    living_costs=RUIN_LIVING_COSTS,
                    after_tax_income=RUIN_AFTER_TAX_EI):
    cfg = _reserve_config(
        emergency_reserve_rate=0.0,
        discretionary_fraction=discretionary_fraction,
    )
    state = SimState(
        mortgage_balance=cfg.mortgage_balance,
        non_reg_balance=5_000, non_reg_acb=5_000,
        jurisdiction_state={'canada': {  # #700: per-adult stores
            'adult_tfsa': {'primary': {'balance': 3_000, 'room': 0.0}},
            'adult_rrsp': {'primary': {'own': 2_000, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}}},
    )
    allocations = {
        '_primary_income': 95_000, '_annual_savings': RUIN_RRSP_CONTRIBUTION,
        'primary_rrsp': RUIN_RRSP_CONTRIBUTION,
    }
    mort = _mort_data(state.mortgage_balance, payment=RUIN_MORTGAGE_PAYMENT)
    result, _ = simulate_year_pure(
        state=state, year=0, calendar_year=2026,
        allocations=allocations, config=cfg, investment_return=0.05,
        primary_marginal_rate=0.30, mortgage_data=mort,
        living_costs=living_costs, after_tax_income=after_tax_income,
        income_shock_active=income_shock_active,
    )
    return result


class TestOverConservativeBehaviourReproduced(unittest.TestCase):
    """The fix's baseline: under a shock, a contract with NO discretionary
    split charges the whole scalar as rigid -- discretionary does NOT
    compress today, and the fix preserves that exactly when no split is
    declared."""

    def test_no_split_under_shock_charges_the_full_scalar(self):
        r = _one_shock_year(discretionary_fraction=None,
                            income_shock_active=True)
        # The whole scalar is the spending outflow; nothing is compressed.
        self.assertAlmostEqual(r.solvency_spending_outflow, RUIN_LIVING_COSTS)
        self.assertEqual(r.solvency_discretionary_compressed, 0.0)
        self.assertAlmostEqual(r.living_costs, RUIN_LIVING_COSTS)

    def test_no_split_under_shock_is_byte_for_byte_today(self):
        # The no-split shock year has the SAME shortfall as a run that never
        # heard of discretionary_fraction -- the new fields are additive
        # reporting, the identity is unchanged.
        r_new = _one_shock_year(discretionary_fraction=None,
                                income_shock_active=True)
        # Historical shortfall = required - available. The RRSP contribution
        # clamps to room (rrsp_room=0 here -> $0 contributed), so required =
        # mortgage 18000 + living 54000 + contributions 0, available = 28000
        # -> shortfall 44000. The new fields are additive reporting; the
        # identity is unchanged from today.
        self.assertAlmostEqual(r_new.solvency_shortfall, 44_000.0)


class TestDiscretionaryCompressesUnderShock(unittest.TestCase):
    """The fix: with a split declared, an income-shock year compresses the
    discretionary portion to zero in the identity -- the shortfall shrinks
    and the runway is LONGER than the no-split case."""

    def test_discretionary_portion_compressed_to_zero(self):
        # 25% discretionary -> the identity charges 75% of the scalar.
        r = _one_shock_year(discretionary_fraction=0.25,
                            income_shock_active=True)
        self.assertAlmostEqual(r.solvency_spending_outflow,
                               RUIN_LIVING_COSTS * 0.75)
        self.assertAlmostEqual(r.solvency_discretionary_compressed,
                               RUIN_LIVING_COSTS * 0.25)
        # The reported living_costs stays the DECLARED scalar (semantics
        # don't drift); only the identity's `required` uses the compressed
        # figure.
        self.assertAlmostEqual(r.living_costs, RUIN_LIVING_COSTS)

    def test_shortfall_shrinks_by_the_compressed_amount(self):
        r_no_split = _one_shock_year(discretionary_fraction=None,
                                     income_shock_active=True)
        r_split = _one_shock_year(discretionary_fraction=0.25,
                                  income_shock_active=True)
        # The discretionary dollars move off the required side of the
        # identity, so the shortfall shrinks by exactly that many dollars
        # (no other term changed -- same mortgage, same income, same RRSP).
        shrink = r_no_split.solvency_shortfall - r_split.solvency_shortfall
        self.assertAlmostEqual(shrink, RUIN_LIVING_COSTS * 0.25)
        self.assertLess(r_split.solvency_shortfall, r_no_split.solvency_shortfall)

    def test_zero_fraction_is_all_rigid_and_compresses_nothing(self):
        # 0.0 is a real declarable "all rigid" value, distinct from None only
        # in what the output states -- both charge the full scalar.
        r = _one_shock_year(discretionary_fraction=0.0,
                            income_shock_active=True)
        self.assertAlmostEqual(r.solvency_spending_outflow, RUIN_LIVING_COSTS)
        self.assertEqual(r.solvency_discretionary_compressed, 0.0)

    def test_one_fraction_is_all_discretionary_and_compresses_all(self):
        # 1.0 = all discretionary -> the identity charges zero living costs
        # under a shock (the household cuts ALL discretionary spend; debt
        # service and contributions still apply).
        r = _one_shock_year(discretionary_fraction=1.0,
                            income_shock_active=True)
        self.assertAlmostEqual(r.solvency_spending_outflow, 0.0)
        self.assertAlmostEqual(r.solvency_discretionary_compressed,
                               RUIN_LIVING_COSTS)

    def test_out_of_range_fraction_does_not_compress(self):
        # apply_solvency guards 0.0 <= frac <= 1.0; a malformed fraction (the
        # schema rejects it, but the engine defends in depth) compresses
        # nothing rather than inventing a negative or >1 compression.
        r = _one_shock_year(discretionary_fraction=1.5,
                            income_shock_active=True)
        self.assertAlmostEqual(r.solvency_spending_outflow, RUIN_LIVING_COSTS)
        self.assertEqual(r.solvency_discretionary_compressed, 0.0)


class TestNoShockScenarioIsUnchanged(unittest.TestCase):
    """DP#32/no-op: a split declared but NO income shock this year must
    charge the full scalar -- discretionary compression never fires without
    a dated shock, so a no-shock scenario reproduces today's numbers
    byte-for-byte whether or not a split is declared."""

    def test_split_declared_but_no_shock_charges_full_scalar(self):
        r = _one_shock_year(discretionary_fraction=0.25,
                            income_shock_active=False,
                            after_tax_income=RUIN_AFTER_TAX_WORKING)
        self.assertAlmostEqual(r.solvency_spending_outflow, RUIN_LIVING_COSTS)
        self.assertEqual(r.solvency_discretionary_compressed, 0.0)

    def test_no_shock_shortfall_matches_with_or_without_split(self):
        # Same comfortable-income year: the split is inert, so the shortfall
        # is identical whether the field is None or 0.25.
        r_none = _one_shock_year(discretionary_fraction=None,
                                 income_shock_active=False,
                                 after_tax_income=RUIN_AFTER_TAX_WORKING)
        r_split = _one_shock_year(discretionary_fraction=0.25,
                                  income_shock_active=False,
                                  after_tax_income=RUIN_AFTER_TAX_WORKING)
        self.assertAlmostEqual(r_none.solvency_shortfall,
                               r_split.solvency_shortfall)
        self.assertEqual(r_none.solvency_discretionary_compressed, 0.0)
        self.assertEqual(r_split.solvency_discretionary_compressed, 0.0)


class TestRunwayIsLongerWithSplitThanWithout(unittest.TestCase):
    """The headline acceptance criterion (#761): a shock scenario WITH a
    declared discretionary fraction shows a LONGER runway than the same
    shock WITHOUT it. Built on the #679 income-collapse trajectory folded
    by hand, so each year's ``income_shock_active`` is controlled directly."""

    def _trajectory(self, *, discretionary_fraction, n_years=5,
                    collapse_year=2):
        cfg = _reserve_config(
            emergency_reserve_rate=0.0,
            discretionary_fraction=discretionary_fraction,
        )
        state = SimState(
            emergency_reserve_balance=10_000,
            mortgage_balance=cfg.mortgage_balance,
            non_reg_balance=5_000, non_reg_acb=5_000,
            jurisdiction_state={'canada': {  # #700: per-adult stores
                'adult_tfsa': {'primary': {'balance': 3_000, 'room': 0.0}},
                'adult_rrsp': {'primary': {'own': 2_000, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}}},
        )
        results = []
        for year in range(n_years):
            working = year < collapse_year
            after_tax_income = (RUIN_AFTER_TAX_WORKING if working
                                else RUIN_AFTER_TAX_EI)
            allocations = {
                '_primary_income': 95_000,
                '_annual_savings': RUIN_RRSP_CONTRIBUTION,
                'primary_rrsp': RUIN_RRSP_CONTRIBUTION,
            }
            mort = _mort_data(state.mortgage_balance,
                              payment=RUIN_MORTGAGE_PAYMENT)
            result, state = simulate_year_pure(
                state=state, year=year, calendar_year=2026 + year,
                allocations=allocations, config=cfg, investment_return=0.05,
                primary_marginal_rate=0.30, mortgage_data=mort,
                living_costs=RUIN_LIVING_COSTS,
                after_tax_income=after_tax_income,
                # The shock is active exactly in the collapse-year-onward
                # working years (the same years income is EI-level).
                income_shock_active=(not working),
            )
            results.append(result)
        return results

    def test_split_yields_longer_runway_than_no_split(self):
        no_split = self._trajectory(discretionary_fraction=None)
        split = self._trajectory(discretionary_fraction=0.25)
        rw_none = compute_runway(no_split, shock_date=date(2026, 1, 1),
                                 start_year=2026)
        rw_split = compute_runway(split, shock_date=date(2026, 1, 1),
                                  start_year=2026)
        self.assertTrue(rw_none.engaged)
        self.assertTrue(rw_split.engaged)
        self.assertIsNotNone(rw_none.runway_months)
        self.assertIsNotNone(rw_split.runway_months)
        self.assertGreater(
            rw_split.runway_months, rw_none.runway_months,
            "a shock WITH a declared discretionary fraction must show a "
            "LONGER runway than the same shock without it -- the household "
            "compresses discretionary spend and survives longer (issue #761)."
        )

    def test_split_trajectory_compresses_only_in_shock_years(self):
        split = self._trajectory(discretionary_fraction=0.25,
                                 collapse_year=2)
        # Pre-shock years (0, 1): no compression.
        for r in split[:2]:
            self.assertEqual(r.solvency_discretionary_compressed, 0.0)
            self.assertAlmostEqual(r.solvency_spending_outflow, RUIN_LIVING_COSTS)
        # Shock years (2+): the discretionary 25% is compressed.
        for r in split[2:]:
            self.assertAlmostEqual(r.solvency_discretionary_compressed,
                                   RUIN_LIVING_COSTS * 0.25)
            self.assertAlmostEqual(r.solvency_spending_outflow,
                                   RUIN_LIVING_COSTS * 0.75)


class TestRetirementDoesNotCompressDiscretionary(unittest.TestCase):
    """The discretionary compression is a WORKING-LIFE shock concept. In
    retirement the identity charges the retirement spending target (what the
    drawdown is sized to), not the working-phase living_costs -- so a declared
    discretionary split must NOT compress anything in a retired year."""

    def test_retired_year_charges_target_not_compressed(self):
        cfg = _reserve_config(emergency_reserve_rate=0.0,
                              discretionary_fraction=0.25)
        state = SimState(
            mortgage_balance=cfg.mortgage_balance,
            non_reg_balance=5_000, non_reg_acb=5_000,
            jurisdiction_state={'canada': {  # #700: per-adult stores
                'adult_tfsa': {'primary': {'balance': 3_000, 'room': 0.0}},
                'adult_rrsp': {'primary': {'own': 2_000, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}}},)
        allocations = {'_primary_income': 95_000, '_annual_savings': 0}
        mort = _mort_data(state.mortgage_balance, payment=RUIN_MORTGAGE_PAYMENT)
        result, _ = simulate_year_pure(
            state=state, year=0, calendar_year=2026,
            allocations=allocations, config=cfg, investment_return=0.05,
            primary_marginal_rate=0.30, mortgage_data=mort,
            living_costs=RUIN_LIVING_COSTS, after_tax_income=10_000,
            # Retired: the identity charges the retirement target, not the
            # working budget; income_shock_active is irrelevant here.
            any_retired=True, retirement_spending_target=40_000,
            income_shock_active=True,
        )
        self.assertEqual(result.solvency_discretionary_compressed, 0.0)
        # The spending outflow is the retirement target, not a compressed
        # fraction of the working living_costs.
        self.assertAlmostEqual(result.solvency_spending_outflow, 40_000.0)


class TestContractRoundTripsTheSplit(unittest.TestCase):
    """The discretionary_fraction round-trips contract -> internal shape ->
    SimulationConfig (DP#24), and a valid split maps without raising."""

    def test_example_split_maps_to_legacy_and_back(self):
        from test_input_contract import _load_example, _two_generation_subset

        import input_contract as ic
        from simulation_config import SimulationConfig
        doc = _two_generation_subset(_load_example())
        # The shipped example declares a 25% discretionary split; the adapter
        # must carry it through to the internal household_budget block.
        legacy = ic.to_internal_config(doc)
        self.assertEqual(legacy['household_budget']['discretionary_fraction'],
                         0.25)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(cfg.discretionary_fraction, 0.25)
        # Round-trip back out (DP#24): the split is re-emitted.
        out = cfg.to_dict()
        self.assertEqual(out['household_budget']['discretionary_fraction'], 0.25)

    def test_no_split_omits_the_key_round_trip(self):
        # A contract with no discretionary_fraction maps to a legacy dict that
        # does NOT carry the key, and SimulationConfig.discretionary_fraction
        # is None (DP#32: absent stays absent, never fabricated).
        from test_input_contract import _load_example, _two_generation_subset

        import input_contract as ic
        from simulation_config import SimulationConfig
        doc = _two_generation_subset(_load_example())
        doc['household_budget'] = {'annual_living_costs': 78000}
        legacy = ic.to_internal_config(doc)
        self.assertNotIn('discretionary_fraction', legacy['household_budget'])
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIsNone(cfg.discretionary_fraction)
        out = cfg.to_dict()
        self.assertNotIn('discretionary_fraction', out['household_budget'])


class TestAbsenceFailsLoudly(unittest.TestCase):
    """DP#32: a contract that declares a discretionary fraction without a
    measured annual_living_costs must FAIL LOUDLY at the loading boundary --
    never default the missing scalar to zero (which would silently make the
    split a no-op) or to the full amount."""

    def test_fraction_without_scalar_is_rejected(self):
        from test_input_contract import _load_example, _two_generation_subset

        import input_contract as ic
        # A schema-valid contract (the shipped example, trimmed to the
        # couple + children the adapter accepts) with the living-cost scalar
        # nullified WHILE a discretionary fraction is declared -- the two
        # halves of the split are not independently optional (DP#32).
        doc = _two_generation_subset(_load_example())
        doc['household_budget'] = {'annual_living_costs': None,
                                   'discretionary_fraction': 0.25}
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            ic.to_internal_config(doc)
        self.assertIn('discretionary_fraction', str(cm.exception))
        self.assertIn('annual_living_costs', str(cm.exception))


# ─── Integration: FamilySimulation.run() auto-detects the shock from the ──
# dated income_segments and compresses only in the shock years. Reuses the
# #674 job-loss household shape (fabricated, role-based names).

def _job_loss_config(*, discretionary_fraction, ei_to=None):
    """A fabricated household on a comfortable base income that collapses to
    EI in calendar 2027 (projection year 1) -- the same shape
    test_issue_674_income_shocks uses, with an optional discretionary split."""
    cfg = SimulationConfig(
        projection_years=4,
        house_value=800_000,
        mortgage_balance=300_000,
        mortgage_rate=0.05,
        amortization_years=25,
        margin_available=0,
        savings_rate=0.0,
        living_costs=54_000,
        discretionary_fraction=discretionary_fraction,
        start_year=2026,
        family_members=[
            {'role': 'primary', 'birth_year': 1985, 'gross_income': 150_000,
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
             'income_segments': [
                 {'kind': 'ei', 'amount': 20_000,
                  'from': '2027-01-01', 'to': ei_to},
             ]},
        ],
    )
    return cfg


class TestFamilySimulationAutoDetectsShock(unittest.TestCase):
    """The live loop (simulation.py) computes income_shock_active from the
    dated income_segments and threads it to apply_solvency -- no caller has
    to pass it by hand."""

    def test_shock_year_compresses_when_split_declared(self):
        from countries.canada.adapter import CanadaAdapter
        cfg = _job_loss_config(discretionary_fraction=0.25,
                               ei_to='2028-01-01')
        from simulation import FamilySimulation
        results = FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()
        # Year 0 (2026): pre-shock, no compression.
        self.assertEqual(results[0].solvency_discretionary_compressed, 0.0)
        self.assertAlmostEqual(results[0].solvency_spending_outflow, 54_000)
        # Year 1 (2027): the EI shock year -- 25% compressed.
        self.assertAlmostEqual(results[1].solvency_discretionary_compressed,
                               54_000 * 0.25)
        self.assertAlmostEqual(results[1].solvency_spending_outflow,
                               54_000 * 0.75)
        # Year 2 (2028): income reverted to base (ei_to=2028-01-01) -- no
        # shock, no compression.
        self.assertEqual(results[2].solvency_discretionary_compressed, 0.0)
        self.assertAlmostEqual(results[2].solvency_spending_outflow, 54_000)

    def test_no_split_under_shock_is_unchanged_from_today(self):
        # The same job-loss household with NO discretionary split: every
        # year charges the full scalar, byte-for-byte today's behaviour.
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        cfg = _job_loss_config(discretionary_fraction=None,
                               ei_to='2028-01-01')
        results = FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()
        for r in results:
            self.assertEqual(r.solvency_discretionary_compressed, 0.0)
            self.assertAlmostEqual(r.solvency_spending_outflow, 54_000)

    def test_stay_scenario_does_not_compress(self):
        # A no-shock scenario (no income_segments) never compresses, even
        # with a split declared -- the no-op property at the engine level.
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        cfg = SimulationConfig(
            projection_years=3, house_value=800_000, mortgage_balance=300_000,
            mortgage_rate=0.05, amortization_years=25, margin_available=0,
            savings_rate=0.0, living_costs=54_000,
            discretionary_fraction=0.25, start_year=2026,
            family_members=[{'role': 'primary', 'birth_year': 1985,
                             'gross_income': 150_000,
                             'rrsp_room_accumulated': 0,
                             'tfsa_room_accumulated': 0}],
        )
        results = FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()
        for r in results:
            self.assertEqual(r.solvency_discretionary_compressed, 0.0)


class TestModelFidelityStatesTheAssumption(unittest.TestCase):
    """#761 acceptance: the output states EXPLICITLY which assumption the
    engine is making -- all-rigid when no split is declared, compresses-to-
    zero when a split is declared. model_fidelity surfaces both branches."""

    def _engaged_runway_cfg(self, *, discretionary_fraction):
        return {
            'household_budget': (
                {'living_costs': 54000,
                 'discretionary_fraction': discretionary_fraction}
                if discretionary_fraction is not None
                else {'living_costs': 54000}
            ),
            'assumptions': {'runway': {
                'engaged': True, 'runway_months': 18.0,
                'relies_on_credit_facility': False,
                'drew_registered': False, 'scenario_label': 'jobloss'}},
        }

    def test_all_rigid_caveat_fires_when_no_split_declared(self):
        import model_fidelity as mf
        ids = {a.id for a in mf.active_approximations(
            self._engaged_runway_cfg(discretionary_fraction=None))}
        self.assertIn('runway_treats_all_spend_as_rigid', ids)
        self.assertNotIn('runway_compresses_discretionary_under_shock', ids)

    def test_compression_caveat_fires_when_split_declared(self):
        import model_fidelity as mf
        ids = {a.id for a in mf.active_approximations(
            self._engaged_runway_cfg(discretionary_fraction=0.25))}
        self.assertIn('runway_compresses_discretionary_under_shock', ids)
        # The all-rigid caveat is SUPPRESSED once a split is declared -- the
        # output never states both assumptions at once.
        self.assertNotIn('runway_treats_all_spend_as_rigid', ids)


if __name__ == '__main__':
    unittest.main()
