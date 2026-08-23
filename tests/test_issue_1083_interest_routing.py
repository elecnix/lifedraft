"""Issue #1083: #1033's double-count fix overcorrected -- once the primary is
retired, the s.20(1)(c) investment-interest deduction reduced ONLY the
cpp+pension drawdown base (OAS-clawback relief + draw re-bracketing). The
rental operating income and private-loan interest income the prologue's
``_income_tax_by_adult`` still taxes (employment is zeroed at retirement;
rental/loan income survive it) got NO offset, and where the base floored at 0
the whole deduction was stranded.

## The fix

``apply_retirement_drawdown`` now splits the federal deduction (``ws.sm_
interest_deduction``) DISJOINTLY across the retiree's two taxable-income
slices:

  * the share the primary's cpp+pension base can absorb -- exactly #1033's
    routing (clawback base + draw re-bracketing, unchanged); and
  * the REMAINDER (``D - min(D, cpp+pension base)``) -- routed against
    ``ctx.primary_taxable_income`` (the prologue-taxed slice), valued at
    bracket-fill via ``tax_calculator.deduction_value`` -- the SAME
    ``tax_on_income`` / year-brackets path that taxed the slice (DP#9) -- and
    booked as REAL CASH by ``apply_solvency`` (the tuition_credit booking
    path), surfaced as ``YearResult.sm_interest_nondrawdown_tax_saving``.

## The ordering constraint (why the routing lives mid-fold)

The issue's suggested shape was to feed the deduction into the prologue's
``_income_tax_by_adult`` directly. That is unreachable without re-deriving
the SM interest pre-fold: the deduction is a rule OUTPUT (``sm_interest``
runs mid-fold, after ``mortgage`` / ``margin_heloc_interest`` / ``sm_
readvance`` / ``borrowing_purpose``), and recomputing the three legs in the
prologue would duplicate the readvance/margin-leg logic (DP#9). The
resolution: keep ONE derivation (the rule's) and extend its ROUTING to the
second slice inside the fold, valuing it on the same statutory calculator
the prologue used -- one spelling of the tax law, one spelling of the legs.

## No double-count

The split is disjoint by construction (``absorbed + remainder == D``), so no
dollar of the deduction offsets two incomes; and #1033's phase gate is
untouched -- the flat objective side-credit (``readvance_tax_savings`` /
``traced_borrowing_tax_savings``) stays ZERO in retirement. The saving flows
through the balance sheet (solvency ``available``), not the objective score.

## Out of scope (unchanged)

The QC-capped slice (``qc_deductible``) and its carry-forward are still worth
$0 in retirement -- the provincial cap's release is #1035.

DP#15: every household below is fabricated, round-numbered, role-named.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
import simulation_rules

# ── The fabricated retired household (DP#15) ────────────────────────────────
# Mortgage-free, LOW cpp/pension (the #1033 review fixture's floor case:
# cpp $2,400 against a ~$22k deduction) so the drawdown base cannot absorb the
# deduction, a $400k margin lump drawn and invested, and a rental owned 100%
# by the retired primary yielding $50k operating income -- exactly the
# borrow-to-invest retiree the issue says the engine under-prices.
HOUSE_VALUE = 800_000
MARGIN_AVAILABLE = 400_000
LUMP_SUM = 400_000          # drawn from the margin, invested in non-reg
HELOC_RATE = 0.055
CPP_MONTHLY = 200           # $2,400/yr -- the floor case from the issue
PENSION = 0
RRSP_BALANCE = 400_000      # runs dry mid-horizon -> real shortfall years
RENT_GROSS = 60_000
RENT_EXPENSES = 10_000      # $50k operating income, no rental mortgage
SPENDING_TARGET = 90_000
LIVING_COSTS = 60_000


def _portfolio(yield_dividends=0.02):
    "The non-reg portfolio block (yield drives the QC investment-income cap)."
    return {"accounts": {"non_reg": {
        "balance": 0, "cost_basis": 0,
        "composition": {"cdn_equity_pct": 0.6, "fixed_income_pct": 0.4},
        "yield": {"eligible_dividends": yield_dividends, "interest": 0.0},
    }}}


def _retiree_with_rental_cfg(projection_years=8, birth_year=1956):
    "Retired primary, low CPP/pension, margin lump, 100%-owned rental."
    member = {"role": "primary", "id": "p1", "birth_year": birth_year,
              "retirement_age": 65, "gross_income": 0,
              "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
              "rrsp_balance": RRSP_BALANCE, "tfsa_balance": 0,
              "cpp_monthly_estimated": CPP_MONTHLY, "cpp_start_age": 65,
              "oas_start_age": 65, "oas_defer_months": 0,
              "pension_income_annual": PENSION}
    return {
        "family": {"members": [member], "children": []},
        "property": {"house_value": HOUSE_VALUE, "mortgage_balance": 0,
                     "margin_available": MARGIN_AVAILABLE,
                     "heloc_rate": HELOC_RATE,
                     "mortgage_rate": 0.05, "ltv_max": 0.80,
                     "amortization_years": 25},
        # Internal-config property shape (the mapped form
        # input_contract._map_owned_properties produces): the rental block the
        # prologue's _rental_income_for reads, owned 100% by the primary.
        "properties": [{
            "id": "rental_a", "kind": "rental",
            "net_equity": 500_000,
            "rental": {"gross_rent_annual": RENT_GROSS,
                       "expenses_annual": RENT_EXPENSES,
                       "mortgage_interest_annual": 0.0,
                       "owner_roles": {"primary": 1.0, "spouse": 0.0}},
        }],
        "assumptions": {"start_year": 2026, "projection_years": projection_years,
                        "investment_return": 0.06, "salary_growth": 0.0,
                        "inflation": 0.0, "frozen_brackets": True},
        "portfolio": _portfolio(),
        "accounts": {"rrsp_annual_max": 0},
        # living_costs > 0 engages the solvency identity (its retirement branch
        # charges retirement_spending_target) -- the path that books the saving
        # as cash. The small RRSP runs dry mid-horizon, so the later years run
        # a REAL shortfall through the liquidation waterfall.
        "household_budget": {"living_costs": LIVING_COSTS},
        "retirement": {"spending_target": SPENDING_TARGET,
                       "net_replacement_rate": 0.0,
                       "drawdown_order": ["rrsp"], "rrif_conversion_age": 71},
        "tax": {"province": "qc"},
    }


def _accumulation_cfg(gross_income=150_000, mortgage_balance=480_000,
                      margin_available=0, living_costs=60_000,
                      projection_years=5, birth_year=1990):
    "A working (pre-retirement) household with a drawn margin / advance."
    return {
        "family": {"members": [{"role": "primary", "birth_year": birth_year,
            "retirement_age": 65, "gross_income": gross_income,
            "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0}],
            "children": []},
        "property": {"house_value": 800_000, "mortgage_balance": mortgage_balance,
            "margin_available": margin_available, "heloc_rate": 0.0470,
            "mortgage_rate": 0.0370 if mortgage_balance else 0.05,
            "ltv_max": 0.80, "amortization_years": 25},
        "assumptions": {"start_year": 2026, "projection_years": projection_years,
                        "investment_return": 0.06, "salary_growth": 0.0,
                        "inflation": 0.0, "frozen_brackets": True},
        "portfolio": _portfolio(),
        "accounts": {"rrsp_annual_max": 0},
        "household_budget": {"annual_living_costs": living_costs},
        "tax": {"province": "qc"},
    }


def _run(cfg_dict, lump_sum=LUMP_SUM):
    "Run the household through the live fold and return the YearResults."
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(
        cfg, adapter=CanadaAdapter(cfg), use_readvanceable=False,
        deduct_later=False, lump_sum=lump_sum)
    return sim.run()


def _run_without_nondrawdown_routing(cfg_dict, lump_sum=LUMP_SUM):
    """Re-run the SAME household with ONLY the #1083 remainder routing
    disabled (``sm_interest_nondrawdown_tax_saving`` zeroed after
    ``apply_retirement_drawdown`` fires). #1033's drawdown-base routing and
    the accumulation side-credit are untouched, so the A/B difference
    isolates exactly the remainder routing this issue adds."""
    original = simulation_rules.RULES["retirement_drawdown"]

    def patched(ws, ctx):
        fired = original(ws, ctx)
        ws.sm_interest_nondrawdown_tax_saving = 0.0
        return fired

    simulation_rules.RULES["retirement_drawdown"] = patched
    try:
        return _run(cfg_dict, lump_sum=lump_sum)
    finally:
        simulation_rules.RULES["retirement_drawdown"] = original


# ============================================================================
# 1. The golden no-SM household is byte-exact
# ============================================================================

class TestGoldenNoBorrowingIsByteExact:
    """The routing is gated on ``sm_interest_deduction > 0``; the golden
    household borrows nothing, so every new line is dead there. Pinned
    byte-exact on the documented golden invariant (computed, not stored)."""

    def test_golden_terminal_total_assets_is_unchanged(self):
        from test_golden_trajectory_581 import (
            golden_household_config, _run as _golden_run)
        results = _golden_run(golden_household_config())
        assert results[-1].total_assets == pytest.approx(9_709_753.139463063)


# ============================================================================
# 2. The premise: deductible interest + rental income coexist in retirement
# ============================================================================

class TestPremise:
    """The fixture must actually exercise the defect shape: a retired primary
    with BOTH deductible margin interest AND prologue-taxed rental income."""

    def test_deductible_margin_interest_exists(self):
        rs = _run(_retiree_with_rental_cfg())
        assert any(r.margin_deductible_interest > 0 for r in rs), (
            "premise: the drawn margin's interest must be deductible")

    def test_rental_income_survives_retirement(self):
        rs = _run(_retiree_with_rental_cfg())
        assert any(r.net_rental_income > 0 for r in rs), (
            "premise: the primary's rental must produce net rental income")

    def test_deduction_exceeds_the_cpp_pension_base(self):
        """The floor case: the cpp+pension drawdown base is far smaller than
        the deduction, so #1033's routing alone strands most of it."""
        rs = _run(_retiree_with_rental_cfg())
        assert any(
            r.margin_deductible_interest > (r.cpp_income + r.pension_income)
            for r in rs), (
            "premise: the deduction must exceed the cpp+pension base in some "
            "year -- otherwise there is no stranded remainder to route")


