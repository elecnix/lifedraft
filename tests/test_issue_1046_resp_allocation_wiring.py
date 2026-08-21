"""Tests for issue #1046: RESP annual allocation wiring and lifetime state advancement.

BUG A: resp_annual_match_cap was never set on FamilyState, defaulting to 0.0,
which zeroed the min() in StrategyEngine.allocate. Every household got $0 RESP.

BUG B: RESPChild lifetime fields (total_cesg_received, total_qesi_received,
total_contributions, total_before_age_15, contribution_years) were never
updated after computing CESG/QESI, so the $7,200 lifetime CESG cap never
bound and 16-17 eligibility always failed.

These integration tests drive FamilySimulation.run() (the fold) and assert
on engine output, verifying both fixes through their observable effects.

DP#4/DP#15: fabricated round numbers, role-based names. No personal data.
DP#17: tests exercise both sides of every threshold.
DP#11: unit tests verify each module; integration tests verify composition.
"""


from countries.canada.adapter import CanadaAdapter
from countries.canada.resp_rules import RESPChild
from simulation import FamilySimulation
from simulation_config import SimulationConfig

START_YEAR = 2026


def _newborn_config(**overrides):
    """Household with a newborn and savings — proves FIX A (resp > 0)."""
    cfg = {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1990, 'gross_income': 150_000,
                 'retirement_age': 65, 'rrsp_room_accumulated': 30_000,
                 'tfsa_room_accumulated': 20_000},
                {'role': 'spouse', 'birth_year': 1992, 'gross_income': 80_000,
                 'retirement_age': 65, 'rrsp_room_accumulated': 15_000,
                 'tfsa_room_accumulated': 10_000},
            ],
            'children': [{'name': 'child_a', 'birth_year': 2026}],
        },
        'accounts': {
            'resp_current_balance': 0,
        },
        'assumptions': {
            'start_year': START_YEAR,
            'projection_years': 10,
            'investment_return': 0.05,
            'salary_growth': 0.02,
            'frozen_brackets': True,
        },
        'property': {
            'house_value': 500_000, 'mortgage_balance': 200_000, 'mortgage_rate': 0.04,
            'amortization_years': 25, 'margin_available': 0, 'ltv_max': 0.80,
        },
        'savings': {'rate': 0.20},
        'tax': {'province': 'qc'},
    }
    cfg.update(overrides)
    return cfg


def _lifetime_cap_config():
    """Household contributing to an RESP for 17+ years — proves FIX B
    (lifetime CESG cap binds, total_cesg never exceeds $7,200)."""
    cfg = {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1985, 'gross_income': 120_000,
                 'retirement_age': 65, 'rrsp_room_accumulated': 50_000,
                 'tfsa_room_accumulated': 30_000},
            ],
            'children': [{'name': 'child_a', 'birth_year': 2025}],
        },
        'accounts': {
            'resp_current_balance': 0,
        },
        'assumptions': {
            'start_year': START_YEAR,
            'projection_years': 20,
            'investment_return': 0.05,
            'salary_growth': 0.02,
            'frozen_brackets': True,
        },
        'property': {
            'house_value': 400_000, 'mortgage_balance': 150_000, 'mortgage_rate': 0.04,
            'amortization_years': 25, 'margin_available': 0, 'ltv_max': 0.80,
        },
        'savings': {'rate': 0.15},
        'tax': {'province': 'qc'},
    }
    return cfg


def _winddown_config():
    """Household with an older child entering the study window — proves
    that CESG tracking through the fold produces correct wind-down values.
    Child born 2008, study window starts at age 18 (year 2026)."""
    cfg = {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1980, 'gross_income': 100_000,
                 'retirement_age': 65, 'rrsp_room_accumulated': 40_000,
                 'tfsa_room_accumulated': 25_000},
            ],
            'children': [{'name': 'child_a', 'birth_year': 2008}],
        },
        'accounts': {
            'resp_current_balance': 40_000,
        },
        'assumptions': {
            'start_year': START_YEAR,
            'projection_years': 8,
            'investment_return': 0.05,
            'salary_growth': 0.02,
            'frozen_brackets': True,
        },
        'property': {
            'house_value': 400_000, 'mortgage_balance': 150_000, 'mortgage_rate': 0.04,
            'amortization_years': 25, 'margin_available': 0, 'ltv_max': 0.80,
        },
        'savings': {'rate': 0.15},
        'tax': {'province': 'qc'},
    }
    return cfg


