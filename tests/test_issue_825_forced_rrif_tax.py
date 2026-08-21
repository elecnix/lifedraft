"""Issue #825: the forced RRIF-minimum withdrawal's tax must be priced through
the real progressive tax + OAS-clawback path, not a flat placeholder.

Before this fix, ``apply_rrif_minimum`` reinvested the forced RRIF excess net of
a single flat marginal rate (evaluated at a nominal ``$40,000`` slice), and the
OAS 15% recovery tax the mandatory minimum triggers was omitted entirely. The
fix reuses the discretionary drawdown's now-correct pricing
(``retirement_transition.price_forced_rrif_tax``): progressive re-bracketing
(#363 PR 1) on the spouse's own recognized income, plus the incremental OAS
clawback (#363 PR 2) it triggers, per spouse (#363 PR 4).

Fabricated round numbers and role-based names only (DP#4, DP#15).
"""

from countries.canada.adapter import CanadaAdapter
from countries.canada.retirement_transition import price_forced_rrif_tax
from simulation import FamilySimulation
from simulation_config import SimulationConfig

# A two-bracket table: 20% up to 50k, 40% above. A slice climbing across the
# boundary is priced below the top rate applied flat.
_BRACKETS = [
    {'min': 0, 'max': 50_000, 'rate': 0.20},
    {'min': 50_000, 'max': None, 'rate': 0.40},
]


def test_zero_forced_is_a_no_op():
    """No forced withdrawal → no tax, no clawback (DP#32: absence is a hard 0)."""
    assert price_forced_rrif_tax(
        other_taxable_income=20_000, oas_gross=8_000, prior_taxable_draw=0.0,
        forced_taxable=0.0, brackets=_BRACKETS, oas_clawback_threshold=90_000) == (0.0, 0.0)


def test_forced_slice_is_re_bracketed_not_taxed_at_a_flat_top_rate():
    """The forced slice climbs the brackets from the spouse's own base — the
    part in the 20% band is taxed at 20%, not the 40% top rate applied flat."""
    # Base 20k (all in the 20% band); forced 60k lifts income to 80k, crossing
    # into the 40% band at 50k. Real tax: 30k*0.20 + 30k*0.40 = 18,000.
    tax, claw = price_forced_rrif_tax(
        other_taxable_income=20_000, oas_gross=0.0, prior_taxable_draw=0.0,
        forced_taxable=60_000, brackets=_BRACKETS, oas_clawback_threshold=None)
    assert claw == 0.0                       # no OAS to claw back
    assert abs(tax - 18_000) < 1e-6
    # The old flat placeholder would have taxed the whole 60k at the top
    # marginal rate (0.40) = 24,000 — strictly more than the progressive figure.
    assert tax < 60_000 * 0.40


def test_forced_slice_stacks_on_the_discretionary_draw_already_recognized():
    """The forced slice re-brackets on TOP of the taxable draw already taken
    this year — starting higher in the table, so it is taxed no less."""
    stacked, _ = price_forced_rrif_tax(
        other_taxable_income=20_000, oas_gross=0.0, prior_taxable_draw=40_000,
        forced_taxable=10_000, brackets=_BRACKETS, oas_clawback_threshold=None)
    # base is 60k (into the 40% band already), so the whole 10k is at 40% = 4,000.
    assert abs(stacked - 4_000) < 1e-6


def test_oas_clawback_is_folded_in_when_the_forced_slice_crosses_the_threshold():
    """The mandatory minimum that pushes net income above the OAS threshold
    claws back OAS at 15% — the recovery tax that used to be omitted entirely."""
    # claw base before forced = 20k other + 8k OAS = 28k (below 90k threshold).
    # +70k forced = 98k → 8k over → 0.15*8k = 1,200 clawed (< the 8k OAS held).
    _, claw = price_forced_rrif_tax(
        other_taxable_income=20_000, oas_gross=8_000, prior_taxable_draw=0.0,
        forced_taxable=70_000, brackets=_BRACKETS, oas_clawback_threshold=90_000)
    assert abs(claw - 1_200) < 1e-6