# ============================================================================
# 3. The fix: the remainder is routed against the rental/loan slice
# ============================================================================

class TestRemainderRoutedAgainstPrologueSlice:
    """The stranded share of the deduction (what the cpp+pension base cannot
    absorb) now books a nonzero, statutory-shaped saving against the
    prologue-taxed rental income."""

    def test_nondrawdown_saving_is_nonzero(self):
        rs = _run(_retiree_with_rental_cfg())
        assert any(r.sm_interest_nondrawdown_tax_saving > 0 for r in rs), (
            "the deduction's stranded remainder must book a nonzero saving "
            "against the retiree's rental/loan income")

    def test_saving_is_statutory_not_flat_top_rate(self):
        """Statutory shape: the booked saving must equal the bracket-fill
        valuation of the remainder against the slice -- the SAME
        ``tax_on_income`` / year-brackets path the prologue taxed the slice
        with (DP#9) -- which for a bracket-crossing deduction is STRICTLY
        LESS than the remainder at the top combined rate (the pre-#1033 flat
        mechanism's signature).

        The expected figures are assembled ONLY from surfaced YearResult
        fields + the shared tax calculator (never engine internals): in this
        fixture the sole deductible leg is the margin (use_readvanceable is
        False, the house is mortgage-free), the rental carries no mortgage
        interest and the primary has no private loans, so the prologue slice
        is exactly the net rental operating income, and the base is exactly
        cpp + pension."""
        from tax_calculator import deduction_value, default_tax_provider
        brackets = default_tax_provider().get_combined_brackets(2026, "qc")
        rs = _run(_retiree_with_rental_cfg())
        checked = 0
        for r in rs:
            if not (r.margin_deductible_interest > 0
                    and r.sm_interest_nondrawdown_tax_saving > 0):
                continue
            deduction = r.margin_deductible_interest
            base = r.cpp_income + r.pension_income
            remainder = deduction - min(deduction, base)
            slice_income = RENT_GROSS - RENT_EXPENSES  # the prologue slice
            expected = deduction_value(slice_income, remainder, brackets)
            assert r.sm_interest_nondrawdown_tax_saving == pytest.approx(
                expected), (
                f"year {r.year}: booked "
                f"{r.sm_interest_nondrawdown_tax_saving:.2f} != bracket-fill "
                f"value {expected:.2f} of the remainder {remainder:.2f} "
                f"against the {slice_income:.0f} slice")
            assert r.sm_interest_nondrawdown_tax_saving < remainder * 0.5350, (
                f"year {r.year}: the saving must be valued at bracket-fill, "
                f"never at a flat top rate")
            checked += 1
        assert checked > 0, (
            "no year exercised the remainder routing -- the fixture no longer "
            "strands part of the deduction")

    def test_saving_never_exceeds_the_full_deduction_face_value(self):
        """Disjointness guard: the booked saving can never exceed the value of
        routing the WHOLE deduction against the slice -- that signature would
        mean a dollar is captured twice (once on the drawdown base, once
        here), the exact #1033 defect shape."""
        from tax_calculator import deduction_value, default_tax_provider
        brackets = default_tax_provider().get_combined_brackets(2026, "qc")
        for r in _run(_retiree_with_rental_cfg()):
            if r.sm_interest_nondrawdown_tax_saving <= 0:
                continue
            ceiling = deduction_value(
                RENT_GROSS - RENT_EXPENSES,
                r.margin_deductible_interest, brackets)
            assert r.sm_interest_nondrawdown_tax_saving <= ceiling + 1e-6, (
                f"year {r.year}: saving exceeds the whole-deduction ceiling "
                f"-- a dollar of the deduction is being captured twice")

    def test_saving_reaches_after_tax_income_as_cash(self):
        """The booking is REAL (the tuition_credit path): each year's reported
        after-tax income is exactly the no-routing figure PLUS the booked
        saving. A field that never touched cash would fail this."""
        rs_with = _run(_retiree_with_rental_cfg())
        rs_without = _run_without_nondrawdown_routing(_retiree_with_rental_cfg())
        moved = False
        for a, b in zip(rs_with, rs_without):
            delta = a.after_tax_income - b.after_tax_income
            assert delta == pytest.approx(
                a.sm_interest_nondrawdown_tax_saving), (
                f"year {a.year}: after-tax income moved by {delta:.2f} but "
                f"the booked saving is "
                f"{a.sm_interest_nondrawdown_tax_saving:.2f}")
            if a.sm_interest_nondrawdown_tax_saving > 0:
                moved = True
        assert moved

    def test_routing_eases_the_shortfall_waterfall(self):
        """Direction: in the late years the small RRSP runs dry, the spending
        target forces a real shortfall, and the booked saving (real cash)
        shrinks the forced liquidation -- terminal assets are HIGHER with the
        routing than without it."""
        rs_with = _run(_retiree_with_rental_cfg())
        rs_without = _run_without_nondrawdown_routing(_retiree_with_rental_cfg())
        assert any(r.solvency_shortfall > 0 for r in rs_without), (
            "premise: the fixture must run real shortfall years for the cash "
            "effect to be observable on the balance sheet")
        assert rs_with[-1].total_assets > rs_without[-1].total_assets, (
            "the routed saving is real cash: it must reduce forced "
            "liquidation and raise terminal assets")