def _run(cfg: dict):
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                            use_readvanceable=False, deduct_later=False)
    return sim.run()


def _run_with_sim(cfg: dict):
    """Run simulation and return (results, sim) for inspecting child state."""
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                            use_readvanceable=False, deduct_later=False)
    results = sim.run()
    return results, sim


# ── FIX A: resp_annual_match_cap must be wired, so RESP contributions > $0 ──

class TestFixARespAllocation:
    """DP#17: the min() term resp_annual_match_cap * eligible_children must
    not zero the allocation. With it wired to get_cesg_contribution_max(),
    a household with an eligible child and savings must see its RESP
    balance grow."""

    def test_newborn_resp_balance_grows_over_early_years(self):
        """A household with a newborn and savings must contribute to RESP
        (the balance grows year over year). Before FIX A, resp was always $0."""
        results = _run(_newborn_config())
        early = results[:5]
        for i, r in enumerate(early[1:], 1):
            assert r.resp_balance > early[i - 1].resp_balance, (
                f"RESP balance did not grow from year {i - 1} to {i}: "
                f"{early[i - 1].resp_balance:.2f} -> {r.resp_balance:.2f}. "
                f"FIX A may not be wiring resp_annual_match_cap correctly."
            )

    def test_resp_allocation_nonzero_when_children_present(self):
        """With eligible children and savings, the annual allocation must
        be > 0 (not silently zeroed by an unset match cap)."""
        results = _run(_newborn_config())
        # Check that at least some years have positive RESP balance
        # (starting from 0, it must become positive)
        balances = [r.resp_balance for r in results[:5]]
        for i, b in enumerate(balances):
            if i == 0:
                continue
            assert b > 0, (
                f"RESP balance is 0 at year {i}, expected positive (FIX A)"
            )


# ── FIX B: lifetime state must advance, so CESG cap binds at $7,200 ──

class TestFixBLifetimeCap:
    """DP#17: The $7,200 lifetime CESG cap must bind. Before FIX B, the
    RESPChild's total_cesg_received was never updated, so the cap was
    unreachable and CESG would accumulate indefinitely."""

    def test_cesg_lifetime_cap_7200_per_child(self):
        """Directly verify the CESG lifetime cap by checking total_cesg_received
        on the child objects after a long simulation. Before FIX B, this was
        always 0 (state never advanced), so the cap was never checked."""
        results, sim = _run_with_sim(_lifetime_cap_config())

        children = sim.resp_children
        for ch in children:
            assert ch.total_cesg_received <= 7200.01, (
                f"Child {ch.name} has total_cesg_received={ch.total_cesg_received:.2f}, "
                f"which exceeds the $7,200 lifetime CESG cap. "
                f"FIX B (lifetime state advancement) may not be working."
            )

    def test_lifetime_state_advances_across_years(self):
        """The RESPChild's total_cesg_received, total_contributions, and
        total_before_age_15 must be > 0 after a multi-year simulation with
        eligible children. Before FIX B, these stayed at 0."""
        results, sim = _run_with_sim(_lifetime_cap_config())

        children = sim.resp_children
        for ch in children:
            assert ch.total_cesg_received > 0, (
                f"Child {ch.name} has total_cesg_received=0. "
                f"FIX B must advance lifetime state after CESG computation."
            )
            assert ch.total_contributions > 0, (
                f"Child {ch.name} has total_contributions=0. "
                f"FIX B must advance total_contributions."
            )
            assert ch.total_before_age_15 > 0, (
                f"Child {ch.name} has total_before_age_15=0. "
                f"FIX B must advance total_before_age_15 for ages <= 15."
            )
            assert len(ch.contribution_years) > 0, (
                f"Child {ch.name} has no contribution_years. "
                f"FIX B must advance contribution_years."
            )

    def test_cesg_cap_binds_before_year_18(self):
        """With contributions over many years, total_cesg_received must
        accumulate significantly and then stop at the $7,200 cap."""
        results, sim = _run_with_sim(_lifetime_cap_config())

        children = sim.resp_children
        for ch in children:
            # CESG must have accumulated over 18+ years
            assert ch.total_cesg_received > 100, (
                f"Child {ch.name} has total_cesg_received={ch.total_cesg_received:.2f}, "
                f"expected significant CESG accumulation over 18 years."
            )
            # And it must not exceed $7,200
            assert ch.total_cesg_received <= 7200.01, (
                f"CESG lifetime cap violated: {ch.total_cesg_received:.2f} > $7,200"
            )


