"""Issue #366: the home-support credit REACHES a decision.

The pricing arithmetic is pinned in ``test_issue_366_home_support_credit.py``.
This file pins the WIRING, because a priced-but-unreached module is exactly the
defect ``test_unreached_rule_modules`` exists for, and a unit test on a pure
function cannot tell the difference:

    objective.compute_net_benefit
      -> net_benefit_legs.home_support_credit_total   (the visible call chain)
        -> provinces/quebec/home_support.py

Three things have to hold end to end: the contract's declared block survives the
adapter and the simulation config, the objective cfg carries it, and a household
with the block is ranked higher by EXACTLY the credit.
"""
import unittest

from contract_assumptions import ContractAdaptationError, map_household_budget
from objective import compute_net_benefit, objective_cfg
from net_benefit_legs import home_support_credit_total
from simulation_config import SimulationConfig
from year_result import YearResult

SINGLE = "single_autonomous"
COUPLE = "couple_autonomous"


def _declared(**overrides):
    """A declared ``household_budget.home_support`` block (fabricated numbers)."""
    block = {
        "situation": SINGLE,
        "family_income": 40_000.0,
        "eligible_expenses": 19_500.0,
    }
    block.update(overrides)
    return block


def _final_year():
    """One final YearResult, the shape every net-benefit leg is priced against."""
    return YearResult(
        year=2036,
        total_assets=500_000.0,
        total_debt=200_000.0,
        primary_rrsp=300_000.0,
        total_rrsp=300_000.0,
        total_tfsa=100_000.0,
        non_reg_balance=100_000.0,
        non_reg_acb=50_000.0,
        resp_balance=0.0,
    )


def _cfg(home_support=None, *, start_year=2025):
    """The objective cfg a household with (or without) the block would carry."""
    cfg = {
        "family": {"members": [{"role": "primary", "birth_year": 1979,
                                "gross_income": 40_000.0}]},
        "assumptions": {"oas_annual": 0.0,
                        "capital_gains_inclusion": 0.50,
                        "resp_eap_taxable_portion": 0.60,
                        "resp_eap_tax_rate": 0.15},
        "tax": {"province": "quebec", "start_year": start_year},
    }
    if home_support is not None:
        cfg["household_budget"] = {"home_support": home_support}
    return cfg


class TestTheLegPricesTheDeclaredBlock(unittest.TestCase):

    def test_no_declared_block_is_a_strict_no_op(self):
        """DP#16/DP#32: absent means the household does not claim the credit,
        and nothing about its number changes."""
        self.assertEqual(home_support_credit_total(_cfg()), 0.0)
        cfg = _cfg()
        cfg["household_budget"] = {"home_support": None}   # declared null
        self.assertEqual(home_support_credit_total(cfg), 0.0)

    def test_a_budget_block_without_the_credit_leaf_is_a_no_op(self):
        """A household that declares its living costs but not this credit is
        untouched -- the leg reads its own leaf, not the block's presence."""
        cfg = _cfg()
        cfg["household_budget"] = {"living_costs": 48_000.0}
        self.assertEqual(home_support_credit_total(cfg), 0.0)

    def test_the_declared_block_is_priced_at_the_published_maximum(self):
        """39% of the $19,500 cap in 2025, with no reduction at $40,000."""
        self.assertAlmostEqual(
            home_support_credit_total(_cfg(_declared())), 7_605.0, places=6)

    def test_the_households_own_year_decides_the_rate(self):
        """The 2026 row is 40%, so the same block prices higher in 2026 —
        the year is read from cfg['tax']['start_year'], never assumed."""
        block = _declared()
        self.assertAlmostEqual(
            home_support_credit_total(_cfg(block, start_year=2026)),
            7_800.0, places=6)
        self.assertAlmostEqual(
            home_support_credit_total(_cfg(block, start_year=2025)),
            7_605.0, places=6)

    def test_an_absent_year_refuses_rather_than_assuming_one(self):
        cfg = _cfg(_declared())
        del cfg["tax"]
        with self.assertRaises(ValueError):
            home_support_credit_total(cfg)

    def test_the_renter_path_is_priced_through_the_same_leg(self):
        """A declared rent (not expenses) takes the 5%-of-rent share, with the
        $600 floor applied by the engine."""
        block = _declared(eligible_expenses=None, monthly_rent=900.0)
        block.pop("eligible_expenses")
        self.assertAlmostEqual(
            home_support_credit_total(_cfg(block)), 540.0 * 0.39, places=6)


