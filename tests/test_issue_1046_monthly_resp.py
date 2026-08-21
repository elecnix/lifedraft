"""Tests for issue #1046: RESP allocation wiring in monthly mode.

The annual path's RESPChild state-advancement and resp_annual_match_cap wiring
is tested in test_issue_1046_resp_allocation_wiring.py, but _run_monthly has its
own copy of those same lines (lines 2330-2331, 2362-2363, 2397-2413). No existing
monthly test has RESP children, so those lines were uncovered and the coverage
gate failed.

This test exercises the _run_monthly path specifically, verifying that:
1. RESP contributions flow through the monthly allocation loop (resp_annual_match_cap
   is non-zero, so the min() term does not zero the allocation).
2. RESPChild lifetime state (total_cesg_received, total_contributions, etc.) is
   advanced each year in monthly mode, so the $7,200 CESG cap binds.

DP#4/DP#15: fabricated round numbers, role-based names. No personal data.
DP#11: integration tests verify composition (we drive the fold, not internal state).
DP#26: monthly mode must use SimState pure-function fold.
"""

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig

START_YEAR = 2026


def _monthly_newborn_config(**overrides):
    """Household with a newborn, savings, and time_step='monthly'.

    This routes FamilySimulation.run() through _run_monthly instead of the
    annual simulate_year path, exercising the monthly-mode RESP allocation
    and CESG state-advancement code.
    """
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
            'time_step': 'monthly',
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


def _monthly_lifetime_cap_config():
    """Long-running monthly-mode household for testing the $7,200 CESG cap.

    Child born 2025, projected for 20 years. Contributions and CESG accrue
    over many years. If BUG B (lifetime state not advanced) were present in
    monthly mode, total_cesg_received would stay at 0 and the cap would never bind.
    """
    return {
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
            'time_step': 'monthly',
        },
        'property': {
            'house_value': 400_000, 'mortgage_balance': 150_000, 'mortgage_rate': 0.04,
            'amortization_years': 25, 'margin_available': 0, 'ltv_max': 0.80,
        },
        'savings': {'rate': 0.15},
        'tax': {'province': 'qc'},
    }


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


# ── FIX A (monthly): resp_annual_match_cap must be wired in _run_monthly ──

class TestFixAMonthlyRespAllocation:
    """DP#17: In monthly mode, the min() term resp_annual_match_cap *
    eligible_children must not zero the allocation. The _run_monthly path
    has its own copy of these lines; this test exercises them."""

    def test_monthly_newborn_resp_balance_grows_over_early_years(self):
        """A household with a newborn and savings must contribute to RESP
        through the _run_monthly allocation path. Before FIX A, resp was $0
        because resp_annual_match_cap defaulted to 0 in the monthly path."""
        results = _run(_monthly_newborn_config())
        early = results[:5]
        for i, r in enumerate(early[1:], 1):
            assert r.resp_balance > early[i - 1].resp_balance, (
                f"Monthly RESP balance did not grow from year {i - 1} to {i}: "
                f"{early[i - 1].resp_balance:.2f} -> {r.resp_balance:.2f}. "
                f"FIX A may not be wiring resp_annual_match_cap in _run_monthly."
            )

    def test_monthly_resp_allocation_nonzero_when_children_present(self):
        """With eligible children and savings, the annual RESP allocation
        must be > 0 in monthly mode (not silently zeroed by an unset cap)."""
        results = _run(_monthly_newborn_config())
        balances = [r.resp_balance for r in results[:5]]
        for i, b in enumerate(balances):
            if i == 0:
                continue
            assert b > 0, (
                f"Monthly RESP balance is 0 at year {i}, expected positive (FIX A)"
            )

    def test_monthly_resp_compounding_correct(self):
        """The RESP balance must reflect correct compounding at 5% in monthly
        mode. After 9 years, the balance should be well under $47k (catches
        a doubled investment return)."""
        results = _run(_monthly_newborn_config())
        b9 = results[-1].resp_balance
        assert b9 > 0, f"Monthly RESP balance at year 9 is {b9:.2f}, expected positive."
        assert b9 < 47_000, (
            f"Monthly RESP balance at year 9 is {b9:.2f}, unexpectedly high — "
            f"investment return may be doubled (COMPOUNDING mutation)."
        )


# ── FIX B (monthly): lifetime state must advance in _run_monthly ──