# ============================================================================
# 4. No double-count: #1033's phase gate holds, the capture is disjoint
# ============================================================================

class TestNoDoubleCount:
    """The flat objective side-credit stays ZERO in retirement (with rental
    income present -- the shape that hid the pre-#1033 double-count), and the
    new routing never re-adds a flat rate anywhere."""

    def test_side_credit_stays_zero_in_retirement_with_rental_income(self):
        rs = _run(_retiree_with_rental_cfg())
        for r in rs:
            assert (r.readvance_tax_savings
                    + r.traced_borrowing_tax_savings) == pytest.approx(0.0), (
                f"year {r.year}: the flat objective side-credit must stay 0 "
                f"in retirement even with rental income present -- the "
                f"#1083 routing is taxable-income routing, not its return")

    def test_nondrawdown_routing_is_dead_in_accumulation(self):
        """In accumulation the deduction is captured by the side-credit alone
        (#1033's phase gate); the remainder routing must not fire. The
        fixture is #1033's own accumulation household (the leveraged mortgage
        ADVANCE leg -- a drawn margin here would breach the charge
        invariant, the guard doing its job)."""
        cfg = _accumulation_cfg()
        rs = _run(cfg, lump_sum=480_000)
        assert any((r.readvance_tax_savings
                    + r.traced_borrowing_tax_savings) > 0 for r in rs), (
            "premise: the accumulation side-credit must still fire")
        for r in rs:
            assert r.sm_interest_nondrawdown_tax_saving == pytest.approx(0.0), (
                f"year {r.year}: the remainder routing is retirement-only "
                f"(gate: ctx.primary_retired)")

    def test_no_brackets_caller_keeps_flat_rate_fallback(self):
        """Direct rule callers that pass ``primary_marginal_rate`` but no
        ``year_brackets`` keep the flat-rate valuation -- byte-for-byte the
        side-credit's own fallback pattern in ``apply_sm_interest`` (#1033's
        documented contract for direct callers; the live fold always passes
        brackets and takes the bracket-fill path, covered above). Drives the
        production rule directly (DP#11), mirroring #1033's own
        TestBaseFlooredAtZero harness."""
        from simulation_rules import YearWorkingState, RuleContext, RULES
        ws = YearWorkingState(year=0)
        ws.drawdown_other_taxable_income = 2_400.0
        ws.drawdown_other_taxable_income_primary = 2_400.0
        ws.drawdown_oas_gross = 0.0
        ws.drawdown_oas_gross_primary = 0.0
        ws.sm_interest_deduction = 22_000.0   # base absorbs 2,400 -> remainder 19,600
        ws.primary_taxable_income = 0.0       # set below via ctx; ws unused here
        ws.drawdown_net_target = 0.0          # exercise the pre-early-return path
        ws.liquidate_to_target = False
        ws.drawdown_two_member_split = False
        ctx = RuleContext(
            year=0, calendar_year=2026, allocations={}, config=None,
            investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
            mortgage_data=None, use_readvanceable=False, deduct_later=False,
            primary_marginal_rate=0.2569, spouse_marginal_rate=0.0,
            resp_data=None, fhsa_contribution=0.0, rrsp_annual_limit=None,
            tfsa_annual_limit=None, fhsa_annual_limit=None,
            non_reg_after_tax_return=None,
            cpp_income=0.0, oas_income=0.0, pension_income=0.0,
            drawdown_order=None, rrif_min_rate_primary=0.0,
            rrif_min_rate_spouse=0.0, drawdown_net_target=0.0,
            retiree_marginal_rate=0.0, drawdown_bracket_target=None,
            drawdown_other_taxable_income=0.0, primary_retired=True,
            spouse_retired=False, year_brackets=None,
            primary_taxable_income=50_000.0)
        RULES["retirement_drawdown"](ws, ctx)
        assert ws.sm_interest_nondrawdown_tax_saving == pytest.approx(
            19_600.0 * 0.2569), (
            "the no-brackets caller must get the flat-rate fallback "
            "(remainder x primary_marginal_rate)")
