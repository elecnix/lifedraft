"""Issue #980: a self_employment income's tax / contribution / RRSP-room bases
are its NET business income (gross fees - T2125 professional expenses), not
its gross fees.

A self-employed professional's tax base is NET business income = gross fees
minus the T2125 expenses (home-office business-use-%, professional dues +
mandatory liability insurance, professional development, meals/entertainment
at 50% inclusion). Before #980 a ``self_employment`` income segment's
``amount`` was the GROSS, with no expense field, so the user had to hand-
compute the net -- and the engine had no way to model the professional-expense
deduction that ALSO flows into the #978 self-employed contribution stack
(QPP both halves + QPIP + individual HSF) and RRSP-room accrual
(ITA s.146(1) "earned income" is net self-employment income, not gross).

#980 lets a ``self_employment`` income/override carry an OPTIONAL
``expenses_annual`` scalar (the T2125 total -- one spelling of the 'net =
gross - expenses' fact, the SAME shape the structurally identical rental case
uses for ``property[kind=rental].expenses_annual`` / CRA T776, DP#9, not a
per-category block the engine merely sums). The engine derives
net = gross - expenses_annual ONCE (:func:`simulation._self_employment_net_amount`)
and feeds that net to all three consumers -- tax (``_income_components_for_year``
total_income), RRSP room (its earned_income), and the contribution stack
(``_self_employment_income_for_year``) -- so expenses reduce the QPP/QPIP/HSF
base too, not just the tax base.

DP#32 absence-safe: a self_employment segment with NO ``expenses_annual`` key
(or null) is taxed on its gross ``amount`` -- byte-identical to pre-#980. An
explicit ``0`` is the SAME as absent (zero is a value, not a fallback; the
explicit ``is None`` test, never ``or 0.0``, keeps that honest and is not
mechanically flagged by the architecture guard). The golden household has NO
self-employment income, so #980 is a strict no-op for it (verified byte-exact
on the 46-year golden fixture in test_golden_trajectory_581).

DP#4/DP#15: fabricated round numbers and role-based names only.
"""

import unittest

from countries.canada.adapter import CanadaAdapter
from countries.canada.cpp_sharing import compute_cpp2_contribution
from countries.canada.provinces.quebec.quebec_credits import (
    quebec_qpip_premium,
    quebec_health_services_fund_individual,
)
from simulation import (
    FamilySimulation,
    _income_components_for_year,
    _self_employed_contribution_stack,
    _self_employment_income_for_year,
    _self_employment_net_amount,
)
from simulation_config import SimulationConfig

GROSS = 120_000          # a fabricated round number (DP#4/DP#15)
EXPENSES = 30_000        # a fabricated round T2125 total (home-office, dues,
                          # liability insurance, PD, meals-at-50% -- entered as
                          # the single scalar the schema declares)
NET = GROSS - EXPENSES   # 90_000
YEAR = 2026               # the engine's default start year (year-versioned data)
LIVING_COSTS = 40_000     # declared so apply_solvency runs and surfaces
                           # YearResult.after_tax_income (the working-phase
                           # disposable figure the cash-flow identity uses)


def _self_employed_stack(net: float, province: str, year: int) -> float:
    """The exact total the fold subtracts on NET business income -- reuses the
    SAME calculators the fold reuses (DP#9), so the test's expected stack is
    structural, not a hand-typed constant that could drift from the
    year-versioned data."""
    if province.lower() not in ('quebec', 'qc'):
        return 0.0
    qpp = compute_cpp2_contribution(net, year=year, province='quebec')[
        'total_self_employed']
    qpip = quebec_qpip_premium(net, is_self_employed=True, year=year)
    hsf = quebec_health_services_fund_individual(net, year=year)
    return qpp + qpip + hsf


def _segment(amount: float, expenses=None) -> dict:
    """A full-year self_employment segment for the given gross amount, with an
    optional ``expenses_annual`` (None = no expenses block, the absence case)."""
    seg = {"kind": "self_employment", "amount": amount,
           "from": f"{YEAR}-01-01", "to": None}
    if expenses is not None:
        seg["expenses_annual"] = expenses
    return seg