class TestFixBMonthlyLifetimeCap:
    """DP#17: The $7,200 lifetime CESG cap must bind in monthly mode.
    Before FIX B, RESPChild.total_cesg_received was never updated in
    _run_monthly, so the cap was unreachable."""

    def test_monthly_cesg_lifetime_cap_7200_per_child(self):
        """After a 20-year monthly-mode simulation, total_cesg_received must
        not exceed $7,200 per child."""
        results, sim = _run_with_sim(_monthly_lifetime_cap_config())
        for ch in sim.resp_children:
            assert ch.total_cesg_received <= 7200.01, (
                f"Monthly: child {ch.name} has total_cesg_received="
                f"{ch.total_cesg_received:.2f}, exceeds $7,200 lifetime cap. "
                f"FIX B must advance lifetime state in _run_monthly."
            )

    def test_monthly_lifetime_state_advances_across_years(self):
        """RESPChild lifetime fields must be > 0 after a multi-year monthly
        simulation. Before FIX B, these stayed at 0 in _run_monthly."""
        results, sim = _run_with_sim(_monthly_lifetime_cap_config())
        for ch in sim.resp_children:
            assert ch.total_cesg_received > 0, (
                f"Monthly: child {ch.name} has total_cesg_received=0. "
                f"FIX B must advance lifetime state in _run_monthly."
            )
            assert ch.total_contributions > 0, (
                f"Monthly: child {ch.name} has total_contributions=0. "
                f"FIX B must advance total_contributions in _run_monthly."
            )
            assert ch.total_before_age_15 > 0, (
                f"Monthly: child {ch.name} has total_before_age_15=0. "
                f"FIX B must advance total_before_age_15 in _run_monthly."
            )
            assert len(ch.contribution_years) > 0, (
                f"Monthly: child {ch.name} has no contribution_years. "
                f"FIX B must advance contribution_years in _run_monthly."
            )

    def test_monthly_cesg_accumulates_significantly(self):
        """CESG must accumulate significantly over 18+ years in monthly mode."""
        results, sim = _run_with_sim(_monthly_lifetime_cap_config())
        for ch in sim.resp_children:
            assert ch.total_cesg_received > 100, (
                f"Monthly: child {ch.name} has total_cesg_received="
                f"{ch.total_cesg_received:.2f}, expected significant accumulation "
                f"over 18 years. FIX B may not be advancing state in _run_monthly."
            )


# ── Integration: both fixes together through the monthly fold ──

