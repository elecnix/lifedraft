"""Issue #1035: the Quebec investment-expense limitation (TA s.336.0.1 /
TP-1 Schedule L) inside ``apply_sm_interest`` was wrong three ways:

  1. the carry-forward was stranded forever -- never released at death, even
     though the terminal deemed disposition IS an investment-income event and
     TA s.336.0.1 permits application in a later year INCLUDING the year of
     death. Fixed: the fold threads ``sm_qc_carry_forward`` into
     ``compute_estate``, which releases it against the deemed disposition's
     taxable gains on the non-reg + SM pots, valued on the QUEBEC provincial
     brackets only;
  2. the cap applied unconditionally to EVERY province, and the capped amount
     was valued at one blended combined rate -- suppressing the FEDERAL
     deduction (the ITA has no investment-income limit). Fixed: the cap is
     applied to Quebec households only, and the saving is valued as a federal
     slice (on the UNCAPPED total) + Quebec slice (on the capped amount);
  3. the income base was ``balance * yield_rate`` instead of the Schedule L
     income breakdown. Fixed: eligible/non-eligible dividends + interest +
     HALF the declared capital-gain yield component, so a dividend-tilted and
     a growth-tilted portfolio with equal total return produce different
     ``sm_qc_deductible`` paths.

Every household below is fabricated, round-numbered, role-named (DP#15).
Tests drive the live fold / the real estate computation (DP#11/DP#18).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copy import copy

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig


HOUSE_VALUE = 800_000
MORTGAGE_BALANCE = 480_000
LUMP_SUM = 480_000          # cash-out advance, invested in non-reg
HELOC_RATE = 0.055
GROSS_INCOME = 150_000
LIVING_COSTS = 60_000


def _portfolio(yield_dict):
    "The non-reg portfolio block (the Schedule L income base comes from here)."
    return {"accounts": {"non_reg": {
        "balance": 0, "cost_basis": 0,
        "composition": {"cdn_equity_pct": 0.6, "fixed_income_pct": 0.4},
        "yield": yield_dict,
    }}}


def _cfg(province="qc", projection_years=25, yield_dict=None):
    "A working household with a large borrowed-to-invest lump sum."
    if yield_dict is None:
        yield_dict = {"eligible_dividends": 0.02}
    return {
        "family": {"members": [{"role": "primary", "birth_year": 1990,
            "retirement_age": 65, "gross_income": GROSS_INCOME,
            "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0}],
            "children": []},
        "property": {"house_value": HOUSE_VALUE,
            "mortgage_balance": MORTGAGE_BALANCE,
            "margin_available": 0, "heloc_rate": HELOC_RATE,
            "mortgage_rate": 0.0370, "ltv_max": 0.80,
            "amortization_years": 25},
        "assumptions": {"start_year": 2026, "projection_years": projection_years,
            "investment_return": 0.06, "salary_growth": 0.0,
            "inflation": 0.0, "frozen_brackets": True},
        "portfolio": _portfolio(yield_dict),
        "accounts": {"rrsp_annual_max": 0},
        "household_budget": {"annual_living_costs": LIVING_COSTS},
        "tax": {"province": province},
    }


def _run(cfg_dict, lump_sum=LUMP_SUM):
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(
        cfg, adapter=CanadaAdapter(cfg), use_readvanceable=False,
        deduct_later=False, lump_sum=lump_sum)
    return sim.run()


def _traced_interest(r):
    "The year's purpose-traced s.20(1)(c) interest across both #850 legs."
    return r.advance_deductible_interest + r.margin_deductible_interest


# ============================================================================
# 1. Defect 2a: a NON-Quebec household is NOT capped
# ============================================================================

class TestNonQuebecHouseholdIsUncapped:
    """No province but Quebec limits investment expenses to investment income.
    An Ontario household deducts the full traced interest every year."""

    def test_ontario_deducts_the_full_traced_interest_every_year(self):
        rs = _run(_cfg(province="ontario", projection_years=8))
        assert any(_traced_interest(r) > 0 for r in rs), (
            "premise: the fixture must produce traced deductible interest")
        for r in rs:
            assert r.sm_qc_deductible == pytest.approx(_traced_interest(r)), (
                f"year {r.year}: an Ontario household faces no QC cap")
        assert all(r.sm_qc_carry_forward == 0.0 for r in rs)

    def test_quebec_same_household_IS_capped(self):
        """The SAME numbers under tax.province=qc: the cap binds and strands."""
        rs = _run(_cfg(province="qc", projection_years=8))
        assert any(r.sm_qc_carry_forward > 0 for r in rs), (
            "premise: the QC household must strand carry-forward (the cap "
            "must bind for this fixture)")
        assert any(r.sm_qc_deductible < _traced_interest(r) - 1e-9
                   for r in rs), (
            "premise: some year must claim less than the traced interest")


# ============================================================================
# 2. Defect 2b: the federal slice is uncapped (separate savings)
# ============================================================================

class TestFederalSliceUncapped:
    """The side-credit must be a federal + Quebec pair, not one capped amount
    at a blended combined rate. When the cap binds, the saving must exceed the
    pre-#1035 spelling (the capped amount at the combined brackets)."""

    def test_savings_exceed_the_pre_1035_combined_valuation_of_the_cap(self):
        """A/B through the live fold: with the split DISABLED (the fallback
        path values the capped amount at the combined brackets -- exactly the
        pre-#1035 spelling), the leveraged QC household's side-credit is
        SMALLER than with the federal slice uncapped."""
        import rules_leverage
        cfg = _cfg(province="qc", projection_years=8)
        rs_new = _run(cfg)
        original = rules_leverage._year_split_brackets_for
        rules_leverage._year_split_brackets_for = lambda ctx: (None, None)
        try:
            rs_old = _run(cfg)
        finally:
            rules_leverage._year_split_brackets_for = original
        saved_new = sum(r.readvance_tax_savings + r.traced_borrowing_tax_savings
                        for r in rs_new)
        saved_old = sum(r.readvance_tax_savings + r.traced_borrowing_tax_savings
                        for r in rs_old)
        assert any(r.sm_qc_deductible < _traced_interest(r) - 1e-9
                   for r in rs_new), (
            "premise: the cap must bind for this fixture")
        assert saved_new > saved_old + 1e-6, (
            "valuing the UNCAPPED federal slice must beat the pre-#1035 "
            f"blended valuation of the capped amount ({saved_new} vs {saved_old})")

    def test_when_the_cap_does_not_bind_the_split_equals_the_combined(self):
        """Sanity on the split: fed(total) + prov(total) reproduces the
        combined valuation when nothing is capped (no double-count)."""
        from tax_calculator import deduction_value
        from tax_data import default_tax_provider
        p = default_tax_provider()
        fed, prov = p.get_split_brackets()
        combined = p.get_combined_brackets()
        income = 90_000.0
        amount = 4_000.0
        split = (deduction_value(income, amount, fed)
                 + deduction_value(income, amount, prov))
        assert split == pytest.approx(deduction_value(income, amount, combined))


