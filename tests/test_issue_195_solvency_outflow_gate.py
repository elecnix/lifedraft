"""Issue #195: `apply_solvency`'s `living_costs <= 0` early return strands
every solvency-only outflow.

`rules_solvency.apply_solvency` gates the WHOLE outflow computation behind

    if ctx.living_costs <= 0:
        return False

The gate is deliberate and documented (DP#16: do not fund a shortfall the
household never budgeted for) -- but it sits BEFORE the `spending_outflow`
identity, so in a household that never declares
`household_budget.living_costs`, every real third-party outflow that exists
only as a term of that identity is silently never charged:

  - #696  a dated mid-horizon PROPERTY PURCHASE (net_equity + closing costs),
  - #1010 a non-principal property's recurring CARRYING COSTS,
  - #760  dated `expense_segments`,
  - #142  management fees.

The balance-sheet side of the same declared facts still lands (the property's
`net_equity` enters `total_assets` via
`simulation_state._property_equity_for_year`), so the household is credited
equity it never paid for and carries assets whose costs never leave. Money
is invented from nothing (DP#18 violated) -- the exact shape this engine was
rebuilt to prevent.

THESE TESTS ARE RED ON `main` BY DESIGN. They are the acceptance criterion
for the #195 repair, written before the repair: the gate must be narrowed so
real third-party outflows are charged unconditionally (a declared carrying
cost and a signed purchase are obligations regardless of whether the
household declared a working-budget scalar), while the shortfall-funding
branch keeps its DP#16 no-op. Until that repair lands on this branch, these
tests fail -- and they MUST be allowed to stay red in CI. Hiding them with
xfail/skip would destroy the instrument.

No xfail, no skip: a red test here is the deliverable.

All fixtures use fabricated ids and round numbers, role-based names
(DP#4/DP#15 -- no personal data, ever). Every scenario drives the live fold
(`FamilySimulation.run()`), never hand-built engine state (DP#11).
"""

import copy
import os
import sys
import unittest

from liquidation_waterfall import summarize_solvency

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig


# ============================================================================
# A compact accumulation household that runs a REAL shortfall every year, so
# every charged outflow must be funded through the liquidation waterfall and
# therefore necessarily bites the balance sheet. Fabricated round numbers
# (DP#15); roles only (DP#4).
#
# Why the shortfall pressure: when the solvency identity is solvent
# (required <= available) it charges nothing but paper -- the waterfall never
# fires. Sizing the savings sweep near/above after-tax income guarantees the
# identity must liquidate real accounts every year, so a stranded outflow
# shows up as money that never leaves (delta 0 on main) and a repaired gate
# shows up as a real, compounding drag.
# ============================================================================

START_YEAR = 2026
HORIZON_YEARS = 12          # 2026..2037, primary age 40..51: never retires
PURCHASE_YEAR = 2031
PURCHASE_INDEX = PURCHASE_YEAR - START_YEAR   # results[5]

GROSS_INCOME = 120_000
SAVINGS_RATE = 0.80         # forces a shortfall every year (see above)
COTTAGE_NET_EQUITY = 200_000
CARRYING_ANNUAL = 5_000
PURCHASE_NET_EQUITY = 60_000
PURCHASE_CLOSING = 10_000
NON_REG_BALANCE = 400_000
NON_REG_ACB = 200_000