def test_clawback_is_only_the_increment_the_forced_slice_adds():
    """When the discretionary draw already pushed income over the threshold,
    the forced minimum books only the ADDITIONAL clawback, capped at the OAS
    that remains — never re-claws what the discretionary path already took."""
    # claw base before forced = 95k other + 7k OAS = 102k (12k over threshold
    # → 0.15*12k = 1,800 already clawed). +300k forced saturates the recovery at
    # the full 7,000 OAS, so the INCREMENT the forced slice books is 7,000-1,800.
    _, claw = price_forced_rrif_tax(
        other_taxable_income=95_000, oas_gross=7_000, prior_taxable_draw=0.0,
        forced_taxable=300_000, brackets=_BRACKETS, oas_clawback_threshold=90_000)
    assert abs(claw - (7_000 - 1_800)) < 1e-6


def test_below_threshold_forced_slice_claws_back_nothing():
    """A forced minimum that leaves net income below the OAS threshold triggers
    no recovery tax (DP#32: 0 is the real answer, not a skipped computation)."""
    _, claw = price_forced_rrif_tax(
        other_taxable_income=10_000, oas_gross=8_000, prior_taxable_draw=0.0,
        forced_taxable=20_000, brackets=_BRACKETS, oas_clawback_threshold=90_000)
    assert claw == 0.0


def test_flat_fallback_when_no_brackets_supplied():
    """Bracket-absent callers fall back to the deprecated flat rate (the #579
    residual), exactly as the discretionary per-source pricing does."""
    tax, _ = price_forced_rrif_tax(
        other_taxable_income=20_000, oas_gross=0.0, prior_taxable_draw=0.0,
        forced_taxable=50_000, brackets=None, oas_clawback_threshold=None,
        flat_rate=0.30)
    assert abs(tax - 50_000 * 0.30) < 1e-6


# ── Engine-level: the fold books the clawback as reduced OAS income ──────────

_START_YEAR = 2026
_BIRTH = 1952  # age 74 at start — past RRIF conversion, large mandatory minimum


def _run_high_rrif():
    """A retiree with a large RRIF and no spending need, so the mandatory
    minimum is fully excess and pushes taxable income well above the OAS
    clawback threshold every year."""
    cfg = {
        'family': {'members': [
            {'role': 'primary', 'birth_year': _BIRTH, 'gross_income': 0,
             'retirement_age': 65, 'cpp_monthly_estimated': 1000,
             'oas_start_age': 65, 'rrsp_room_accumulated': 0,
             'tfsa_room_accumulated': 0},
        ]},
        'assumptions': {'start_year': _START_YEAR, 'horizon_age': 85,
                        'investment_return': 0.05, 'salary_growth': 0.0},
        'portfolio': {'accounts': {
            'rrsp': {'balance': 2_000_000},
            'tfsa': {'balance': 0},
            'non_reg': {'balance': 0, 'cost_basis': 0},
        }},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'margin_available': 0, 'heloc_readvance': False},
        'retirement': {'spending_target': 0, 'drawdown_order': ['rrsp'],
                       'rrif_conversion_age': 71},
        'tax': {'province': 'qc', 'year': 2026},
    }
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()


def test_forced_rrif_clawback_reduces_reported_oas_income():
    """A retiree whose forced RRIF minimum drives income far above the OAS
    threshold has OAS recovered — the clawback the old model omitted (it left
    oas_income untouched by the forced minimum) is now booked as reduced
    oas_income, mirroring the discretionary draw."""
    res = _run_high_rrif()
    gross = _run_no_oas_baseline()
    assert gross > 0                          # sanity: OAS exists gross
    oas_years = [r.oas_income for r in res if r.oas_income is not None]
    # The forced minimum recovers OAS: reported income is driven well below the
    # gross the retiree would otherwise receive — near zero in the peak years.
    assert min(oas_years) < 0.5 * gross


def _run_no_oas_baseline():
    """Gross OAS the same retiree would receive absent any clawback (a member
    at the OAS start age gets a positive OAS max) — proves the assertion above
    is testing a real recovery, not an absent benefit."""
    from countries.canada.retirement import get_oas_annual_max
    return get_oas_annual_max(_START_YEAR)