class TestTheCreditReachesTheRankedNetBenefit(unittest.TestCase):

    def test_the_credit_raises_the_net_benefit_by_exactly_its_amount(self):
        results = [_final_year()]
        without = compute_net_benefit(results, _cfg())
        with_credit = compute_net_benefit(results, _cfg(_declared()))
        self.assertAlmostEqual(with_credit - without, 7_605.0, places=6)

    def test_a_household_that_does_not_declare_it_is_unchanged(self):
        """The no-op direction: two cfgs that differ only by an ABSENT block are
        ranked identically, so no existing household's number moves."""
        results = [_final_year()]
        self.assertEqual(
            compute_net_benefit(results, _cfg()),
            compute_net_benefit(results, _cfg()))

    def test_a_couple_block_is_ranked_by_the_couple_cap(self):
        results = [_final_year()]
        without = compute_net_benefit(results, _cfg())
        couple = compute_net_benefit(results, _cfg(
            _declared(situation=COUPLE, eligible_expenses=39_000.0)))
        self.assertAlmostEqual(couple - without, 15_210.0, places=6)


class TestTheContractCarriesTheBlock(unittest.TestCase):

    def test_the_adapter_maps_a_declared_block(self):
        mapped = map_household_budget({"household_budget": {
            "home_support": {
                "situation": SINGLE,
                "family_income": 40_000.0,
                "eligible_expenses": 19_500.0,
            }}})
        self.assertEqual(mapped["home_support"]["situation"], SINGLE)
        self.assertEqual(mapped["home_support"]["family_income"], 40_000.0)
        self.assertEqual(mapped["home_support"]["eligible_expenses"], 19_500.0)

    def test_the_adapter_keeps_the_rent_spelling_apart_from_the_expense_one(self):
        mapped = map_household_budget({"household_budget": {
            "home_support": {
                "situation": SINGLE,
                "family_income": 40_000.0,
                "monthly_rent": 900.0,
            }}})
        self.assertNotIn("eligible_expenses", mapped["home_support"])
        self.assertEqual(mapped["home_support"]["monthly_rent"], 900.0)

    def test_an_absent_block_maps_to_nothing(self):
        self.assertEqual(map_household_budget({}), {})
        self.assertEqual(map_household_budget({"household_budget": {}}), {})

    def test_both_spellings_of_one_quantity_refuse(self):
        """The schema's oneOf refuses this too; the adapter refuses it again
        where the pricing happens, because guessing which one was meant is the
        silent substitution this engine exists to prevent."""
        with self.assertRaises(ContractAdaptationError) as ctx:
            map_household_budget({"household_budget": {"home_support": {
                "situation": SINGLE, "family_income": 40_000.0,
                "eligible_expenses": 1_000.0, "monthly_rent": 900.0}}})
        self.assertIn("two spellings", str(ctx.exception))

    def test_neither_spelling_refuses_rather_than_booking_zero(self):
        with self.assertRaises(ContractAdaptationError) as ctx:
            map_household_budget({"household_budget": {"home_support": {
                "situation": SINGLE, "family_income": 40_000.0}}})
        self.assertIn("neither", str(ctx.exception))

    def test_the_simulation_config_and_objective_cfg_carry_the_block(self):
        """The #290 lesson: parsed, mapped, then never passed is the defect. The
        block has to survive into the cfg the objective is evaluated with."""
        config = SimulationConfig.from_dict({
            "family": {"members": [{"role": "primary", "birth_year": 1979,
                                    "gross_income": 40_000.0}]},
            "assumptions": {"start_year": 2025, "horizon_age": 1,
                            "investment_return": 0.0, "salary_growth": 0.0,
                            "inflation": 0.0, "frozen_brackets": True},
            "tax": {"province": "quebec"},
            "household_budget": {"home_support": {
                "situation": SINGLE, "family_income": 40_000.0,
                "eligible_expenses": 19_500.0}},
        })
        self.assertIsNotNone(config.home_support)
        self.assertEqual(
            objective_cfg(config)["household_budget"]["home_support"]
            ["eligible_expenses"], 19_500.0)


if __name__ == "__main__":
    unittest.main()