class TestFixBCesg1617Eligibility:
    """DP#17: A child with sufficient early contributions must continue earning
    CESG at ages 16-17; one without must not. Before FIX B, cesg_16_17_eligible
    always failed because total_before_age_15 and contribution_years were 0."""

    def test_child_with_early_contributions_gets_cesg_at_16_17(self):
        """A child with $2,000+ contributed before age 16 should be eligible
        for CESG at ages 16-17 (cesg_16_17_eligible returns True)."""
        child = RESPChild(name="child_a", birth_year=2010)
        for year in range(2011, 2021):
            child.total_contributions += 500
            child.total_before_age_15 += 500
            child.contribution_years.append((year, 500))
            child.total_cesg_received += 100

        assert child.cesg_16_17_eligible(2026), (
            "Child with $5,000 before age 15 should be eligible for CESG at 16-17"
        )

    def test_child_without_early_contributions_not_eligible_at_16_17(self):
        """A child with zero contributions before age 16 should NOT be
        eligible for CESG at 16-17. DP#17: the other side of the threshold."""
        child = RESPChild(name="child_b", birth_year=2010)
        assert not child.cesg_16_17_eligible(2026), (
            "Child with no contributions should not be CESG-eligible at 16-17"
        )

    def test_child_with_4_years_100_dollars_eligible_at_16_17(self):
        """DP#17: 4+ years of $100+ contributions before age 16 qualifies."""
        child = RESPChild(name="child_c", birth_year=2010)
        for year in range(2015, 2019):
            child.contribution_years.append((year, 100))
            child.total_contributions += 100

        assert child.cesg_16_17_eligible(2026), (
            "4+ years of $100 contributions should make child eligible at 16-17"
        )