# ── Per-spouse attribution: spouse retired, primary still working ───────────
#
# When only ONE spouse is retired the drawdown uses the single 'household'
# schedule (no two-member split), so apply_retirement_drawdown must attribute
# the whole taxable draw to whichever spouse is actually retired — the branch
# `ws.drawdown_taxable_spouse = tbo['household']` when the SPOUSE is the sole
# retiree. That per-spouse taxable draw is exactly what the forced RRIF minimum
# (apply_rrif_minimum) then re-brackets its own forced slice on top of (#825),
# so mis-attributing it to the working primary would price the spouse's forced
# minimum on the wrong base. This drives the fold directly (like #711/#712).

_SPLIT_START = 2026
# A working (not retired) primary and a retired spouse past RRIF age. Only the
# spouse is retired, so the split is off and the household schedule prices the
# whole draw against the sole retiree.
_WORKING_PRIMARY = {'role': 'primary', 'gross_income': 50_000,
                    'birth_year': _SPLIT_START - 45, 'retirement_age': 65,
                    'cpp_monthly_estimated': 0}
_RETIRED_SPOUSE = {'role': 'spouse', 'gross_income': 0,
                   'birth_year': _SPLIT_START - 75, 'retirement_age': 65,
                   'cpp_monthly_estimated': 100, 'pension_income_annual': 5_000,
                   'rrsp_balance': 1_000_000}


def _run_spouse_only_retired_fold():
    """Drive retirement_income + retirement_drawdown for one year with the
    spouse as the sole retiree, and return the populated working state."""
    from simulation_rules import RULES, RuleContext, YearWorkingState
    cfg = SimulationConfig.from_dict({
        'assumptions': {'start_year': _SPLIT_START, 'investment_return': 0.05,
                        'inflation': 0.02, 'horizon_age': 95},
        'property': {'house_value': 500_000, 'mortgage_balance': 0,
                     'margin_available': 0, 'ltv_max': 0.80,
                     'amortization_years': 25, 'mortgage_rate': 0.045},
        'family': {'members': [_WORKING_PRIMARY, _RETIRED_SPOUSE], 'children': []},
        'accounts': {'rrsp_annual_max': 31_000},
        'retirement': {'spending_target': 80_000, 'drawdown_order': ['rrsp']},
        'tax': {'province': 'qc'},
    })
    sim = FamilySimulation(cfg)
    ws = YearWorkingState(year=0)
    ws.new_rrsp_bal = 0.0
    ws.new_spouse_rrsp_bal = 1_000_000.0
    ctx = RuleContext(
        year=0, calendar_year=_SPLIT_START, allocations={}, config=cfg,
        investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=0.0, spouse_marginal_rate=0.0, resp_data=None,
        fhsa_contribution=0.0, rrsp_annual_limit=None, tfsa_annual_limit=None,
        fhsa_annual_limit=None, non_reg_after_tax_return=None,
        cpp_income=0.0, oas_income=0.0, pension_income=0.0, drawdown_order=None,
        rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0, retiree_marginal_rate=0.0,
        drawdown_bracket_target=None, drawdown_other_taxable_income=0.0,
        primary_income_pre=0.0, spouse_income_pre=0.0,
        primary_retired=False, spouse_retired=True,
        base_primary_income=sim._primary_income,
        base_spouse_income=sim._spouse_income,
        year_brackets=sim.brackets,
        tax_indexation_rate=sim.tax_provider.indexation_rate,
    )
    RULES['retirement_income'](ws, ctx)
    RULES['retirement_drawdown'](ws, ctx)
    return ws


def test_household_draw_is_attributed_to_the_sole_retired_spouse():
    """With only the spouse retired, the whole taxable draw is booked to the
    spouse (not the working primary), so the spouse's forced RRIF minimum will
    re-bracket on the spouse's OWN income."""
    ws = _run_spouse_only_retired_fold()
    assert ws.drawdown_total > 0                     # a draw actually fired
    assert ws.drawdown_taxable_primary == 0.0        # nothing to the working primary
    assert abs(ws.drawdown_taxable_spouse - ws.drawdown_total) < 1e-6