class TestIntegrationMonthlyRespAllocationAndLifetime:
    """End-to-end test: both fixes working together through the monthly fold."""

    def test_monthly_resp_balance_grows_and_cesg_cap_binds(self):
        """After FIX A and FIX B together in monthly mode: RESP balance
        grows during accumulation AND total_cesg_received respects the
        lifetime cap."""
        results, sim = _run_with_sim(_monthly_lifetime_cap_config())

        # FIX A: RESP balance must grow during accumulation years
        balances = [r.resp_balance for r in results[:10]]
        for i in range(1, len(balances)):
            assert balances[i] > balances[i - 1], (
                f"Monthly: RESP balance did not grow from year {i - 1} to {i}: "
                f"{balances[i - 1]:.2f} -> {balances[i]:.2f}"
            )

        # FIX B: total CESG per child must respect the lifetime cap
        for ch in sim.resp_children:
            assert ch.total_cesg_received <= 7200.01, (
                f"Monthly: CESG lifetime cap violated: "
                f"{ch.total_cesg_received:.2f} > $7,200"
            )
            assert ch.total_cesg_received > 0, (
                "Monthly: CESG must be > 0 when child is eligible"
            )

    def test_monthly_resp_allocation_nonzero_with_cap(self):
        """With resp_annual_match_cap correctly set ($2,500 per child),
        a household with savings must contribute to RESP in monthly mode.
        High savings rate ensures resp_pct * savings > $2,500."""
        cfg = _monthly_newborn_config()
        cfg['savings'] = {'rate': 0.30}  # High savings: ~$69k/yr
        results = _run(cfg)
        balances = [r.resp_balance for r in results[:5]]
        for i in range(1, len(balances)):
            assert balances[i] > balances[i - 1], (
                f"Monthly: RESP must grow year over year. "
                f"Year {i}: {balances[i - 1]:.2f} -> {balances[i]:.2f}"
            )

    def test_monthly_total_cesg_positive_and_capped(self):
        """After a long monthly-mode simulation, total_cesg_received must be
        positive (state advanced each year) and at most $7,200 (cap binds).
        This catches BUG B in monthly mode: if RESPChild.total_cesg_received
        is not advanced in _run_monthly, the cap never binds."""
        results, sim = _run_with_sim(_monthly_lifetime_cap_config())
        for ch in sim.resp_children:
            assert ch.total_cesg_received > 100, (
                f"Monthly: child {ch.name}: total_cesg_received="
                f"{ch.total_cesg_received:.2f}, expected significant accumulation "
                f"over 18+ years. BUG B in _run_monthly."
            )
            assert ch.total_cesg_received <= 7200.01, (
                f"Monthly: CESG lifetime cap violated: "
                f"{ch.total_cesg_received:.2f} > $7,200."
            )

    def test_monthly_cesg_ineligible_child_else_branch_advances_state(self):
        """A child aged 18+ is CESG-ineligible, so the _run_monthly path takes
        the `else` branch (lines 2417-2421): contributions are tracked but
        no CESG/QESI is computed. This test covers that branch.

        When resp_eligible_children=0, the strategy allocates 0 to RESP,
        so ch_contrib is 0. The else branch still runs and appends
        contribution_years entries with amount 0. The key observable is
        that the code path is exercised (contribution_years is populated)
        rather than skipped entirely (which would be BUG B)."""
        cfg = {
            'family': {
                'members': [
                    {'role': 'primary', 'birth_year': 1960, 'gross_income': 100_000,
                     'retirement_age': 65, 'rrsp_room_accumulated': 20_000,
                     'tfsa_room_accumulated': 15_000},
                ],
                'children': [{'name': 'child_a', 'birth_year': 2007}],  # age 19 in 2026
            },
            'accounts': {'resp_current_balance': 5_000},
            'assumptions': {
                'start_year': START_YEAR,
                'projection_years': 3,
                'investment_return': 0.05,
                'salary_growth': 0.02,
                'frozen_brackets': True,
                'time_step': 'monthly',
            },
            'property': {
                'house_value': 300_000, 'mortgage_balance': 100_000, 'mortgage_rate': 0.04,
                'amortization_years': 25, 'margin_available': 0, 'ltv_max': 0.80,
            },
            'savings': {'rate': 0.10},
            'tax': {'province': 'qc'},
        }
        results, sim = _run_with_sim(cfg)
        for ch in sim.resp_children:
            # CESG-ineligible child: no CESG grant, but the else branch
            # must still append contribution_years (proving the path ran).
            assert len(ch.contribution_years) > 0, (
                f"Monthly: CESG-ineligible child {ch.name} has "
                f"no contribution_years. The else branch must still "
                f"append contribution_years."
            )
            # CESG must be 0 for an ineligible child
            assert ch.total_cesg_received == 0, (
                f"Monthly: CESG-ineligible child {ch.name} has "
                f"total_cesg_received={ch.total_cesg_received:.2f}, expected 0."
            )