# ============================================================================
# 3. Defect 3: the income base is the Schedule L breakdown
# ============================================================================

class TestScheduleLIncomeBase:
    """A dividend-tilted and a growth-tilted QC portfolio with equal declared
    total yield must produce DIFFERENT sm_qc_deductible paths: dividends count
    at 100% (Schedule L lines 2-3), capital gains only at the inclusion rate
    (line 5)."""

    def test_dividend_tilt_vs_growth_tilt_diverge(self):
        div_rs = _run(_cfg(projection_years=8,
                           yield_dict={"eligible_dividends": 0.02}))
        gro_rs = _run(_cfg(projection_years=8,
                           yield_dict={"capital_gains": 0.02}))
        div_claimed = sum(r.sm_qc_deductible for r in div_rs)
        gro_claimed = sum(r.sm_qc_deductible for r in gro_rs)
        assert div_claimed > gro_claimed + 1e-6, (
            "the dividend portfolio (100%-counting income) must claim more "
            "than the growth portfolio (50%-counting) over the same horizon")
        assert all(r.sm_qc_carry_forward >= 0 for r in gro_rs)

    def test_no_declared_yield_reproduces_the_balance_times_yield_base(self):
        """Without a declared portfolio yield, every component falls back to
        the configurable non_reg_yield_rate as interest -- byte-identical to
        the pre-#1035 base (DP#32)."""
        cfg = _cfg(projection_years=5)
        del cfg["portfolio"]
        rs_new = _run(cfg)
        # The old engine's base was balance * non_reg_yield_rate; assert the
        # new path claims exactly what that base allows in year 1.
        y1 = rs_new[0]
        expected_cap = y1.non_reg_balance * 0.02
        assert y1.sm_qc_deductible == pytest.approx(
            min(expected_cap, _traced_interest(y1))), (
            "fallback base must equal balance x non_reg_yield_rate")