class TestIntegrationRespAllocationAndLifetime:
    """End-to-end test: both fixes working together through the fold.

    These tests catch specific mutations by checking observable effects
    that would change if RESP allocation, compounding, or CESG tracking
    were broken.
    """

    def test_resp_balance_grows_and_cesg_cap_binds(self):
        """After FIX A and FIX B together: RESP balance grows during
        accumulation AND total_cesg_received respects the lifetime cap."""
        results, sim = _run_with_sim(_lifetime_cap_config())

        # FIX A: RESP balance must grow during accumulation years
        balances = [r.resp_balance for r in results[:10]]
        for i in range(1, len(balances)):
            assert balances[i] > balances[i - 1], (
                f"RESP balance did not grow from year {i-1} to {i}: "
                f"{balances[i-1]:.2f} -> {balances[i]:.2f}"
            )

        # FIX B: total CESG per child must respect the lifetime cap
        for ch in sim.resp_children:
            assert ch.total_cesg_received <= 7200.01, (
                f"CESG lifetime cap violated: {ch.total_cesg_received:.2f} > $7,200"
            )
            assert ch.total_cesg_received > 0, (
                "CESG must be > 0 when child is eligible and contributions > 0"
            )

    def test_resp_compounding_correct_through_fold(self):
        """The RESP balance must reflect correct compounding at the
        investment return rate (5%). This catches the COMPOUNDING mutation
        which doubles investment_return in apply_resp, causing the RESP
        balance to grow much faster than expected.

        After 9 years with 5% return, the RESP balance should be about $42k.
        With doubled 10% return, it would be about $53k+.
        We check that the year-9 balance is under $47k.
        """
        results = _run(_newborn_config())
        b9 = results[-1].resp_balance  # last year of 10-year projection
        # With correct 5% compounding, year-9 balance ≈ $42k.
        # With 10% compounding (COMPOUNDING mutation), it would be ≈ $53k+.
        # Use $47k as a bound that distinguishes 5% from 10%.
        assert b9 < 47_000, (
            f"RESP balance at year 9 is {b9:.2f}, which is unexpectedly high. "
            f"Investment return may be doubled (COMPOUNDING mutation)."
        )
        # Also ensure it's positive (FIX A)
        assert b9 > 0, (
            f"RESP balance at year 9 is {b9:.2f}, expected positive (FIX A)."
        )

    def test_resp_winddown_eap_positive_during_study_window(self):
        """The RESP wind-down (EAP/PSE) must produce positive EAP payments
        during the study window. This catches the STATE_ADVANCE mutation
        which zeros opening_resp_cesg: incorrect CESG tracking would
        miscalculate the EAP/earnings breakdown.

        Child born 2008, age 18 at START_YEAR. The study window is
        2026-2029. EAP must be paid each year.
        """
        results = _run(_winddown_config())
        # Child is 18 at START_YEAR -> study window starts immediately
        in_window = results[:4]
        assert all(r.resp_eap_paid > 0 for r in in_window), (
            "EAP must be paid every year in the study window"
        )
        assert all(r.resp_pse_paid > 0 for r in in_window), (
            "PSE (contributions) must be returned every year in the study window"
        )
        # After the study window, RESP must wind down
        after_window = results[4:]
        if after_window:
            assert all(r.resp_balance < 1.0 for r in after_window), (
                "RESP must be wound down after the study window"
            )

    def test_resp_allocation_nonzero_with_cap(self):
        """With resp_annual_match_cap correctly set ($2,500 per child),
        a household with savings must contribute to RESP. This directly
        catches the ALLOC_RESP mutation (which sets cap to 0, zeroing
        the allocation).

        The test creates a scenario where resp_pct * savings > $2,500,
        so the min() term constrains the allocation to $2,500 per child.
        If cap = 0, the min() would be 0 and no RESP contribution occurs.
        """
        cfg = _newborn_config()
        cfg['savings'] = {'rate': 0.30}  # High savings: ~$69k/yr
        results = _run(cfg)
        # RESP must receive contributions (balance must grow)
        balances = [r.resp_balance for r in results[:5]]
        for i in range(1, len(balances)):
            assert balances[i] > balances[i - 1], (
                f"RESP must grow year over year with contributions. "
                f"Year {i}: {balances[i-1]:.2f} -> {balances[i]:.2f}"
            )

    def test_total_cesg_positive_and_capped_through_fold(self):
        """After a long simulation, total_cesg_received must be positive
        (state advanced each year) and at most $7,200 (cap binds).
        This catches the STATE_ADVANCE mutation: if opening_resp_cesg
        is zeroed each year, the CESG tracking in the fold is wrong,
        but more importantly, if RESPChild.total_cesg_received is not
        advanced (BUG B), the cap never binds and CESG accumulates
        indefinitely."""
        results, sim = _run_with_sim(_lifetime_cap_config())

        for ch in sim.resp_children:
            # CESG must accumulate (state was advanced each year)
            assert ch.total_cesg_received > 100, (
                f"Child {ch.name}: total_cesg_received={ch.total_cesg_received:.2f}, "
                f"expected significant accumulation over 18+ years. "
                f"Lifetime state may not be advancing (BUG B)."
            )
            # CESG must be capped at $7,200
            assert ch.total_cesg_received <= 7200.01, (
                f"CESG lifetime cap violated: {ch.total_cesg_received:.2f} > $7,200. "
                f"CESG cap is not binding (state not advancing or cap not enforced)."
            )

    def test_resp_cesg_tracking_through_fold_affects_aip(self):
        """The per-balance CESG tracking (opening_resp_cesg flowing through
        the fold) must be correct for AIP tax calculations. If
        opening_resp_cesg is zeroed each year (STATE_ADVANCE mutation),
        the CESG/earnings split would be wrong, overstating earnings and
        the AIP tax.

        With correct CESG tracking, the AIP tax is about $9,000.
        With zeroed opening_resp_cesg, the earnings are overstated by
        the accumulated CESG (~$4,000 for a $40k balance), making the
        AIP tax about $11,200 (over $10,000)."""
        # Child born 2008 (age 18 at START_YEAR), RESP not used for education
        # The RESP collapses via AIP, which taxes earnings at the
        # subscriber's marginal rate + 20% penalty.
        cfg = _winddown_config()
        cfg['accounts']['resp_used_for_education'] = False
        results = _run(cfg)

        # When the RESP collapses (first year, child is 18), the AIP tax
        # must be positive and within a specific range.
        # With correct CESG tracking, the AIP tax is about $8,979.
        # With zeroed opening_resp_cesg (STATE_ADVANCE mutation),
        # AIP tax would be about $11,200 (over $10,000).
        aip_tax = results[0].resp_aip_tax
        assert aip_tax > 0, (
            f"AIP tax must be positive when RESP is collapsed unused. "
            f"Got {aip_tax:.2f}."
        )
        # With correct CESG tracking, AIP tax ≈ $9,000.
        # With zeroed opening_resp_cesg (STATE_ADVANCE mutation),
        # AIP tax would be ≈ $11,200 (over $10,000).
        assert aip_tax < 10_000, (
            f"AIP tax {aip_tax:.2f} exceeds $10,000. CESG tracking may be wrong "
            f"(earnings overstated due to zeroed opening_resp_cesg)."
        )