class TestIneligibleChildElseBranchMonthly:
    """Cover the _run_monthly else branch (lines 2415-2420) for a
    CESG-ineligible child.

    When a child is age > 17, cesg_eligible returns False, so the RESP
    prologue takes the else branch: it tracks total_contributions,
    age, total_before_age_15, and contribution_years, but skips CESG/QESI.

    To get a non-zero contribution flowing through the else branch, the
    household must have at least one CESG-eligible child (driving the
    strategy to allocate to RESP); the contribution is then split across
    all children, so the ineligible child receives a non-zero share.

    DP#4/DP#15: fabricated round numbers, role-based names.
    DP#11/DP#26: drive the fold, assert engine output."""

    def test_ineligible_child_receives_contribution_share_monthly(self):
        """A CESG-ineligible child (age 19) in a mixed household receives a
        non-zero RESP contribution share through the else branch in
        _run_monthly, but no CESG.

        The else branch (lines 2416-2420) tracks total_contributions and
        contribution_years. With an eligible sibling present, the strategy
        allocates to RESP, so ch_contrib > 0 for the ineligible child."""
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
                'children': [
                    {'name': 'child_young', 'birth_year': 2021},  # age 5, CESG-eligible
                    {'name': 'child_old', 'birth_year': 2007},    # age 19, not eligible
                ],
            },
            'accounts': {'resp_current_balance': 5_000},
            'assumptions': {
                'start_year': START_YEAR,
                'projection_years': 3,
                'investment_return': 0.05,
                'salary_growth': 0.02,
                'frozen_brackets': True,
                'time_step': 'monthly',
            },
            'property': {
                'house_value': 500_000, 'mortgage_balance': 200_000,
                'mortgage_rate': 0.04, 'amortization_years': 25,
                'margin_available': 0, 'ltv_max': 0.80,
            },
            'savings': {'rate': 0.20},
            'tax': {'province': 'qc'},
        }
        results, sim = _run_with_sim(cfg)

        # Find the ineligible child
        ineligible = [ch for ch in sim.resp_children
                      if not ch.cesg_eligible(START_YEAR)]
        eligible = [ch for ch in sim.resp_children
                    if ch.cesg_eligible(START_YEAR)]
        assert len(ineligible) == 1, (
            f"Expected 1 ineligible child, found {len(ineligible)}"
        )
        assert len(eligible) == 1, (
            f"Expected 1 eligible child, found {len(eligible)}"
        )

        old_child = ineligible[0]

        # The else branch must track contributions for the ineligible child
        assert old_child.total_contributions > 0, (
            f"Monthly: ineligible child {old_child.name} has "
            f"total_contributions={old_child.total_contributions:.2f}, "
            f"expected > 0 (else branch must track contributions)."
        )
        assert len(old_child.contribution_years) > 0, (
            f"Monthly: ineligible child {old_child.name} has "
            f"no contribution_years (else branch must append them)."
        )
        # CESG must be 0 — the else branch skips CESG/QESI calculation
        assert old_child.total_cesg_received == 0, (
            f"Monthly: ineligible child {old_child.name} has "
            f"total_cesg_received={old_child.total_cesg_received:.2f}, "
            f"expected 0 (else branch skips CESG)."
        )
        # total_before_age_15 must be 0 — child is 19, so age > 15
        # in the else branch, and the `if age <= 15` guard is False.
        assert old_child.total_before_age_15 == 0, (
            f"Monthly: ineligible child {old_child.name} has "
            f"total_before_age_15={old_child.total_before_age_15:.2f}, "
            f"expected 0 (age 19 > 15, so the guard is False)."
        )

        # The eligible child must have CESG > 0 (sanity: eligible path works)
        young_child = eligible[0]
        assert young_child.total_cesg_received > 0, (
            f"Monthly: eligible child {young_child.name} has "
            f"total_cesg_received={young_child.total_cesg_received:.2f}, "
            f"expected > 0."
        )

        # RESP balance must be positive (contributions are flowing)
        assert results[1].resp_balance > 0, (
            f"Monthly: RESP balance should be positive when children are present, "
            f"got {results[1].resp_balance:.2f}."
        )


class TestFixCMonthlyFHSABlock:
    """Cover the FHSA allocation block inside _run_monthly (lines 2317-2336).

    The if self.has_fhsa: block in _run_monthly allocates annual FHSA contributions
    before the 12-month loop. Without FHSA data, has_fhsa is False and the block
    is skipped. This test provides FHSA data to route through it."""

    def test_monthly_fhsa_allocation_with_resp_children(self):
        """A household with FHSA room, RESP children, and time_step='monthly'
        must route through the FHSA allocation block in _run_monthly and still
        allocate to RESP (both FHSA and RESP are funded).

        The FHSA block (lines 2317-2336) is gated on `if self.has_fhsa` and
        constructs a FamilyState + StrategyEngine allocation. The default
        strategy allocates fhsa_pct=0, so fhsa_contrib is 0 but the BLOCK
        still executes (coverage counts block entry, not value). This test
        verifies has_fhsa is True and the RESP allocation still works."""
        cfg = _monthly_newborn_config()
        cfg['family']['members'][0]['fhsa_room_accumulated'] = 8_000
        results, sim = _run_with_sim(cfg)
        # FHSA must be active -- this gates the `if self.has_fhsa` block
        assert sim.has_fhsa, "FHSA should be active when fhsa_room_accumulated > 0"
        # RESP must still be funded in monthly mode (the block does not break
        # the monthly RESP allocation that follows)
        early = results[:5]
        for i, r in enumerate(early[1:], 1):
            assert r.resp_balance > early[i - 1].resp_balance, (
                f"Monthly: RESP balance must grow year over year even with FHSA active. "
                f"Year {i}: {early[i-1].resp_balance:.2f} -> {r.resp_balance:.2f}"
            )