def _run(province: str, amount: float, expenses=None) -> float:
    """Run ONE working year for a single self-employed earner at the given gross
    ``amount`` with optional T2125 ``expenses``, and return the year's
    after-tax (disposable) income the solvency identity saw. ``savings_rate=0``
    so nothing is contributed and the comparison is purely the tax +
    contribution stack on the net base, not room consumption."""
    cfg = SimulationConfig(
        projection_years=1,
        house_value=0, mortgage_balance=0, margin_available=0,
        start_year=YEAR,
        province=province,
        savings_rate=0.0,
        living_costs=LIVING_COSTS,
        family_members=[
            {"role": "primary", "birth_year": 1985, "gross_income": amount,
             "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
             "income_segments": [_segment(amount, expenses)]},
        ],
    )
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
    return sim.run()[0].after_tax_income


def _run_and_get_room(amount: float, expenses=None) -> float:
    """Run one working year and return the primary's NEW RRSP room the
    contribution-room rule wrote (savings_rate=0 so none is consumed the same
    year -- the room added is purely the 18% accrual on the earned-income
    base). Mirrors test_issue_674's ``_run_and_get_room`` helper exactly."""
    cfg = SimulationConfig(
        projection_years=1,
        house_value=0, mortgage_balance=0, margin_available=0,
        start_year=YEAR, province="quebec", savings_rate=0.0,
        family_members=[
            {"role": "primary", "birth_year": 1985, "gross_income": amount,
             "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
             "income_segments": [_segment(amount, expenses)]},
        ],
    )
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
    sim.run()
    from simulation_state import adult_rrsp_slot
    return adult_rrsp_slot(sim._state.jurisdiction_state["canada"], 0)[1]


class TestNetAmountHelper(unittest.TestCase):
    """The single spelling of 'net = gross - expenses' (DP#9): all three
    consumers read this helper's output, so the net is computed ONCE."""

    def test_no_expenses_block_returns_gross(self):
        """DP#32 absence-safe: a segment with no ``expenses_annual`` key has
        net == gross -- byte-identical to pre-#980."""
        seg = {"kind": "self_employment", "amount": GROSS,
               "from": f"{YEAR}-01-01", "to": None}
        self.assertEqual(_self_employment_net_amount(seg), GROSS)

    def test_null_expenses_returns_gross(self):
        """DP#32: an explicit ``null`` is the schema's spelling of 'no expenses
        declared', identical to the key's absence -- net == gross."""
        seg = {"kind": "self_employment", "amount": GROSS,
               "from": f"{YEAR}-01-01", "to": None,
               "expenses_annual": None}
        self.assertEqual(_self_employment_net_amount(seg), GROSS)

    def test_explicit_zero_expenses_returns_gross(self):
        """DP#32: zero is a VALUE, not a fallback. An explicit ``0`` (a self-
        employed earner who genuinely had no deductible expenses this year)
        yields net == gross, and is NOT confused with 'absent' by an ``or``
        -- the helper uses an explicit ``is None`` test."""
        seg = {"kind": "self_employment", "amount": GROSS,
               "from": f"{YEAR}-01-01", "to": None,
               "expenses_annual": 0}
        self.assertEqual(_self_employment_net_amount(seg), GROSS)

    def test_expenses_reduce_gross_to_net(self):
        """The load-bearing T2125 computation: net = gross - expenses_annual."""
        seg = _segment(GROSS, EXPENSES)
        self.assertEqual(_self_employment_net_amount(seg), NET)

    def test_expenses_exceeding_gross_yield_a_deductible_loss(self):
        """A self-employment loss (expenses > gross) is left AS-IS -- a
        deductible loss carried against other income is the user's T2125
        reality, not an engine opinion. Flooring it at zero would silently
        drop a real deduction (the 'returning 0 on <= 0' trap)."""
        seg = _segment(40_000, 60_000)
        self.assertEqual(_self_employment_net_amount(seg), -20_000)


