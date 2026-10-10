"""Issue #437: a reinvested portfolio distribution is taxed twice.

Non-reg and Smith-Manoeuvre growth is modelled NET of tax. The declared
distribution yield is removed from the growth rate component-by-component
(``AccountPortfolio.after_tax_return_by_account`` -> per-income-type
``effective_tax_rate``), so the distribution is taxed once, in the year it is
earned. That part is correct.

It was then taxed a SECOND time. ACB is cost basis and was pinned to the
opening value plus contributions -- ``rules_growth``'s own comment said "ACB
does NOT grow with returns (it's cost basis)" (DP#19). So the after-tax
distribution, reinvested into the pot, ended up INSIDE ``balance - acb``, and
the drawdown's taxable slice is ``take * gain_frac * cg_inclusion`` with
``gain_frac = (non_reg_balance - non_reg_acb) / non_reg_balance``
(``retirement_transition.py``). The already-taxed dollars were therefore
realized as capital gain at 50% inclusion, in whatever year the pot is drawn.

Statutorily that is wrong: interest or dividends received form part of the
cost of the securities purchased with them, so selling later realizes the gain
over the NEW cost, not over the original outlay. DP#19's intent is explicit --
cost basis records what you paid, and only the appreciation above it is gain.

Measured on the pre-fix tree at ``c949015``: a household with a 6% gross return
and a 4% declared interest distribution carried 908,551 of "unrealized gain"
after 29 accumulation years, where the engine charged capital-gains tax on the
whole thing at withdrawal. Only 2%/yr of that 6% is appreciation.

Tests drive the live fold and read ``YearResult`` (DP#11/DP#18) -- never a
reimplementation of the engine.

DP#15: every household here is fabricated, round-numbered, role-named.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig


HOUSE_VALUE = 800_000
MORTGAGE_BALANCE = 0
LUMP_SUM = 400_000
GROSS_INCOME = 120_000
LIVING_COSTS = 60_000
GROSS_RETURN = 0.06
DECLARED_YIELD = 0.04
START_YEAR = 2026

# The only part of the 6% gross return that is genuinely capital appreciation.
APPRECIATION = GROSS_RETURN - DECLARED_YIELD


def _cfg(yield_dict, projection_years=10, retirement_age=100):
    """A single-member household whose non-reg pot is a lump sum.

    ``retirement_age=100`` keeps the horizon inside the accumulation phase, so
    there is no drawdown to confound the ACB reading: ACB moves only with
    contributions and, after #437, with reinvested distributions.
    """
    return {
        "family": {"members": [{"role": "primary", "birth_year": 1990,
            "retirement_age": retirement_age, "gross_income": GROSS_INCOME,
            "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0}],
            "children": []},
        "property": {"house_value": HOUSE_VALUE,
            "mortgage_balance": MORTGAGE_BALANCE,
            "margin_available": 0, "heloc_rate": 0.055,
            "mortgage_rate": 0.0370, "ltv_max": 0.80,
            "amortization_years": 25},
        "assumptions": {"start_year": START_YEAR,
            "projection_years": projection_years,
            "investment_return": GROSS_RETURN, "salary_growth": 0.0,
            "inflation": 0.0, "frozen_brackets": True},
        "portfolio": {"accounts": {"non_reg": {
            "balance": 0, "cost_basis": 0,
            "composition": {"cdn_equity_pct": 0.6, "fixed_income_pct": 0.4},
            "yield": yield_dict,
        }}},
        "accounts": {"rrsp_annual_max": 0},
        "household_budget": {"annual_living_costs": LIVING_COSTS},
        "tax": {"province": "ontario"},
    }


def _run(yield_dict, **kw):
    cfg = SimulationConfig.from_dict(_cfg(yield_dict, **kw))
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False,
                           lump_sum=LUMP_SUM)
    return sim.run()


def _interest_eff_rate(marginal_rate):
    from countries.canada.income_type import IncomeType, effective_tax_rate
    return effective_tax_rate(IncomeType.INTEREST, marginal_rate,
                              "ontario", "non_reg")


class TestReinvestedDistributionRaisesAcb:
    """A distribution is new money entering the account. It must join ACB."""

    def test_acb_rises_when_a_distribution_is_reinvested(self):
        rs = _run({"interest": DECLARED_YIELD, "capital_gains": 0.0})
        first, last = rs[0], rs[-1]
        assert first.non_reg_acb > LUMP_SUM, (
            f"premise: ACB starts at the declared {LUMP_SUM} and year 1 "
            f"already absorbs a distribution, got {first.non_reg_acb:,.2f}")
        assert last.non_reg_acb > first.non_reg_acb, (
            f"ACB never rose across {len(rs)} years of a "
            f"{DECLARED_YIELD:.0%} declared distribution: "
            f"{first.non_reg_acb:,.0f} -> {last.non_reg_acb:,.0f}. The "
            "reinvested distribution is not joining cost basis, so it sits "
            "inside balance - acb and is re-taxed as capital gain on "
            "withdrawal (issue #437).")

    def test_unrealized_gain_accrues_at_the_appreciation_rate_only(self):
        """The load-bearing assertion.

        ``non_reg_unrealized_gains`` is ``balance - acb`` -- the pot's claimed
        capital gain. Only the part of the gross return that is NOT declared
        as a distribution is appreciation, so the gain must accrue at exactly
        the appreciation rate on the opening balance, never at the whole
        after-tax return.

        Stated per year, because that is the only form in which it is exact:
        appreciation is a percentage of a balance that itself includes the
        reinvested distributions, so the gain does not compound as an isolated
        sub-pot and a closed form over many years would be a guess.
        """
        rs = _run({"interest": DECLARED_YIELD, "capital_gains": 0.0},
                  projection_years=10)
        prev_gain = 0.0
        prev_balance = LUMP_SUM
        for r in rs:
            gain = r.non_reg_unrealized_gains
            expected_increment = prev_balance * APPRECIATION
            assert gain - prev_gain == pytest.approx(
                expected_increment, rel=0.01), (
                f"year {r.year}: unrealized gain rose by "
                f"{gain - prev_gain:,.2f} but only {expected_increment:,.2f} "
                f"is capital appreciation ({GROSS_RETURN:.0%} gross - "
                f"{DECLARED_YIELD:.0%} declared = {APPRECIATION:.0%} of the "
                f"opening {prev_balance:,.0f}). The excess is distribution "
                "income already taxed as earned and now carried as gain "
                "(issue #437).")
            prev_gain = gain
            prev_balance = r.non_reg_balance

    def test_no_declared_distribution_leaves_acb_alone(self):
        """The control: with nothing distributed, ACB must not drift.

        A fix that raised ACB unconditionally would pass the tests above while
        silently erasing a non-distributing household's entire gain.
        """
        rs = _run({"interest": 0.0, "capital_gains": 0.0})
        for r in rs:
            assert r.non_reg_acb == pytest.approx(LUMP_SUM), (
                f"year {r.year}: a pot that distributes nothing must keep ACB "
                f"at its declared {LUMP_SUM}, got {r.non_reg_acb:,.2f}")


class TestSmithManoeuvreSleeve:
    """The sleeve is legally non-registered, so it carries the same double
    taxation (#437's scope names it explicitly, and leaving it out would make
    this a half fix -- the fix must hold for every taxable pot, not just the
    declared non-reg one).

    Driven with a readvanceable household (the ``_cfg`` default sets
    ``use_readvanceable=False``, so this one flips it) whose sleeve is funded
    by a cash-out advance.
    """

    def _leveraged(self, declared_yield, projection_years=10):
        cfg_dict = _cfg({"interest": declared_yield, "capital_gains": 0.0},
                        projection_years=projection_years)
        # A 5-year amortization pays the mortgage off inside the horizon, so
        # the LATE years readvance nothing (``mortgage_principal == 0``) and
        # the sleeve's FMV moves only by growth. Without such a year the gain
        # identity below never gets a chance to run -- an earlier version of
        # this fixture readvanced every single year and passed vacuously.
        cfg_dict["property"]["mortgage_balance"] = 400_000
        cfg_dict["property"]["amortization_years"] = 5
        cfg = SimulationConfig.from_dict(cfg_dict)
        sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                               use_readvanceable=True, deduct_later=False,
                               lump_sum=300_000)
        return sim.run()

    def test_sleeve_cost_basis_absorbs_its_reinvested_distribution(self):
        rs = self._leveraged(DECLARED_YIELD)
        assert any(r.sm_investment_balance > 0 for r in rs), (
            "premise: the household must actually open and grow the sleeve, "
            "or this proves nothing about it")
        readvance_years = [r for r in rs if r.mortgage_principal == 0]
        assert readvance_years, (
            "premise: the fixture must contain years that readvance nothing, "
            "or the sleeve's growth is confounded with new borrowed money")
        # Measured only across a GROWTH-ONLY year. Comparing the first and last
        # year instead would pass vacuously: a readvance raises the sleeve's
        # cost basis all by itself, so the sleeve's ACB climbs whether or not
        # the distribution is tracked. Indexed by POSITION rather than by
        # ``rs.index(...)-1``, which silently wraps to the last year if the
        # first year happens to readvance nothing.
        positions = [i for i, r in enumerate(rs)
                     if r.mortgage_principal == 0 and i > 0]
        assert positions, (
            "premise: need a growth-only year that is not the first year, so "
            "there is an opening balance to measure the growth against")
        pos = positions[0]
        prev, curr = rs[pos - 1], rs[pos]
        acb_delta = (curr.sm_investment_cost_basis
                     - prev.sm_investment_cost_basis)
        fmv_delta = curr.sm_investment_balance - prev.sm_investment_balance
        assert acb_delta > 0.0, (
            f"in year {curr.year}, a growth-only year, the sleeve's cost "
            f"basis did not move at all ({prev.sm_investment_cost_basis:,.0f} "
            f"-> {curr.sm_investment_cost_basis:,.0f}). Only the reinvested "
            "distribution can raise it there, and it was already taxed as "
            "earned -- without it the sleeve carries already-taxed income as "
            "gain and is taxed again on the unwind (issue #437).")
        assert acb_delta < fmv_delta, (
            f"in year {curr.year} the sleeve's cost basis rose {acb_delta:,.0f} "
            f"against an FMV rise of {fmv_delta:,.0f}. Only the NET "
            "distribution joins cost basis -- capital appreciation is "
            "unrealized and must never do so (DP#19).")

    def test_sleeve_gain_fraction_is_appreciation_only(self):
        """``gain_frac = (fmv - acb) / fmv`` on the unwind -- the same
        identity the non-reg pot must satisfy."""
        rs = self._leveraged(DECLARED_YIELD)
        prev_gain = 0.0
        prev_fmv = 0.0
        checked = 0
        for r in rs:
            fmv, acb = r.sm_investment_balance, r.sm_investment_cost_basis
            gain = fmv - acb
            # Only years in which the sleeve grew on its own (no readvance
            # this year) accrue gain; a readvance adds cost basis and FMV
            # together and is not this rule's business.
            if r.mortgage_principal == 0:
                expected_increment = prev_fmv * APPRECIATION
                assert gain - prev_gain == pytest.approx(
                    expected_increment, rel=0.01), (
                    f"year {r.year}: the sleeve's unrealized gain rose by "
                    f"{gain - prev_gain:,.2f} but only {expected_increment:,.2f} "
                    f"is capital appreciation ({GROSS_RETURN:.0%} gross - "
                    f"{DECLARED_YIELD:.0%} declared) of the opening "
                    f"{prev_fmv:,.0f}. The excess is already-taxed "
                    "distribution carried as gain (issue #437).")
                checked += 1
            prev_gain = gain
            prev_fmv = fmv
        assert checked >= 2, (
            f"premise: only {checked} growth-only year(s) were checked; the "
            "identity needs at least two to be meaningful")


class TestAcbIncrementIsTheNetDistribution:
    """The magnitude of the ACB increment, not just its sign.

    What joins cost basis is the AFTER-TAX distribution -- the money actually
    reinvested -- not the gross one. Pinning it to the engine's own
    per-income-type effective rates is a unit test of that contract (DP#11
    permits a unit test of a pure function); the tests above pin the
    consequence through the fold.
    """

    def test_the_distribution_rate_is_the_declared_yield_net_of_tax(self):
        from simulation import _non_reg_after_tax_distribution_for
        rate = _non_reg_after_tax_distribution_for(
            0, 0.40, portfolio=None, non_reg_yield_rate=DECLARED_YIELD,
            province="ontario")
        assert 0.0 < rate < DECLARED_YIELD, (
            f"the distribution rate {rate} must be the declared yield net of "
            f"its own effective tax rate -- strictly less than the gross "
            f"{DECLARED_YIELD}")
        assert rate == pytest.approx(
            DECLARED_YIELD * (1 - _interest_eff_rate(0.40)), rel=1e-6)

    def test_the_acb_increment_is_net_of_tax_not_gross(self):
        """Through the fold: the first year's ACB increment is the NET
        distribution, so it is strictly between zero and the gross one."""
        one = _run({"interest": DECLARED_YIELD, "capital_gains": 0.0},
                   projection_years=1)[0]
        increment = one.non_reg_acb - LUMP_SUM
        assert increment > 0.0, (
            "premise: a declared distribution must raise ACB")
        assert increment < LUMP_SUM * DECLARED_YIELD, (
            f"ACB rose {increment:,.2f} -- at least the gross "
            f"{DECLARED_YIELD:.0%} ({LUMP_SUM * DECLARED_YIELD:,.2f}). Only "
            "the AFTER-TAX distribution is reinvested, so only that joins "
            "cost basis.")