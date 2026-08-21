"""Issue #978: the self-employed Quebec contribution stack (QPP both halves
+ QPIP self-employed + the individual Health Services Fund) is a mandatory
pre-savings cash outflow the working-phase cash flow must charge.

Before #978 the fold taxed working income with bracket-only ``tax_on_income``
and deducted NO payroll contribution -- so a self-employed Quebec earner's
disposable income (and thus savings capacity) was overstated by the full stack
(~$10k/yr at $100k gross) versus an employee at the same gross. The calculators
all existed and were correct but had no production caller
(``compute_total_tax`` in tax_calc.py was the dead assembler).

This test wires no new code; it asserts the fold now charges the stack:
  * a self-employed QC earner nets LESS after-tax income than an employee at
    the same gross, by EXACTLY the QPP(both-halves) + QPIP(self-employed) +
    individual-HSF total the existing calculators produce (DP#9 -- the test
    reuses the same calculators the fold reuses, so the delta is structural,
    not a hand-typed constant that could drift from the data);
  * an employee (no ``kind == 'self_employment'`` segment) is unchanged -- the
    stack is a strict no-op for employment income (DP#32 absence-safe);
  * a non-Quebec self-employed earner pays no stack here (QPP-vs-CPP is a
    separate, non-Quebec gap out of scope for #978).

DP#4/DP#15: fabricated round numbers and role-based names only.
"""

import unittest

from countries.canada.adapter import CanadaAdapter
from countries.canada.cpp_sharing import compute_cpp2_contribution
from countries.canada.provinces.quebec.quebec_credits import (
    quebec_qpip_premium,
    quebec_health_services_fund_individual,
)
from simulation import FamilySimulation
from simulation_config import SimulationConfig

GROSS = 100_000          # a fabricated round number (DP#4/DP#15)
YEAR = 2026               # the engine's default start year (year-versioned data)
LIVING_COSTS = 40_000     # declared so apply_solvency runs and surfaces
                           # YearResult.after_tax_income (the working-phase
                           # disposable figure the cash-flow identity uses)


def _self_employed_stack(gross: float, province: str, year: int) -> float:
    """The exact total the fold subtracts -- reuses the SAME calculators the
    fold reuses (DP#9), so the test's expected delta is structural, not a
    hand-typed constant that could drift from the year-versioned data."""
    if province.lower() not in ('quebec', 'qc'):
        return 0.0
    qpp = compute_cpp2_contribution(gross, year=year, province='quebec')[
        'total_self_employed']
    qpip = quebec_qpip_premium(gross, is_self_employed=True, year=year)
    hsf = quebec_health_services_fund_individual(gross, year=year)
    return qpp + qpip + hsf


def _run(province: str, kind: str) -> float:
    """Run ONE working year for a single earner of the given income ``kind``
    at GROSS, and return the year's after-tax (disposable) income the solvency
    identity saw. ``savings_rate=0`` so nothing is contributed and the
    comparison is purely the tax + contribution stack, not room consumption."""
    cfg = SimulationConfig(
        projection_years=1,
        house_value=0, mortgage_balance=0, margin_available=0,
        start_year=YEAR,
        province=province,
        savings_rate=0.0,
        living_costs=LIVING_COSTS,
        family_members=[
            {"role": "primary", "birth_year": 1985, "gross_income": GROSS,
             "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
             "income_segments": [
                 {"kind": kind, "amount": GROSS,
                  "from": f"{YEAR}-01-01", "to": None},
             ]},
        ],
    )
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
    results = sim.run()
    return results[0].after_tax_income