class TestNetFeedsTaxAndContributionsAndRRSPRoom(unittest.TestCase):
    """The three consumers of a self_employment segment's amount all read the
    NET, not the gross -- one spelling (DP#9)."""

    def test_tax_base_reads_net(self):
        """``_income_components_for_year``'s ``total_income`` (the tax base)
        is gross - expenses for a self_employment segment."""
        ti_no, _ = _income_components_for_year(
            0, [_segment(GROSS)], YEAR, 0.03, 0)
        ti_exp, _ = _income_components_for_year(
            0, [_segment(GROSS, EXPENSES)], YEAR, 0.03, 0)
        self.assertEqual(ti_no, GROSS)
        self.assertEqual(ti_exp, NET)
        self.assertEqual(ti_no - ti_exp, EXPENSES,
                         "expenses must reduce the tax base by exactly the "
                         "declared T2125 total.")

    def test_rrsp_room_base_reads_net(self):
        """``_income_components_for_year``'s ``earned_income`` (the RRSP-room
        accrual base, ITA s.146(1) "earned income" = NET self-employment
        income) is gross - expenses for a self_employment segment."""
        _, ei_no = _income_components_for_year(
            0, [_segment(GROSS)], YEAR, 0.03, 0)
        _, ei_exp = _income_components_for_year(
            0, [_segment(GROSS, EXPENSES)], YEAR, 0.03, 0)
        self.assertEqual(ei_no, GROSS)
        self.assertEqual(ei_exp, NET)
        self.assertEqual(ei_no - ei_exp, EXPENSES,
                         "expenses must reduce the RRSP-room base by exactly "
                         "the declared T2125 total -- RRSP room accrues on "
                         "NET self-employment income (ITA s.146(1)).")

    def test_contribution_stack_base_reads_net(self):
        """``_self_employment_income_for_year`` (the #978 contribution-stack
        base) is gross - expenses -- expenses reduce the QPP/QPIP/HSF base
        too, not just the tax base."""
        se_no = _self_employment_income_for_year(
            0, [_segment(GROSS)], YEAR, 0.03, 0)
        se_exp = _self_employment_income_for_year(
            0, [_segment(GROSS, EXPENSES)], YEAR, 0.03, 0)
        self.assertEqual(se_no, GROSS)
        self.assertEqual(se_exp, NET)
        self.assertEqual(se_no - se_exp, EXPENSES)

    def test_stack_magnitude_is_lower_on_net(self):
        """The #978 stack is a percentage of NET, not gross: the stack on the
        net base is materially lower than on the gross base, by EXACTLY the
        difference the existing calculators produce on (gross vs net)."""
        stack_gross = _self_employed_contribution_stack(GROSS, 'quebec', YEAR)
        stack_net = _self_employed_contribution_stack(NET, 'quebec', YEAR)
        self.assertGreater(stack_gross, stack_net,
                           "the QPP/QPIP/HSF stack must be lower on the net "
                           "(lower) base than on the gross base (#980).")
        # And each equals the calculators on its own base (DP#9 -- structural).
        self.assertAlmostEqual(stack_gross,
                               _self_employed_stack(GROSS, 'quebec', YEAR),
                               places=2)
        self.assertAlmostEqual(stack_net,
                               _self_employed_stack(NET, 'quebec', YEAR),
                               places=2)


class TestGrossVsNetEquivalentToALowerGross(unittest.TestCase):
    """The structural identity: an earner with $X gross + $Y expenses nets the
    SAME disposable income as an earner with $(X-Y) gross and NO expenses --
    because tax AND the #978 stack both read the net. This is the load-bearing
    end-to-end assertion that expenses reduce BOTH bases by the same amount."""

    def test_disposable_income_equals_a_lower_gross_with_no_expenses(self):
        """$GROSS gross + $EXPENSES expenses == $NET gross + no expenses, for
        disposable income (tax + #978 stack both on the net base)."""
        with_expenses = _run('quebec', GROSS, expenses=EXPENSES)
        lower_gross_no_expenses = _run('quebec', NET, expenses=None)
        self.assertAlmostEqual(with_expenses, lower_gross_no_expenses, places=2,
                              msg="a self-employed earner with $X gross and $Y "
                              "T2125 expenses must dispose of the SAME income "
                              "as an earner with $(X-Y) gross and no expenses "
                              "-- tax AND the #978 stack both read the net.")

    def test_expenses_lower_disposable_vs_untaxed_gross(self):
        """Sanity: the ``amount`` is GROSS fees and ``expenses_annual`` is money
        ACTUALLY SPENT on the business, so the earner's net cash is
        (gross - expenses). Declaring the expenses makes the engine see the
        real (lower) net and tax/stack THAT -- so disposable income is LOWER
        than the pre-#980 engine that (wrongly) taxed the full gross as if it
        were all kept. The pre-#980 figure OVERSTATED disposable; #980 makes
        it correct (lower), not higher."""
        no_expenses = _run('quebec', GROSS, expenses=None)
        with_expenses = _run('quebec', GROSS, expenses=EXPENSES)
        self.assertLess(with_expenses, no_expenses,
                        "declaring T2125 expenses reveals the real (lower) net "
                        "business income, so disposable income is LOWER than "
                        "the pre-#980 engine that taxed the full gross as if "
                        "it were all kept. The pre-#980 figure OVERSTATED "
                        "disposable; #980 corrects it downward, not upward.")
        # And the with-expenses disposable equals the disposable of an earner
        # at the NET gross with no expenses (both read the same net base) --
        # the structural identity asserted in the test above, re-checked here
        # at the disposable level for clarity.
        lower_gross_no_expenses = _run('quebec', NET, expenses=None)
        self.assertAlmostEqual(with_expenses, lower_gross_no_expenses,
                               places=2)