# ============================================================================
# 4. Defect 1: the carry-forward is released at death
# ============================================================================

class TestCarryForwardReleasedAtDeath:
    """TA s.336.0.1 permits applying the carry-forward in a later year
    INCLUDING the year of death. The deemed disposition's taxable gains are
    investment income; compute_estate must release the stranded amount
    against them, on the QUEBEC provincial brackets."""

    def test_stranded_carry_forward_is_recovered_in_the_estate(self):
        from objective import compute_after_tax_estate
        cfg = _cfg(projection_years=12)
        rs = _run(cfg)
        stranded = rs[-1].sm_qc_carry_forward
        assert stranded > 0, (
            "premise: the leveraged QC household must strand carry-forward")
        estate = compute_after_tax_estate(rs, cfg)
        assert estate.qc_carryforward_relief > 0, (
            "the terminal deemed disposition must release the carry-forward")
        assert estate.qc_carryforward_relief <= stranded + 1e-6

        # Control: the same terminal balance sheet with the carry-forward
        # zeroed (the pre-#1035 world) recovers NOTHING.
        stripped = [copy(r) for r in rs]
        stripped[-1].sm_qc_carry_forward = 0.0
        control = compute_after_tax_estate(stripped, cfg)
        assert control.qc_carryforward_relief == 0.0
        assert estate.net_estate == pytest.approx(
            control.net_estate + estate.qc_carryforward_relief)

    def test_compute_estate_demands_provincial_brackets_for_a_carry_forward(self):
        """A Quebec-only deduction cannot be valued on combined brackets:
        refuse loudly rather than guess (DP#32)."""
        import countries.canada.estate as estate_mod
        plan = estate_mod.EstatePlan(
            spousal_rollover=False, tfsa_successor_holder=True,
            non_reg_primary_share=1.0)
        members = estate_mod.couple_terminal_returns(
            registered_primary=100_000.0, registered_spouse=50_000.0,
            plan=plan)
        with pytest.raises(estate_mod.EstateInputError):
            estate_mod.compute_estate(
                members=members, tfsa=0.0, non_reg_fmv=400_000.0,
                non_reg_acb=100_000.0, house_equity=500_000.0, debts=0.0,
                brackets=[{"min": 0, "max": 0, "rate": 0.3, "label": ""}],
                plan=plan, qc_carry_forward=7_000.0)

    def test_compute_estate_release_is_valued_on_provincial_brackets_only(self):
        """Unit: one member, no rollover. The relief equals the Quebec-only
        bracket-fill value of min(carry_forward, taxable gains) -- NOT the
        combined-bracket value."""
        import countries.canada.estate as estate_mod
        from tax_calculator import deduction_value
        from tax_data import default_tax_provider
        _, prov = default_tax_provider().get_split_brackets()
        plan = estate_mod.EstatePlan(
            spousal_rollover=False, tfsa_successor_holder=True,
            non_reg_primary_share=1.0)
        member = estate_mod.TerminalReturn(
            registered=0.0, non_reg_share=1.0, property_share=1.0)
        cf = 7_000.0
        result = estate_mod.compute_estate(
            members=[member], tfsa=0.0, non_reg_fmv=600_000.0,
            non_reg_acb=200_000.0, house_equity=0.0, debts=0.0,
            brackets=default_tax_provider().get_combined_brackets(),
            plan=plan, qc_carry_forward=cf,
            qc_provincial_brackets=prov)
        taxable_gain = (600_000.0 - 200_000.0) * 0.5
        allowed = min(cf, taxable_gain)
        assert result.qc_carryforward_relief == pytest.approx(
            deduction_value(taxable_gain, allowed, prov))
        assert result.total_tax == pytest.approx(
            result.registered_tax + result.non_reg_tax + result.sm_investment_tax
            + result.taxable_property_tax + result.cca_recapture_tax
            - result.qc_carryforward_relief)

    def test_zero_carry_forward_is_byte_identical(self):
        """A household that never stranded anything: the estate is untouched."""
        import countries.canada.estate as estate_mod
        from tax_data import default_tax_provider
        p = default_tax_provider()
        plan = estate_mod.EstatePlan(
            spousal_rollover=False, tfsa_successor_holder=True,
            non_reg_primary_share=1.0)
        member = estate_mod.TerminalReturn(
            registered=0.0, non_reg_share=1.0, property_share=1.0)
        kwargs = dict(
            members=[member], tfsa=0.0, non_reg_fmv=600_000.0,
            non_reg_acb=200_000.0, house_equity=0.0, debts=0.0,
            brackets=p.get_combined_brackets(), plan=plan)
        baseline = estate_mod.compute_estate(**kwargs)
        with_cf = estate_mod.compute_estate(
            **kwargs, qc_carry_forward=0.0,
            qc_provincial_brackets=p.get_split_brackets()[1])
        assert with_cf.total_tax == pytest.approx(baseline.total_tax)
        assert with_cf.net_estate == pytest.approx(baseline.net_estate)


