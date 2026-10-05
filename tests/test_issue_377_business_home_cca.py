"""Issue #377: Capital Cost Allowance on the business-use portion of a home.

THE CONTRACT THIS ISSUE PROMISES, and the engine now keeps
--------------------------------------------------------
A household where one member runs a business out of part of the principal
residence must be able to declare that portion and price the CCA election on
it -- and, just as importantly, price what the election COSTS. CRA keeps a
property in principal-residence status only while no CCA is claimed on it
(Income Tax Folio S1-F3-C2 para 2.59-2.60); claim CCA on the business fraction
and the deemed disposition under ITA s.45(1)(c) settles that fraction, its gain
loses the principal-residence exemption, and the CCA is recaptured as ORDINARY
income on the s.70(5) deemed disposition at death. That trade-off is usually a
bad one, and the only alternative before #377 -- folding the claim into
``income.expenses_annual`` by hand -- silently drops the recapture, the
declining UCC and the lost PRE, so the engine OVERSTATED the value of claiming
CCA on a home.

Grounded in the CFFP research-backed examples audit (M11), which works the
scenario through with a 25% business portion carrying a UCC of 65,625:
  https://cffp.recherche.usherbrooke.ca/outils-ressources/transitions-de-vie/changement-dans-lusage-dune-propriete/

The rules asserted below are transcribed from the issue's acceptance criteria
and from the statute, not from the engine's current behaviour, so they cannot be
satisfied by accident. Two layers, deliberately:

* ``TestThePureLaw`` exercises the pure jurisdiction functions
  (``countries.canada.business_use``) -- the shape a unit test of a pure
  function is entitled to take (DP#11).
* Every other class drives the ENGINE (``FamilySimulation.run`` on the internal
  config, or ``input_contract.to_internal_config`` + the fold on a real
  contract document) and asserts on ``YearResult`` / the estate -- never on an
  intermediate the test built by hand (the shortcut AGENTS.md names).

DP#15: every household here is fabricated, round-numbered and role-named.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import contract_errors
import contract_schema
import input_contract as ic
import objective
from config_serde import config_to_dict
from countries.canada.adapter import CanadaAdapter
from countries.canada.business_use import (business_fraction_cost_and_value,
                                           business_use_claim)
from simulation import FamilySimulation
from simulation_config import SimulationConfig

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_input_contract import _load_example  # noqa: E402  (path set above)


# ── The CFFP scenario's figures ────────────────────────────────────────────
BUSINESS_FRACTION = 0.25
CCA_RATE = 0.04
BUSINESS_CAPITAL_COST = 65_625
OPENING_UCC = 65_625
FIRST_CLAIM = BUSINESS_CAPITAL_COST * CCA_RATE        # 2,625
UCC_AFTER_FIRST_CLAIM = 63_000
# The change in use is dated mid-2028, so the first FULL year the claim can run
# is 2029 -- the index below is calendar_year - START_YEAR.
CHANGE_YEAR = 2028
START_YEAR = 2026
FIRST_CLAIM_INDEX = CHANGE_YEAR + 1 - START_YEAR      # 3


def _cfg(with_business_use=True, projection_years=10,
         change_in_use_year=CHANGE_YEAR, appreciation=0.0,
         self_employment=120_000, cca=True):
    """A self-employed primary whose principal residence carries a business
    portion. The self-employment income is large enough that the claim is NOT
    income-capped in the ordinary year, so the cap and the declining balance can
    be told apart.

    Internal-config shape: the household declares ``business_use`` as a list of
    its OWN (``config.business_use``), because the principal residence is
    deliberately absent from the non-principal ``properties`` list -- its value
    reaches the engine through ``property.house_value``."""
    d = {
        "family": {"members": [{"role": "primary", "birth_year": 1975,
            "retirement_age": 65, "gross_income": 0,
            "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
            "income_segments": [
                {"id": "biz", "kind": "self_employment",
                 "amount": self_employment,
                 "from": "2026-01-01", "to": "2036-01-01"}],
            }],
            "children": []},
        # A saving rate so the tax the claim saves becomes OBSERVABLE in cash:
        # without it every terminal figure is 0 and "the deduction reached the
        # return" would be unfalsifiable (an unfalsifiable assertion is a lie).
        "savings": {"rate": 0.4},
        "assumptions": {"start_year": START_YEAR,
            "projection_years": projection_years,
            "investment_return": 0.05, "salary_growth": 0.0,
            "inflation": 0.0, "frozen_brackets": True},
        "tax": {"province": "ontario"},
        "property": {"house_value": 700_000,
                     "appreciation_rate": appreciation},
    }
    if with_business_use:
        block = {
            "property_id": "home",
            "fraction": BUSINESS_FRACTION,
            "role": "primary",
            "is_principal": True,
            "change_in_use_year": change_in_use_year,
        }
        if cca:
            block["cca"] = {
                "rate": CCA_RATE,
                "capital_cost": BUSINESS_CAPITAL_COST,
                "opening_ucc": OPENING_UCC,
                # The couple's share of the BUSINESS FRACTION's value, the
                # figure `_map_business_use` computes from the contract.
                "fmv_at_disposition": 700_000 * BUSINESS_FRACTION,
            }
        d["business_use"] = [block]
    return d


def _run(cfg_dict, monthly=False):
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False,
                           lump_sum=0.0)
    return (sim._run_monthly() if monthly else sim.run()), cfg


class TestThePureLaw:
    """The pure jurisdiction functions (DP#11) -- the arithmetic itself."""

    def test_the_claim_starts_in_the_first_full_year_after_the_change_in_use(self):
        """s.45(1)(c) fixes the fraction's cost base in the change year; the
        claim runs from the year after (the deemed addition's half-year
        allowance is deliberately not modelled -- see the module docstring)."""
        assert business_use_claim(65_625, 0.04, 120_000, 2028, 2028) == 0.0
        assert business_use_claim(65_625, 0.04, 120_000, 2028, 2029) == \
            pytest.approx(2_625.0)

    def test_the_claim_is_capped_at_the_net_business_income(self):
        """ITA s.20(1)(a): CCA cannot create or deepen a business loss. Both
        sides of the threshold, because a cap with no positive case is
        satisfied by claiming nothing at all."""
        assert business_use_claim(65_625, 0.04, 2_625, 2028, 2029) == \
            pytest.approx(2_625.0)
        assert business_use_claim(65_625, 0.04, 2_624.99, 2028, 2029) == \
            pytest.approx(2_624.99)

    def test_a_negative_business_fraction_figure_is_refused(self):
        """The estate prices the portion's gain from an (fmv, acb) pair, and a
        negative magnitude in either is not a gain -- it is a broken record.
        Refused by the plan's own validator, exactly as `property_gains` is,
        rather than quietly producing a gain that taxes the wrong way."""
        from countries.canada.estate import EstateInputError, EstatePlan
        with pytest.raises(EstateInputError) as exc:
            EstatePlan(non_reg_primary_share=0.5, property_primary_share=0.5,
                       registered_rolled_fraction=1.0, non_reg_rolled_fraction=0.0,
                       taxable_property_fmv=0.0, taxable_property_acb=0.0,
                       principal_residence_fmv=0.0, principal_residence_acb=0.0,
                       life_insurance_death_benefit=0.0,
                       spousal_rollover=True, tfsa_successor_holder=False,
                       business_use_gains=({'fmv': -1.0, 'acb': 0.0},))
        assert 'business_use_gains' in str(exc.value)

    def test_the_cost_base_is_the_value_at_the_change_in_use(self):
        """s.45(1)(c) reacquires the fraction at FMV -- so a quarter of a
        $700,000 home converted in 2026 has a $175,000 cost base, whatever the
        whole property was bought for."""
        acb, fmv = business_fraction_cost_and_value(700_000, 700_000, 0.25)
        assert (acb, fmv) == (175_000.0, 700_000 * 0.25)
        # A year later the home is worth more: the gap is the gain the
        # principal-residence exemption no longer shelters.
        acb, fmv = business_fraction_cost_and_value(721_000, 700_000, 0.25)
        assert fmv - acb == pytest.approx(5_250.0)


class TestTheClaimItself:
    """The engine claim, driven through ``FamilySimulation.run``."""

    def test_claim_is_the_declared_rate_on_the_opening_ucc(self):
        """One year after the conversion: 4% of 65,625 is 2,625."""
        rs, _ = _run(_cfg())
        assert rs[FIRST_CLAIM_INDEX].cca_claimed == pytest.approx(FIRST_CLAIM), (
            f"expected {FIRST_CLAIM:,.2f} of CCA in the first full year, got "
            f"{rs[FIRST_CLAIM_INDEX].cca_claimed:,.2f}")

    def test_nothing_is_claimed_before_the_change_in_use(self):
        """DP#16: the block is inert until its trigger, exactly as a rental
        block is inert before its purchase year."""
        rs, _ = _run(_cfg())
        assert [r.cca_claimed for r in rs[:CHANGE_YEAR + 1 - START_YEAR]] == \
            [0.0] * (CHANGE_YEAR + 1 - START_YEAR)

    def test_ucc_declines_and_closes_at_the_remaining_balance(self):
        """The declining pool is the whole point -- the claim must reduce it."""
        rs, _ = _run(_cfg())
        assert rs[FIRST_CLAIM_INDEX].business_use_ucc['home'] == \
            pytest.approx(UCC_AFTER_FIRST_CLAIM)
        # ... and it keeps declining, dollar for dollar, year over year.
        for prev, cur in zip(rs, rs[1:]):
            claimed = cur.cca_claimed
            if claimed <= 0.0:
                continue
            assert (prev.business_use_ucc['home'] - claimed
                    == pytest.approx(cur.business_use_ucc['home']))

    def test_the_claim_reduces_the_tax_the_return_can_shelter(self):
        """The deduction must reach the RETURN, not just a reported scalar.

        The engine reports no per-member tax line, so this reads the one place
        the tax actually shows: the refund the year's RRSP deduction earns is
        capped by the tax that is left to shelter (issue #286 -- the refund can
        never exceed the tax it reduces). With the claim, less tax is left, so
        the same contribution earns a smaller refund. That is a tax number
        moving, computed by the engine, in the claim's first year and not
        before it -- an assertion that cannot be satisfied by a claim that was
        merely computed and dropped.
        """
        with_bu, _ = _run(_cfg())
        without, _ = _run(_cfg(with_business_use=False))
        y = FIRST_CLAIM_INDEX
        assert with_bu[y - 1].rrsp_tax_savings == pytest.approx(
            without[y - 1].rrsp_tax_savings), (
            "before the change in use the two households are the same household "
            "-- premise for the comparison below")
        assert with_bu[y].rrsp_tax_savings < without[y].rrsp_tax_savings, (
            "claiming CCA must reduce the tax on the return: the RRSP refund "
            "is capped by the tax left to shelter, so it must fall")
        assert with_bu[y].rrsp_tax_savings > 0.0, (
            "the deduction still shelters real tax -- the claim is a deferral, "
            "not a wipe-out")

    def test_the_monthly_path_agrees_with_the_yearly_one(self):
        """The claim is wired into both fold paths. Wired into only one, the
        monthly run would depreciate the business portion while the yearly run
        did not -- and nothing would notice, because the two paths are compared
        per household rather than per implementation."""
        def check(cfg_dict):
            yearly, _ = _run(cfg_dict)
            monthly, _ = _run(cfg_dict, monthly=True)
            assert [r.cca_claimed for r in monthly] == \
                [r.cca_claimed for r in yearly]
            assert [r.business_use_ucc for r in monthly] == \
                [r.business_use_ucc for r in yearly]
            assert [r.cca_recapture_ordinary for r in monthly] == \
                [r.cca_recapture_ordinary for r in yearly]

        check(_cfg())
        # ... and once with the SPOUSE's own business portion, because the two
        # roles are separate branches on both paths and a one-member household
        # can only reach one of them.
        spouse_cfg = _cfg()
        spouse_cfg["family"]["members"].append({
            "role": "spouse", "birth_year": 1976, "retirement_age": 65,
            "gross_income": 0, "rrsp_room_accumulated": 0,
            "tfsa_room_accumulated": 0,
            "income_segments": [{"id": "biz", "kind": "self_employment",
                                 "amount": 90_000, "from": "2026-01-01",
                                 "to": "2036-01-01"}]})
        spouse_cfg["business_use"][0]["role"] = "spouse"
        check(spouse_cfg)

    def test_a_claim_larger_than_the_business_income_is_capped_at_it(self):
        """CCA cannot create or deepen a business loss (ITA s.20(1)(a) via the
        same rule the rental path already implements)."""
        rs, _ = _run(_cfg(self_employment=2_000))
        year = rs[FIRST_CLAIM_INDEX]
        # Premise, and the reason this xpassed while the engine was unwired: an
        # unwired household reports cca_claimed == 0.0, which satisfies
        # "claim <= income" trivially. A cap assertion with no floor is
        # satisfied by the absence of the feature.
        assert year.cca_claimed > 0.0, (
            "premise: the household must actually claim CCA in this year, "
            "otherwise 'the claim is capped at income' is satisfied by claiming "
            "nothing at all")
        assert year.cca_claimed <= 2_000 + 1e-6, (
            f"the claim must be capped at the net business income before CCA, "
            f"got {year.cca_claimed:,.2f} against an income of 2,000")


class TestThePriceOfTheElection:
    """What the claim COSTS. Without this the election looks free, which is the
    defect the issue was filed against."""

    def test_the_claimed_cca_is_recaptured_as_ordinary_income(self):
        """Everything claimed comes back as ORDINARY income (100% inclusion, no
        50%) at the s.70(5) deemed disposition on death -- and it grows as the
        claim grows, so the price is visible mid-projection rather than only at
        the end."""
        rs, _ = _run(_cfg(projection_years=25))
        recaps = [r.cca_recapture_ordinary for r in rs]
        assert max(recaps) > 0, (
            "premise: at least one year must price the recapture the CCA would "
            "cost at a disposition")
        assert recaps == sorted(recaps), (
            "the outstanding recapture must never shrink: it is the unclaimed "
            "balance of a pool the household keeps electing to depreciate")
        assert recaps[-1] > recaps[FIRST_CLAIM_INDEX], (
            "years of claiming must build a bigger recapture than one year's")

    def test_the_estate_charges_the_recapture(self):
        """The fold's per-year figure is not a display-only estimate: the
        terminal estate taxes exactly it. Without the block the same household
        pays no recapture at all."""
        rs, cfg = _run(_cfg(projection_years=25))
        without, cfg_without = _run(_cfg(with_business_use=False,
                                         projection_years=25))
        with_block = objective.compute_after_tax_estate(rs, config_to_dict(cfg))
        no_block = objective.compute_after_tax_estate(
            without, config_to_dict(cfg_without))
        assert with_block.total_tax > no_block.total_tax, (
            "the election's price must be charged at the deemed disposition -- "
            "a recapture that is only ever reported and never taxed is exactly "
            "the silent zero this engine exists to prevent")
        assert rs[-1].cca_recapture_ordinary > 0.0

    def test_the_business_fraction_loses_its_principal_residence_exemption(self):
        """The gain the portion accrued since the change in use carries no PRE.
        The engine's own cost base for it is the value at the change in use
        (s.45(1)(c)), so with an appreciating home fmv exceeds it..."""
        rs, _ = _run(_cfg(projection_years=25, appreciation=0.03))
        final = rs[-1]
        assert final.property_business_fraction_fmv > \
            final.property_business_fraction_acb > 0.0, (
            "the portion's gain since the change in use must be visible, priced "
            "from its s.45(1)(c) cost base")

    def test_the_lost_exemption_is_actually_taxed(self):
        """...and it is DENIED, not merely reported.

        The recapture is IDENTICAL in the two comparisons below (it depends only
        on how much CCA was claimed, which does not depend on the home's
        appreciation), so the ONLY thing that can grow the tax difference
        between "with a business portion" and "without one" is the denied
        principal-residence exemption on the portion's gain. An appreciating
        home therefore has to cost the household MORE tax than a static one --
        which is exactly the asymmetry CRA's Folio describes and the reason
        claiming CCA on a home is usually a bad choice.
        """
        def _estate_tax(with_bu, appreciation):
            rs, cfg = _run(_cfg(with_business_use=with_bu, projection_years=25,
                                appreciation=appreciation))
            return objective.compute_after_tax_estate(
                rs, config_to_dict(cfg)).total_tax

        flat = _estate_tax(True, 0.0) - _estate_tax(False, 0.0)
        grown = _estate_tax(True, 0.03) - _estate_tax(False, 0.03)
        assert flat > 0.0, (
            "premise: the election always costs its recapture, so the two "
            "estates cannot be identical even on a static home")
        assert grown > flat, (
            "a business portion of the home must cost the household its "
            "principal-residence exemption on the portion's gain -- an "
            "appreciating home has to make that denial MORE expensive, not "
            "leave it unchanged")

    def test_a_home_that_never_appreciates_owes_no_lost_exemption(self):
        """A static home has no gain since the change in use, so there is no
        exemption to deny -- a real $0, not a silent stand-in for a missing
        input (DP#32)."""
        rs, cfg = _run(_cfg(projection_years=25, appreciation=0.0))
        final = rs[-1]
        assert final.property_business_fraction_fmv == \
            pytest.approx(final.property_business_fraction_acb)
        without, cfg_without = _run(_cfg(with_business_use=False,
                                         projection_years=25,
                                         appreciation=0.0))
        # The precise assertion: with nothing to deny, the ONLY tax difference
        # the business portion can create is the recapture it built.
        diff = (objective.compute_after_tax_estate(rs, config_to_dict(cfg)).total_tax
                - objective.compute_after_tax_estate(
                    without, config_to_dict(cfg_without)).total_tax)
        assert 0.0 < diff <= rs[-1].cca_recapture_ordinary + 1e-6, (
            "with no appreciation there is no gain to deny, so the whole "
            f"difference must be the recapture ({rs[-1].cca_recapture_ordinary:,.0f}) "
            f"and nothing else -- got {diff:,.0f}")

    def test_a_portion_that_declares_no_election_claims_and_owes_nothing(self):
        """Declaring the business use WITHOUT electing CCA is the treatment
        that PRESERVES principal-residence status (the Folio keys on the
        election): no claim, no recapture, no UCC ledger entry."""
        rs, _ = _run(_cfg(cca=False))
        assert max(r.cca_claimed for r in rs) == 0.0
        assert max(r.cca_recapture_ordinary for r in rs) == 0.0
        assert rs[-1].business_use_ucc == {}


class TestAbsenceIsInert:
    """DP#32: the feature is additive, and the repo-wide golden must not move."""

    def test_golden_is_unmoved(self):
        """The golden household's terminal assets are unchanged: it declares no
        business portion, so no branch of this feature can be reached."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tests"))
        from test_golden_trajectory_581 import (golden_household_config, _run
                                                 as golden_run)
        assert golden_run(golden_household_config())[-1].total_assets == \
            pytest.approx(9_709_753.139463063)


class TestTheContractPath:
    """The document side: the block must survive validation, reach the engine,
    and be REFUSED where it cannot be honoured."""

    @staticmethod
    def _doc(business_use=None, self_employment=None, sale=None):
        from test_input_contract import _two_generation_subset
        doc = _two_generation_subset(_load_example())
        principal = doc["properties"][0]
        if business_use is not None:
            block = dict(business_use)
            principal["business_use"] = block
        if sale is not None:
            principal["sale"] = sale
        if self_employment is not None:
            person = next(p for p in doc["people"] if p["id"] == "p1")
            person["incomes"] = [copy.deepcopy(person["incomes"][0])]
            person["incomes"][0].update(self_employment)
        return doc

    def _internal(self, doc):
        contract_schema.validate_contract(doc)
        return ic.to_internal_config(doc)

    def test_a_principal_residence_accepts_a_business_use_block(self):
        """Premise for everything below: the contract admits the block at all."""
        doc = self._doc(business_use={
            "fraction": BUSINESS_FRACTION, "role": "primary",
            "change_in_use_date": "2028-05-01",
            "cca": {"rate": CCA_RATE, "capital_cost": BUSINESS_CAPITAL_COST,
                    "opening_ucc": OPENING_UCC}})
        internal = self._internal(doc)
        assert len(internal["business_use"]) == 1
        block = internal["business_use"][0]
        assert block["property_id"] == "principal_residence"
        assert block["is_principal"] is True
        assert block["change_in_use_year"] == 2028, (
            "the DATED leaf is the fact (DP#1); the mapper resolves it to the "
            "calendar year once, at the boundary")
        # The couple's share of the business FRACTION's value -- the recapture
        # ceiling and the gain on the same property must not disagree about
        # what it is worth (DP#9).
        assert block["cca"]["fmv_at_disposition"] == \
            pytest.approx(650_000 * BUSINESS_FRACTION)

    def test_the_block_reaches_the_engine_through_the_contract_path(self):
        """DP#18 in the shape it actually bites: a leaf READ BY THE ADAPTER is
        not the same as the block reaching the ENGINE. Drive the fold."""
        doc = self._doc(
            business_use={
                "fraction": BUSINESS_FRACTION, "role": "primary",
                "change_in_use_date": "2028-05-01",
                "cca": {"rate": CCA_RATE, "capital_cost": BUSINESS_CAPITAL_COST,
                        "opening_ucc": OPENING_UCC}},
            # p1 is an employee in the shipped example; the claim is capped at
            # the declaring role's net BUSINESS income, so an employee would
            # produce a $0 claim that proves nothing.
            self_employment={"kind": "self_employment", "amount": 90000})
        cfg = SimulationConfig.from_dict(self._internal(doc))
        rs = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                              use_readvanceable=False, deduct_later=False,
                              lump_sum=0.0).run()
        claims = [r.cca_claimed for r in rs]
        assert max(claims) > 0.0, (
            "the declared election must claim CCA through the contract path -- "
            "a block that is validated and mapped but never serviced is a "
            "silent no-op (DP#16/DP#18)")
        assert claims[FIRST_CLAIM_INDEX] == pytest.approx(FIRST_CLAIM)

    def test_a_role_the_household_does_not_have_is_refused(self):
        """A one-adult household declaring a spouse-owned portion: the claim
        would have no income to offset, and a $0 answer would read as "the
        business earns nothing" rather than "the declaration is wrong".

        Driven through the pure mapper with ``spouse_id=None``, which IS the
        one-adult household's shape -- building a whole one-adult document here
        would test the document, not the guard.
        """
        from contract_property import _map_business_use
        doc = self._doc(business_use={
            "fraction": BUSINESS_FRACTION, "role": "spouse",
            "change_in_use_date": "2028-05-01",
            "cca": {"rate": CCA_RATE, "capital_cost": BUSINESS_CAPITAL_COST,
                    "opening_ucc": OPENING_UCC}})
        with pytest.raises(contract_errors.ContractAdaptationError) as exc:
            _map_business_use(doc, "p1", None)
        assert 'spouse' in str(exc.value)

    def test_a_property_the_household_does_not_own_is_skipped(self):
        """Someone else's cottage with a business-use block is not this
        household's business portion: the claim would offset income that is not
        theirs and the recapture would be charged against a property they do not
        own. Skipped, not refused -- the document is fine, it simply describes
        more people than it models."""
        from contract_property import _map_business_use
        doc = self._doc(business_use={
            "fraction": BUSINESS_FRACTION, "role": "primary",
            "change_in_use_date": "2028-05-01",
            "cca": {"rate": CCA_RATE, "capital_cost": BUSINESS_CAPITAL_COST,
                    "opening_ucc": OPENING_UCC}})
        doc["properties"].append({
            "id": "someone_elses_shop",
            "owner": "ggm",                      # the grandmother's, not theirs
            "kind": "recreational",
            "value": {"amount": 300_000, "as_of": "2026-06-30"},
            "acb": 300_000,
            "designated_principal_residence_years": [],
            "business_use": {
                "fraction": 0.5, "role": "primary",
                "change_in_use_date": "2028-05-01",
                "cca": {"rate": 0.04, "capital_cost": 150_000,
                        "opening_ucc": 150_000}},
        })
        mapped = _map_business_use(doc, "p1", "p2")
        assert [b["property_id"] for b in mapped] == ["principal_residence"]

    def test_a_property_with_no_business_use_contributes_nothing(self):
        """The ordinary property block -- the shape every existing household
        declares -- must produce no entry, and no empty placeholder (an empty
        entry would read downstream as a portion claiming nothing, which is a
        different fact)."""
        from contract_property import _map_business_use
        doc = self._doc()
        assert _map_business_use(doc, "p1", "p2") == []

    def test_a_business_that_closed_before_the_snapshot_pays_nothing(self):
        """A self-employment income whose window ENDED before `as_of` is not
        carried: it earns nothing in any projected year, and carrying it forward
        would have the engine pay a business that no longer trades -- and, worse,
        an income to cap this feature's claim against."""
        doc = self._doc(
            business_use={
                "fraction": BUSINESS_FRACTION, "role": "primary",
                "change_in_use_date": "2028-05-01",
                "cca": {"rate": CCA_RATE, "capital_cost": BUSINESS_CAPITAL_COST,
                        "opening_ucc": OPENING_UCC}},
            self_employment={"kind": "self_employment", "amount": 90_000,
                             "from": "2015-01-01", "to": "2020-01-01"})
        internal = self._internal(doc)
        member = next(m for m in internal['family']['members']
                      if m.get('role') == 'primary')
        assert 'income_segments' not in member, (
            "an income that ended before the snapshot date is not carried "
            "forward -- the key is absent, not an empty entry")
        assert member['gross_income'] == 0.0, (
            "and it is not folded into the base scalar either -- it neither "
            "trades nor is it undated")

    def test_an_election_plus_a_dated_sale_is_refused(self):
        """Both consequences fall due in the SALE year, and the disposition
        rules price neither for a business portion -- modelling only the death
        path would hand the household a silently undertaxed sale."""
        doc = self._doc(
            business_use={
                "fraction": BUSINESS_FRACTION, "role": "primary",
                "change_in_use_date": "2028-05-01",
                "cca": {"rate": CCA_RATE, "capital_cost": BUSINESS_CAPITAL_COST,
                        "opening_ucc": OPENING_UCC}},
            sale={"date": "2040-06-30", "selling_costs": 0})
        with pytest.raises(contract_errors.ContractAdaptationError) as exc:
            self._internal(doc)
        assert 'sale' in str(exc.value)


def _load_example_trimmed():
    from test_input_contract import _two_generation_subset
    return _two_generation_subset(_load_example_raw())


def _load_example_raw():
    from test_input_contract import _load_example
    return _load_example()