class TestAbsenceSafe(unittest.TestCase):
    """DP#32: a self_employment segment with NO expense block is taxed on its
    gross amount byte-identically -- the #980 change is a strict no-op when
    no expenses are declared."""

    def test_no_expenses_block_is_byte_identical_to_explicit_zero(self):
        """The absence case (no ``expenses_annual`` key) and the explicit-zero
        case (``expenses_annual: 0``) produce byte-identical disposable income
        -- zero is a value, not a fallback."""
        absent = _run('quebec', GROSS, expenses=None)
        explicit_zero = _run('quebec', GROSS, expenses=0)
        self.assertEqual(absent, explicit_zero,
                         "a missing expenses block and an explicit $0 must be "
                         "identical (DP#32: zero is a value, not a fallback).")

    def test_rrsp_room_accrual_on_net_not_gross(self):
        """End-to-end: the year's NEW RRSP room is 18% of the NET
        (gross - expenses), NOT 18% of gross. savings_rate=0 so no room is
        consumed the same year. At GROSS=120k / NET=90k neither base hits the
        ~$32k 2026 annual cap, so the room is the uncapped 18% -- the cleanest
        assertion (no cap arithmetic to reproduce)."""
        room_gross = _run_and_get_room(GROSS, expenses=None)
        room_net = _run_and_get_room(GROSS, expenses=EXPENSES)
        # 18% of each base, uncapped at these levels (0.18*120k = 21.6k).
        self.assertAlmostEqual(room_gross, 0.18 * GROSS, places=2,
                              msg="with no expenses, RRSP room accrues on the "
                              "gross (18% of GROSS, uncapped at this level).")
        self.assertAlmostEqual(room_net, 0.18 * NET, places=2,
                              msg="with expenses, RRSP room accrues on the NET "
                              "(gross - expenses), NOT the gross (18% of NET, "
                              "uncapped at this level).")
        # The room REDUCED by exactly 18% of the expenses (the deduction's
        # RRSP-room cost) -- ITA s.146(1) earned income is net self-employment.
        self.assertAlmostEqual(room_gross - room_net, 0.18 * EXPENSES, places=2,
                              msg="the RRSP-room reduction must equal 18% of "
                              "the declared T2125 expenses -- RRSP room "
                              "accrues on NET self-employment income (ITA "
                              "s.146(1)).")
        self.assertLess(room_net, room_gross,
                        "expenses must REDUCE the RRSP room accrued (the base "
                        "is net self-employment income, ITA s.146(1)).")

    def test_expenses_on_an_employment_segment_are_ignored(self):
        """The engine reads ``expenses_annual`` ONLY on a self_employment
        segment -- an employment segment carrying the field must be taxed on
        its gross (the field is a no-op for non-self_employment kinds). This
        guards against the schema allowing the field on any kind while the
        engine scopes it to self_employment."""
        def _emp_run(expenses):
            seg = {"kind": "employment", "amount": GROSS,
                   "from": f"{YEAR}-01-01", "to": None}
            if expenses is not None:
                seg["expenses_annual"] = expenses
            cfg = SimulationConfig(
                projection_years=1, house_value=0, mortgage_balance=0,
                margin_available=0, start_year=YEAR, province="quebec",
                savings_rate=0.0, living_costs=LIVING_COSTS,
                family_members=[{"role": "primary", "birth_year": 1985,
                                 "gross_income": GROSS,
                                 "rrsp_room_accumulated": 0,
                                 "tfsa_room_accumulated": 0,
                                 "income_segments": [seg]}],
            )
            return FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()[0]\
                .after_tax_income
        self.assertEqual(_emp_run(None), _emp_run(EXPENSES),
                         "expenses_annual on an EMPLOYMENT segment must be "
                         "ignored -- the engine reads it only on "
                         "self_employment (#980).")


if __name__ == '__main__':
    unittest.main()