# ============================================================================
# 6. The split's own fallback branches (DP#17: both sides of the seam)
# ============================================================================

class TestSplitFallbackBranches:
    """When no split exists for the year, the fold falls back to the pre-#1035
    combined valuation instead of fabricating a split (DP#32 -- and never
    crashes a run that the combined brackets could price)."""

    def test_year_split_brackets_falls_back_without_config(self):
        import rule_registry
        import rules_leverage
        ctx = rule_registry.RuleContext(
            year=0, calendar_year=2026, allocations={}, config=None,
            investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
            mortgage_data=None, use_readvanceable=False, deduct_later=False,
            primary_marginal_rate=0.4, spouse_marginal_rate=0.3, resp_data=None,
            fhsa_contribution=0.0, rrsp_annual_limit=None,
            tfsa_annual_limit=None, fhsa_annual_limit=None,
            non_reg_after_tax_return=None, cpp_income=0.0, oas_income=0.0,
            pension_income=0.0, drawdown_order=None,
            rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
            drawdown_net_target=0.0, retiree_marginal_rate=0.0,
            drawdown_bracket_target=None, drawdown_other_taxable_income=0.0)
        assert rules_leverage._year_split_brackets_for(ctx) == (None, None)

    def test_year_split_brackets_falls_back_when_provider_has_no_data(self):
        import rule_registry
        import rules_leverage
        class _NoDataProvider:
            def get_split_brackets(self, year, province="quebec"):
                raise ValueError(f"No tax data for {year}")
        cfg = SimulationConfig.from_dict(_cfg(projection_years=2))
        ctx = rule_registry.RuleContext(
            year=0, calendar_year=2026, allocations={}, config=cfg,
            investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
            mortgage_data=None, use_readvanceable=False, deduct_later=False,
            primary_marginal_rate=0.4, spouse_marginal_rate=0.3, resp_data=None,
            fhsa_contribution=0.0, rrsp_annual_limit=None,
            tfsa_annual_limit=None, fhsa_annual_limit=None,
            non_reg_after_tax_return=None, cpp_income=0.0, oas_income=0.0,
            pension_income=0.0, drawdown_order=None,
            rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
            drawdown_net_target=0.0, retiree_marginal_rate=0.0,
            drawdown_bracket_target=None, drawdown_other_taxable_income=0.0,
            tax_provider=_NoDataProvider())
        assert rules_leverage._year_split_brackets_for(ctx) == (None, None)

    def test_qc_provincial_brackets_returns_none_without_data(self):
        from objective import _qc_provincial_brackets
        # A province with no tax data at all -> ValueError -> None.
        assert _qc_provincial_brackets(
            2026, {"tax": {"province": "atlantis"}}) is None


# ============================================================================
# 5. The golden gate
# ============================================================================

class TestGoldenUnchanged:
    """The golden household carries no SM/margin debt: none of this fires."""

    def test_golden_terminal_total_assets_is_byte_exact(self):
        from test_golden_trajectory_581 import (
            golden_household_config, _run as _golden_run)
        results = _golden_run(golden_household_config())
        assert results[-1].total_assets == pytest.approx(9_709_753.139463063)
