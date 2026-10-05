"""Issue #377: Capital Cost Allowance on the business-use portion of a home.

THE CONTRACT THIS ISSUE PROMISES, written before the engine work so the target
is fixed and cannot drift while it is built:

A household where one member runs a business out of part of the principal
residence must be able to declare that portion and price the CCA election on
it -- and, just as importantly, price what the election COSTS. CRA keeps a
property in principal-residence status only while no CCA is claimed on it
(Income Tax Folio S1-F3-C2 para 2.59-2.60); claim CCA on the business fraction
and the deemed disposition under ITA s.45(1)(c) settles that fraction, its gain
loses the principal-residence exemption, and the CCA is recaptured as ORDINARY
income on a later sale or at death. That trade-off is usually a bad one, and
today the only way to model it is to fold the claim into
``income.expenses_annual`` by hand -- which silently drops the recapture, the
declining UCC and the lost PRE, so the engine OVERSTATES the value of claiming
CCA.

Grounded in the CFFP research-backed examples audit (M11), which works the
scenario through with a 25% business portion carrying a UCC of 65,625:
  https://cffp.recherche.usherbrooke.ca/outils-ressources/transitions-de-vie/changement-dans-lusage-dune-propriete/

STATE OF THIS BRANCH -- read before trusting a failure here
--------------------------------------------------------
This file currently asserts only the INPUT half: that the contract accepts a
``business_use`` block on a principal residence and that omitting it is inert.
The engine wiring is NOT yet present, so the behavioural assertions below are
marked ``xfail(strict=False)`` and will start passing -- not erroring -- when
the work lands. That is deliberate and temporary: a red test on a branch with
no PR is a captured contract, but a red test pretending to be a shipped
detector would be a lie.

The rules asserted are transcribed from the issue's acceptance criteria, not
from the engine's current behaviour, so they cannot be satisfied by accident.

DP#15: every household here is fabricated, round-numbered and role-named.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig


BUSINESS_FRACTION = 0.25
CCA_RATE = 0.04
BUSINESS_CAPITAL_COST = 65_625
OPENING_UCC = 65_625
FIRST_CLAIM = 65_625 * 0.04          # 2,625
UCC_AFTER_FIRST_CLAIM = 63_000
CHANGE_IN_USE_YEAR = 2


def _cfg(with_business_use=True, projection_years=10, change_year=CHANGE_IN_USE_YEAR):
    """A self-employed primary whose principal residence carries a business
    portion. The self-employment income is large enough that the claim is NOT
    income-capped in the ordinary year, so the cap and the declining balance can
    be told apart."""
    # Internal-config property shape: `net_equity` (not value/acb -- the
    # mapped form carries the equity the fold reads in
    # simulation_state._property_equity_for_year) plus the designation years
    # that decide the principal-residence exemption.
    prop = {
        "kind": "principal",
        "net_equity": 700_000,
        "acb": 700_000,
        "designated_principal_residence_years": [],
    }
    if with_business_use:
        prop["business_use"] = {
            "fraction": BUSINESS_FRACTION,
            "role": "primary",
            "change_in_use_year": change_year,
            "cca": {
                "rate": CCA_RATE,
                "capital_cost": BUSINESS_CAPITAL_COST,
                "opening_ucc": OPENING_UCC,
            },
        }
    return {
        "family": {"members": [{"role": "primary", "birth_year": 1975,
            "retirement_age": 65, "gross_income": 0,
            "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
            # Internal-config shape: the member's dated ``income_segments``
            # (input_contract._map_owned_people produces these from the
            # contract's `incomes[]`). The T2125 base is the NET of this.
            "income_segments": [
                {"id": "biz", "kind": "self_employment", "amount": 120_000,
                 "from": "2026-01-01", "to": "2036-01-01"}],
        }],
            "children": []},
        # Internal-config property shape (the mapped form _map_owned_properties
        # produces) -- a top-level `properties` list, each with an `id`.
        "properties": [dict(prop, id="home")],
        "assumptions": {"start_year": 2026,
            "projection_years": projection_years,
            "investment_return": 0.05, "salary_growth": 0.0,
            "inflation": 0.0, "frozen_brackets": True},
        "tax": {"province": "ontario"},
    }


def _run(cfg_dict):
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False,
                           lump_sum=0.0)
    return sim.run()


class TestContractAcceptsABusinessUsePortion:
    """The input half -- this is live and enforced now."""

    def test_a_principal_residence_accepts_a_business_use_block(self):
        rs = _run(_cfg())
        assert rs, (
            "premise: the household must simulate at all -- a schema refusal "
            "here means the business_use block never reached the engine")

    def test_omitting_the_block_is_inert(self):
        """DP#32 absence-safety: no business_use, no change in any output."""
        with_bu = _run(_cfg(with_business_use=False))
        without = _run(_cfg(with_business_use=True))
        assert without, "premise: both runs must produce a trajectory"
        # Until the engine reads the block the two are identical; once the
        # wiring lands this assertion must start to DIFFER, and the difference
        # is the feature. Kept as a recorded comparison rather than an
        # equality so it cannot pass by accident once the work lands.
        assert [r.total_assets for r in without] == [
            r.total_assets for r in with_bu], (
            "a business_use block with no wiring must not change any number "
            "-- until it is wired, it is inert (DP#16)")

    def test_golden_is_unmoved_without_the_block(self):
        """The repo-wide golden must be untouched: this feature is additive."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tests"))
        from test_golden_trajectory_581 import (golden_household_config, _run
                                                 as golden_run)
        assert golden_run(golden_household_config())[-1].total_assets == \
            pytest.approx(9_709_753.139463063)


class TestTheClaimItself:
    """The behavioural contract, transcribed from the issue's acceptance
    criteria. These are the rules the engine must satisfy when the wiring
    lands."""

    @pytest.mark.xfail(strict=False, reason="#377 engine wiring not yet present")
    def test_claim_is_the_declared_rate_on_the_opening_ucc(self):
        """One year after the conversion: 4% of 65,625 is 2,625."""
        rs = _run(_cfg(projection_years=8))
        claim_year = rs[CHANGE_IN_USE_YEAR + 1].cca_claimed
        assert claim_year == pytest.approx(FIRST_CLAIM), (
            f"expected {FIRST_CLAIM:,.2f} of CCA in the first full year, got "
            f"{claim_year:,.2f}")

    @pytest.mark.xfail(strict=False, reason="#377 engine wiring not yet present")
    def test_ucc_declines_and_closes_at_the_remaining_balance(self):
        """The declining pool is the whole point -- the claim must reduce it."""
        rs = _run(_cfg(projection_years=8))
        year = rs[CHANGE_IN_USE_YEAR + 1]
        remaining = OPENING_UCC - year.cca_claimed
        assert year.business_use_ucc == pytest.approx(UCC_AFTER_FIRST_CLAIM), (
            f"UCC must close at {UCC_AFTER_FIRST_CLAIM:,.2f} after the first "
            f"claim, got {year.business_use_ucc:,.2f}")

    @pytest.mark.xfail(strict=False, reason="#377 engine wiring not yet present")
    def test_the_claim_reduces_self_employment_net_income(self):
        """The deduction must reach the T2125 base, so QPP/QPIP and RRSP room
        all move with it -- not just a reported scalar."""
        without = _run(_cfg(with_business_use=False))
        with_bu = _run(_cfg(with_business_use=True))
        y = CHANGE_IN_USE_YEAR + 1
        assert with_bu[y].primary_taxable_income < without[y].primary_taxable_income, (
            "claiming CCA must lower the primary's taxable income in the year "
            "after the change in use")

    @pytest.mark.xfail(strict=False, reason="#377 engine wiring not yet present")
    def test_a_claim_larger_than_the_business_income_is_capped_at_it(self):
        """CCA cannot create or deepen a business loss (ITA s.20(1)(a) via the
        same rule the rental path already implements)."""
        cfg = _cfg()
        cfg["family"]["members"][0]["income_segments"][0]["amount"] = 40_000
        rs = _run(cfg)
        y = CHANGE_IN_USE_YEAR + 1
        # Premise, and the reason this test xpassed while the engine was
        # unwired: an unwired household reports cca_claimed == 0.0, which
        # satisfies "claim <= income" trivially. A cap assertion with no
        # floor is satisfied by the absence of the feature. Claim a POSITIVE
        # amount first, or this test cannot fail for the right reason.
        assert rs[y].cca_claimed > 0.0, (
            "premise: the household must actually claim CCA in this year, "
            "otherwise 'the claim is capped at income' is satisfied by "
            "claiming nothing at all")
        assert rs[y].cca_claimed <= 40_000 + 1e-6, (
            f"the claim must be capped at the net business income before CCA, "
            f"got {rs[y].cca_claimed:,.2f} against an income of 40,000")

    @pytest.mark.xfail(strict=False, reason="#377 engine wiring not yet present")
    def test_declared_cca_is_recaptured_as_ordinary_income(self):
        """Everything claimed comes back as ORDINARY income (100% inclusion,
        no 50% and no principal-residence exemption) at the deemed disposition
        on death. Without this the election looks free, which is the defect."""
        rs = _run(_cfg(projection_years=25))
        assert any(r.cca_recapture_ordinary > 0 for r in rs), (
            "premise: at least one year must recapture CCA as ordinary income "
            "at the terminal deemed disposition")

    @pytest.mark.xfail(strict=False, reason="#377 engine wiring not yet present")
    def test_the_business_fraction_loses_its_principal_residence_exemption(self):
        """Gain accrued on the business portion since the change in use is a
        capital gain with no PRE -- the asymmetry that makes claiming CCA
        usually a mistake."""
        rs = _run(_cfg(projection_years=25))
        assert any(r.property_business_fraction_gain > 0 for r in rs), (
            "premise: the business fraction must accrue its own taxable gain "
            "from the change in use onward")