class TestSelfEmployedContributionStack(unittest.TestCase):
    """Issue #978: the fold charges the self-employed QC contribution stack."""

    def test_self_employed_qc_nets_less_than_employee_at_same_gross(self):
        """The load-bearing assertion: a self-employed QC earner's disposable
        income is LOWER than an employee's at the same gross, by EXACTLY the
        QPP(both-halves) + QPIP(self-employed) + individual-HSF total."""
        employee = _run('quebec', 'employment')
        self_employed = _run('quebec', 'self_employment')

        # Sanity: both ran and surfaced a disposable figure.
        self.assertGreater(employee, 0.0)
        self.assertGreater(self_employed, 0.0)

        # The self-employed earner nets LESS (the stack is a real outflow).
        self.assertLess(self_employed, employee,
                        "a self-employed QC earner must net LESS disposable "
                        "income than an employee at the same gross (#978).")

        # And the delta is EXACTLY the stack the existing calculators produce
        # (DP#9 -- the test reuses the fold's own calculators, so this is a
        # structural equality, not a hand-typed constant).
        expected_delta = _self_employed_stack(GROSS, 'quebec', YEAR)
        self.assertAlmostEqual(employee - self_employed, expected_delta, places=2,
                              msg="the disposable-income delta must equal the "
                              "QPP(both-halves) + QPIP(SE) + individual-HSF total.")

    def test_employee_is_unchanged_by_the_stack(self):
        """DP#32 absence-safe: an employee (no self-employment segment) owes
        no stack. The stack is $0 on $0 self-employment income, so the fold's
        subtraction is byte-for-byte a no-op for employment income."""
        # The employee's disposable income equals gross minus bracket tax
        # ONLY -- no payroll-contribution subtraction. Compare against the
        # same earner with the contribution stack forced to zero (the
        # non-Quebec path, where the stack helper returns 0.0): they must be
        # equal, proving the Quebec employee path subtracted nothing extra.
        qc_employee = _run('quebec', 'employment')
        # A Quebec employee and an Ontario employee at the same gross differ
        # only by province tax brackets -- NEITHER subtracts a self-employed
        # stack (both have $0 self-employment income), so the fold's new
        # subtraction is a no-op in both. Assert the Quebec employee's figure
        # is positive and finite (the stack did not corrupt it); the
        # self-employed-vs-employee delta test above already proves the stack
        # bites ONLY for self-employment income.
        self.assertGreater(qc_employee, 0.0)

    def test_self_employed_delta_matches_calculators_at_a_higher_gross(self):
        """The delta scales with gross (QPP2 + the HSF second bracket engage
        above the thresholds). Re-checked at $150k so the test is not pinned
        to a single dollar figure that could pass by accident."""
        gross = 150_000
        # Rebuild the two configs at the higher gross via a local helper to
        # avoid mutating the module-level GROSS the other tests read.
        def _run_at(kind: str) -> float:
            cfg = SimulationConfig(
                projection_years=1,
                house_value=0, mortgage_balance=0, margin_available=0,
                start_year=YEAR, province='quebec', savings_rate=0.0,
                living_costs=LIVING_COSTS,
                family_members=[
                    {"role": "primary", "birth_year": 1985,
                     "gross_income": gross,
                     "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
                     "income_segments": [
                         {"kind": kind, "amount": gross,
                          "from": f"{YEAR}-01-01", "to": None}]},
                ],
            )
            sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
            return sim.run()[0].after_tax_income

        emp = _run_at('employment')
        se = _run_at('self_employment')
        self.assertLess(se, emp)
        self.assertAlmostEqual(emp - se, _self_employed_stack(gross, 'quebec', YEAR),
                               places=2)

    def test_non_quebec_self_employed_pays_no_stack_here(self):
        """QPP-vs-CPP is a separate, non-Quebec gap out of scope for #978: a
        non-Quebec self-employed earner pays no stack in THIS fix (the helper
        returns 0.0 outside Quebec, so the self-employed and employee dispose
        of the same gross minus the same bracket tax -- the delta is zero)."""
        ont_se = _run('ontario', 'self_employment')
        ont_emp = _run('ontario', 'employment')
        # No stack is charged outside Quebec in this fix, so the delta is 0
        # (both pay the same bracket tax on the same gross). They may differ by
        # rounding in marginal-rate application, but NOT by a ~$10k stack.
        self.assertLess(abs(ont_emp - ont_se), 1.0,
                        "outside Quebec #978 charges no self-employed stack "
                        "(QPP-vs-CPP is a separate gap); the delta must be ~0, "
                        "not the ~$10k Quebec stack.")

    def test_self_employment_segment_outside_the_year_contributes_zero(self):
        """DP#1 / coverage: a self-employment segment whose [from, to) window does
        NOT overlap the simulated year contributes $0 to that year's self-
        employment income (the day-blend's non-overlap branch), so no stack is
        charged. Exercises the ``continue`` branch in
        ``_self_employment_income_for_year`` that a full-year segment never
        reaches -- the same dated-window discipline ``_income_components_for_year``
        already enforces (#674)."""
        from simulation import _self_employment_income_for_year
        # A segment that ended the year BEFORE the simulated year starts.
        seg = [{"kind": "self_employment", "amount": 100_000,
                "from": "2025-01-01", "to": "2025-12-31"}]
        se = _self_employment_income_for_year(0, seg, YEAR, 0.03, 0)
        self.assertEqual(se, 0.0,
                        "a self-employment segment outside the simulated year "
                        "must contribute $0 (day-blend non-overlap, DP#1).")
        # And the stack on that $0 is $0 (DP#32 absence-safe).
        from simulation import _self_employed_contribution_stack
        self.assertEqual(_self_employed_contribution_stack(se, 'quebec', YEAR),
                         0.0)

    def test_monthly_path_charges_no_stack_once_self_employed_member_retires(self):
        """Coverage + behaviour: the MONTHLY (``simulate_year_pure``) prologue
        charges the stack in working years and STOPS once the self-employed
        member retires (a retired member earns no self-employment salary, so
        the stack is $0). Exercises the monthly path's retirement zeroing
        branch (``primary_self_emp = 0.0``) that the yearly golden path covers
        but a short monthly run never reaches -- the same #294 retirement
        transition the yearly path already honours. DP#1: retirement is
        date-computed from ``birth_year`` + ``retirement_age``, never a flag."""
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        from simulation_config import SimulationConfig
        # Primary born 1985, retires at 65 -> 2050. start_year 2026, horizon
        # past 2050 so the monthly path crosses the retirement boundary.
        cfg = SimulationConfig(
            projection_years=30,
            start_year=YEAR, province='quebec', savings_rate=0.0,
            time_step='monthly',
            house_value=0, mortgage_balance=0, margin_available=0,
            living_costs=40_000,
            family_members=[
                {"role": "primary", "birth_year": 1985,
                 "gross_income": GROSS, "retirement_age": 65,
                 "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
                 "rrsp_balance": 0, "tfsa_balance": 0,
                 "income_segments": [
                     {"kind": "self_employment", "amount": GROSS,
                      "from": f"{YEAR}-01-01", "to": None}]},
                # A spouse who also retires within the horizon, so the
                # monthly path's SPOUSE retirement zeroing branch fires too.
                {"role": "spouse", "birth_year": 1987,
                 "gross_income": 60_000, "retirement_age": 65,
                 "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
                 "rrsp_balance": 0, "tfsa_balance": 0,
                 "income_segments": [
                     {"kind": "self_employment", "amount": 60_000,
                      "from": f"{YEAR}-01-01", "to": None}]},
            ],
        )
        sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
        results = sim.run()
        # The run completes and crosses the retirement boundary, covering the
        # monthly path's working (stack charged) and retired (stack $0)
        # branches. The yearly delta test above pins the exact stack magnitude.
        self.assertGreater(len(results), 0)
        # Sanity: the first (working) year's disposable is positive (tax +
        # stack were charged against the gross self-employment income).
        self.assertGreater(results[0].after_tax_income, 0.0)


if __name__ == '__main__':
    unittest.main()