def _household(properties=None):
    cfg = {
        "family": {
            "members": [
                {"role": "primary", "birth_year": 1986, "retirement_age": 75,
                 "gross_income": GROSS_INCOME, "cpp_monthly_estimated": 0,
                 "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0},
            ],
            "children": [],
        },
        "property": {"house_value": 500_000, "mortgage_balance": 0,
                     "margin_available": 0, "mortgage_rate": 0.05,
                     "ltv_max": 0.80, "amortization_years": 25},
        # Internal-config property shape (the mapped form
        # input_contract._map_owned_properties produces): the household owns
        # 100%, so equity and annual amounts pass at the identity.
        "properties": properties or [],
        "assumptions": {"start_year": START_YEAR,
                        "projection_years": HORIZON_YEARS,
                        "investment_return": 0.06, "salary_growth": 0.0,
                        "inflation": 0.0, "frozen_brackets": True},
        "portfolio": {"accounts": {"non_reg": {
            "balance": NON_REG_BALANCE, "cost_basis": NON_REG_ACB,
            "composition": {"cdn_equity_pct": 0.6, "fixed_income_pct": 0.4},
            "yield": {"eligible_dividends": 0.015, "interest": 0.01}}}},
        "accounts": {"rrsp_annual_max": 0},
        # Deliberately NO household_budget key: living_costs is undeclared,
        # ctx.living_costs == 0.0 every year, the gate fires -- and on main
        # strands every outflow below.
        "savings": {"rate": SAVINGS_RATE},
        "tax": {"province": "qc"},
    }
    return cfg


def _cottage(carrying_costs=None):
    prop = {"id": "cottage_a", "kind": "recreational",
            "net_equity": COTTAGE_NET_EQUITY,
            "owner_roles": {"primary": 1.0, "spouse": 0.0}}
    if carrying_costs is not None:
        prop["carrying_costs"] = {"annual_amount": carrying_costs}
    return prop


def _bought_property():
    return {"id": "bought_a", "kind": "recreational",
            "net_equity": PURCHASE_NET_EQUITY,
            "owner_roles": {"primary": 1.0, "spouse": 0.0},
            "purchase": {"year": PURCHASE_YEAR,
                         "closing_costs": PURCHASE_CLOSING}}


def _run(cfg):
    sim_cfg = SimulationConfig.from_dict(copy.deepcopy(cfg))
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()


def _run_declared_living_costs(cfg, living_costs):
    """The same household WITH a declared living-costs budget -- the control
    that shows the solvency identity charges these outflows when engaged."""
    cfg = copy.deepcopy(cfg)
    cfg["household_budget"] = {"living_costs": living_costs}
    return _run(cfg)


class TestCarryingCostsChargedWithoutLivingCosts(unittest.TestCase):
    """#1010 carrying costs in a household that declares NO living costs.

    The cottage's equity lands on the balance sheet either way; the $5k/yr
    carrying cost must leave too. On main it never does: the household ends
    richer for owning a costing asset -- money invented from nothing.
    """

    def setUp(self):
        self.with_costs = _run(_household([_cottage(CARRYING_ANNUAL)]))
        self.without_costs = _run(_household([_cottage()]))

    def test_carrying_cost_charged_every_year_of_ownership(self):
        """The carrying cost must appear in the identity's spending outflow
        in every year of the ownership window -- unconditionally, not only
        when a living-costs budget happens to be declared."""
        for r in self.with_costs:
            self.assertGreaterEqual(
                r.solvency_spending_outflow, CARRYING_ANNUAL - 1e-6,
                f"year {r.year}: solvency_spending_outflow "
                f"{r.solvency_spending_outflow!r} does not include the "
                f"declared ${CARRYING_ANNUAL}/yr carrying cost -- the #195 "
                f"gate dropped a real third-party outflow")

    def test_total_charged_over_the_fold_covers_the_declared_costs(self):
        expected = CARRYING_ANNUAL * HORIZON_YEARS
        charged = sum(r.solvency_spending_outflow
                      for r in self.with_costs)
        self.assertGreaterEqual(
            charged, expected - 1e-6,
            f"charged {charged!r} over the fold; the declared "
            f"{expected!r} of carrying costs never left the household")

    def test_money_is_conserved_terminal_wealth_is_lower_with_costs(self):
        """End-to-end conservation across the fold: a household paying
        $5k/yr it cannot absorb from income must end with strictly less
        than its cost-free twin. On main the two trajectories are
        byte-identical (delta 0.0): the cost is stranded, the asset is free.

        Quantitative, not directional: the terminal delta must be at least
        the ACCUMULATED declared outflows ($5k x 12 = $60k) -- every charged
        dollar leaves before it can compound, so it can only cost the
        household its face value or more by the fold's end (measured:
        compounding plus liquidation drag put it well above). A gate that
        charged $1 of the $60k stream would pass a sign test and still be
        badly wrong; this bound fails it.
        """
        delta = (self.without_costs[-1].total_assets
                 - self.with_costs[-1].total_assets)
        self.assertLess(
            self.with_costs[-1].total_assets,
            self.without_costs[-1].total_assets,
            f"terminal total_assets identical-or-higher with a declared "
            f"${CARRYING_ANNUAL}/yr carrying cost "
            f"({self.with_costs[-1].total_assets!r} vs "
            f"{self.without_costs[-1].total_assets!r}): the household ended "
            f"richer for owning a costing asset -- money invented "
            f"from nothing (#195)")
        self.assertGreaterEqual(
            delta, CARRYING_ANNUAL * HORIZON_YEARS - 1e-6,
            f"terminal delta {delta!r} is below the accumulated declared "
            f"carrying costs {CARRYING_ANNUAL * HORIZON_YEARS!r} -- the "
            "gate is under-charging the stream it claims to charge")


class TestPurchaseOutflowChargedWithoutLivingCosts(unittest.TestCase):
    """#696 mid-horizon purchase in a household that declares NO living
    costs. On main the property's net_equity is credited in the purchase
    year while the down payment + closing costs are never charged: the
    household acquires real property for free (+net_equity at the fold's
    end)."""

    def setUp(self):
        self.with_purchase = _run(_household([_bought_property()]))
        self.without_property = _run(_household())

    def test_purchase_year_charges_equity_plus_closing_costs(self):
        outflow = self.with_purchase[PURCHASE_INDEX].solvency_spending_outflow
        expected = PURCHASE_NET_EQUITY + PURCHASE_CLOSING
        self.assertGreaterEqual(
            outflow, expected - 1e-6,
            f"year {PURCHASE_YEAR}: solvency_spending_outflow {outflow!r} "
            f"does not include the ${expected} down payment + closing costs "
            f"-- the property was acquired for free (#195)")

    def test_money_is_conserved_terminal_wealth_is_lower_with_purchase(self):
        """A purchase converts portfolio dollars into property equity at a
        net LOSS of the closing costs (plus liquidation drag): the buyer must
        end strictly below the twin that never bought. On main the buyer ends
        ABOVE by exactly the credited net_equity -- free property.

        Quantitative, not directional: the charged outflow is equity +
        closing costs, but the equity does not LEAVE the household -- it
        converts into property that stays on the balance sheet. The pure
        leak is the closing costs, so the terminal delta must be at least
        the declared closing costs. A gate that charged $1 of the $10k
        would pass a sign test and still be badly wrong; this bound fails
        it (the exact $70k charge is pinned separately above).
        """
        delta = (self.without_property[-1].total_assets
                 - self.with_purchase[-1].total_assets)
        self.assertLess(
            self.with_purchase[-1].total_assets,
            self.without_property[-1].total_assets,
            f"terminal total_assets identical-or-higher after buying real "
            f"property for ${PURCHASE_NET_EQUITY + PURCHASE_CLOSING} "
            f"({self.with_purchase[-1].total_assets!r} vs "
            f"{self.without_property[-1].total_assets!r}): the household "
            f"was credited equity it never paid for (#195)")
        self.assertGreaterEqual(
            delta, PURCHASE_CLOSING - 1e-6,
            f"terminal delta {delta!r} is below the declared closing costs "
            f"{PURCHASE_CLOSING!r} -- the purchase outflow is under-charged "
            "even before the equity conversion is accounted for")


class TestControlOutflowsChargedWhenLivingCostsDeclared(unittest.TestCase):
    """The GREEN control, passing on main today: the identical outflows ARE
    charged once the household declares a living-costs budget. This proves
    the red tests above isolate the #195 gate itself -- not a broken fixture
    or an unreachable rule path."""

    def test_carrying_costs_charged_when_living_costs_declared(self):
        results = _run_declared_living_costs(
            _household([_cottage(CARRYING_ANNUAL)]), 60_000)
        for r in results:
            self.assertGreaterEqual(
                r.solvency_spending_outflow, CARRYING_ANNUAL - 1e-6,
                f"year {r.year}: control household failed to charge the "
                f"carrying cost -- the fixture itself is broken, not #195")
        without = _run_declared_living_costs(_household([_cottage()]), 60_000)
        self.assertLess(results[-1].total_assets,
                        without[-1].total_assets)

    def test_purchase_outflow_charged_when_living_costs_declared(self):
        results = _run_declared_living_costs(
            _household([_bought_property()]), 60_000)
        expected = PURCHASE_NET_EQUITY + PURCHASE_CLOSING
        self.assertGreaterEqual(
            results[PURCHASE_INDEX].solvency_spending_outflow,
            expected - 1e-6,
            f"year {PURCHASE_YEAR}: control household failed to charge the "
            f"purchase outflow -- the fixture itself is broken, not #195")


class TestDp16NoOpSurvivesForThePureHousehold(unittest.TestCase):
    """The GREEN control for the OTHER half of the narrowed gate.

    The repair must not over-correct into "always run the solvency
    machinery". A household that declares NEITHER a living-costs budget
    NOR any real third-party obligation has nothing to charge and nothing
    to fund: the DP#16 no-op must still fire in every year of the fold,
    and the trajectory must be byte-identical to the same household
    declaring an explicit ``living_costs`` of 0 (an explicitly-zero budget
    and an absent budget leave nothing to charge in exactly the same way).
    """

    def test_pure_household_still_hits_the_no_op(self):
        """No budget, no properties, no purchase: the solvency identity must
        never run -- no outflow charged, no shortfall funded, no liquidation,
        and the report must say UNCHECKED (engaged=False), not safe."""
        results = _run(_household())
        for r in results:
            self.assertEqual(
                r.solvency_spending_outflow, 0.0,
                f"year {r.year}: the pure household charged an outflow "
                f"{r.solvency_spending_outflow!r} -- the narrowed gate no "
                "longer no-ops for a household with nothing declared (#195 "
                "over-correction)")
            self.assertEqual(
                r.solvency_shortfall, 0.0,
                f"year {r.year}: the pure household funded a shortfall "
                f"{r.solvency_shortfall!r} nobody budgeted for -- the DP#16 "
                "no-op is gone")
            self.assertEqual(
                r.forced_liquidation_events, [],
                f"year {r.year}: the pure household liquidated accounts "
                "through the waterfall -- the DP#16 no-op is gone")
            self.assertFalse(
                r.ruined,
                f"year {r.year}: the pure household was marked ruined -- "
                "the solvency machinery ran where the DP#16 no-op should "
                "have fired")
        summary = summarize_solvency(results)
        self.assertFalse(
            summary['engaged'],
            "a household that never declared a budget or an obligation must "
            "be reported as UNCHECKED, not as safely solvent (DP#32)")

    def test_explicit_zero_budget_is_byte_identical_to_absent_budget(self):
        """Declaring ``living_costs: 0`` and omitting the key are the same
        fact (zero is a value, not a fallback -- DP#32): both must no-op and
        produce byte-identical trajectories."""
        absent = _run(_household())
        zero_cfg = copy.deepcopy(_household())
        zero_cfg["household_budget"] = {"living_costs": 0}
        zero = _run(zero_cfg)
        self.assertEqual(
            repr(absent), repr(zero),
            "an explicit living_costs of 0 produced a different trajectory "
            "from an absent one -- zero was coerced or the gate treated the "
            "two absences differently (DP#32/#195)")


if __name__ == "__main__":
    unittest.main()
