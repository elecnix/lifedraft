#!/usr/bin/env python3
"""Rules as a registry (issue #584, DP#10/DP#26, epic #603).

## The gap this closes

Before this file existed, every government-program rule that fires during a
simulated year was inlined as an anonymous block inside one ~790-line
function (``simulation_state.simulate_year_pure``). There was no place to
look and see "here are all the rules that must fire in a given year" --
nothing enumerated the rule space, so nothing could notice a hole in it.
That is precisely the shape that let #574 (RRIF minimums), #578 (RESP
wind-down) and #627 (eight rules silently missing from the optimizer path,
including the entire retirement transition) go unnoticed while ~4,100 tests
stayed green.

## The fix

Each government-program computation below is a small function registered
under a name via ``@rule(name)``, mirroring ``tests/trajectory_invariants.py``'s
``@invariant(name)`` pattern from issue #581. ``RULE_ORDER`` declares the
sequence they run in -- explicitly, as data, not as an emergent property of
dict insertion order (tax rules are order-dependent: contributions must be
clamped before they're deducted, accounts must grow before retirement
drawdown sizes against post-growth balances, and so on; each rule's
docstring says what it depends on from an earlier rule).

``tests/test_issue_584_rules_registry.py`` enforces the payoff: an
independently-declared ``EXPECTED_RULE_NAMES`` set must equal
``RULE_ORDER`` exactly (a rule silently added to the registry without being
expected, or expected but never registered, fails the build -- DP#32, "a
rule either registers or it doesn't, and an unregistered rule is a loud
absence, not a quiet zero"), and a coverage sweep over representative
households asserts every registered rule actually *fires* (has an
observable effect) in some year -- catching a rule that is nominally
registered but whose wiring is broken (the #627 shape) even though nothing
crashes.

## Shape

Each rule is ``(ws: YearWorkingState, ctx: RuleContext) -> bool``:

- ``ctx`` is per-call, read-only input (config, this year's rates,
  allocations, the retirement-transition outputs) -- the same shape of data
  ``simulate_year_pure``'s caller (``simulation.simulate_year``) already
  assembles. Frozen; no rule mutates it.
- ``ws`` is this year's mutable working state: the opening (Jan-1) balances
  read from ``SimState.jurisdiction_state['canada']`` plus every ``new_*``
  value rules compute and thread to later rules and to the final
  ``YearResult``/``SimState`` assembly (which stays in
  ``simulation_state.simulate_year_pure`` -- assembling the output record
  from whatever the rules produced is bookkeeping, not itself a
  government-program rule to enumerate).
- The return value is whether the rule had an observable effect *this
  year* (used only for the coverage sweep, never for control flow) --
  e.g. the RESP rule returns ``True`` only in a year it actually
  contributed, granted, paid an EAP, or collapsed a plan, not merely
  because it executed (every rule executes every year; "fired" means
  "did something", not "ran").

Per DP#26/#583: no rule function reads ``self`` or any instance -- every
input arrives explicitly via ``ws``/``ctx``. Per DP#25: this module makes no
module-level ``countries.canada`` import; jurisdiction-specific helpers are
imported inside each rule function body, exactly as
``simulation_state.simulate_year_pure`` already did before this refactor.
"""

from __future__ import annotations

from tax_data import default_tax_provider

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from charge_limits import charge_room_for_readvance, ReadvanceableWithoutPropertyError
from simulation_config import SimulationConfig
# Issue #688/#679: pure, jurisdiction-agnostic reserve sizing (DP#25 -- this
# module imports no jurisdiction code at module level, and
# liquidation_waterfall imports none of ours).
from liquidation_waterfall import months_covered, reserve_target

logger = logging.getLogger(__name__)


def _principal_value_for_year(config: SimulationConfig, cal_year: int) -> float:
    """Issue #963 (epic #956 bite F): the principal residence's GROSS value in
    calendar ``cal_year``, applying the declared ``appreciation_rate`` so the
    home's value compounds year over year exactly as Bite A does for a
    non-principal property.

    The principal's value is NOT in ``total_assets`` (it flows via
    ``house_value`` / LTV / charge math and, at the horizon, the estate's
    deemed disposition); this helper is the ONE spelling of the appreciated
    value every consumer of the principal's gross value reads (DP#9 -- one
    spelling, not a second per consumer): the annual LTV/charge room (the
    ``apply_sm_readvance`` / ``apply_margin_heloc_interest`` rules), Bite E's
    principal-sale gross (the ``apply_principal_disposition`` rule), and the
    estate's terminal FMV (``objective._estate_call_args``).

    The base value is ``config.house_value`` (the principal is held from the
    projection start -- never a dated mid-horizon purchase, so the ownership
    year IS ``config.start_year``), and the value at calendar year Y is
    ``house_value * (1 + rate) ** (Y - start_year)``. Absence-safe (DP#32): an
    absent or 0.0 rate returns the static ``house_value`` unchanged and never
    reads the exponent, so a household that declares no appreciation (incl.
    the golden fixture) is byte-identical to today. A negative rate is honored
    (the schema permits it; a falling market is a real scenario a sell/keep
    sweep must be robust to). """
    rate = config.appreciation_rate
    if rate is None or rate == 0.0:
        return config.house_value
    years = max(0, cal_year - config.start_year)
    return config.house_value * ((1.0 + rate) ** years)


# =============================================================================
# Per-call context and working state
# =============================================================================

@dataclass(frozen=True)
class RuleContext:
    """Everything a rule needs besides the mutable ``ws`` (DP#26): this
    year's config, rates, allocations and retirement-transition outputs.
    Frozen -- no rule mutates its inputs."""
    year: int
    calendar_year: int
    allocations: Dict[str, float]
    config: SimulationConfig
    investment_return: float
    mortgage_rate: float
    heloc_rate: float
    mortgage_data: Optional[Dict]
    use_readvanceable: bool
    deduct_later: bool
    primary_marginal_rate: float
    spouse_marginal_rate: float
    resp_data: Optional[List[Dict]]
    fhsa_contribution: float
    rrsp_annual_limit: Optional[float]
    tfsa_annual_limit: Optional[float]
    fhsa_annual_limit: Optional[float]
    non_reg_after_tax_return: Optional[float]
    cpp_income: float
    oas_income: float
    pension_income: float
    drawdown_order: Optional[List[str]]
    rrif_min_rate_primary: float
    rrif_min_rate_spouse: float
    drawdown_net_target: float
    retiree_marginal_rate: float
    drawdown_bracket_target: Optional[float]
    drawdown_other_taxable_income: float
    # Issue #758: the retirement-phase flag and the EFFECTIVE retirement
    # spending target (spending_target or net_replacement_rate x pre-net;
    # 0.0 in every pre-retirement year). Used by apply_solvency to charge
    # the RETIREMENT spending figure in retirement instead of the
    # working-phase `living_costs`, so the drawdown (which funds retirement
    # spending) is not double-counted as available against a working budget
    # the household no longer spends. See apply_solvency's docstring.
    any_retired: bool = False
    retirement_spending_target: float = 0.0
    # ── epic #795 bite 1 (DP#26): inputs for the registered
    # `retirement_income` rule, which computes the per-year retirement
    # transition (CPP/OAS/pension onset, income stop, drawdown sizing)
    # that the fold's prologue used to compute inline. The rule lazy-imports
    # countries.canada.retirement_transition primitives (DP#25) and writes
    # its outputs to YearWorkingState (cpp_income/oas_income/.../any_retired
    # above), which retirement_drawdown / rrif_minimum / solvency then read.
    # Defaults preserve pre-retirement / direct-unit-test behaviour exactly:
    # when no member is retired the rule is a no-op, and a caller that
    # passes the transition OUTPUTS directly (cpp_income=..., etc.) seeds
    # ws before run_rules so isolated rule tests keep working.
    # Pre-retirement GROWN employment incomes (the inputs to the transition);
    # the rule zeroes them itself for the covered_net shortfall math.
    primary_income_pre: float = 0.0
    spouse_income_pre: float = 0.0
    primary_retired: bool = False
    spouse_retired: bool = False
    # Year-0 (base) gross employment incomes per member -- the stable
    # pre-retirement standard of living the NET replacement target is sized
    # against (pre_gross/pre_net in the transition). Distinct from
    # *_income_pre, which are this year's salary-GROWN figures.
    base_primary_income: float = 0.0
    base_spouse_income: float = 0.0
    # Resolved year-brackets (the prologue already computes these for the
    # marginal rates) so the rule does not re-resolve them -- and does not
    # import simulation._year_brackets_for (which would be a circular
    # simulation_rules -> simulation import, DP#25).
    year_brackets: Optional[List[Dict]] = None
    # DP#20: the tax-data indexation rate, used to index an explicit OAS
    # override forward from start_year. Passed as a scalar (not the whole
    # tax_provider) to keep RuleContext jurisdiction-agnostic.
    tax_indexation_rate: float = 0.0
    # Issue #1020 (S04 Step 1): the PRIOR year's GIS-countable income
    # (CPP + pension + RRSP/RRIF drawdown + LIF + employment -- everything
    # taxable EXCEPT OAS, per ``gis_benefit``'s ``net_income`` contract and
    # CRA's prior-year income test). The ``retirement_income`` rule calls
    # ``gis_benefit(prior_countable, is_coupled, sim_year)`` from this. None
    # (the default) means "no prior year available / GIS not engaged" -- the
    # rule leaves ``ws.gis_income`` at its seeded 0.0, preserving every
    # direct unit-test caller and every GIS-ineligible household byte-for-byte
    # (DP#32: absence is a loud no-op, never a silent zero-coercion).
    prior_gis_countable_income: Optional[float] = None
    # Issue #679: the household's own measured working-phase living-cost
    # budget and this year's after-tax employment income. Default 0.0
    # preserves pre-existing behaviour exactly for every caller that does
    # not supply them -- see apply_solvency's docstring for why
    # living_costs<=0 is this module's "not engaged" state, not a DP#32
    # zero-as-fallback trap.
    living_costs: float = 0.0
    after_tax_income: float = 0.0
    # Issue #679: this year's contributions that were funded by BORROWING, not
    # by income -- the year-0 leveraged lump sum (a HELOC margin draw plus a
    # mortgage cash-out; see simulation.margin_draw_for_lump_sum). The engine
    # allocates it into RRSP/TFSA/non-reg exactly like a cash contribution, so
    # it lands in the identity's `contributions` term -- but no dollar of it
    # came out of the household's pay. Its financing is already booked as DEBT
    # (mortgage_balance + heloc_balance) and serviced in every later year.
    #
    # Counting the outflow while ignoring the inflow that funded it invents a
    # cash shortfall the household never had, and forces a liquidation to
    # cover it -- which would report RUIN for precisely the leveraged
    # strategies (the Smith Manoeuvre) this repo exists to evaluate. A false
    # ruin is the same defect class as a false solvency, pointed the other
    # way, so the identity counts this as the inflow it actually is.
    borrowed_investment: float = 0.0
    # Issue #914: the non-borrowed year-0 free cash (RESP-collapse/EAP proceeds)
    # invested this year. It lands in `contributions` exactly like the borrowed
    # lump above, but it arrived as CASH (from winding down the RESP), not from
    # borrowing -- so it is an inflow with NO debt attached. Counting the
    # contribution outflow without counting this inflow would invent the same
    # false shortfall borrowed_investment guards against, one seam over. 0.0
    # after year 0 and for any run with no free cash (DP#32).
    free_cash_invested: float = 0.0
    # Issue #761: True in a WORKING-LIFE year whose income is reduced below
    # the no-override baseline by a dated decisions.income[] shock (a job
    # loss / salary cut -- simulation.py detects this from income_segments).
    # When True AND the household declared household_budget.discretionary_fraction,
    # apply_solvency compresses the discretionary portion of living_costs to
    # zero in the identity's `required` term (groceries/insurance/utilities
    # and debt service stay rigid). False in every no-shock year and in
    # retirement (retirement spending is the drawdown target, a different
    # figure). Default False preserves pre-existing behaviour exactly for
    # every caller that does not supply it (DP#16: no split declared OR no
    # shock -> identity charges the full scalar, byte-for-byte unchanged).
    income_shock_active: bool = False
    # Issue #641: per-registered-pot foreign-withholding-tax drag, derived from
    # each account's OWN declared holdings (``PortfolioConfig.registered_wht_
    # drag``). Maps {kind: drag_rate} for rrsp/tfsa pots holding foreign equity;
    # the growth rule subtracts it from the pot's gross rate so a sheltered
    # account's composition reaches its compounding (the one tax that leaks from
    # a registered account -- unrecoverable/one-level WHT). None (or an absent
    # kind) is a strict no-op: the flat gross rate is preserved, byte-for-byte,
    # for a household that declares no registered composition (golden, DP#32).
    registered_wht_drag: Optional[Dict[str, float]] = None
    # Issue #747: the opening AMT / Quebec-IMR minimum-tax credit balances
    # carried in from the prior year (ITA s.120.2; Revenu Québec TP-776.42),
    # each a tuple of ``AMTCredit`` (frozen year+amount). Empty for any household
    # that has never paid a minimum tax -- e.g. the golden household -- which is
    # exactly why the ``amt`` rule can stay a strict no-op for it (DP#32).
    amt_credit_opening: tuple = ()
    qc_imr_credit_opening: tuple = ()
    # ── epic #795 bite 3 (DP#26): inputs for the registered `tuition_credit`
    # rule, which computes the federal (+ QC provincial) tuition tax credit
    # -- own-credit application with carry-forward (#784) + inter-member /
    # child transfers (#785) -- that the fold's prologue used to compute
    # inline (two spellings: simulate_year and _run_monthly). The prologue
    # now passes only these INPUTS and the rule writes its outputs to
    # YearWorkingState (the applied tax reduction + the new carry-forwards);
    # apply_solvency reads the applied amounts and the epilogue / result
    # surface the carry-forwards. Defaults preserve the no-tuition / direct-
    # unit-test behaviour exactly: when no tuition is declared the rule is a
    # no-op.
    # The tax provider the prologue resolved (DP#20: year-versioned credit
    # rates). Opaque -- this dataclass holds no jurisdiction import (DP#25);
    # the rule lazy-imports the jurisdiction primitive and passes this
    # through. None => the rule uses tax_data.default_tax_provider().
    tax_provider: object = None
    # Each taxed member's pre-credit tax_on_income (the figure the credit is
    # subtracted from, floored at 0). Computed by the prologue's per-adult
    # tax loop and passed as a scalar so the rule does not re-derive it.
    primary_tax_before: float = 0.0
    spouse_tax_before: float = 0.0
    # Issue #956 bite B: each taxed member's TAXABLE INCOME base (employment
    # + net rental income - deductible interest - CCA, the dollars the
    # pre-credit tax above is computed on) -- passed so the property_disposition
    # rule can band a sold property's capital gain against the owner's actual
    # taxable income (the marginal rate the gain lands in), reusing
    # estate.tax_on_capital_gain_at_death's ``other_income`` argument exactly
    # as the terminal-estate path does (DP#9 -- one spelling of the gain-banding
    # math, not a second). Canada has no joint filing, so a jointly-owned
    # property's gain is split per owner and each share bands against that
    # owner's own taxable income. 0.0 for a no-income member (the golden
    # household's pre-retirement years price the gain at the lowest bracket).
    primary_taxable_income: float = 0.0
    spouse_taxable_income: float = 0.0


@dataclass
class YearWorkingState:
    """This year's mutable working state (DP#26: explicit, not ``self``).

    Populated from the opening ``SimState`` by ``from_state()``; rules read
    the ``opening_*`` fields (the Jan-1 values, e.g. the RRIF-minimum rule
    needs the January 1 RRSP balance, not the post-contribution/post-growth
    one) and read/write the ``new_*`` fields as the year's fold progresses.
    """
    year: int = 0

    # ── Opening (Jan-1) balances/rooms, read once from jurisdiction_state['canada'] ──
    opening_rrsp_balance: float = 0.0
    opening_rrsp_room: float = 0.0
    opening_spousal_rrsp_balance: float = 0.0
    opening_spouse_rrsp_balance: float = 0.0
    opening_spouse_rrsp_room: float = 0.0
    opening_tfsa_primary_balance: float = 0.0
    opening_tfsa_primary_room: float = 0.0
    opening_tfsa_spouse_balance: float = 0.0
    opening_tfsa_spouse_room: float = 0.0
    opening_resp_balances: list = field(default_factory=list)
    opening_resp_contributions: list = field(default_factory=list)
    opening_resp_cesg: list = field(default_factory=list)
    opening_resp_qesi: list = field(default_factory=list)
    # Issue #968: the per-property rental UCC at the START of this year (the
    # closing UCC threaded from the prior year -- the UCC immediately before a
    # mid-year disposition). The prologue computes the year's CLOSING UCC in
    # ``_rental_income_for`` and writes it to ``jurisdiction_state`` AFTER
    # ``simulate_year_pure`` returns, so during rule execution the state still
    # holds the OPENING ledger -- exactly the figure a sold rental's CCA
    # recapture (ITA s.13(1)) is priced on. {} for a household with no CCA
    # election (the golden path -- a strict no-op, DP#32).
    opening_rental_ucc: dict = field(default_factory=dict)
    opening_readvance_heloc_balance: float = 0.0
    opening_sm_investment_balance: float = 0.0
    opening_sm_investment_cost_basis: float = 0.0
    opening_readvance_total_interest_paid: float = 0.0
    opening_readvance_total_tax_saved: float = 0.0
    opening_heloc_tracing: dict = field(default_factory=dict)
    opening_spousal_contribution_years: list = field(default_factory=list)
    opening_rrsp_ledger: list = field(default_factory=list)
    opening_deduct_later_staggered_total: float = 0.0
    opening_deduct_later_first_claim_income: float = 0.0
    opening_deduct_later_total_deducted: float = 0.0
    opening_heloc_rrsp_paydown: float = 0.0
    opening_fhsa_balance: float = 0.0
    opening_fhsa_room: float = 0.0
    opening_fhsa_lifetime_used: float = 0.0
    opening_fhsa_lifetime_limit: float = 0.0
    opening_qc_carry_forward: float = 0.0
    opening_lira_balance: float = 0.0
    opening_lira_birth_year: int = 0
    opening_lira_jurisdiction: str = 'federal'
    opening_lira_reference_rate: float = 0.06
    # Issue #708: elected early-conversion calendar year (0 = no election;
    # the age-71 mandatory backstop then applies).
    opening_lira_conversion_year: int = 0
    opening_lif_balance: float = 0.0
    opening_lif_birth_year: int = 0
    opening_lif_jurisdiction: str = 'federal'
    opening_lif_reference_rate: float = 0.06
    opening_non_reg_balance: float = 0.0
    opening_non_reg_acb: float = 0.0
    opening_mortgage_balance: float = 0.0
    opening_heloc_balance: float = 0.0
    # Issue #679: liquid emergency reserve (universal, SimState top-level --
    # not jurisdiction_state, a cash reserve is not a tax construct).
    opening_emergency_reserve: float = 0.0
    # Issue #936: opening (Jan-1) balance parked in a taken deposit product.
    # 0.0 for a household with no taken product (DP#32). Read by
    # apply_deposit_product_growth, which grows it at its rate_schedule.
    opening_deposit_product_balance: float = 0.0
    # Issue #689: the revolving credit facility's DRAWN balance (universal,
    # SimState top-level, same reasoning as opening_heloc_balance -- this is
    # a distinct facility from the mortgage-paired HELOC, never merged with
    # it; see SimulationConfig.credit_facility_limit's docstring).
    opening_credit_facility_balance: float = 0.0
    # Issue #763: each consumer loan's opening (Jan-1) balance, parallel to
    # ctx.config.consumer_loans by index. Read by apply_consumer_loans to
    # amortize the year's payment/principal/interest and produce the
    # new_consumer_loan_balances the fold carries forward.
    opening_consumer_loan_balances: list = field(default_factory=list)
    # Issue #967: each financed property's mid-horizon mortgage opening
    # (Jan-1) balance, parallel to ctx.config.properties by index (a property
    # with no financing carries 0.0). Read by apply_second_property_mortgage
    # to service the year's principal/interest from the precomputed schedule
    # and produce the new_second_property_mortgage_balances the fold carries
    # forward. The balance originates at the purchase year (0.0 before then).
    opening_second_property_mortgage_balances: list = field(default_factory=list)
    # Issue #759: each installment plan's opening (Jan-1) remaining-payment
    # balance, parallel to ctx.config.installments by index. Read by
    # apply_installments to apply the date-scheduled payment and produce the
    # new_installment_balances the fold carries forward. NOT a callable debt
    # (excluded from total_debt); the balance is the remaining scheduled
    # payments forward, a reporting figure.
    opening_installment_balances: list = field(default_factory=list)
    # Epic #795 bite 3 (DP#26): opening (Jan-1) tuition-tax-credit carry-
    # forwards -- the per-member / per-child unused credit remainder threaded
    # through jurisdiction_state['canada'] (#784/#785). Read by the registered
    # `tuition_credit` rule (the fold's prologue used to compute the whole
    # credit inline; it is now a rule that writes the new_* fields below).
    # 0.0 / [] for a household that declares no tuition (the golden fixture).
    opening_primary_tuition_carryforward: float = 0.0
    opening_spouse_tuition_carryforward: float = 0.0
    opening_child_tuition_carryforwards: list = field(default_factory=list)

    # ── This year's allocations (extracted from ctx.allocations) ──
    p_rrsp: float = 0.0
    s_rrsp: float = 0.0
    sp_rrsp: float = 0.0
    p_tfsa: float = 0.0
    sp_tfsa: float = 0.0
    non_reg_alloc: float = 0.0
    resp_alloc: float = 0.0

    # ── contributions rule ──
    p_rrsp_actual: float = 0.0
    s_rrsp_actual: float = 0.0
    sp_rrsp_actual: float = 0.0
    p_tfsa_actual: float = 0.0
    sp_tfsa_actual: float = 0.0
    new_rrsp_bal: float = 0.0
    new_spousal_rrsp_bal: float = 0.0
    new_spouse_rrsp_bal: float = 0.0
    new_tfsa_p_bal: float = 0.0
    new_tfsa_sp_bal: float = 0.0
    new_nonreg_bal: float = 0.0
    new_nonreg_acb: float = 0.0
    new_rrsp_room: float = 0.0
    new_spousal_rrsp_room: float = 0.0
    new_spouse_rrsp_room: float = 0.0
    new_tfsa_p_room: float = 0.0
    new_tfsa_sp_room: float = 0.0

    # ── rrsp_ledger rule ──
    new_ledger: object = None

    # ── rrsp_deduction rule ──
    rrsp_deduction_savings: float = 0.0
    spouse_deduction_savings: float = 0.0
    deduction_claims: list = field(default_factory=list)
    deduct_later_staggered_total: float = 0.0
    deduct_later_first_claim_income: float = 0.0
    deduct_later_total_deducted: float = 0.0
    deduction_advantage_vs_now: float = 0.0

    # ── heloc_tracing rule ──
    new_tracing: dict = None

    # ── non_reg_growth rule ──
    non_reg_growth_rate: float = 0.0

    # ── lira_lif rule ──
    lif_withdrawal: float = 0.0
    # Issue #1002: the LIF statutory MAXIMUM withdrawal for the year (computed
    # on the opening/converted LIF balance, the same fund the forced-minimum
    # path builds in apply_lira_lif). The discretionary drawdown (apply_retirement_drawdown)
    # caps the discretionary LIF draw at ``max(0, lif_maximum_withdrawal -
    # lif_withdrawal)`` so the FORCED minimum + DISCRETIONARY draw never exceed
    # the annual statutory ceiling -- the forced path already caps the minimum
    # at this max; this field lets the discretionary path cap the remainder.
    # 0.0 when there is no LIF activity this year (no LIF, or the conversion-year
    # branch that takes no forced minimum) -- a hard zero, not a fallback (DP#32).
    lif_maximum_withdrawal: float = 0.0
    new_lira_balance: float = 0.0
    new_lif_balance: float = 0.0
    new_lif_birth_year: int = 0
    new_lif_jurisdiction: str = 'federal'
    new_lif_reference_rate: float = 0.06

    # ── resp rule ──
    new_resp_balances: list = field(default_factory=list)
    new_resp_contributions: list = field(default_factory=list)
    new_resp_cesg: list = field(default_factory=list)
    new_resp_qesi: list = field(default_factory=list)
    resp_eap_paid: float = 0.0
    resp_pse_paid: float = 0.0
    resp_aip_tax: float = 0.0

    # ── mortgage rule ──
    mort: dict = field(default_factory=dict)
    principal_paid: float = 0.0
    new_mortgage_balance: float = 0.0

    # ── sm_readvance / sm_interest / sm_investment_growth rules ──
    new_sm_heloc: float = 0.0
    new_sm_investment: float = 0.0
    new_sm_cost_basis: float = 0.0
    # Issue #1017: the liquidate-to-target SM unwind (sm_unwind rule). The
    # proceeds/tax/heloc_repaid/net are surfaced on YearResult; the rule also
    # reduces new_sm_investment / new_sm_cost_basis / new_sm_heloc directly.
    sm_unwind_proceeds: float = 0.0
    sm_unwind_tax: float = 0.0
    sm_unwind_heloc_repaid: float = 0.0
    sm_unwind_net_delivered: float = 0.0
    sm_unwind_realized_gain: float = 0.0
    readvance_interest: float = 0.0
    readvance_tax_savings: float = 0.0
    deductible_proportion: float = 0.0
    qc_deductible: float = 0.0
    new_qc_carry_forward: float = 0.0
    # Issue #1033: the FEDERAL s.20(1)(c) investment-interest deduction
    # (``total_deductible`` -- the UNCAPPED sum across every purpose-traced
    # borrowing: the SM readvance line + the mortgage advance + the drawn
    # revolving margin, #850). ``apply_retirement_drawdown`` subtracts this
    # from ``drawdown_other_taxable_income`` so the deduction reduces the
    # OAS-clawback base and the draw's progressive base, instead of the
    # pre-#1033 flat ``* primary_marginal_rate`` side-credit that never
    # touched taxable income. The Quebec-capped slice is ``qc_deductible``
    # (the provincial side-credit amount #1035 will refine); it is NOT routed
    # through the federal clawback base -- the QC cap is a provincial limit
    # (TA s.336.0.1) and the OAS recovery tax is federal. 0.0 for a household
    # that borrowed nothing (the golden path) -> the drawdown base is
    # unchanged (the routing is gated and floored, DP#32).
    sm_interest_deduction: float = 0.0
    # Issue #1083: the STATUTORY tax saving from routing the part of the
    # s.20(1)(c) deduction the drawdown base cannot absorb (the remainder after
    # ``apply_retirement_drawdown`` floors the cpp+pension base at 0) against
    # the PRIMARY's prologue-taxed slice (``ctx.primary_taxable_income`` --
    # rental operating + private-loan interest income, which survive
    # retirement). Valued at bracket-fill via ``tax_calculator.deduction_value``
    # -- the same ``tax_on_income`` / year-brackets path that taxed the slice
    # (DP#9), not a flat rate. Booked as REAL CASH by ``apply_solvency`` (the
    # tuition_credit precedent: a tax reduction on income already inside
    # ``ctx.after_tax_income`` is added back to `available`). 0.0 unless the
    # primary is retired AND the deduction exceeds the drawdown base; 0.0 for
    # the golden household (no borrowing) -> byte-exact.
    sm_interest_nondrawdown_tax_saving: float = 0.0

    # ── borrowing_purpose / sm_interest rules (issue #850) ──
    # The purpose tracing of the mortgage ADVANCE and the DRAWN revolving
    # margin -- the two legs of #849's trade-off. Set once at year 0, carried
    # forward unchanged after that.
    opening_advance_tracing: dict = field(default_factory=dict)
    opening_margin_tracing: dict = field(default_factory=dict)
    new_advance_tracing: dict = field(default_factory=dict)
    new_margin_tracing: dict = field(default_factory=dict)
    # The year-0 BORROWED lump sum, and the non-registered (= income-producing,
    # hence s.20(1)(c)-qualifying) portion of it. The latter is the lump's own
    # share -- NOT this year's total non-reg allocation (``non_reg_alloc``),
    # which also carries salary-funded savings that were never borrowed and so
    # must not be traced to a borrowing.
    lump_sum: float = 0.0
    lump_non_reg: float = 0.0
    advance_deductible_balance: float = 0.0
    advance_deductible_interest: float = 0.0
    margin_deductible_balance: float = 0.0
    margin_deductible_interest: float = 0.0
    traced_borrowing_tax_savings: float = 0.0
    # issue #681: what the charge actually allowed this year, and what it
    # refused. ``sm_readvanced`` is the amount ACTUALLY readvanced (<=
    # principal_paid); ``readvance_blocked`` is the principal the household
    # paid down but could NOT re-borrow because the charge was full -- a
    # first-class, reported number, not a silent truncation (DP#32). Both
    # surface on YearResult.
    sm_readvanced: float = 0.0
    readvance_room: float = 0.0
    readvance_blocked: float = 0.0

    # ── margin_heloc_interest / heloc_interest_servicing rules ──
    margin_heloc_interest: float = 0.0
    new_heloc_balance: float = 0.0
    # issue #681: a revolving facility cannot capitalize interest past the
    # charge. What the charge had room for is capitalized; the rest is
    # serviced in cash out of non-reg/SM investment, and anything the
    # household cannot fund at all is reported, not absorbed (DP#32).
    margin_heloc_interest_capitalized: float = 0.0
    margin_heloc_interest_serviced: float = 0.0
    heloc_interest_unfunded: float = 0.0
    # Issue #1069: the slice of the serviced interest the pots ACTUALLY
    # delivered (net of the gross-up tax), accumulated independently by each
    # disposition leg. Together with ``heloc_interest_unfunded`` (the
    # remainder) it closes the conservation identity the trajectory invariant
    # checks every year: charged = capitalized + funded + unfunded.
    heloc_servicing_funded: float = 0.0
    # Issue #1034: forced dispositions of the non-reg and SM pots to service
    # HELOC interest realize a capital gain (taxed) and reduce the cost basis
    # PROPORTIONALLY to the units sold -- mirroring sm_unwind (which reuses
    # price_sm_unwind). ``heloc_servicing_realized_gain`` is the year's total
    # pre-inclusion gain from both legs; ``heloc_servicing_tax`` is the tax
    # paid (funded by grossing the sale up); ``heloc_servicing_taxable`` is
    # the included slice that stacks on the year's taxable income for the
    # year-end AMT comparison's REGULAR-tax side (apply_amt reads it; it is
    # NOT surfaced on YearResult -- it is an internal AMT accumulator, not a
    # reporting figure). 0.0 in every year no pot is sold to service interest
    # (incl. the golden household, which has no SM sleeve and never draws its
    # HELOC) -- inert, byte-identical (DP#32).
    heloc_servicing_realized_gain: float = 0.0
    heloc_servicing_tax: float = 0.0
    heloc_servicing_taxable: float = 0.0

    # ── rrsp_refund_heloc_paydown rule ──
    rrsp_refund: float = 0.0
    heloc_paydown: float = 0.0
    new_heloc_rrsp_paydown: float = 0.0

    # ── fhsa rule ──
    fhsa_lifetime_remaining: float = 0.0
    fhsa_actual: float = 0.0
    # Issue #893: the household FHSA contribution slot 0 could NOT absorb (over
    # its own room/lifetime) -- spilled into the further owners' FHSAs at the
    # per-adult writeback (rebuild_adult_fhsa). 0 for a one-owner household.
    fhsa_overflow: float = 0.0
    new_fhsa_bal: float = 0.0
    new_fhsa_room: float = 0.0
    new_fhsa_lifetime_used: float = 0.0

    # ── retirement_income rule (epic #795 bite 1, DP#26) ──
    # The retirement transition (CPP/OAS/pension onset + income stop +
    # drawdown sizing) was computed inline in the fold's prologue (both
    # simulate_year and _run_monthly spelled it). It is now a registered
    # rule that writes these fields; retirement_drawdown / rrif_minimum /
    # solvency read them off `ws` instead of off `ctx`. Seeded from the
    # simulate_year_pure kwargs (defaults 0.0/False/None) BEFORE run_rules so
    # direct unit-test callers that pass e.g. drawdown_net_target=205_784
    # to exercise a single rule in isolation still work -- the rule
    # OVERWRITES them when it fires (retirement inputs present), which is
    # the only path the live fold takes.
    cpp_income: float = 0.0
    oas_income: float = 0.0
    pension_income: float = 0.0
    # Issue #1020 (S04 Step 1): GIS paid this retirement year, written by the
    # ``retirement_income`` rule (reusing ``gis_benefit``, DP#9) from the
    # prior year's GIS-countable income. Seeded 0.0 so direct unit-test callers
    # that don't pass ``prior_gis_countable_income`` see byte-identical
    # behaviour (GIS stays 0 -- the no-op every pre-retirement year and every
    # GIS-ineligible household relies on, DP#32).
    gis_income: float = 0.0
    drawdown_order: Optional[List[str]] = None
    rrif_min_rate_primary: float = 0.0
    rrif_min_rate_spouse: float = 0.0
    drawdown_net_target: float = 0.0
    retiree_marginal_rate: float = 0.0
    drawdown_bracket_target: Optional[float] = None
    # Issue #1033 round-2 NEW-1: True when ``drawdown_bracket_target`` came
    # from an EXPLICIT ``retirement.bracket_fill_target`` election (DP#13),
    # False when it was auto-derived via ``bracket_ceiling(bracket_fill_base)``.
    # The s.20(1)(c) routing in ``apply_retirement_drawdown`` must NOT overwrite
    # an explicit ceiling (a fixed dollar amount the household declared) with a
    # re-derived one -- that silently discards the election, the defect class
    # this repo exists to prevent (DP#32). It only re-derives when auto-derived.
    drawdown_bracket_target_explicit: bool = False
    drawdown_other_taxable_income: float = 0.0
    # Issue #618: OAS-inclusive taxable base (CPP + pension + full OAS) the
    # 'rrsp_bracket_fill' headroom fills against. None => plan_drawdown_net
    # falls back to drawdown_other_taxable_income (the pre-#618 OAS-excluded
    # ceiling), so it only ever tightens the bracket-fill cap.
    drawdown_bracket_fill_base: Optional[float] = None
    # Issue #363 PR 2: gross OAS + recovery-tax threshold handed to the drawdown
    # so it can fold the 15% OAS clawback the taxable draw triggers into the
    # draw (combined household figures — the single-spouse approximation). 0.0 /
    # None disable the fold (zero-OAS household, or no year-versioned threshold).
    drawdown_oas_gross: float = 0.0
    drawdown_oas_threshold: Optional[float] = None
    # Issue #363 PR 4: per-spouse drawdown pricing. When BOTH members are
    # retired the discretionary draw is split across the two spouses' SEPARATE
    # bracket sets (Canada has no joint filing): each spouse's own RRSP/RRIF
    # slice re-brackets against — and claws back OAS from — that spouse's own
    # income. ``drawdown_two_member_split`` gates it (False => the single
    # 'household' schedule above, i.e. the pre-PR-4 path used in every
    # one-retiree year and by direct constructors). The *_primary / *_spouse
    # fields carry each spouse's own other-taxable-income, gross OAS,
    # bracket-fill ceiling and OAS-inclusive headroom base; the net TARGET stays
    # the single pooled ``drawdown_net_target`` (money conservation is unchanged
    # — only the tax pricing is per-spouse). retiree_marginal_rate_* are the
    # deprecated per-spouse flat fallbacks (consulted only when no brackets are
    # supplied — never in the live fold).
    drawdown_two_member_split: bool = False
    drawdown_other_taxable_income_primary: float = 0.0
    drawdown_other_taxable_income_spouse: float = 0.0
    drawdown_oas_gross_primary: float = 0.0
    drawdown_oas_gross_spouse: float = 0.0
    drawdown_bracket_target_primary: Optional[float] = None
    drawdown_bracket_target_spouse: Optional[float] = None
    drawdown_bracket_fill_base_primary: Optional[float] = None
    drawdown_bracket_fill_base_spouse: Optional[float] = None
    retiree_marginal_rate_primary: float = 0.0
    retiree_marginal_rate_spouse: float = 0.0
    any_retired: bool = False
    retirement_spending_target: float = 0.0
    # Issue #1009: opt-in "die-with-(near)-zero" drawdown mode. When True the
    # discretionary drawdown liquidates EVERY drawable financial account the
    # configured ``drawdown_order`` did not already exhaust (a residual sweep
    # appended after the configured tokens), so ``first_shortfall_year`` means
    # "financial savings genuinely exhausted" rather than "the usual accounts
    # emptied while a LIF/FHSA sits and compounds". The principal residence is
    # NOT a drawable financial account and stays out of the sweep; the spouse's
    # slot-1 LIRA/LIF/FHSA balances are not yet read into this two-slot
    # WorkingState (the #901 follow-up), so this bite liquidates slot-0 + both
    # spouses' TFSA/RRSP/non-reg. Absent (the default) => the configured order
    # runs unchanged, byte-identical (DP#32). The LIF statutory maximum is
    # respected on the new path (see apply_retirement_drawdown), so #1002 (LIF
    # max on the discretionary draw) is neither duplicated nor re-broken.
    liquidate_to_target: bool = False

    # ── retirement_drawdown / rrif_minimum rules ──
    drawdown_total: float = 0.0
    drawdown_taxable: float = 0.0
    drawdown_net_delivered: float = 0.0
    # Issue #754: the realized capital gain (proceeds - ACB, 100% inclusion)
    # crystallized by the year's non-reg drawdown disposition. Written by
    # apply_retirement_drawdown from plan.realized_capital_gain; folded (with the
    # forced-liquidation gain below) into YearResult.realized_capital_gains so
    # the year-end AMT assessment (#710) has a real realized-gain base.
    drawdown_realized_capital_gain: float = 0.0
    # Issue #707: NET spending gap left unfunded because every drawable
    # account was exhausted (target - delivered, only when target > 0 and
    # post-drawdown drawable balance ~0). Set in apply_retirement_drawdown;
    # threaded onto YearResult.drawdown_shortfall.
    drawdown_shortfall: float = 0.0
    spend_draw_primary_rrsp: float = 0.0
    spend_draw_spouse_rrsp: float = 0.0
    forced_rrif_total: float = 0.0
    # Issue #1001: the forced RRIF minimum's AFTER-TAX proceeds priced in
    # apply_retirement_income (pure, for sizing) and the net spending shortfall
    # BEFORE the RRIF is netted in. apply_rrif_minimum splits the after-tax into
    # a spending-funded slice (rrif_after_tax_to_spending, up to the pre-RRIF
    # shortfall — NOT reinvested) and the excess (reinvested into non-reg), and
    # apply_solvency counts the spending slice in `available` so the cash-flow
    # identity balances (available = drawdown_net_delivered + cpp+oas+pension +
    # rrif_to_spending = retirement_spending_target, money conserved).
    forced_rrif_after_tax: float = 0.0
    drawdown_net_target_pre_rrif: float = 0.0
    rrif_after_tax_to_spending: float = 0.0
    # Issue #825: per-spouse taxable draw the discretionary drawdown recognized
    # this year (set in apply_retirement_drawdown), so the forced RRIF minimum
    # re-brackets its own forced slice on top of it — 0.0 when no discretionary
    # draw fired, which correctly stacks the forced slice on CPP/pension alone.
    drawdown_taxable_primary: float = 0.0
    drawdown_taxable_spouse: float = 0.0

    # ── emergency_reserve_growth rule (issue #688) ──
    new_emergency_reserve: float = 0.0
    emergency_reserve_target: float = 0.0
    emergency_reserve_months_covered: float = 0.0

    # ── deposit_product_growth rule (issue #936) ──
    # The end-of-year deposit-product balance, grown from
    # opening_deposit_product_balance at its rate_schedule net of
    # interest tax. 0.0 for a household with no taken product (DP#32).
    new_deposit_product_balance: float = 0.0
    deposit_product_rate: float = 0.0

    # ── solvency rule (issue #679) ──
    solvency_after_tax_income: float = 0.0
    solvency_living_costs: float = 0.0
    solvency_debt_service: float = 0.0
    solvency_contributions: float = 0.0
    # Issue #761: the spending figure actually charged in the identity's
    # `required` term (living_costs, or the non-discretionary portion under
    # a shock when a split was declared) and the discretionary dollars
    # compressed to zero that year. Reported on YearResult so the identity
    # is transparent about which figure it charged (DP#32).
    solvency_spending_outflow: float = 0.0
    solvency_discretionary_compressed: float = 0.0
    # Issue #760: this year's dated living-cost segment outflow CHARGED in the
    # identity's `required` term (on top of living_costs), net of any
    # discretionary segment compressed under an income shock.
    expense_segment_outflow: float = 0.0
    solvency_shortfall: float = 0.0
    solvency_covered: float = 0.0
    solvency_tax_paid: float = 0.0
    solvency_realized_loss: float = 0.0
    # Issue #754: the positive realized capital gain (100% inclusion) crystallized
    # by a FORCED non-reg liquidation in the solvency waterfall this year --
    # mirror of solvency_realized_loss (the negative part, #679). Folded into
    # YearResult.realized_capital_gains alongside the drawdown-side gain.
    solvency_realized_gain: float = 0.0
    solvency_liquidations: list = field(default_factory=list)
    solvency_credit_facility_unrepresentable: bool = False
    solvency_ruined: bool = False
    # Issue #689: the revolving credit facility's end-of-year DRAWN balance
    # -- carried forward (plus any interest accrued, plus any new draw the
    # waterfall made this year) regardless of whether a shortfall occurred.
    new_credit_facility_balance: float = 0.0
    # Issue #763: closed-end consumer loans -- the year's total payment
    # (folded into apply_solvency's debt-service term + the #758 reserve
    # sizing), the interest portion (not deductible -- consumption debt),
    # and the end-of-year per-loan balances carried forward.
    consumer_loan_payment: float = 0.0
    consumer_loan_interest: float = 0.0
    new_consumer_loan_balances: list = field(default_factory=list)
    # Issue #967: mid-horizon mortgages originated by properties'
    # `purchase.financing` -- the year's total payment (folded into
    # apply_solvency's debt-service term, the same NON-COMPRESSIBLE channel as
    # the principal mortgage + consumer-loan + installment payments), the
    # interest portion (DEDUCTIBLE when the property is a rental, surfaced so
    # the rental fold can claim it under s.20(1)(c)), the principal originated
    # this year (an INFLOW to the solvency identity in the origination year,
    # mirroring borrowed_investment -- money conserved: the mortgage funds the
    # purchase, only the down payment leaves the portfolio), and the
    # end-of-year per-property balances carried forward. All zero for a
    # household with no financed property (the golden path) -> byte-identical
    # (DP#32).
    second_property_mortgage_payment: float = 0.0
    second_property_mortgage_interest: float = 0.0
    second_property_mortgage_originated: float = 0.0
    new_second_property_mortgage_balances: list = field(default_factory=list)
    # Issue #759: fixed-term installment obligations -- the year's total
    # payment (folded into apply_solvency's debt-service term + the #758
    # reserve/runway sizing, the same NON-COMPRESSIBLE channel as the
    # mortgage + consumer-loan payments) and the end-of-year per-plan
    # remaining-payment balances carried forward. No interest field: the
    # plan is 0% by definition (a non-zero rate is refused at the contract
    # boundary, input_contract.py).
    installment_payment: float = 0.0
    new_installment_balances: list = field(default_factory=list)

    # ── amt rule (issue #710) ──
    # The Alternative Minimum Tax surcharge ASSESSED this year: max(0, minimum
    # amount - regular federal tax after credits), computed at year-end over the
    # year's realized income (ITA s.127.5-127.6). 0.0 in every year the fold
    # realizes no capital gain -- which, in this engine, is every year AMT
    # cannot bite (see apply_amt's docstring and #754). The taxable income the
    # assessment ran against is surfaced alongside it for transparency (DP#32).
    amt_surcharge: float = 0.0
    amt_taxable_income: float = 0.0
    # Issue #747: the Quebec impôt minimum de remplacement surcharge (a SEPARATE
    # tax from the federal AMT surcharge above), and the minimum-tax credit
    # bookkeeping for both jurisdictions. ``*_credit_recovered`` is the dollar
    # credit applied this year to reduce tax (ITA s.120.2); ``*_credit_closing``
    # is the surviving credit list to carry into next year. All 0.0/empty in
    # every year the fold assesses no minimum tax and carries no balance (the
    # golden household, all 46 years -- DP#32).
    qc_imr_surcharge: float = 0.0
    amt_credit_recovered: float = 0.0
    qc_imr_credit_recovered: float = 0.0
    amt_credit_closing: tuple = ()
    qc_imr_credit_closing: tuple = ()
    # Issue #1082: the NET minimum-tax charge actually assessed this year
    # (new surcharges minus recovered credits, floored at 0 -- a net refund is
    # not a charge) and the slice of it the household could NOT fund from the
    # non-registered pot it is charged against. Before #1082 the unfunded
    # remainder was silently discarded -- $380k of assessed minimum tax
    # vanished in the issue's reported case (fabricated round figures,
    # DP#4/DP#15) -- understating tax and overstating
    # leveraged net worth. Reported, never absorbed (DP#32), mirroring the
    # #681 heloc_interest_unfunded treatment. 0.0 in every year no minimum tax
    # is assessed or the pot fully funds it (the golden household, all 46
    # years -- DP#32).
    amt_net_charge: float = 0.0
    amt_unfunded: float = 0.0

    # ── tuition_credit rule (epic #795 bite 3, DP#26) ──
    # The federal (+ QC provincial) tuition tax credit -- own-credit application
    # with carry-forward (#784), inter-member / child transfers (#785) -- used
    # to be computed inline in the fold's prologue (both simulate_year and
    # _run_monthly spelled it). It is now a registered rule that writes these
    # fields; apply_solvency reads the applied amounts (the credit reduces tax,
    # so it raises the after-tax income the solvency identity counts as
    # `available`), and the epilogue / build_year_result surface the new
    # carry-forwards. Seeded from the opening_* fields above; the rule
    # OVERWRITES them when it fires (tuition declared), which is the only path
    # the live fold takes. All 0.0 / [] for a household that declares no
    # tuition (the golden path) -- a strict no-op, so the golden invariant is
    # unchanged by construction.
    # The ACTUAL tax reduction each member's credit + received transfers
    # produced this year (tax_before - final tax, after all non-refundable
    # flooring). apply_solvency adds this to `available` so the identity sees
    # the POST-credit after-tax income the pre-refactor prologue passed.
    tuition_credit_applied_primary: float = 0.0
    tuition_credit_applied_spouse: float = 0.0
    # The END-of-year unused-credit remainder carried to the next year
    # (per-member / per-child; #784/#785). Written to jurisdiction_state by the
    # epilogue and surfaced on YearResult by build_year_result.
    new_primary_tuition_carryforward: float = 0.0
    new_spouse_tuition_carryforward: float = 0.0
    new_child_tuition_carryforwards: list = field(default_factory=list)

    # ── property_disposition rule (issue #956 bite B, DP#26) ──
    # A declared mid-horizon SALE of a property settles in the sale year: the
    # net proceeds (gross value less the secured mortgage, selling costs, and
    # the disposition tax) are invested into the non-registered account, the
    # realized capital gain is surfaced for reporting, and the household
    # receives the after-tax proceeds as a solvency inflow (so funding the
    # sale-year non-reg contribution is not misread as a shortfall -- the same
    # inflow==outflow discipline free_cash_invested / borrowed_investment use).
    # All 0.0 for a household that declares no sale (the golden path) -- a
    # strict no-op, so the golden invariant is unchanged by construction.
    # `sale_proceeds_invested` is the TOTAL net proceeds invested this year
    # (the sum of every property sold this year's P_net); the prologue's
    # sale-year allocation path (simulation.py) reads the per-property proceeds
    # off ctx.config and adds them to alloc.non_reg (mirroring free_cash), then
    # threads this inflow so apply_solvency counts it in `available`.
    sale_proceeds_invested: float = 0.0
    # `sale_disposition_tax` is the TOTAL capital-gains tax (net of the PRE
    # apportionment) the sales this year crystallized -- already netted out of
    # P_net, so the household receives the after-tax proceeds. Surfaced on
    # YearResult for transparency (DP#32); NOT added to the year's ordinary
    # income-tax base (that would double-tax -- the tax is computed once, here).
    sale_disposition_tax: float = 0.0
    # `sale_realized_gain` is the TOTAL pre-inclusion capital gain (proceeds -
    # ACB, 100%) the sales this year crystallized -- folded into
    # YearResult.realized_capital_gains alongside the drawdown / solvency gains
    # so the year-end AMT base (#710) sees a real realized base.
    sale_realized_gain: float = 0.0

    # ── principal_disposition rule (issue #956 bite E, DP#26) ──
    # A declared mid-horizon SALE of the PRINCIPAL residence settles in its
    # sale year: the home + its mortgage + any HELOC/SM secured against it
    # leave the balance sheet, and the net proceeds (gross value less the
    # discharged debt, selling costs, and the PRE-apportioned disposition
    # tax) are invested into non-reg POST-GROWTH. The principal's value is
    # NOT in total_assets (it flows via house_value/LTV/charge math, not via
    # property_equities), so its conservation identity is on NET_ASSETS
    # (Δnet_assets = V - selling_costs - tax), not total_assets -- distinct
    # from Bite B's non-principal identity (the non-principal's equity IS in
    # total_assets). All 0.0 for a household that declares no principal sale
    # (the golden path) -- a strict no-op, so the golden invariant is
    # unchanged by construction.
    # `principal_sale_proceeds_invested` is the net proceeds invested into
    # non-reg this year (P_net); `principal_sale_disposition_tax` is the
    # PRE-apportioned capital-gains tax (already netted out of P_net);
    # `principal_sale_realized_gain` is the pre-inclusion gain surfaced for
    # the AMT base; `principal_sale_discharged_debt` is the mortgage + HELOC
    # + SM-HELOC debt retired at the sale (transparency + the conservation
    # identity's debt leg). Surfaced on YearResult; NOT added to the year's
    # ordinary income-tax base (computed once, here).
    principal_sale_proceeds_invested: float = 0.0
    principal_sale_disposition_tax: float = 0.0
    principal_sale_realized_gain: float = 0.0
    principal_sale_discharged_debt: float = 0.0

    @classmethod
    def from_state(cls, state, allocations: Dict[str, float], year: int) -> 'YearWorkingState':
        """Build the opening working state from ``SimState`` + this year's
        allocations (DP#26: explicit arguments, nothing read off ``self``)."""
        # Imported lazily (DP#25: no module-level countries.canada import,
        # and this keeps simulation_rules.py free of a hard dependency on
        # simulation_state.py's private helpers at import time).
        from simulation_state import (
            _default_heloc_tracing, adult_rrsp_slot, adult_tfsa_slot,
            adult_fhsa_slot, adult_lira_slot, adult_lif_slot,
        )

        canada = state.jurisdiction_state.get('canada')
        if not isinstance(canada, dict):
            canada = {}

        ws = cls(year=year)
        # Issue #700/#643: read the two-slot compute's opening RRSP scalars from
        # the per-adult store (slot 0 = primary, slot 1 = spouse). Spousal RRSP
        # room is not stored (it shares the contributor's room), so it stays 0.
        p_own, p_room, _ = adult_rrsp_slot(canada, 0)
        s_own, s_room, s_spousal = adult_rrsp_slot(canada, 1)
        ws.opening_rrsp_balance = p_own
        ws.opening_rrsp_room = p_room
        ws.opening_spousal_rrsp_balance = s_spousal
        ws.opening_spouse_rrsp_balance = s_own
        ws.opening_spouse_rrsp_room = s_room
        # Issue #700/#643: read the opening TFSA scalars from the per-adult
        # store (slot 0 = primary, slot 1 = spouse).
        tp_bal, tp_room = adult_tfsa_slot(canada, 0)
        ts_bal, ts_room = adult_tfsa_slot(canada, 1)
        ws.opening_tfsa_primary_balance = tp_bal
        ws.opening_tfsa_primary_room = tp_room
        ws.opening_tfsa_spouse_balance = ts_bal
        ws.opening_tfsa_spouse_room = ts_room
        ws.opening_resp_balances = canada.get('resp_balances', [])
        ws.opening_resp_contributions = canada.get('resp_contributions', [])
        ws.opening_resp_cesg = canada.get('resp_cesg', [])
        ws.opening_resp_qesi = canada.get('resp_qesi', [])
        # Issue #968: the opening rental UCC ledger (the UCC immediately before a
        # mid-year disposition). See the field's docstring above.
        ws.opening_rental_ucc = canada.get('rental_ucc', {})
        ws.opening_readvance_heloc_balance = canada.get('readvance_heloc_balance', 0)
        ws.opening_sm_investment_balance = canada.get('sm_investment_balance', 0)
        ws.opening_sm_investment_cost_basis = canada.get('sm_investment_cost_basis', 0)
        ws.opening_readvance_total_interest_paid = canada.get('readvance_total_interest_paid', 0)
        ws.opening_readvance_total_tax_saved = canada.get('readvance_total_tax_saved', 0)
        ws.opening_heloc_tracing = canada.get('heloc_tracing', _default_heloc_tracing())
        # Issue #850: the advance's and the drawn margin's own purpose tracing,
        # distinct from the SM readvance line's above (three balances, three
        # traces). An absent key means a state built before #850 existed, i.e.
        # a household that borrowed nothing -- an all-zero trace, which yields
        # a 0.0 deductible proportion (DP#32: a hard zero, not a guess).
        ws.opening_advance_tracing = canada.get('advance_tracing', _default_heloc_tracing())
        ws.opening_margin_tracing = canada.get('margin_tracing', _default_heloc_tracing())
        ws.opening_spousal_contribution_years = canada.get('spousal_contribution_years', [])
        ws.opening_rrsp_ledger = canada.get('rrsp_ledger', [])
        ws.opening_deduct_later_staggered_total = canada.get('deduct_later_staggered_total', 0)
        ws.opening_deduct_later_first_claim_income = canada.get('deduct_later_first_claim_income', 0)
        ws.opening_deduct_later_total_deducted = canada.get('deduct_later_total_deducted', 0)
        ws.opening_heloc_rrsp_paydown = canada.get('heloc_rrsp_paydown', 0)
        # Issue #700/#643/#704: the single-slot compute reads the primary's
        # FHSA/LIRA/LIF (slot 0) from the per-adult store. A second adult's
        # FHSA (slot 1) is grown by the writeback's growth pass, not here.
        fhsa0 = adult_fhsa_slot(canada, 0)
        ws.opening_fhsa_balance = fhsa0['balance']
        ws.opening_fhsa_room = fhsa0['room']
        ws.opening_fhsa_lifetime_used = fhsa0['lifetime_used']
        ws.opening_fhsa_lifetime_limit = fhsa0['lifetime_limit']
        ws.opening_qc_carry_forward = canada.get('qc_carry_forward', 0)
        lira0 = adult_lira_slot(canada, 0)
        ws.opening_lira_balance = lira0['balance']
        ws.opening_lira_birth_year = lira0['birth_year']
        ws.opening_lira_jurisdiction = lira0['jurisdiction']
        ws.opening_lira_reference_rate = lira0['reference_rate']
        ws.opening_lira_conversion_year = lira0['conversion_year']
        lif0 = adult_lif_slot(canada, 0)
        ws.opening_lif_balance = lif0['balance']
        ws.opening_lif_birth_year = lif0['birth_year']
        ws.opening_lif_jurisdiction = lif0['jurisdiction']
        ws.opening_lif_reference_rate = lif0['reference_rate']
        ws.opening_non_reg_balance = state.non_reg_balance
        ws.opening_non_reg_acb = state.non_reg_acb
        ws.opening_mortgage_balance = state.mortgage_balance
        ws.opening_heloc_balance = state.heloc_balance
        ws.opening_emergency_reserve = state.emergency_reserve_balance
        # issue #936: carry the deposit-product balance into the fold (grown by
        # apply_deposit_product_growth). 0.0 for a household with no taken product.
        ws.opening_deposit_product_balance = state.deposit_product_balance
        ws.opening_credit_facility_balance = state.credit_facility_balance
        # issue #763: carry each consumer loan's opening balance into the
        # fold (parallel to ctx.config.consumer_loans by index).
        ws.opening_consumer_loan_balances = list(state.consumer_loan_balances)
        # Issue #967: carry each financed property's mid-horizon mortgage
        # opening balance into the fold (parallel to ctx.config.properties by
        # index). 0.0 before the purchase year (the mortgage has not yet
        # originated); the servicing rule originates it from the schedule.
        ws.opening_second_property_mortgage_balances = list(
            state.second_property_mortgage_balances)
        # issue #759: carry each installment plan's opening remaining-payment
        # balance into the fold (parallel to ctx.config.installments by index).
        ws.opening_installment_balances = list(state.installment_balances)
        # Epic #795 bite 3: carry the opening tuition-credit carry-forwards
        # into the fold (read by the registered `tuition_credit` rule). 0.0 / []
        # for a household that declares no tuition (the golden fixture).
        ws.opening_primary_tuition_carryforward = canada.get(
            'primary_tuition_carryforward', 0.0)
        ws.opening_spouse_tuition_carryforward = canada.get(
            'spouse_tuition_carryforward', 0.0)
        ws.opening_child_tuition_carryforwards = list(
            canada.get('child_tuition_carryforwards', []))

        ws.p_rrsp = allocations.get('primary_rrsp', 0)
        ws.s_rrsp = allocations.get('spousal_rrsp', 0)
        ws.sp_rrsp = allocations.get('spouse_rrsp', 0)
        ws.p_tfsa = allocations.get('primary_tfsa', 0)
        ws.sp_tfsa = allocations.get('spouse_tfsa', 0)
        ws.non_reg_alloc = allocations.get('non_reg', 0)
        ws.resp_alloc = allocations.get('resp', 0)
        # Issue #850: how much of the YEAR-0 LUMP SUM `fill_room` put into the
        # non-registered account -- the only income-producing use, hence the
        # only s.20(1)(c)-qualifying one. Carried as allocation metadata (the
        # `_`-prefixed convention `_annual_savings`/`_primary_income` already
        # use) because only the two callers that SIZE the lump allocation
        # (simulation.simulate_year and FamilySimulation._run_monthly) can know
        # it; `non_reg_alloc` above is the year's TOTAL and includes
        # salary-funded savings that were never borrowed. Absent => 0.0: a year
        # with no lump sum traces no borrowing (DP#32). ``_lump_sum`` travels
        # beside it from the SAME caller rather than being read off a context
        # field, so the two facts the tracing needs -- how much was borrowed,
        # and how much of it reached an income-producing use -- can never
        # describe two different lump sums.
        ws.lump_sum = allocations.get('_lump_sum', 0.0)
        ws.lump_non_reg = allocations.get('_lump_non_reg', 0.0)

        return ws


# =============================================================================
# The registry: an explicit, ordered, enumerable rule space
# =============================================================================

RuleFn = Callable[[YearWorkingState, RuleContext], bool]

RULES: Dict[str, RuleFn] = {}

# Ordering is declared as data, not left to emerge from dict/insertion order
# (issue #584: "order matters and must be explicit"). Each rule's docstring
# states what it depends on from an earlier rule in this sequence:
#   contributions -> ledger -> deduction -> HELOC tracing
#     -> registered growth -> non-reg growth -> LIRA/LIF -> RESP
#     -> registered growth -> non-reg growth -> emergency-reserve growth
#     -> LIRA/LIF -> RESP
#     -> mortgage -> margin HELOC interest -> SM readvance -> SM interest
#     -> SM investment growth -> HELOC interest servicing
#     -> RRSP-refund paydown -> FHSA
#     -> contribution room -> retirement drawdown -> RRIF minimum -> solvency
# i.e. income/contributions are clamped and booked first, deductions and
# tax-adjacent bookkeeping next, then every account grows, then the
# decumulation rules (retirement drawdown, RRIF minimum) run, because
# they price against POST-growth balances (and, for RRIF minimum, the
# OPENING Jan-1 balance captured before any of this year's activity).
#
# Issue #681 moved 'margin_heloc_interest' from after 'sm_investment_growth'
# to before 'sm_readvance'. It is a pure function of ``opening_heloc_balance``
# and the HELOC rate (it reads nothing any rule between those two positions
# writes), so the move changes no number on its own -- but 'sm_readvance' now
# needs ``new_heloc_balance`` to size the room left under the shared charge:
# the drawn revolving balance is the SM-readvanced line PLUS the personal-draw
# margin, and both consume the same charge. Using the POST-capitalization,
# PRE-paydown margin balance (i.e. this rule's output, before
# 'rrsp_refund_heloc_paydown' reduces it) makes the bound conservative in the
# right direction: the year's end-of-year drawn margin can only be <= the
# figure the readvance was sized against, so the charge invariant holds at
# year end, not merely at the instant of the draw.
#
# Issue #688: 'emergency_reserve_growth' sits with the other growth rules but
# is deliberately NOT one of them in substance -- the reserve compounds at its
# own declared instrument rate (cash/short-term), never the portfolio's. A
# reserve modelled as compounding at the equity return is not a reserve.
#
# Issue #679: ``solvency`` runs LAST of all -- it is the only rule that checks
# whether everything every earlier rule booked was actually affordable out of
# this year's cash flow, and it may itself further reduce the very balances
# those rules just produced to fund a shortfall.
RULE_ORDER: tuple = (
    # epic #795 bite 1 (DP#26): the retirement transition (CPP/OAS/pension
    # onset, employment-income stop, drawdown-net-target sizing) used to be
    # computed inline in the fold's prologue (two spellings: simulate_year
    # and _run_monthly). It is now a registered rule that writes its outputs
    # to YearWorkingState; retirement_drawdown / rrif_minimum / solvency
    # read them off `ws`. Runs FIRST: it depends only on member data +
    # pre-retirement income + year-brackets (not on any account balance),
    # and every consumer runs later in the order.
    'retirement_income',
    'contributions',
    'rrsp_ledger',
    'rrsp_deduction',
    'heloc_tracing',
    # issue #850: trace the year-0 lump sum's two borrowings (the mortgage
    # advance, the drawn revolving margin) to purpose. Sits beside
    # 'heloc_tracing' -- same job, ITA s.20(1)(c) purpose, for the two OTHER
    # balances -- and must precede 'sm_interest', the rule that deducts all
    # three. Depends on no earlier rule (it reads ctx.lump_sum, ctx.config and
    # the opening mortgage balance), so its position is free above 'mortgage';
    # it is placed here to sit with the tracing it parallels.
    'borrowing_purpose',
    'registered_growth',
    'non_reg_growth',
    'emergency_reserve_growth',
    # issue #936: grow a taken deposit-product balance at its own rate_schedule
    # (like emergency_reserve_growth, a carved-out balance at its OWN rate, not
    # the portfolio's). Sits with the growth rules; a strict no-op when no
    # product is taken (DP#32).
    'deposit_product_growth',
    'lira_lif',
    'resp',
    'mortgage',
    # issue #763: amortize closed-end consumer loans (car_loan/student_loan/
    # personal_loan) BEFORE solvency reads their payment as debt service.
    'consumer_loans',
    # issue #759: apply fixed-term installment obligations' date-scheduled
    # payment BEFORE solvency reads it as debt service -- same position
    # relative to 'solvency' as 'consumer_loans' (both publish a payment
    # apply_solvency folds into its debt-service term + reserve sizing).
    'installments',
    # Issue #967: service mid-horizon mortgages originated by properties'
    # `purchase.financing` BEFORE solvency reads their payment as debt
    # service -- same position relative to 'solvency' as 'consumer_loans' /
    # 'installments' (all three publish a payment apply_solvency folds into
    # its debt-service term + reserve sizing). Originates the balance from
    # the precomputed schedule in the purchase year and amortizes it to
    # payoff; a strict no-op for a household with no financed property (the
    # golden path) (DP#32).
    'second_property_mortgage',
    'margin_heloc_interest',
    'sm_readvance',
    'sm_investment_growth',
    'heloc_interest_servicing',
    # Issue #1036 D4/N2: 'sm_interest' runs AFTER 'heloc_interest_servicing' so
    # the Leg 3 (drawn-margin) deduction can EXCLUDE the unfunded interest --
    # the portion that was neither paid (serviced from pots) nor capitalized
    # (added to the balance). A s.20(1)(c) deduction requires interest paid or
    # payable; the unfunded is neither (it evaporates from the balance sheet),
    # so it must not be deducted. sm_interest's outputs (readvance_interest,
    # tax_savings, qc_*, carry-forward) are read only at the year-end snapshot,
    # so moving it later is safe; its inputs (new_sm_investment, new_nonreg_bal)
    # are now post-growth/post-servicing, which changes the QC investment-income
    # cap base for drawn-margin households (a correction -- the cap is on the
    # investment income the grown pot earns). The golden household hits the
    # `if not sm_active and traced_deductible <= 0` early return (no draw,
    # personal mortgage), so it is byte-identical (DP#32).
    'sm_interest',
    # Issue #956 bite E (principal-residence disposition): a declared
    # mid-horizon SALE of the PRINCIPAL residence settles in its sale year.
    # The principal flows via house_value/mortgage_balance/heloc_balance/
    # sm_heloc (LTV/charge math), NOT via config.properties -- so Bite B's
    # `property_disposition` rule cannot sell it (the principal is excluded
    # from _map_owned_properties). This rule runs AFTER `mortgage` (to read
    # the amortized `new_mortgage_balance`), AFTER `margin_heloc_interest` /
    # `sm_readvance` / `sm_investment_growth` / `heloc_interest_servicing`
    # (so the HELOC/SM interest, readvance, and growth rules have set their
    # `new_*` values, and the SM investment -- a real asset that STAYS --
    # has grown), and BEFORE `rrsp_refund_heloc_paydown` / `fhsa` /
    # `retirement_drawdown` / `property_disposition` / `solvency`. In the sale
    # year AND every subsequent year, it force-zeros the discharged secured
    # debt (`new_mortgage_balance`, `new_heloc_balance`, `new_sm_heloc`) so
    # the home + its mortgage + any HELOC/SM secured against it leave the
    # balance sheet; in the sale year it ALSO computes the PRE-apportioned
    # disposition gain (reusing estate.tax_on_capital_gain_at_death +
    # pre_designation.taxable_gain_fraction, DP#9), bands it against the
    # owners' taxable income, and injects the net proceeds `P_net = V -
    # discharged_debt - selling_costs - T` into non-reg POST-GROWTH (non_reg_
    # growth already ran, so P_net does not compound in the sale year -- the
    # conservation identity on net_assets holds exactly). The SM investment
    # (`new_sm_investment`) is NOT zeroed -- it is a real asset that stays
    # (the loan is discharged, the asset remains, ACB unchanged); only its
    # financing (the SM HELOC) is retired. Absence-safe (DP#32): a household
    # with no principal sale (the golden fixture) -> strict no-op -> the
    # golden invariant is unchanged by construction.
    'principal_disposition',
    'rrsp_refund_heloc_paydown',
    'fhsa',
    'contribution_room',
    'retirement_drawdown',
    'rrif_minimum',
    # Issue #1017: under liquidate_to_target, unwind the Smith-Manoeuvre sleeve
    # to fund the spending shortfall the ordinary financial drawdown + forced
    # RRIF minimum could not cover (sell the SM portfolio, repay the HELOC,
    # pay the capital-gains tax, deliver the net to spending). Runs AFTER
    # rrif_minimum (so the shortfall is the true post-RRIF gap) and BEFORE
    # property_disposition / solvency (so the net it delivers counts in the
    # cash-flow identity and the waterfall does not force-liquidate for a gap
    # the unwind already filled). Gated on liquidate_to_target + an SM sleeve ->
    # a strict no-op for the golden household (byte-identical, DP#32).
    'sm_unwind',
    # issue #956 bite B (sale-core): a declared mid-horizon property SALE
    # settles in its sale year -- the net proceeds (gross value less the
    # secured mortgage, selling costs, and disposition tax) are invested into
    # non-reg POST-GROWTH (this rule runs after non_reg_growth, so P_net does
    # not compound in the sale year -- the conservation identity
    # Δtotal_assets = -(selling_costs + T) holds exactly), the realized gain
    # is surfaced for the year-end AMT base, and the disposition tax is
    # surfaced for transparency. Runs AFTER every account-growth / drawdown
    # rule (the gain bands against the owner's taxable income, already on the
    # year's return; the proceeds inject post-growth) and BEFORE 'solvency'
    # (so the invested non-reg is on the balance sheet the waterfall reads).
    # Absence-safe (DP#32): a household with no sale (the golden fixture) ->
    # strict no-op -> the golden invariant is unchanged by construction.
    'property_disposition',
    # epic #795 bite 3 (DP#26): the federal (+ QC provincial) tuition tax
    # credit -- own-credit application with carry-forward (#784) + inter-
    # member / child transfers (#785) -- used to be computed inline in the
    # fold's prologue (two spellings: simulate_year and _run_monthly). It is
    # now a registered rule that writes the per-member tax reduction to
    # YearWorkingState; apply_solvency (next) reads it so the cash-flow
    # identity counts the POST-credit after-tax income as `available`, and
    # the epilogue / build_year_result surface the new carry-forwards. Runs
    # immediately before 'solvency' (its sole consumer) and after every
    # account-growth / drawdown rule: it depends only on the prologue-passed
    # pre-credit tax + the opening carry-forwards + member/child data, not on
    # any account balance a rule writes, so its position is free above
    # 'solvency'; it is placed here to sit beside the rule that reads it.
    'tuition_credit',
    'solvency',
    # issue #710/#747: the Alternative Minimum Tax is a YEAR-END assessment over
    # all of the year's realized income, so it runs DEAD LAST -- after solvency
    # has run every forced liquidation (each of which can realize a capital gain
    # the AMT base reads). It compares the minimum amount (net of 50% of credits,
    # #747) against regular tax, books the surcharge (max(regular, AMT) -
    # regular), recovers any carried minimum-tax credit (ITA s.120.2), and books
    # Quebec's separate IMR for QC residents (#747). See apply_amt.
    'amt',
)


def rule(name: str) -> Callable[[RuleFn], RuleFn]:
    """Decorator: register a named simulation rule under ``name`` (mirrors
    ``trajectory_invariants.invariant``). Registering the same name to two
    different functions is a build error, not a silent overwrite."""
    def _decorator(fn: RuleFn) -> RuleFn:
        if name in RULES and RULES[name] is not fn:
            raise ValueError(f"rule {name!r} already registered")
        RULES[name] = fn
        return fn
    return _decorator


def all_rule_names() -> List[str]:
    """Every registered rule name, sorted (for parametrized discovery)."""
    return sorted(RULES)


def run_rules(ws: YearWorkingState, ctx: RuleContext) -> Dict[str, bool]:
    """Run every rule in ``RULE_ORDER`` against ``ws``/``ctx``, in order.

    DP#32: a name present in ``RULE_ORDER`` but missing from ``RULES`` (or
    vice versa) is exactly the "unregistered rule" failure mode this whole
    module exists to make loud -- raise immediately rather than silently
    skip it. Returns ``{rule_name: fired}`` for the coverage sweep.
    """
    missing = [name for name in RULE_ORDER if name not in RULES]
    if missing:
        raise RuntimeError(
            f"RULE_ORDER declares {missing} but no @rule(...) registered "
            f"under that name -- a rule that is expected but not registered "
            f"is a silent no-op (DP#32), not an acceptable gap."
        )
    fired: Dict[str, bool] = {}
    for name in RULE_ORDER:
        fired[name] = bool(RULES[name](ws, ctx))
    return fired


from contextlib import contextmanager


@contextmanager
def trace_firing():
    """Test-only instrumentation: yields a ``{rule_name: bool}`` dict that
    accumulates (via OR) whether each registered rule fired across every
    ``simulate_year_pure`` call made while the context is active.

    Does not change ``simulate_year_pure``'s signature or behavior -- it
    temporarily wraps the registered rule functions themselves and restores
    the originals on exit. Used by the coverage sweep (issue #584 mission
    #2: "assert every registered rule fires in some year of a
    representative household, and report the ones that never did").
    """
    fired_ever = {name: False for name in RULE_ORDER}
    originals = dict(RULES)

    def _wrap(rule_name, fn):
        def _wrapped(ws, ctx):
            fired = bool(fn(ws, ctx))
            fired_ever[rule_name] = fired_ever[rule_name] or fired
            return fired
        return _wrapped

    for rule_name, fn in originals.items():
        RULES[rule_name] = _wrap(rule_name, fn)
    try:
        yield fired_ever
    finally:
        RULES.clear()
        RULES.update(originals)


# =============================================================================
# Rule implementations
# =============================================================================

@rule('retirement_income')
def apply_retirement_income(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Epic #795 bite 1 (DP#10/#26): the per-year retirement transition.

    Once a member reaches ``retirement_age`` (a date-computed eligibility
    gate, DP#1/#28), stop their employment income and turn on CPP/OAS/pension
    + size the NET drawdown target. This used to be computed inline in the
    fold's prologue (``simulation._retirement_transition_for``), spelled
    twice -- once in ``simulate_year`` and once in ``_run_monthly`` (DP#9).
    It is now a single registered rule; the prologue passes only the
    pre-retirement GROWN incomes + the resolved retirement status + the
    year-brackets + the tax indexation rate (``RuleContext``), and this rule
    writes the government-income / drawdown-param outputs to
    ``YearWorkingState``, which ``retirement_drawdown`` / ``rrif_minimum`` /
    ``solvency`` read.

    DP#25: ``countries.canada`` is imported lazily inside the body (this
    module keeps no jurisdiction import at top level); ``tax_calculator``
    primitives (``marginal_rate`` / ``tax_on_income`` / ``bracket_ceiling``)
    are jurisdiction-agnostic and imported lazily too. The body is the
    verbatim computation ``simulation._retirement_transition_for`` performed
    -- moved, not changed -- so every number is preserved byte-for-byte.

    Returns True when any member is retired this year (the transition had an
    observable effect), False for a pure pre-retirement year (no-op, all
    outputs stay at their seeded 0.0/False/None defaults).
    """
    from countries.canada.retirement_transition import (
        member_retirement_income,
        DEFAULT_NET_REPLACEMENT_RATE, retirement_spending_target,
    )
    from countries.canada.retirement import (
        get_oas_annual_max, get_oas_clawback_threshold, _get_rrif_rates,
    )
    from tax_calculator import marginal_rate, tax_on_income, bracket_ceiling

    config = ctx.config
    members = config.family_members
    primary = next((m for m in members if m.get('role') == 'primary'), {})
    spouse = next((m for m in members if m.get('role') == 'spouse'), {})

    sim_year = ctx.calendar_year
    start_year = ctx.calendar_year - ctx.year

    p_retired = ctx.primary_retired
    s_retired = ctx.spouse_retired
    any_retired = p_retired or s_retired
    if not any_retired:
        return False

    primary_income = ctx.primary_income_pre
    spouse_income = ctx.spouse_income_pre

    # Year-versioned OAS figures, with explicit input.retirement overrides.
    # Issue #592: `0` is a value ("this household gets no OAS"), not an
    # absent key -- `or` treats it as falsy and silently reinstates the
    # code table. Use `is None`: absent/None -> year-versioned table;
    # any configured number (including 0) -> that number.
    #
    # A non-zero override is a scalar, and the fold runs one sim_year per
    # call -- a bare override would freeze OAS at one number across the
    # whole horizon, defeating the year-versioning CPP_OAS_BY_YEAR exists
    # to provide. Per DP#20, treat the override as the amount *as of
    # start_year* and index it forward at the same inflation assumption the
    # tax-data year-projection uses, so an override participates in
    # indexation instead of silently disabling it.
    ret = config.retirement_data or {}
    years_ahead = sim_year - start_year
    indexation_factor = (1 + ctx.tax_indexation_rate) ** years_ahead

    oas_max_override = ret.get('oas_annual_max')
    if oas_max_override is None:
        oas_max = get_oas_annual_max(sim_year)
    else:
        oas_max = oas_max_override * indexation_factor

    oas_threshold_override = ret.get('oas_clawback_threshold')
    if oas_threshold_override is None:
        oas_threshold = get_oas_clawback_threshold(sim_year)
    else:
        oas_threshold = oas_threshold_override * indexation_factor

    drawdown_order = ret.get('drawdown_order')
    if drawdown_order is None:
        drawdown_order = ['tfsa', 'non_reg', 'rrsp']
    # Issue #301: size drawdown to a NET spending target, not gross income.
    net_replacement_rate = ret.get('net_replacement_rate', DEFAULT_NET_REPLACEMENT_RATE)
    spending_target = ret.get('spending_target')
    # Issue #1009: opt-in die-with-(near)-zero drawdown mode. A bool leaf --
    # absent and an explicit False both mean "off" (a boolean has no
    # "zero is a value" concern, DP#32), so a plain truthiness test is
    # correct here. Read once and threaded to apply_retirement_drawdown via
    # ws.liquidate_to_target; absent => the configured order runs unchanged.
    liquidate_to_target = bool(ret.get('liquidate_to_target', False))

    # Stop employment income for retired members (no salary, no growth).
    new_primary = 0.0 if p_retired else primary_income
    new_spouse = 0.0 if s_retired else spouse_income

    # Issue #363 PR 4: keep the PER-MEMBER government income, not just the
    # household sums. A retired member's cpp/oas/pension seeds THAT spouse's own
    # bracket stack in the per-spouse drawdown split below; a non-retired member
    # contributes nothing (member_retirement_income returns zeros).
    from countries.canada.retirement_transition import MemberRetirementIncome
    ri_p = (member_retirement_income(primary, sim_year, oas_max, oas_threshold)
            if (primary and p_retired) else MemberRetirementIncome())
    ri_s = (member_retirement_income(spouse, sim_year, oas_max, oas_threshold)
            if (spouse and s_retired) else MemberRetirementIncome())
    # Issues #711/#712: CPP/QPP sharing + pension income splitting, as ELECTIONS
    # the optimizer sweeps (DP#22/#30), applied here as PURE income transfers
    # between the two retired spouses BEFORE the per-spouse bases below are
    # built. Both conserve the household total, so covered_net / net_shortfall /
    # the drawdown net target are byte-unchanged (money conservation) — only the
    # per-spouse SPLIT moves, so each spouse's own progressive-drawdown stack and
    # OAS-clawback base shift, and the household's retirement tax with them. The
    # per-spouse marginal rates the drawdown prices against (this rule's
    # draw_rate_primary/spouse, #363 PR 4) are the same rates that decide the
    # split direction — no parallel tax model is built. Both are no-ops unless
    # BOTH spouses are retired (there is no spouse to share/split with otherwise)
    # AND the election is present; absent the election they default to 0 and the
    # transfer vanishes, preserving current behaviour exactly (DP#13/#32).
    both_retired = bool(primary and spouse and p_retired and s_retired)
    cpp_share = ret.get('cpp_share', 0.0)
    pension_split_pct = ret.get('pension_split_pct', 0.0)
    if both_retired and cpp_share:
        from countries.canada.cpp_sharing import share_cpp_amounts
        ri_p.cpp, ri_s.cpp = share_cpp_amounts(ri_p.cpp, ri_s.cpp, cpp_share)
    if both_retired and pension_split_pct:
        from countries.canada.pension_split_optimizer import split_pension_amounts
        # Split from the higher-bracket spouse to the lower one; the per-spouse
        # taxable base (own CPP + pension) ranks them, and (brackets progressive)
        # a higher base is a higher marginal rate — the rate the drawdown prices.
        primary_is_higher = (ri_p.cpp + ri_p.pension) >= (ri_s.cpp + ri_s.pension)
        ri_p.pension, ri_s.pension = split_pension_amounts(
            ri_p.pension, ri_s.pension, pension_split_pct, primary_is_higher)

    cpp = ri_p.cpp + ri_s.cpp
    oas = ri_p.oas + ri_s.oas
    pension = ri_p.pension + ri_s.pension

    # Issue #1020 (S04 Step 1): GIS (Guaranteed Income Supplement) -- the one
    # government program whose computation lived in a helper
    # (``countries.canada.retirement.gis_benefit``) but was never WIRED into
    # the fold, so the optimizer was blind to it. Now computed here, reusing
    # the existing helper (DP#9 -- the GIS math is spelled once, in the
    # year-versioned pure function; this rule calls it).
    #
    # CRA's income test is PRIOR-YEAR: GIS for year N is determined by the
    # year N-1 countable income. That is what makes the pre-65 preservation
    # maneuver effective (low income in the year before 65 -> full GIS at
    # 65+) and it is what breaks the circularity that would otherwise arise
    # (GIS depends on the drawdown amount, and the drawdown shortfall --
    # which this rule sizes -- depends on GIS). The prior year's countable
    # base is the prior year's retirement income EXCLUDING OAS (OAS is
    # excluded from the GIS test by statute -- see ``gis_benefit``'s
    # ``net_income`` contract), threaded in from the prior ``YearResult`` by
    # the prologue. None (no prior year -- the first retirement year's prior
    # year is a WORKING year, or a direct unit-test caller) => GIS stays at
    # its seeded 0.0 (DP#32: absence is a loud no-op, never a silent zero-
    # coercion; a unit test that does not pass ``prior_gis_countable_income``
    # is byte-identical to before).
    gis = 0.0
    prior_countable = ctx.prior_gis_countable_income
    if prior_countable is not None:
        from countries.canada.retirement import gis_benefit
        # is_coupled: a spouse member exists (the household files as a couple
        # for GIS purposes). gis_benefit applies the coupled max + the $4k
        # employment-income exemption + the 50% reduction on the rest.
        is_coupled = bool(spouse)
        gis = gis_benefit(prior_countable, is_coupled=is_coupled,
                         year=sim_year)['gis_amount']
    ws.gis_income = gis

    year_brackets = ctx.year_brackets if ctx.year_brackets is not None else []

    # Drawdown need (issues #301/#363): size to the NET spending shortfall
    # after other retirement income; simulation_state.plan_drawdown_net then
    # fills that net target account-by-account, tax-exact per source (#579).
    # CPP/OAS/pension are netted off the target first.
    net_shortfall = 0.0
    draw_rate = 0.0
    # Issue #758: the EFFECTIVE retirement spending target (spending_target
    # or net_replacement_rate x pre-net), 0.0 in every pre-retirement year.
    # Passed to the #679 solvency rule so that in retirement it charges the
    # RETIREMENT spending figure (what the drawdown is sized to) -- not the
    # working-phase `living_costs` -- and does not double-count spending the
    # household is already funding via the drawdown.
    eff_retirement_spending_target = 0.0
    pre_gross = ctx.base_primary_income + ctx.base_spouse_income
    pre_net = pre_gross - (
        tax_on_income(ctx.base_primary_income, year_brackets)
        + tax_on_income(ctx.base_spouse_income, year_brackets)
    )
    target_net = retirement_spending_target(
        pre_net, net_replacement_rate, spending_target)
    eff_retirement_spending_target = target_net
    # Net income already covered by government benefits + any remaining
    # employment pay (CPP/OAS/pension are received net of further tax here;
    # remaining employment income is netted by its own marginal tax).
    # Issue #1020: GIS is non-taxable cash, so it covers spending
    # dollar-for-dollar and reduces the discretionary drawdown shortfall --
    # the whole point of the preservation maneuver (GIS replaces registered
    # drawdown that would have been taxed + clawed back GIS).
    covered_net = cpp + oas + pension + gis
    covered_net += (new_primary + new_spouse) - (
        tax_on_income(new_primary, year_brackets)
        + tax_on_income(new_spouse, year_brackets)
    )
    net_shortfall_pre_rrif = max(0.0, target_net - covered_net)
    # Issue #1001: the forced RRIF minimum's AFTER-TAX proceeds also cover
    # part of the net spending target (the mandatory withdrawal is cash the
    # household receives), so the discretionary drawdown must be sized to the
    # TRUE residual shortfall, not the full gap. The RRIF after-tax is priced
    # here (pure, no side effects — apply_rrif_minimum books the tax/clawback
    # exactly once, later) using the SAME per-spouse machinery
    # (price_forced_rrif_tax) and the per-spouse bases built below, then netted
    # into the shortfall. See the #1001 block after the per-spouse bases.
    net_shortfall = net_shortfall_pre_rrif
    # DEPRECATED flat-rate fallback (#579). Since #363 the drawdown re-brackets
    # the taxable draw progressively against the year-versioned `year_brackets`
    # (passed to plan_drawdown_net via ws.drawdown_other_taxable_income); this
    # scalar is only consulted when no bracket table is available. Kept so the
    # plan_drawdown_net signature stays additive.
    draw_rate = marginal_rate(cpp + pension + net_shortfall, year_brackets)

    # `other_taxable_income` is the taxable income the progressive draw stacks
    # on and the non-OAS part of the OAS-clawback base (#363). OAS is kept OUT
    # of it: the clawback base adds gross OAS separately (plan_drawdown_net), so
    # folding OAS in here would double-count it.
    other_taxable_income = cpp + pension

    # Issue #618: the bracket-fill drawdown ceiling. OAS IS taxable income for
    # bracket purposes — it is RECEIVED net of the 15% recovery tax, but the tax
    # brackets still see the full taxable OAS — so the RRSP-draw ceiling the
    # 'rrsp_bracket_fill' order fills against sits on top of CPP + pension + OAS.
    # Excluding OAS (the pre-#618 behaviour) let the bracket-fill order over-draw
    # by the OAS slice. This OAS-inclusive base feeds ONLY the bracket-fill
    # headroom (ws.drawdown_bracket_fill_base -> plan_drawdown_net), never the
    # progressive/clawback base above. DP#13: an explicit
    # `retirement.bracket_fill_target` wins; absent that, auto-detect from the
    # year-versioned brackets (DP#20) rather than hardcoding a dollar figure —
    # and the ceiling is the top of the bracket the OAS-INCLUSIVE income sits in.
    bracket_fill_base = other_taxable_income + oas
    bracket_fill_target = ret.get('bracket_fill_target')
    if bracket_fill_target is None:
        bracket_fill_target = bracket_ceiling(bracket_fill_base, year_brackets)

    # Issue #363 PR 4: the per-spouse pricing bases. Only meaningful when BOTH
    # spouses are retired — that is when the discretionary draw splits across
    # their two SEPARATE bracket sets (Canada has no joint filing). Each spouse's
    # own progressive base is their own CPP + pension; their bracket-fill headroom
    # sits on their own OAS-inclusive base (same #618 rule as the household one),
    # and an explicit `bracket_fill_target` override applies per spouse just as it
    # does household-wide. In a one-retiree year the split is off and these are
    # unused (the household schedule above prices the draw exactly as pre-PR-4).
    two_member_split = both_retired  # (computed above for the split elections)
    other_taxable_primary = ri_p.cpp + ri_p.pension
    other_taxable_spouse = ri_s.cpp + ri_s.pension
    bracket_fill_base_primary = other_taxable_primary + ri_p.oas
    bracket_fill_base_spouse = other_taxable_spouse + ri_s.oas
    if ret.get('bracket_fill_target') is not None:
        bracket_target_primary = bracket_target_spouse = ret['bracket_fill_target']
    else:
        bracket_target_primary = bracket_ceiling(bracket_fill_base_primary, year_brackets)
        bracket_target_spouse = bracket_ceiling(bracket_fill_base_spouse, year_brackets)
    # Deprecated per-spouse flat fallbacks (consulted only when no brackets are
    # supplied — never in the live fold, which always passes year_brackets).
    draw_rate_primary = marginal_rate(other_taxable_primary, year_brackets)
    draw_rate_spouse = marginal_rate(other_taxable_spouse, year_brackets)

    # -- RRIF minimum-withdrawal rates (mandatory decumulation) --
    # Once the RRSP is a RRIF (required by age 71), the CRA age factor forces
    # a minimum taxable withdrawal each year. We pass the age-based *rate*
    # (0 before conversion) so the pure step applies it to the Jan-1 balance.
    # DP#32/#621: an explicit `rrif_conversion_age: 0` is bad input (no RRSP
    # converts to a RRIF at age 0), but it must surface as bad data, not be
    # silently coerced to the statutory default via truthiness.
    _conv_age = ret.get('rrif_conversion_age')
    conv_age = 71 if _conv_age is None else _conv_age
    rrif_rates = _get_rrif_rates(year=sim_year)

    def _rrif_rate(m, retired):
        if not m or not retired:
            return 0.0
        by = m.get('birth_year', 0)
        if not by:
            return 0.0
        age = sim_year - by
        if age < conv_age:
            return 0.0
        return rrif_rates.get(age, 0.20)

    rrif_min_rate_primary = _rrif_rate(primary, p_retired)
    rrif_min_rate_spouse = _rrif_rate(spouse, s_retired)
    # Issue #825: the tax on the forced RRIF minimum is no longer a flat
    # placeholder rate computed here. apply_rrif_minimum now prices each spouse's
    # forced slice through the SAME progressive re-bracketing + OAS-clawback
    # machinery as the discretionary drawdown (price_forced_rrif_tax), using the
    # per-spouse bases already written below.

    ws.cpp_income = cpp
    ws.oas_income = oas
    ws.pension_income = pension
    ws.drawdown_order = drawdown_order
    ws.liquidate_to_target = liquidate_to_target
    # Issue #674: per-member flags so callers can also zero *_earned_income
    # (a retired member accrues no new RRSP room from employment they no
    # longer have) without re-deriving retirement status themselves.
    ws.primary_retired = p_retired
    ws.spouse_retired = s_retired
    ws.rrif_min_rate_primary = rrif_min_rate_primary
    ws.rrif_min_rate_spouse = rrif_min_rate_spouse
    # Issue #1001: drawdown_net_target is set AFTER the per-spouse bases below,
    # where the forced RRIF minimum's after-tax is priced and netted in.
    # Issue #758: the effective retirement spending target + the
    # retirement-phase flag, so apply_solvency can use the correct
    # spending figure in retirement instead of the working-phase
    # `living_costs` (which double-counts spending the drawdown funds).
    ws.any_retired = any_retired
    ws.retirement_spending_target = eff_retirement_spending_target
    ws.retiree_marginal_rate = draw_rate
    ws.drawdown_bracket_target = bracket_fill_target
    # Issue #1033 round-2 NEW-1: record whether the target was an EXPLICIT
    # retirement.bracket_fill_target election (DP#13) so the s.20(1)(c) routing
    # in apply_retirement_drawdown does not overwrite a declared ceiling.
    ws.drawdown_bracket_target_explicit = ret.get('bracket_fill_target') is not None
    ws.drawdown_other_taxable_income = other_taxable_income
    # Issue #618: OAS-inclusive base for the bracket-fill headroom only.
    ws.drawdown_bracket_fill_base = bracket_fill_base
    # Issue #363 PR 2: hand the gross OAS + recovery-tax threshold to the
    # drawdown rule so it folds the OAS clawback the taxable draw triggers into
    # the draw. covered_net above nets FULL OAS (the base clawback on CPP alone
    # is structurally zero — max CPP << the ~$95k threshold), so net_shortfall
    # is the full-OAS shortfall and the drawdown grosses up to REPLACE whatever
    # OAS the draw claws back. Booking the reduced OAS lives in the drawdown rule
    # (where the draw — and thus the clawback base — is known), not here (this
    # runs before the draw is sized): part (i), "feed the taxable draw into the
    # clawback base", realized at the one point the draw exists.
    ws.drawdown_oas_gross = oas
    ws.drawdown_oas_threshold = oas_threshold
    # Issue #363 PR 4: per-spouse pricing bases + the split gate.
    ws.drawdown_two_member_split = two_member_split
    ws.drawdown_other_taxable_income_primary = other_taxable_primary
    ws.drawdown_other_taxable_income_spouse = other_taxable_spouse
    ws.drawdown_oas_gross_primary = ri_p.oas
    ws.drawdown_oas_gross_spouse = ri_s.oas
    ws.drawdown_bracket_target_primary = bracket_target_primary
    ws.drawdown_bracket_target_spouse = bracket_target_spouse
    ws.drawdown_bracket_fill_base_primary = bracket_fill_base_primary
    ws.drawdown_bracket_fill_base_spouse = bracket_fill_base_spouse
    ws.retiree_marginal_rate_primary = draw_rate_primary
    ws.retiree_marginal_rate_spouse = draw_rate_spouse

    # Issue #1001: price the forced RRIF minimum's AFTER-TAX proceeds (pure —
    # no side effects on ws.oas_income / balances; apply_rrif_minimum books the
    # tax + clawback + reinvest exactly once, later) and net them into the
    # discretionary drawdown target. The mandatory RRIF minimum is cash the
    # household RECEIVES in retirement, so it covers part of the net spending
    # target before any discretionary TFSA/non-reg draw is sized — fixing the
    # defect where the household drew tax-free TFSA it did not need while the
    # already-taxed RRIF surplus was reinvested into taxable non-reg for 25
    # years. The per-spouse forced slice re-brackets on that spouse's own
    # CPP/pension base (prior_taxable_draw=0 here — the discretionary draw has
    # not run yet; it draws TFSA-first so its taxable slice is ~0, making this
    # pricing consistent with apply_rrif_minimum's post-draw pricing in the
    # common case). Both the income tax and the incremental OAS recovery tax
    # reduce the after-tax proceeds available to cover spending.
    forced_rrif_after_tax = 0.0
    if net_shortfall_pre_rrif > 0.0:
        from countries.canada.retirement_transition import price_forced_rrif_tax
        forced_primary = (ws.opening_rrsp_balance * rrif_min_rate_primary
                          if rrif_min_rate_primary > 0 else 0.0)
        forced_spouse = ((ws.opening_spouse_rrsp_balance
                          + ws.opening_spousal_rrsp_balance) * rrif_min_rate_spouse
                         if rrif_min_rate_spouse > 0 else 0.0)
        if (forced_primary > 0.0 or forced_spouse > 0.0):
            tax_p, claw_p = price_forced_rrif_tax(
                other_taxable_income=other_taxable_primary,
                oas_gross=ri_p.oas,
                prior_taxable_draw=0.0,
                forced_taxable=forced_primary,
                brackets=ctx.year_brackets,
                oas_clawback_threshold=oas_threshold,
                flat_rate=draw_rate_primary)
            tax_s, claw_s = price_forced_rrif_tax(
                other_taxable_income=other_taxable_spouse,
                oas_gross=ri_s.oas,
                prior_taxable_draw=0.0,
                forced_taxable=forced_spouse,
                brackets=ctx.year_brackets,
                oas_clawback_threshold=oas_threshold,
                flat_rate=draw_rate_spouse)
            forced_rrif_after_tax = (
                (forced_primary + forced_spouse)
                - (tax_p + tax_s))
            # (The incremental OAS recovery tax claw_p+claw_s is NOT subtracted
            # here: it is booked in apply_rrif_minimum as ws.oas_income -= claw,
            # and OAS income already flows into covered_net above and the
            # solvency `available` below, so subtracting it here would
            # double-count the clawback's hit to spending capacity.)
            if forced_rrif_after_tax > 0.0:
                net_shortfall = max(0.0, net_shortfall_pre_rrif
                                    - forced_rrif_after_tax)
    ws.drawdown_net_target = net_shortfall
    ws.forced_rrif_after_tax = forced_rrif_after_tax
    ws.drawdown_net_target_pre_rrif = net_shortfall_pre_rrif
    return True


@rule('contributions')
def apply_contributions(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Clamp this year's allocations to available room and book them.

    First rule in the fold: every later rule (deduction, HELOC tracing,
    growth) reads the post-contribution balances/rooms this rule produces.
    """
    combined_rrsp_room = max(0, ws.opening_rrsp_room)
    p_rrsp_actual = min(ws.p_rrsp, combined_rrsp_room)
    remaining_rrsp_room = combined_rrsp_room - p_rrsp_actual
    s_rrsp_actual = min(ws.s_rrsp, remaining_rrsp_room)
    remaining_rrsp_room -= s_rrsp_actual
    sp_rrsp_actual = min(ws.sp_rrsp, max(0, ws.opening_spouse_rrsp_room))
    p_tfsa_actual = min(ws.p_tfsa, max(0, ws.opening_tfsa_primary_room))
    sp_tfsa_actual = min(ws.sp_tfsa, max(0, ws.opening_tfsa_spouse_room))

    ws.p_rrsp_actual = p_rrsp_actual
    ws.s_rrsp_actual = s_rrsp_actual
    ws.sp_rrsp_actual = sp_rrsp_actual
    ws.p_tfsa_actual = p_tfsa_actual
    ws.sp_tfsa_actual = sp_tfsa_actual

    ws.new_rrsp_bal = ws.opening_rrsp_balance + p_rrsp_actual
    ws.new_spousal_rrsp_bal = ws.opening_spousal_rrsp_balance + s_rrsp_actual
    ws.new_spouse_rrsp_bal = ws.opening_spouse_rrsp_balance + sp_rrsp_actual
    ws.new_tfsa_p_bal = ws.opening_tfsa_primary_balance + p_tfsa_actual
    ws.new_tfsa_sp_bal = ws.opening_tfsa_spouse_balance + sp_tfsa_actual
    ws.new_nonreg_bal = ws.opening_non_reg_balance + ws.non_reg_alloc
    ws.new_nonreg_acb = ws.opening_non_reg_acb + ws.non_reg_alloc

    ws.new_rrsp_room = max(0, remaining_rrsp_room)
    ws.new_spousal_rrsp_room = 0  # Spousal RRSP shares primary's room
    ws.new_spouse_rrsp_room = max(0, ws.opening_spouse_rrsp_room - sp_rrsp_actual)
    ws.new_tfsa_p_room = max(0, ws.opening_tfsa_primary_room - p_tfsa_actual)
    ws.new_tfsa_sp_room = max(0, ws.opening_tfsa_spouse_room - sp_tfsa_actual)

    return (p_rrsp_actual + s_rrsp_actual + sp_rrsp_actual
            + p_tfsa_actual + sp_tfsa_actual + ws.non_reg_alloc) > 0


@rule('rrsp_ledger')
def apply_rrsp_ledger(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Track this year's RRSP contributions in the per-contribution ledger
    (DP#19: deduction timing is recorded per contribution, not assumed).
    Depends on ``contributions`` for the *_actual amounts.
    """
    from simulation_state import RRSPListLedger

    # Issue #1059: the ledger entries are flat dicts of scalars (year, amount,
    #    role, deducted, deduction_year, deduction_marginal_rate) -- no nested
    #    containers.  Downstream mutates entries in place (claim_all_deductions
    #    sets e['deducted']=True; claim_deferred_deduction adjusts entry['amount']),
    #    so each dict MUST be a fresh copy to avoid corrupting the prior year's
    #    state (DP#26).  But deepcopy is overkill: a shallow dict copy per entry
    #    ({**e}) is enough because all values are immutable scalars.
    #    Verified: grep opening_rrsp_ledger.append/extend/[..]= -> empty;
    #    only .add_contribution (appends a new dict) and .claim_* (mutates
    #    existing entries' scalar values) touch the ledger after this point.
    new_ledger = RRSPListLedger([{**e} for e in ws.opening_rrsp_ledger])
    fired = False
    if ws.p_rrsp_actual > 0:
        new_ledger.add_contribution(year=ws.year, amount=ws.p_rrsp_actual, role='primary')
        fired = True
    if ws.s_rrsp_actual > 0:
        new_ledger.add_contribution(year=ws.year, amount=ws.s_rrsp_actual, role='spousal')
        fired = True
    if ws.sp_rrsp_actual > 0:
        new_ledger.add_contribution(year=ws.year, amount=ws.sp_rrsp_actual, role='spouse')
        fired = True
    ws.new_ledger = new_ledger
    return fired


@rule('rrsp_deduction')
def apply_rrsp_deduction(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Deduct-now or deduct-later (issue #546: bracket-fill staggering).
    Depends on ``rrsp_ledger`` (mutates the same ledger object) and
    ``contributions`` for the *_actual amounts.
    """
    rrsp_deduction_savings = 0.0
    spouse_deduction_savings = 0.0
    deduction_claims = []
    deduct_later_staggered_total = ws.opening_deduct_later_staggered_total
    deduct_later_first_claim_income = ws.opening_deduct_later_first_claim_income
    deduct_later_total_deducted = ws.opening_deduct_later_total_deducted

    if not ctx.deduct_later:
        ws.new_ledger.claim_all_deductions(year=ws.year, marginal_rate=ctx.primary_marginal_rate)
        rrsp_deduction_savings = (ws.p_rrsp_actual + ws.s_rrsp_actual) * ctx.primary_marginal_rate
        spouse_deduction_savings = ws.sp_rrsp_actual * ctx.spouse_marginal_rate
    else:
        # Issue #546: stagger the primary/spousal claim toward the bracket
        # target, valuing each year's slice at its bracket-fill marginal rate.
        if ws.sp_rrsp_actual > 0:
            spouse_deduction_savings = ws.sp_rrsp_actual * ctx.spouse_marginal_rate

        brackets = default_tax_provider().get_combined_brackets(ctx.config.start_year, province=ctx.config.province)
        primary_income_this_year = ctx.allocations.get('_primary_income', 0)
        claim = ws.new_ledger.claim_deferred_deduction(
            year=ws.year,
            income=primary_income_this_year,
            brackets=brackets,
            bracket_target=ctx.config.deduct_later_bracket_target,
        )
        rrsp_deduction_savings = claim['savings']
        deduction_claims = claim['claims']
        if claim['amount'] > 0:
            deduct_later_staggered_total += claim['savings']
            if deduct_later_total_deducted == 0:
                deduct_later_first_claim_income = primary_income_this_year
            deduct_later_total_deducted += claim['amount']

    # Issue #546: staggered bracket-fill total minus the bracket-fill value
    # of deducting the same total all in the first claim year.
    if deduct_later_total_deducted > 0:
        from tax_calculator import deduction_value
        adv_brackets = default_tax_provider().get_combined_brackets(ctx.config.start_year, province=ctx.config.province)
        lump_now_total = deduction_value(
            deduct_later_first_claim_income, deduct_later_total_deducted, adv_brackets)
        deduction_advantage_vs_now = deduct_later_staggered_total - lump_now_total
    else:
        deduction_advantage_vs_now = 0.0

    ws.rrsp_deduction_savings = rrsp_deduction_savings
    ws.spouse_deduction_savings = spouse_deduction_savings
    ws.deduction_claims = deduction_claims
    ws.deduct_later_staggered_total = deduct_later_staggered_total
    ws.deduct_later_first_claim_income = deduct_later_first_claim_income
    ws.deduct_later_total_deducted = deduct_later_total_deducted
    ws.deduction_advantage_vs_now = deduction_advantage_vs_now

    return (rrsp_deduction_savings + spouse_deduction_savings) > 0


@rule('heloc_tracing')
def apply_heloc_tracing(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Record this year's contribution advances into HELOC tracing buckets
    (ITA §20(1)(c) purpose test). Depends on ``contributions``.
    """
    opening = ws.opening_heloc_tracing
    added = ws.p_rrsp_actual + ws.s_rrsp_actual + ws.p_tfsa_actual + ws.sp_tfsa_actual + ws.non_reg_alloc
    ws.new_tracing = {
        'total_advances': opening.get('total_advances', 0) + added,
        'investment_advances': opening.get('investment_advances', 0) + ws.non_reg_alloc,
        'rrsp_advances': opening.get('rrsp_advances', 0) + ws.p_rrsp_actual + ws.s_rrsp_actual,
        'tfsa_advances': opening.get('tfsa_advances', 0) + ws.p_tfsa_actual + ws.sp_tfsa_actual,
        'personal_draws': opening.get('personal_draws', 0),
    }
    return added > 0


@rule('borrowing_purpose')
def apply_borrowing_purpose(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Trace the year-0 leveraged lump sum's two borrowings -- the mortgage
    ADVANCE and the DRAWN revolving margin -- to purpose, ITA s.20(1)(c)
    (issue #850). Depends on nothing; consumed by ``sm_interest``.

    #849's question ("advance or line?") is asked ENTIRELY because of a
    deductibility asymmetry, and before #850 the engine modelled it on neither
    leg: ``config.cash_out`` only ever sized the invested lump sum, and
    ``apply_margin_heloc_interest`` never deducted. This rule is where the two
    borrowings acquire a traced purpose, so ``apply_sm_interest`` can price
    them on the same deduction rule the readvance line already uses (DP#9 --
    the arithmetic is ``simulation_state.borrowing_purpose_tracings``, beside
    the ``margin_draw_for_lump_sum`` rule whose split it reuses, not a fourth
    copy here).

    Fires at YEAR 0 ONLY. The purpose of a borrowing is fixed when the money is
    spent; every later year carries the same trace forward, and it is the
    BALANCES priced against it that move. That is exactly what makes the
    erosion visible: a FIXED deductible proportion of an AMORTIZING mortgage is
    a falling deductible balance, while the same fixed proportion of a
    non-amortizing revolving balance is not.

    Inert for a household that borrowed no lump sum (``ws.lump_sum <= 0``) --
    the golden household included: both traces stay all-zero, so the deductible
    proportion is 0.0 and no deduction arises anywhere (DP#32).
    """
    if ws.year != 0 or ws.lump_sum <= 0:
        ws.new_advance_tracing = ws.opening_advance_tracing
        ws.new_margin_tracing = ws.opening_margin_tracing
        return False

    from simulation_state import borrowing_purpose_tracings
    ws.new_advance_tracing, ws.new_margin_tracing = borrowing_purpose_tracings(
        lump_sum=ws.lump_sum,
        lump_non_reg=ws.lump_non_reg,
        margin_available=ctx.config.margin_available,
        # The post-refinance charge this run opened with: the advance's blended
        # denominator (the pre-existing, personal-purpose balance plus the
        # advance itself). Read off the OPENING balance, before this year's
        # amortization -- the proportion is fixed at the moment of borrowing.
        mortgage_balance=ws.opening_mortgage_balance,
        # Issue #1039: ADD the new draw to a declared opening drawn balance's
        # trace instead of clobbering it -- the historical position's purpose
        # is a carried-in fact, not something a year-0 decision overwrites.
        opening_margin_tracing=ws.opening_margin_tracing,
    )
    return True


def _blended_pot_rate(ctx: 'RuleContext', kind: str, pot_total: float) -> float:
    """Issue #823/#691: the growth rate for one aggregate pot
    (rrsp/tfsa/non_reg/...).

    Two per-account overrides, composed, both balance-weighted into the pot:

    - #823 ``expected_return``: if the household declared one on any account of
      ``kind``, the pot's GROSS rate is a balance-weighted blend of the override
      rate and the global ``ctx.investment_return``; otherwise the gross rate is
      the global rate (today's behaviour).
    - #691 ``mer``: a declared per-account fee is subtracted from that gross
      rate -- ``net = gross - weighted_mer_sum / pot_total`` -- so a declared fee
      reduces the compounded balance. An account carrying BOTH grows at
      (expected_return - mer): the two terms simply add here.

    Absence is a strict no-op: no override and no fee -> the global rate is
    returned unchanged (the golden household declares neither). See
    ``apply_registered_growth`` for the approximation caveat (the blend uses the
    DECLARED opening balance of the flagged accounts, not a per-account
    sub-balance tracked through the run -- a first-order approximation the issue
    itself flags as numerically small).
    """
    if pot_total <= 0:
        return ctx.investment_return
    # Gross rate: the #823 expected_return blend, or the global rate when no
    # account of this kind declared one (identity-preserving for the golden run).
    gross = ctx.investment_return
    overrides = ctx.config.account_return_overrides
    entry = overrides.get(kind) if overrides else None
    if entry:
        override_balance = entry.get('override_balance', 0.0)
        weighted_rate_sum = entry.get('weighted_rate_sum', 0.0)
        if override_balance > 0:
            gross = (weighted_rate_sum
                     + max(0.0, pot_total - override_balance) * ctx.investment_return
                     ) / pot_total
    # Issue #691: subtract the balance-weighted MER of fee-flagged accounts in
    # this pot from the gross rate. Absent (no mer_drag entry) or fee-free
    # (weighted_mer_sum == 0) -> no change, so the gross rate is returned
    # untouched (golden no-op, DP#32).
    mer_drag = ctx.config.account_mer_drag
    fee = mer_drag.get(kind) if mer_drag else None
    if fee:
        weighted_mer_sum = fee.get('weighted_mer_sum', 0.0)
        if weighted_mer_sum:
            gross -= weighted_mer_sum / pot_total
    # Issue #641: subtract the foreign-withholding-tax drag of this REGISTERED
    # pot's declared holdings (rrsp/tfsa) -- the one tax that leaks from an
    # otherwise tax-sheltered account. Absent (no registered composition, or a
    # domestic/fixed-income-only pot) -> no entry -> gross unchanged (golden
    # no-op, DP#32). non_reg never appears here (its WHT is recoverable and its
    # composition reaches growth via non_reg_after_tax_return -- no double count).
    wht_drag = ctx.registered_wht_drag
    if wht_drag:
        gross -= wht_drag.get(kind, 0.0)
    return gross


def _still_locked(ctx: 'RuleContext', kind: str) -> float:
    """Issue #823: the total balance still locked (not yet liquid) for the
    ``kind`` pot in ``ctx.calendar_year``.

    Sums the ``balance`` of every account of ``kind`` that declared
    ``locked_until`` whose owner has NOT yet reached the account's
    ``unlock_age`` this year. After the owner passes the unlock age the
    balance is liquid and contributes 0 here. Returns 0.0 when no account of
    ``kind`` declared ``locked_until`` (today's fully-liquid behaviour,
    golden). DP#1: the owner's age is ``calendar_year - owner_birth_year``.
    """
    locked = ctx.config.account_locked
    entries = locked.get(kind) if locked else None
    if not entries:
        return 0.0
    year = ctx.calendar_year
    total = 0.0
    for e in entries:
        owner_age = year - e.get('owner_birth_year', year)
        if owner_age < e.get('unlock_age', 0):
            total += e.get('balance', 0.0)
    return total


@rule('registered_growth')
def apply_registered_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Grow RRSP/TFSA balances at the gross (tax-sheltered) rate.
    Depends on ``contributions`` for the post-contribution balances.

    Issue #823: if the household declared a per-account ``expected_return``
    override on any rrsp/tfsa account, that pot grows at a BALANCE-WEIGHTED
    blend of the override rate and the global ``investment_return`` -- so a
    flagged account (e.g. Fonds FTQ at 7.3%) grows at its own rate while the
    rest of the pot uses the global rate. No override declared -> the global
    rate (today's behaviour, golden). The blend uses the DECLARED opening
    balance of the flagged accounts (from the contract) against the pot's
    current post-contribution total -- a first-order approximation the issue
    itself flags as numerically small (7.3% vs 7% on ~$34k ~$100/yr); it is
    not a second growth model, just a per-pot rate (DP#21: the return model
    stays the single source of the global rate; this is an override ON it).
    """
    rrsp_rate = _blended_pot_rate(ctx, 'rrsp',
                                  ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal)
    tfsa_rate = _blended_pot_rate(ctx, 'tfsa',
                                  ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal)
    pre = ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal + ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal
    ws.new_rrsp_bal *= (1 + rrsp_rate)
    ws.new_spousal_rrsp_bal *= (1 + rrsp_rate)
    ws.new_spouse_rrsp_bal *= (1 + rrsp_rate)
    ws.new_tfsa_p_bal *= (1 + tfsa_rate)
    ws.new_tfsa_sp_bal *= (1 + tfsa_rate)
    return pre > 0 and (ctx.investment_return != 0 or rrsp_rate != 0 or tfsa_rate != 0)


@rule('non_reg_growth')
def apply_non_reg_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """DP#27: non-reg investments grow at the income-type-specific
    after-tax rate (portfolio composition + marginal rate), not the flat
    gross rate registered accounts use. Depends on ``contributions``.
    """
    if ctx.non_reg_after_tax_return is not None:
        non_reg_growth_rate = ctx.non_reg_after_tax_return
    else:
        non_reg_growth_rate = ctx.investment_return
        logger.warning(
            "non_reg_after_tax_return not provided; falling back to flat investment_return=%.4f. "
            "For accurate non-reg projections, provide non_reg_after_tax_return "
            "from portfolio composition data (DP#27).",
            ctx.investment_return
        )
    # Issue #823: blend a per-account expected_return override on non_reg
    # accounts into the pot rate (see _blended_pot_rate). Applied on the
    # fallback (flat investment_return) path; when portfolio composition
    # supplies an after-tax rate (the DP#27 normal path), the override is not
    # blended in -- threading a per-account pre-tax override through the
    # portfolio after-tax adjustment is a future refinement. FTQ (the issue's
    # subject) lives in RRSP, not non_reg, so this is not the load-bearing
    # path for #823.
    if non_reg_growth_rate == ctx.investment_return:
        non_reg_growth_rate = _blended_pot_rate(ctx, 'non_reg', ws.new_nonreg_bal)
    ws.non_reg_growth_rate = non_reg_growth_rate
    pre = ws.new_nonreg_bal
    ws.new_nonreg_bal *= (1 + non_reg_growth_rate)
    # ACB does NOT grow with returns (it's cost basis)
    return pre > 0 and non_reg_growth_rate != 0


@rule('emergency_reserve_growth')
def apply_emergency_reserve_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Grow the emergency reserve at its OWN declared instrument rate --
    never the portfolio's (issue #688).

    **A reserve modelled as compounding at the equity return is not a
    reserve.** That is the whole point of the field: the household is
    choosing to hold money OUT of the market, and the cost of that choice
    (forgone return) is exactly what the ``emergency_reserve_months`` sweep
    exists to price against the benefit (not being forced to sell at the
    bottom). Growing the sleeve at ``ctx.investment_return`` would make the
    reserve free, and the sweep would report that holding 24 months of cash
    costs nothing -- a confident, wrong, and dangerous number.

    DP#32: ``emergency_reserve_rate`` is a REQUIRED field of the schema
    block, so a declared reserve always has a rate. A rate of exactly 0 (a
    chequing account paying nothing) is a legitimate, representable value and
    is honoured as such -- it is not treated as "unset" and quietly upgraded
    to some assumed cash yield.

    Runs with the growth rules (it is a balance that compounds) but is
    deliberately not one of them in substance. Depends on nothing any other
    rule writes; the reserve's opening balance is the Jan-1 sleeve carved out
    by ``SimState.initial`` (#688) or left by last year's ``solvency`` rule.
    """
    opening = ws.opening_emergency_reserve
    if opening <= 0:
        ws.new_emergency_reserve = opening
        return False

    rate = ctx.config.emergency_reserve_rate
    if rate is None:
        # Only reachable for a hand-built SimulationConfig that seeded a
        # reserve balance directly without declaring the policy that governs
        # it. Refuse rather than pick a rate: guessing here is precisely the
        # "plausible number from absent data" this codebase exists to reject
        # (DP#32). Every contract-sourced config has the rate, because the
        # schema requires it whenever the block is present.
        raise ValueError(
            f"SimState carries a ${opening:,.2f} emergency reserve but "
            f"SimulationConfig.emergency_reserve_rate is None -- the rate its "
            f"declared instrument earns was never supplied, so there is no "
            f"honest rate to compound it at (#688, DP#32). Declare "
            f"assumptions.emergency_reserve.rate (0 is a valid answer: a "
            f"chequing account that pays nothing)."
        )

    ws.new_emergency_reserve = opening * (1 + rate)
    return rate != 0


def _deposit_step_duration_years(step: dict) -> Optional[float]:
    """Issue #936: how many years a rate step lasts, or None if it is
    OPEN-ENDED (the final, ongoing step). A step may declare its duration in
    ``duration_years`` or ``duration_days`` (730 days = 2.0 years); a step with
    neither runs to the horizon."""
    if 'duration_years' in step:
        return step['duration_years']
    if 'duration_days' in step:
        return step['duration_days'] / 365.0
    return None


def _deposit_rate_at(schedule: list, elapsed_years: float) -> float:
    """Issue #936: the gross interest rate a deposit product pays at
    ``elapsed_years`` since funding, by walking its ordered ``rate_schedule``
    steps. A step with no duration is open-ended (the ongoing rate); once every
    termed step has elapsed, the final step's rate holds to the horizon. This
    is the generic ``rate_path``/variable shape (#936 capability #2), bound to
    one account -- one mechanism expresses a flat HISA (one open step), a
    promo teaser ([{teaser, 730d}, {base}]) and a term/GIC (a single termed
    step), which are different field values, not different concepts."""
    cumulative = 0.0
    for step in schedule:
        dur = _deposit_step_duration_years(step)
        if dur is None:
            return step['rate']
        cumulative += dur
        if elapsed_years < cumulative:
            return step['rate']
    return schedule[-1]['rate']


def _deposit_product_after_tax_rate(product: dict, elapsed_years: float,
                                    balance: float, marginal_rate: float) -> float:
    """Issue #936: the AFTER-TAX rate a taken deposit product earns THIS year
    on ``balance``, given the 0-indexed ``elapsed_years`` since funding.

    Three of the product's capabilities live here:

    * **#2 rate-step schedule.** The gross rate is whichever step of the
      product's ``rate_schedule`` contains ``elapsed_years`` (``_deposit_rate_at``
      walks the ordered steps by elapsed time). This one mechanism expresses a
      flat rate, a dated teaser->base step-down, and a fixed term alike.
    * **#3 rate_eligible_cap (OPTIONAL).** A capped-rate product pays the
      current step's rate only on the portion of the balance up to
      ``rate_eligible_cap``; any excess earns the product's ONGOING (final-step)
      rate. So the effective gross rate above the cap is the balance-weighted
      blend -- exactly how a real capped HISA ("3.00% on the first $500k, 1.50%
      above") pays. Absent cap = the whole balance earns the current step's
      rate (the trivial case for a plain HISA/GIC).
    * **#1 interest tax character.** A HISA/GIC yield is ordinary interest --
      100% taxable at the marginal rate each year as it accrues, NOT a
      deferred/50%-inclusion capital return. So the after-tax rate is the gross
      rate times ``(1 - marginal_rate)``: every dollar of yield is taxed this
      year, none deferred. (This mirrors the non-reg after-tax-return path,
      DP#27 -- interest is fully included, unlike a capital gain.)

    A non-positive balance earns nothing (returns 0.0) -- there is no cap blend
    to compute and no interest to tax.
    """
    if balance <= 0:
        return 0.0
    schedule = product['rate_schedule']
    current = _deposit_rate_at(schedule, elapsed_years)
    cap = product.get('rate_eligible_cap')
    if cap is None or balance <= cap:
        gross = current
    else:
        # The excess above the cap earns the product's ongoing (final-step)
        # rate -- the rate the schedule holds after every termed step elapses.
        ongoing = _deposit_rate_at(schedule, float('inf'))
        gross = (cap * current + (balance - cap) * ongoing) / balance

    # #936 capability #1: ordinary interest -- 100% taxable at the marginal rate.
    return gross * (1 - marginal_rate)


@rule('deposit_product_growth')
def apply_deposit_product_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #936: grow a TAKEN deposit-product balance (a HISA, a term/GIC, a
    promotional teaser -- one generic mechanism) at the interest rate its
    ``rate_schedule`` prescribes for the elapsed time since funding, on the
    portion up to any ``rate_eligible_cap``, taxing the yield as ordinary
    interest.

    Sits with the other growth rules (issue #688's emergency_reserve_growth is
    the closest analog: a carved-out balance compounding at its OWN declared
    cash rate, NOT the portfolio's) but is deliberately not one of them in
    substance -- a deposit is money the household is choosing to hold at a fixed
    interest rate instead of the market, and pricing that choice is the whole
    take-vs-leave trade the optimizer ranks (#936 capability #4).

    Reads ``ctx.config.deposit_product`` -- the SINGLE product this scenario
    took (apply_overlay wrote it; None for the "leave it" baseline and every
    no-product household). None, or a 0.0 opening balance, is a strict no-op:
    the deposit balance stays 0.0 and the golden trajectory is byte-identical
    (DP#32). The opening balance was carved out of the product's funding_source
    by SimState.initial (money-conserving, capability #5).

    The interest is taxed at the funding member's marginal rate
    (``ctx.primary_marginal_rate`` -- the non-registered deposit is the
    primary's; #936 does not split a deposit across two owners) by growing at
    the after-tax rate ``_deposit_product_after_tax_rate`` returns. The fold's
    0-indexed ``ctx.year`` is the elapsed years since funding (the product is
    funded at year 0).
    """
    opening = ws.opening_deposit_product_balance
    product = ctx.config.deposit_product
    if product is None or opening <= 0:
        ws.new_deposit_product_balance = opening
        return False

    after_tax_rate = _deposit_product_after_tax_rate(
        product, ctx.year, opening, ctx.primary_marginal_rate)
    ws.deposit_product_rate = after_tax_rate
    ws.new_deposit_product_balance = opening * (1 + after_tax_rate)
    return after_tax_rate != 0


@rule('lira_lif')
def apply_lira_lif(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """CRI/LIRA growth + conversion to LIF at the statutory age, and LIF
    mandatory min/max withdrawals once converted (issue #230/#343).
    Independent of every other rule this year (locked-in accounts are not
    touched by contributions/deductions); must run before ``resp`` only
    because that is this file's declared narrative order, not a real
    dependency -- kept immediately after growth per the original engine's
    section order (DP#26: preserving relative order keeps the refactor
    byte-identical to what it replaces).

    Issue #912: the LIRA and LIF grow at the same per-pot rate machinery the
    rrsp/tfsa pots use (``_blended_pot_rate``) so a declared foreign-equity
    composition drags their return via #641's ``registered_wht_drag`` -- LIRA/
    LIF are locked-in RETIREMENT accounts, so their foreign holdings carry the
    RRSP US-treaty exemption (leak like an RRSP's). Absent a declared lira/lif
    composition (and override/fee) the blended rate IS the flat
    ``ctx.investment_return`` (golden no-op, DP#32). The conversion year itself
    does not grow the balance (``convert_to_lif`` is a transfer, not a return),
    so no drag applies there.
    """
    from simulation_state import _get_lif_conversion_provider

    lif_withdrawal = 0.0
    new_lira_balance = ws.opening_lira_balance
    new_lif_balance = ws.opening_lif_balance
    new_lif_birth_year = ws.opening_lif_birth_year
    new_lif_jurisdiction = ws.opening_lif_jurisdiction
    new_lif_reference_rate = ws.opening_lif_reference_rate

    fired = False
    lif_provider = None
    if ((ws.opening_lira_balance > 0 and ws.opening_lira_birth_year > 0)
            or (ws.opening_lif_balance > 0 and ws.opening_lif_birth_year > 0)):
        lif_provider = _get_lif_conversion_provider()

    # Issue #1002: the LIF statutory MAXIMUM withdrawal for the year, computed
    # on the same fund the forced-minimum path builds (opening balance for an
    # existing LIF, the just-converted balance in a LIRA->LIF conversion year).
    # Stays 0.0 when there is no LIF activity this year. Stored on ws so the
    # discretionary drawdown (apply_retirement_drawdown, later in the fold) can
    # cap the discretionary LIF draw at ``max - lif_withdrawal`` -- preventing
    # the total (forced min + discretionary) from exceeding the annual ceiling
    # the forced path already enforces on its own slice.
    lif_maximum_withdrawal = 0.0

    if ws.opening_lira_balance > 0 and ws.opening_lira_birth_year > 0:
        # Issue #343: compare against the absolute calendar year, not the
        # 0-based projection index.
        # Issue #708: the conversion year is event-driven — the EARLIER of an
        # elected conversion date (lira.conversion_date) and the mandatory
        # age-71 backstop. An absent election (opening_lira_conversion_year
        # == 0) yields the backstop, byte-identical to the pre-#708 path. The
        # jurisdiction's earliest-permitted conversion age is enforced inside
        # the provider (Quebec: no minimum, sourced; federal/Ontario: rejected
        # rather than guessed).
        election_year = ws.opening_lira_conversion_year
        convert_year = lif_provider.lif_conversion_year(
            ws.opening_lira_birth_year,
            ws.opening_lira_jurisdiction,
            election_year if election_year > 0 else None,
        )
        if ctx.calendar_year >= convert_year:
            account = lif_provider.make_locked_in_account(
                balance=ws.opening_lira_balance,
                birth_year=ws.opening_lira_birth_year,
                jurisdiction=ws.opening_lira_jurisdiction,
            )
            lif_fund, depleted_account = account.convert_to_lif(
                ctx.calendar_year, reference_rate=ws.opening_lira_reference_rate)
            new_lira_balance = 0.0
            new_lif_balance = lif_fund.balance
            new_lif_birth_year = lif_fund.owner_birth_year
            new_lif_jurisdiction = lif_fund.jurisdiction
            new_lif_reference_rate = lif_fund.reference_rate
            # Issue #1002: the freshly-converted LIF is a live account this
            # year -- record its statutory maximum so a discretionary draw
            # ordered against it (drawdown_order front-loads 'lif') is capped
            # rather than over-drawing. No forced minimum is taken in the
            # conversion year (the block below is gated on opening_lif_balance
            # > 0, which is 0 here), so the whole ceiling is discretionary room.
            lif_maximum_withdrawal = lif_fund.maximum_withdrawal(ctx.calendar_year)
            fired = True
        else:
            lira_rate = _blended_pot_rate(ctx, 'lira', ws.opening_lira_balance)
            new_lira_balance = ws.opening_lira_balance * (1 + lira_rate)
            fired = True

    if ws.opening_lif_balance > 0 and ws.opening_lif_birth_year > 0 and new_lira_balance == 0:
        fund = lif_provider.make_lif_fund(
            balance=new_lif_balance if new_lif_balance > 0 else ws.opening_lif_balance,
            owner_birth_year=ws.opening_lif_birth_year,
            reference_rate=ws.opening_lif_reference_rate,
            jurisdiction=ws.opening_lif_jurisdiction,
        )
        lif_withdrawal = fund.minimum_withdrawal(ctx.calendar_year)
        max_withdrawal = fund.maximum_withdrawal(ctx.calendar_year)
        if lif_withdrawal > max_withdrawal and max_withdrawal > 0:
            lif_withdrawal = max_withdrawal
        # Issue #1002: record the statutory maximum (on the opening fund) so the
        # discretionary drawdown caps the residual at ``max - lif_withdrawal``.
        lif_maximum_withdrawal = max_withdrawal
        actual_withdrawal, updated_fund = fund.withdraw(lif_withdrawal, ctx.calendar_year)
        lif_rate = _blended_pot_rate(ctx, 'lif', updated_fund.balance)
        _, grown_fund = updated_fund.grow(lif_rate)
        new_lif_balance = grown_fund.balance
        new_lif_birth_year = grown_fund.owner_birth_year
        new_lif_jurisdiction = grown_fund.jurisdiction
        new_lif_reference_rate = grown_fund.reference_rate
        fired = True
    elif ws.opening_lif_balance > 0 and ws.opening_lira_balance > 0:
        # Defensive: LIF exists but LIRA hasn't converted yet.
        lif_rate = _blended_pot_rate(ctx, 'lif', ws.opening_lif_balance)
        new_lif_balance = ws.opening_lif_balance * (1 + lif_rate)
        fired = True

    ws.lif_withdrawal = lif_withdrawal
    ws.lif_maximum_withdrawal = lif_maximum_withdrawal
    ws.new_lira_balance = new_lira_balance
    ws.new_lif_balance = new_lif_balance
    ws.new_lif_birth_year = new_lif_birth_year
    ws.new_lif_jurisdiction = new_lif_jurisdiction
    ws.new_lif_reference_rate = new_lif_reference_rate
    return fired


@rule('resp')
def apply_resp(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """RESP: contribute, grow, then wind down as EAP/PSE across the study
    window, or collapse via AIP once the window closes with money left
    over (issue #578, DP#1/DP#28: eligibility is date-computed from
    birth_year). Depends on ``contributions`` only via ``ctx.resp_data``
    (pre-computed CESG/QESI grants), not on any other rule's ``ws`` output.
    """
    from countries.canada.resp_rules import (
        resp_study_window_for_child,
        resp_annual_withdrawal, resp_collapse_aip,
    )

    resp_balances = ws.opening_resp_balances
    resp_contributions = ws.opening_resp_contributions
    resp_cesg = ws.opening_resp_cesg
    resp_qesi = ws.opening_resp_qesi
    n_resp_children = len(resp_balances)
    new_resp_balances = []
    new_resp_contributions = []
    new_resp_cesg = []
    new_resp_qesi = []
    resp_eap_paid = 0.0
    resp_pse_paid = 0.0
    resp_aip_tax = 0.0
    fired = False

    for i in range(n_resp_children):
        bal = resp_balances[i]
        contrib_i = resp_contributions[i] if i < len(resp_contributions) else 0.0
        cesg_i = resp_cesg[i] if i < len(resp_cesg) else 0.0
        qesi_i = resp_qesi[i] if i < len(resp_qesi) else 0.0

        if ctx.resp_data and i < len(ctx.resp_data):
            ch_contrib = ctx.resp_data[i].get('contribution', 0)
            ch_cesg = ctx.resp_data[i].get('cesg', 0)
            ch_qesi = ctx.resp_data[i].get('qesi', 0)
        elif not ctx.resp_data:
            ch_contrib = ws.resp_alloc / n_resp_children
            ch_cesg = 0.0
            ch_qesi = 0.0
        else:
            ch_contrib = ch_cesg = ch_qesi = 0.0

        if ch_contrib or ch_cesg or ch_qesi:
            fired = True

        bal = (bal + ch_contrib + ch_cesg + ch_qesi) * (1 + ctx.investment_return)
        contrib_i += ch_contrib
        cesg_i += ch_cesg
        qesi_i += ch_qesi
        earnings_i = max(0.0, bal - contrib_i - cesg_i - qesi_i)

        birth_year = ctx.config.children[i].get('birth_year', 0) if i < len(ctx.config.children) else 0
        if not birth_year and i < len(ctx.config.children):
            age = ctx.config.children[i].get('age', 0)
            birth_year = (ctx.config.start_year - age) if age else 0

        if birth_year > 0:
            # Issue #714: THIS child's declared study window (people[].
            # study_periods[] -> child['study_periods']) when they gave one,
            # and only otherwise the household-wide age assumption. Before
            # this, study_periods was mapped and read by nobody, so every
            # beneficiary wound down on the GLOBAL assumptions.resp.
            # study_start_age regardless of when they actually study.
            #
            # Computed ONCE and used by all three predicates below. It used to
            # be derived here and then independently re-derived inside
            # is_resp_study_year()/has_aged_out_of_resp_study() from the same
            # inputs -- three spellings of one window, which is how a per-child
            # window could be added to the config and silently disagree with
            # the one the withdrawal maths actually used (DP#9).
            child_cfg = ctx.config.children[i] if i < len(ctx.config.children) else {}
            first_year, last_year = resp_study_window_for_child(
                child_cfg, birth_year,
                ctx.config.resp_study_start_age, ctx.config.resp_study_duration_years)
            in_study_year = first_year <= ctx.calendar_year <= last_year
            has_aged_out = ctx.calendar_year > last_year

            if ctx.config.resp_used_for_education and in_study_year:
                years_left = last_year - ctx.calendar_year + 1
                draw = resp_annual_withdrawal(contrib_i, cesg_i, qesi_i, earnings_i, years_left)
                contrib_i -= draw['contributions_withdrawn']
                cesg_i -= draw['cesg_withdrawn']
                qesi_i -= draw['qesi_withdrawn']
                earnings_i -= draw['earnings_withdrawn']
                bal -= (draw['pse'] + draw['eap'])
                resp_pse_paid += draw['pse']
                resp_eap_paid += draw['eap']
                fired = True
            elif bal > 1e-6 and (
                    has_aged_out
                    or (not ctx.config.resp_used_for_education and ctx.calendar_year >= first_year)):
                collapse = resp_collapse_aip(cesg_i, qesi_i, earnings_i, ctx.primary_marginal_rate)
                resp_pse_paid += contrib_i
                resp_aip_tax += collapse['aip_tax']
                bal = 0.0
                contrib_i = 0.0
                cesg_i = 0.0
                qesi_i = 0.0
                earnings_i = 0.0
                fired = True

        new_resp_balances.append(max(0.0, bal))
        new_resp_contributions.append(max(0.0, contrib_i))
        new_resp_cesg.append(max(0.0, cesg_i))
        new_resp_qesi.append(max(0.0, qesi_i))

    ws.new_resp_balances = new_resp_balances
    ws.new_resp_contributions = new_resp_contributions
    ws.new_resp_cesg = new_resp_cesg
    ws.new_resp_qesi = new_resp_qesi
    ws.resp_eap_paid = resp_eap_paid
    ws.resp_pse_paid = resp_pse_paid
    ws.resp_aip_tax = resp_aip_tax
    return fired


@rule('mortgage')
def apply_mortgage(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Extract this year's mortgage principal/interest/end-balance from the
    pre-computed amortization schedule. Independent of every rule above;
    feeds ``sm_readvance`` (the principal paid is what gets readvanced).
    """
    mort = ctx.mortgage_data or {}
    principal_paid = mort.get('total_principal', 0)
    new_mortgage_balance = mort.get('end_balance', ws.opening_mortgage_balance)
    ws.mort = mort
    ws.principal_paid = principal_paid
    ws.new_mortgage_balance = new_mortgage_balance
    return principal_paid > 0


@rule('consumer_loans')
def apply_consumer_loans(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Amortize the household's closed-end consumer loans (issue #763).

    Each car_loan/student_loan/personal_loan is an amortizing, unsecured,
    non-revolving liability with a DECLARED monthly payment and a payoff
    date. Before this rule existed, every one of these was schema-valid,
    parsed, accepted -- and then silently dropped before the engine saw it
    (DP#32's founding defect). This rule makes their balance, rate and
    payment reach the fold: it amortizes each loan one year, carries the
    declining balance forward on SimState, and publishes the year's total
    payment + interest for ``apply_solvency`` to fold into the cash-flow
    identity's debt-service term and the #758 reserve/runway sizing.

    Mechanics (DP#3: pure function of the opening balances + the loan's own
    declared facts): for each loan, annual_payment = payment_monthly * 12;
    interest = opening_balance * rate; principal = annual_payment -
    interest; new_balance = opening_balance - principal. The loan stops at
    its DECLARED payoff term (amortization_years, counted from year 0) -- in
    the term's final year the remaining balance is closed exactly (pay
    balance + interest) so no residual lingers past the payoff date, and a
    declared payment that slightly over-amortizes is clamped to never push
    the balance negative. A loan whose opening balance is already 0 pays
    nothing and stays 0 -- it has reached its payoff date earlier.

    Interest is NOT deductible (these finance consumption, not income-
    earning property): the #656 default-to-deductible guard lives at the
    contract boundary (input_contract refuses investment_portion > 0), so
    this rule never deducts consumer-loan interest anywhere -- the interest
    is simply part of the after-tax debt-service payment, the same way the
    mortgage's interest is part of ``mort.total_payment``.

    Independent of every other rule (it reads only the opening balances +
    ``ctx.config.consumer_loans``); runs before ``solvency``, which consumes
    ``ws.consumer_loan_payment``.
    """
    loans = ctx.config.consumer_loans
    opening = ws.opening_consumer_loan_balances
    # DP#32: a mismatched pair is a programming error, not a silent
    # truncation -- SimState.initial seeds the balances parallel to the
    # config list, so a length mismatch here means state was built against a
    # different config than the one this rule is running.
    if len(opening) != len(loans):
        raise ValueError(
            f"consumer_loans: config has {len(loans)} loan(s) but SimState "
            f"carries {len(opening)} balance(s) -- mismatched state/config "
            f"(issue #763). This is a bug in the simulation wiring, not a "
            f"user-facing input error."
        )
    new_balances: list = []
    total_payment = 0.0
    total_interest = 0.0
    for loan, bal in zip(loans, opening):
        rate = loan['rate']
        term = loan['amortization_years']
        # The loan is off the books once its balance reached 0 (an earlier
        # year closed it) OR once the projection is past the loan's declared
        # payoff term (amortization_years, counted from year 0 -- the same
        # term-starts-at-the-simulation-start convention the mortgage's
        # precomputed schedule uses). A loan cannot pay beyond its term.
        if bal <= 0 or ctx.year >= term:
            new_balances.append(0.0)
            continue
        annual_payment = loan['payment_monthly'] * 12
        interest = bal * rate
        if ctx.year + 1 >= term:
            # The FINAL year of the declared term: close the loan -- pay the
            # remaining balance plus this year's interest exactly, so the
            # balance floors at 0 at the payoff date even if the declared
            # payment slightly under- or over-amortizes. Without this, a
            # small residual balance would linger past the term forever.
            payment = bal + interest
        else:
            # Clamp to never pay more than what closes the loan (balance +
            # this year's interest), so a declared payment that slightly
            # over-amortizes floors the balance at 0 rather than going
            # negative.
            payment = min(annual_payment, bal + interest)
        principal = payment - interest
        new_balances.append(max(0.0, bal - principal))
        total_payment += payment
        total_interest += interest
    ws.new_consumer_loan_balances = new_balances
    ws.consumer_loan_payment = total_payment
    ws.consumer_loan_interest = total_interest
    return total_payment > 0


# Issue #759: the date-scheduled payment math for a fixed-term installment
# plan, as a module-level pure function (DP#3) so it has ONE spelling --
# ``apply_installments`` (the fold rule) and ``_annual_installment_service``
# (the year-0 reserve-sizing helper in simulation_state) both call it, so a
# reserve sized against the year-0 payment can never disagree with the
# payment the engine actually charges (the same clone-avoidance reasoning as
# ``_annual_consumer_loan_service``'s use of each loan's declared payment).
#
# The plan pays ``monthly_amount`` on ``start_date`` and on each monthly
# anniversary for ``number_of_payments`` payments; the optional ``final_payment``
# balloon is paid on the SAME date as the last monthly payment
# (``start_date + (number_of_payments - 1) months``). For a given calendar
# year Y, the scheduled outflow is the sum of ``monthly_amount`` for every
# payment date that falls in Y, plus ``final_payment`` if the last payment
# date falls in Y. Summed over every year of the projection this is exactly
# ``number_of_payments * monthly_amount + final_payment`` -- money conserved,
# the plan ENDS at its declared term (no payment is carried to the horizon).
def _add_months(d, n):
    """Add ``n`` calendar months to a ``datetime.date`` ``d``, returning a
    datetime.date. The caller converts the contract's ``start_date`` string
    to a date once (in ``_installment_payment_in_year``) -- this helper is
    date-only, so there is one spelling of the string→date conversion, not
    two (DP#9). No external dependency: dateutil.relativedelta is available
    but not a declared project dependency, and this is the whole of the
    month arithmetic the plan needs."""
    from datetime import date
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    # Clamp the day to the target month's last day (a plan starting Jan 31
    # pays on the 28th of February, not Mar 3 -- the standard calendar-month
    # roll a payment schedule actually uses).
    import calendar as _cal
    day = min(d.day, _cal.monthrange(y, m)[1])
    return date(y, m, day)


def _installment_payment_in_year(plan: Dict[str, Any], calendar_year: int) -> float:
    """The scheduled dollar outflow for ``plan`` in calendar year ``calendar_year``.

    0.0 in every year the plan is not yet active (start_date in a LATER year)
    or has already ended (last payment date in an EARLIER year) -- the plan
    is finite, not perpetual. The final-year balloon is included only in the
    year of the last monthly payment.
    """
    from datetime import date
    start = plan['start_date']
    if not isinstance(start, date):
        start = date.fromisoformat(start)
    n = plan['number_of_payments']
    monthly = plan['monthly_amount']
    final = plan['final_payment']
    last_date = _add_months(start, n - 1)
    payment = 0.0
    for i in range(n):
        if _add_months(start, i).year == calendar_year:
            payment += monthly
    if final and last_date.year == calendar_year:
        payment += final
    return payment


# Issue #760: the ANNUAL amount a dated, finite-term living-cost segment
# contributes in one calendar year, as a module-level pure function (DP#3) so
# apply_solvency has ONE spelling of the day-count blend. A segment is an
# annual recurring amount active over the half-open window [from, to); for a
# given calendar year Y it contributes ``amount * (days of [from, to) ∩ Y) /
# (days in Y)`` -- the outflow-side analog of #674's dated income windows.
# ``to = None`` means perpetual (active through year-end and beyond); a
# non-null ``to`` means the expense ENDS there and contributes $0 from that
# date on (never carried to the horizon the way annual_living_costs is). A
# segment whose window does not touch Y at all (before ``from``, or on/after a
# non-null ``to``) contributes 0.0. Leap years are exact (days-in-year is
# 365 or 366, from the calendar), so the blend never approximates to a flat
# 1/365.
def _property_purchase_outflow_in_year(config, calendar_year: int) -> float:
    """Issue #696 (epic #690 bite 5): the total cash outflow the household's
    dated property purchases require in ``calendar_year`` -- the couple's down
    payment (``net_equity``) plus its share of ``closing_costs`` for each
    property whose ``purchase.year`` is this year. Zero in every other year and
    for every property held from year 0 (no ``purchase``), so a household with
    no mid-horizon purchase is byte-for-byte today (DP#32). The purchased
    property's equity enters the balance sheet the same year
    (``simulation_state._property_equity_for_year``); charging the down payment
    here draws it from the liquidation waterfall, conserving money (DP#18)."""
    total = 0.0
    for prop in config.properties:
        purchase = prop.get('purchase')
        if purchase is not None and purchase['year'] == calendar_year:
            total += prop['net_equity'] + purchase['closing_costs']
    return total


# Issue #1010 (epic #956): the ANNUAL recurring carrying-cost outflow a
# NON-principal property charges in ``calendar_year`` -- property tax,
# maintenance, insurance, condo/HOA -- the holding cost of owning the home,
# modelled as a per-property attribute the way ``rental_facts.expenses_annual``
# models a rental's T776 operating expense. A module-level pure function
# (DP#3) so apply_solvency has ONE spelling of the ownership-window gate.
#
# Active over the OWNERSHIP WINDOW: from the purchase year if the property was
# bought mid-horizon (#696), else the projection ``start_year`` (held from year
# 0, the #692 path), THROUGH the sale year (#956 bite B): a property sold mid-
# horizon charges its carrying cost UP TO but not including the sale year (the
# property leaves the balance sheet in the sale year and its net proceeds
# replace it -- there is nothing left to carry). The gate mirrors
# ``_property_equity_for_year`` exactly (purchase-then-sale ordering, so a sold
# property charges nothing in its sale year regardless of appreciation) --
# DP#9, one spelling of the ownership window.
#
# The outflow is the couple's share of the flat ``annual_amount`` (already scaled
# at map time by ``couple_share``, mirroring ``purchase.closing_costs``) PLUS
# ``fraction_of_value`` applied to the couple's share of the property's CURRENT
# (appreciating) gross value. The gross value compounds at ``appreciation_rate``
# from the ownership year exactly as ``_property_disposition_for`` prices a
# sale's ``P_gross`` (``value_share * (1+rate)**years_held``), so a carrying
# cost declared as a % of value tracks the appreciating asset, not a static
# snapshot -- the composition with appreciation the issue asks for.
#
# Absence-safe (DP#32): a property with no ``carrying_costs`` (the #692/#696/
# #956 default, incl. the golden fixture) returns 0.0 and never reads
# ``value_share``/``appreciation_rate`` -- so a household that declares no
# carrying cost is byte-identical to today. A declared ``{}`` (both components
# null) is a real $0 and also returns 0.0. ``fraction_of_value == 0.0`` is a
# real zero (0% of value) and is skipped, never coerced via ``or``. The flat
# ``annual_amount == 0`` (a real zero) adds 0.0 and is read explicitly.
def _property_carrying_cost_in_year(prop: Dict[str, Any],
                                    calendar_year: int,
                                    start_year: int) -> float:
    cc = prop.get('carrying_costs')
    if cc is None:
        return 0.0
    purchase = prop.get('purchase')
    sale = prop.get('sale')
    # Ownership window -- mirrors _property_equity_for_year's gate exactly
    # (DP#9): not yet owned before the purchase year, gone from the sale year
    # on (nothing left to carry in the sale year itself).
    owned_from = purchase['year'] if purchase is not None else start_year
    if calendar_year < owned_from:
        return 0.0
    if sale is not None and calendar_year >= sale['year']:
        return 0.0
    total = 0.0
    amount = cc.get('annual_amount')
    if amount is not None:
        total += amount
    fraction = cc.get('fraction_of_value')
    if fraction is not None and fraction != 0.0:
        # The couple's share of the CURRENT gross value, appreciating from the
        # ownership year (the SAME compounding _property_disposition_for uses
        # for a sale's P_gross). value_share is carried by the adapter whenever
        # fraction_of_value is non-null (input_contract._map_owned_properties),
        # so it is present here by construction.
        value_share = prop['value_share']
        rate = prop.get('appreciation_rate')
        if rate is None or rate == 0.0:
            gross = value_share
        else:
            years_held = max(0, calendar_year - owned_from)
            gross = value_share * ((1.0 + rate) ** years_held)
        total += fraction * gross
    return total


def _total_carrying_cost_in_year(config, calendar_year: int, start_year: int) -> float:
    """The aggregate recurring carrying-cost outflow across every non-principal
    property in ``calendar_year`` -- the loop wrapper apply_solvency calls, so
    the rule cites ONE function (DP#9). Zero for a household that declares no
    carrying costs on any property, byte-for-byte today (DP#32)."""
    total = 0.0
    for prop in config.properties:
        total += _property_carrying_cost_in_year(prop, calendar_year, start_year)
    return total


def _expense_segment_contribution_in_year(segment: Dict[str, Any],
                                          calendar_year: int) -> float:
    from datetime import date
    # Issue #882: an OPTIONAL intra-year SEASONALITY. When the segment declares
    # ``active_months`` (a subset of 1..12), its ANNUAL ``amount`` is spent in
    # equal shares across ONLY those months (heating that runs Nov-Mar, a
    # property-tax bill paid each July) and the within-year pattern is resolved
    # at MONTHLY granularity -- the year's charge is the sum of the active
    # months touched by the [from, to) window. Absent the key the segment is
    # active every day of the window and the untouched day-count blend below
    # runs, byte-for-byte #760 (DP#32 no-op when undeclared).
    active_months = segment.get('active_months')
    if active_months is not None:
        return sum(_expense_segment_monthly_outflows(segment, calendar_year))
    start = segment['from']
    if not isinstance(start, date):
        start = date.fromisoformat(start)
    end = segment['to']
    if end is not None and not isinstance(end, date):
        end = date.fromisoformat(end)
    year_start = date(calendar_year, 1, 1)
    next_year_start = date(calendar_year + 1, 1, 1)  # exclusive upper bound
    win_start = max(start, year_start)
    win_end = next_year_start if end is None else min(end, next_year_start)
    if win_end <= win_start:
        return 0.0
    days_active = (win_end - win_start).days
    days_in_year = (next_year_start - year_start).days
    return segment['amount'] * days_active / days_in_year


# Issue #882: the 12-length vector (one entry per calendar month) of the amount
# a SEASONAL segment charges in ``calendar_year``, at monthly resolution.
# ``amount`` is the ANNUAL figure split evenly over the declared active months;
# a month contributes its ``amount / len(active_months)`` share prorated by the
# days of that month falling inside the [from, to) window (so a boundary month
# the window enters or leaves mid-way stays day-exact, and an active month
# before ``from`` or on/after a non-null ``to`` contributes 0). Summed, this is
# the year's charge (``_expense_segment_contribution_in_year``); its PEAK entry
# sizes the emergency reserve (``apply_solvency``) so a concentrated seasonal
# bill is bridged at its peak month, not its annual average. Only called for a
# segment that declares ``active_months`` -- there is no monthly vector for a
# non-seasonal segment (its cost is a continuous day-count, not a monthly one).
def _expense_segment_monthly_outflows(segment: Dict[str, Any],
                                      calendar_year: int) -> List[float]:
    from datetime import date
    active_months = segment['active_months']
    start = segment['from']
    if not isinstance(start, date):
        start = date.fromisoformat(start)
    end = segment['to']
    if end is not None and not isinstance(end, date):
        end = date.fromisoformat(end)
    per_month = segment['amount'] / len(active_months)
    outflows = [0.0] * 12
    for month in active_months:
        month_start = date(calendar_year, month, 1)
        month_end = (date(calendar_year + 1, 1, 1) if month == 12
                     else date(calendar_year, month + 1, 1))
        win_start = max(month_start, start)
        win_end = month_end if end is None else min(month_end, end)
        if win_end <= win_start:
            continue
        days_active = (win_end - win_start).days
        days_in_month = (month_end - month_start).days
        outflows[month - 1] = per_month * days_active / days_in_month
    return outflows


@rule('installments')
def apply_installments(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Service the household's fixed-term, zero-interest installment
    obligations (issue #759).

    A medical/dental/education payment plan -- an up-front lump already paid
    (before the snapshot, not modelled), then N equal monthly payments and an
    optional final balloon, at 0% interest, over a FIXED term. Before this
    rule the contract had no shape for it: the only place it could go was
    ``household_budget.annual_living_costs``, which smeared a FINITE,
    must-pay plan into a PERPETUAL, compressible-looking scalar -- the
    current wrong behavior the issue reproduces. This rule makes its payment
    reach the fold: it applies the date-scheduled payment one year, carries
    the declining remaining-payment balance forward on SimState, and publishes
    the year's total payment for ``apply_solvency`` to fold into the cash-flow
    identity's debt-service term and the #758 reserve/runway sizing.

    Mechanics (DP#3: a pure function of the opening remaining balances + the
    plan's own declared facts): for each plan, the year's payment is
    ``_installment_payment_in_year(plan, ctx.calendar_year)`` -- the sum of
    ``monthly_amount`` for each of the plan's payment dates that fall in this
    calendar year, plus ``final_payment`` if the last payment date falls in
    this year. The remaining-payment balance declines by exactly that payment
    (0% interest: no interest component, by definition and by the contract
    boundary's refusal of a non-zero ``rate``). The plan is INACTIVE (pays 0,
    balance unchanged) in every year before its start_date and AFTER its last
    payment date -- it ENDS, it is not carried to the horizon. The year's
    payment is clamped to never exceed the opening balance, so a rounding
    residue can never push the balance negative; once the balance reaches 0
    the plan is off the books.

    NOT the same as ``apply_consumer_loans`` (DP#9: one spelling per rule).
    A consumer loan is an interest-bearing amortizing DEBT with a balance the
    household could refinance or prepay, counted in ``total_debt``; its rule
    amortizes from year 0 with ``interest = balance * rate`` and a final-year
    close. An installment plan is a 0%-interest committed payment SCHEDULE
    for services already received, NOT a callable debt (excluded from
    ``total_debt``), with a START DATE that can fall in a future year and an
    explicit optional balloon -- inputs that do not map onto the consumer-
    loan rule's year-0-relative, no-balloon, interest-bearing path. The two
    rules share the COMPOSITION (both publish a payment ``apply_solvency``
    folds into the same debt-service term + reserve sizing), which is where
    the reuse belongs; the amortization is a separate, smaller pure function
    because the logic genuinely differs.

    Non-negotiable under stress by construction: the payment lands in the
    solvency identity's debt-service term, the same NON-COMPRESSIBLE channel
    as the mortgage + consumer-loan payments -- an income-shock year cannot
    cut it (contrast #761's discretionary split, which compresses
    ``annual_living_costs``). Independent of every other rule (it reads only
    the opening balances + ``ctx.config.installments`` + ``ctx.calendar_year``);
    runs before ``solvency``, which consumes ``ws.installment_payment``.
    """
    plans = ctx.config.installments
    opening = ws.opening_installment_balances
    # DP#32: a mismatched pair is a programming error, not a silent
    # truncation -- SimState.initial seeds the balances parallel to the
    # config list, so a length mismatch here means state was built against a
    # different config than the one this rule is running (same guard
    # apply_consumer_loans carries).
    if len(opening) != len(plans):
        raise ValueError(
            f"installments: config has {len(plans)} plan(s) but SimState "
            f"carries {len(opening)} balance(s) -- mismatched state/config "
            f"(issue #759). This is a bug in the simulation wiring, not a "
            f"user-facing input error."
        )
    new_balances: list = []
    total_payment = 0.0
    for plan, bal in zip(plans, opening):
        scheduled = _installment_payment_in_year(plan, ctx.calendar_year)
        # The plan is off the books once its balance reached 0 (an earlier
        # year closed it) OR no payment is scheduled this year (before
        # start_date or after the last payment date -- the plan is finite).
        if bal <= 0 or scheduled <= 0:
            new_balances.append(max(0.0, bal))
            continue
        # Clamp to never pay more than what closes the plan, so a rounding
        # residue can never push the remaining balance negative (same
        # floor-at-0 discipline as apply_consumer_loans).
        payment = min(scheduled, bal)
        new_balances.append(max(0.0, bal - payment))
        total_payment += payment
    ws.new_installment_balances = new_balances
    ws.installment_payment = total_payment
    return total_payment > 0


# Issue #967: a one-spelling accessor for a property's mid-horizon
# `purchase.financing` block -- returns None when the property has no
# purchase OR the purchase declares no financing. An explicit `is None` test
# on the purchase (never `p.get('purchase') or {}`, DP#32 forbids that shape
# -- a property that means "no purchase" carries None, not an empty dict that
# a `or {}` would launder into a real one). Used by the servicing rule's
# fast no-op check and by the rule body so the two read the same field
def _prop_financing(prop: Dict[str, Any]):
    purchase = prop.get('purchase')
    if purchase is None:
        return None
    return purchase.get('financing')


@rule('second_property_mortgage')
def apply_second_property_mortgage(ws: YearWorkingState,
                                   ctx: RuleContext) -> bool:
    """Issue #967: service each mid-horizon mortgage originated by a property's
    ``purchase.financing``.

    A property bought mid-horizon (#696/Bite B) is equity-financed today:
    the full value leaves the portfolio as the down payment. When the
    purchase declares ``financing``, a MORTGAGE originates against the property
    in the purchase year -- only the DOWN PAYMENT (value - mortgage_amount,
    couple share -- already ``net_equity`` mapped by input_contract) leaves
    the portfolio, the mortgage funds the rest, and the mortgage is serviced
    (principal + interest) from the purchase year to its payoff.

    Mechanics (DP#3: a pure function of the opening balance + the property's
    own precomputed schedule -- input_contract.
    _annual_amortization_schedule built it once at map time from the standard
    annuity formula, so the servicing, the rental interest deduction, and the
    balance-sheet total_debt all read ONE schedule, never three computations
    that could drift). For each financed property:

      - ORIGINATION (purchase year): the balance originates at the schedule's
        ``opening_balance`` (== the couple-share mortgage_amount). The
        originated principal is surfaced as ``second_property_mortgage_
        originated`` so apply_solvency can count it as an INFLOW in the
        purchase year (money conservation, DP#18 -- the mortgage funds the
        purchase: it is an inflow that arrives from the lender AND an
        outflow that leaves for the seller in the same breath, exactly the
        inflow==outflow discipline ``borrowed_investment`` uses for the
        year-0 leveraged lump sum). Without this inflow the solvency
        identity would invent a shortfall equal to the down payment (the
        outflow) with no matching inflow, and force a spurious liquidation.
      - SERVICING (every year from purchase to payoff): the schedule's annual
        slice gives the year's ``interest``, ``principal``, and ``payment``;
        the balance declines to the slice's ``end_balance``. The payment
        joins the cash-flow identity's debt-service term (apply_solvency),
        the same NON-COMPRESSIBLE channel the principal mortgage,
        consumer loans, and installments already use.
      - PAYOFF: once the balance reaches 0 the mortgage pays nothing and
        stays 0 -- it has reached its payoff date.

    Interest DEDUCTIBILITY: the per-year ``interest`` is surfaced on
    ``ws.second_property_mortgage_interest`` (the TOTAL across financed
    properties). The rental fold (``simulation._rental_income_for``) reads the
    per-property interest off the financing schedule and adds it to the
    rental's s.20(1)(c) deduction for a RENTAL property; a COTTAGE
    (kind=recreational) has no ``rental`` block, so its financed interest
    never reaches the deduction -- NON-deductible by construction, as the
    issue requires. This rule does NOT deduct the interest itself (it is
    not a tax rule, DP#10/DP#25); it only surfaces the figure the rental
    fold claims.

    Absence-safe (DP#32): a household with no financed property (the golden
    fixture -- every property is either held from year 0 or equity-financed at
    purchase) has an all-zero opening balance list and no financing block on
    any property -- the rule is a strict no-op, every output stays at its
    seeded 0.0, and the golden invariant is unchanged by construction.

    Independent of every rule above (it reads only the opening balances +
    ``ctx.config.properties`` + ``ctx.calendar_year``); runs before
    ``solvency``, which consumes ``ws.second_property_mortgage_payment`` as
    debt service and ``ws.second_property_mortgage_originated`` as an inflow.
    """
    props = getattr(ctx.config, 'properties', [])
    opening = ws.opening_second_property_mortgage_balances
    # Fast no-op when no property declares financing: a household with no
    # mid-horizon mortgage (the golden fixture, and every bare-SimState unit
    # test that carries properties without financing) has nothing to service.
    # This avoids requiring the parallel-length guard below to hold for unit
    # tests that construct SimState directly (bypassing SimState.initial, which
    # is what seeds the parallel list) -- the consumer_loans/installments rules
    # have the identical shape: their guard only bites when their config list
    # is non-empty. Carry the opening balances through unchanged (a no-op).
    has_any_financing = any(
        _prop_financing(p) is not None
        for p in props)
    if not has_any_financing:
        ws.new_second_property_mortgage_balances = list(opening)
        # ws.second_property_mortgage_payment / _interest / _originated stay
        # at their seeded 0.0 (the rule is a strict no-op).
        return False
    # DP#32: a mismatched pair is a programming error, not a silent
    # truncation -- SimState.initial seeds the balances parallel to the
    # config list, so a length mismatch here (when financing IS declared)
    # means state was built against a different config than the one this rule
    # is running (same guard apply_consumer_loans / apply_installments carry).
    if len(opening) != len(props):
        raise ValueError(
            f"second_property_mortgage: config has {len(props)} property(ies) "
            f"but SimState carries {len(opening)} mortgage balance(s) -- "
            f"mismatched state/config (issue #967). This is a bug in the "
            f"simulation wiring, not a user-facing input error."
        )
    new_balances: list = []
    total_payment = 0.0
    total_interest = 0.0
    total_originated = 0.0
    for prop, opening_bal in zip(props, opening):
        financing = _prop_financing(prop)
        if financing is None:
            # No financing on this property: the balance stays 0 (it never
            # originates), and nothing is serviced. Parallel-by-index holds.
            new_balances.append(opening_bal)
            continue
        cal_year = ctx.calendar_year
        schedule = financing['schedule']
        # ORIGINATION: in the purchase year the mortgage originates at the
        # schedule's first entry's opening_balance (== the couple-share
        # mortgage_amount). Before the purchase year the balance is 0 (the
        # mortgage does not exist yet).
        if cal_year < financing['origination_year']:
            new_balances.append(opening_bal)
            continue
        # Find this calendar year's slice in the precomputed schedule. The
        # schedule starts at origination_year and stops at payoff (or the
        # horizon); a year past the last entry means the mortgage has paid
        # off -- balance 0, nothing to service.
        slice_ = None
        for entry in schedule:
            if entry['year'] == cal_year:
                slice_ = entry
                break
        if slice_ is None:
            # Past payoff: the balance is whatever remains (0 once paid off;
            # an amortization that exactly hits 0 at the term leaves 0 here).
            new_balances.append(0.0)
            continue
        # In the ORIGINATION year the balance originates: opening_bal is 0
        # (seeded at SimState.initial), and the year's opening balance is the
        # originated principal. The schedule's opening_balance IS that
        # principal; the payment/interest/principal are computed against it.
        balance = slice_['opening_balance'] if opening_bal <= 0 else opening_bal
        payment = slice_['payment']
        interest = slice_['interest']
        end_balance = slice_['end_balance']
        # ORIGINATION inflow: the principal originated this year (the full
        # mortgage_amount, only in the purchase year). The schedule's first
        # entry's opening_balance is the principal; in the origination year
        # opening_bal was 0, so the originated amount = the schedule's
        # opening_balance.
        if cal_year == financing['origination_year'] and opening_bal <= 0:
            total_originated += slice_['opening_balance']
        new_balances.append(end_balance)
        total_payment += payment
        total_interest += interest
    ws.new_second_property_mortgage_balances = new_balances
    ws.second_property_mortgage_payment = total_payment
    ws.second_property_mortgage_interest = total_interest
    ws.second_property_mortgage_originated = total_originated
    return total_payment > 0 or total_originated > 0


@rule('sm_readvance')
def apply_sm_readvance(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Readvanceable mortgage (DP#7: the mechanism, not the branded product):
    readvance this year's mortgage principal paydown into the HELOC and
    invest it -- BOUNDED BY THE REGISTERED CHARGE (issue #681).
    Depends on ``mortgage`` (principal_paid, new_mortgage_balance),
    ``margin_heloc_interest`` (new_heloc_balance, the drawn personal margin)
    and ``heloc_tracing`` (mutates the same tracing dict further).

    Issue #681: the readvance used to book ``principal_paid`` into the line
    unconditionally, every year, with no bound at all -- so a household
    re-borrowed its way to $3,057,904 of secured debt against an $800,000
    house (382% LTV), and the LESS it borrowed up front the MORE debt it
    ended with (a small mortgage amortizes faster -> more principal paid ->
    more readvanced, for longer). #664 bounded the year-0 *overlay* against
    the charge; nothing bounded the year-by-year *readvance*, so the
    constraint held at t=0 and was violated by t=1.

    The bound IS the mechanism (DP#7: model the mechanism, not the product).
    A readvanceable mortgage cannot lend past the charge registered against
    the property; the only reason paying principal down creates line room is
    that the charge is FIXED. So this year's draw is capped by whichever
    binds first:

      - the combined charge (OSFI B-20: 80% LTV for an uninsured combined
        loan plan) -- room = charge_limit - (mortgage + drawn revolving);
      - the revolving-only ceiling (OSFI B-20: 65% LTV for the readvanceable
        segment, independent of the 80% combined cap) -- room =
        heloc_revolving_limit - drawn revolving.

    Both are floored at zero: when the charge is full, the readvance STOPS.

    ``house_value`` is the principal's APPRECIATED value this year
    (``_principal_value_for_year``, issue #963 epic #956 bite F): a home that
    declares an ``appreciation_rate`` compounds at that rate, so a grown home
    has more collateral to re-borrow against. Absent/0.0 rate = the static
    ``config.house_value`` (byte-identical to today, DP#32) -- the
    conservative fixed-charge reading the contract states
    (``properties[].value``, an ``as_of``-dated fact) unless the household
    EXPLICITLY declares a rate (never a hardcoded default, DP#2/DP#13).
    """
    new_sm_heloc = ws.opening_readvance_heloc_balance
    new_sm_investment = ws.opening_sm_investment_balance
    new_sm_cost_basis = ws.opening_sm_investment_cost_basis
    fired = False

    # DP#32: a readvanceable line is a claim on a charge registered against a
    # PROPERTY. With no declared property value there is no charge, so this
    # rule cannot know what it is allowed to advance -- and it must say so,
    # not quietly advance nothing (a silently-zeroed readvance would look
    # exactly like a household that legitimately had no room) and not quietly
    # advance everything (the #681 bug: unbounded re-borrowing).
    if ctx.use_readvanceable and ctx.config.house_value <= 0:
        raise ReadvanceableWithoutPropertyError(
            "The readvanceable-mortgage strategy is active but "
            "config.house_value is 0 -- there is no declared property, so "
            "there is no registered charge, so the line's advanceable room "
            "is unknowable (issue #681). Declare property.house_value, or "
            "run with use_readvanceable=False. Refusing rather than "
            "advancing an unbounded amount against a property that was "
            "never stated (DP#32)."
        )

    readvance_room = charge_room_for_readvance(
        # Issue #963 (epic #956 bite F): the charge is registered against the
        # principal's APPRECIATED value this year, not the static `house_value`
        # -- a home that has grown has more collateral to re-borrow against
        # (mirrors Bite A's appreciated equity for a non-principal property).
        # Absence-safe: `_principal_value_for_year` returns the static
        # `house_value` when no rate is declared, byte-identical to today.
        house_value=_principal_value_for_year(ctx.config, ctx.calendar_year),
        mortgage_balance=ws.new_mortgage_balance,
        drawn_revolving=new_sm_heloc + ws.new_heloc_balance,
        charge_ltv_limit=ctx.config.charge_ltv_limit,
        heloc_ltv_limit=ctx.config.heloc_ltv_limit,
    )
    readvanced = min(ws.principal_paid, readvance_room)
    ws.readvance_room = readvance_room
    ws.readvance_blocked = max(0.0, ws.principal_paid - readvanced) if ctx.use_readvanceable else 0.0

    if ctx.use_readvanceable and readvanced > 0:
        from simulation_state import _new_heloc_tracing
        new_sm_heloc += readvanced
        new_sm_investment += readvanced
        new_sm_cost_basis += readvanced
        ws.new_tracing = _new_heloc_tracing(
            ws.new_tracing,
            total_advances=ws.new_tracing['total_advances'] + readvanced,
            investment_advances=ws.new_tracing['investment_advances'] + readvanced,
        )
        fired = True

    ws.sm_readvanced = readvanced if ctx.use_readvanceable else 0.0
    ws.new_sm_heloc = new_sm_heloc
    ws.new_sm_investment = new_sm_investment
    ws.new_sm_cost_basis = new_sm_cost_basis
    # These five are set unconditionally here (matching the original
    # engine's section boundary); `sm_interest` may overwrite the first
    # four and always sets/overwrites `new_qc_carry_forward`.
    ws.readvance_interest = 0.0
    ws.readvance_tax_savings = 0.0
    ws.deductible_proportion = 0.0
    ws.qc_deductible = 0.0
    ws.sm_interest_deduction = 0.0
    return fired


def _sm_schedule_l_income(ctx: RuleContext, pot_balances) -> float:
    """The year's TP-1 Schedule L net investment income (lines 2-6 total) of
    the non-reg pots the traced borrowings bought (#1035).

    Per pot: eligible + non-eligible dividends + interest/other + foreign
    (line 4's "other"), plus HALF the declared capital-gain yield component
    (line 5: only REALIZED taxable capital gains count, at the inclusion
    rate). The per-type rates come from the config's declared non-reg portfolio
    yield (DP#2/DP#27); when none is declared every component falls back to the
    configurable ``non_reg_yield_rate`` as interest -- reproducing the pre-
    #1035 ``balance * yield_rate`` base exactly (DP#32).

    Pure function of config + balances (DP#3): no fold state is read.
    """
    from countries.canada.portfolio import compute_investment_income
    yield_data = None
    portfolio_block = None if ctx.config is None else ctx.config.portfolio_data
    if isinstance(portfolio_block, dict):
        accounts = portfolio_block.get('accounts')
        if isinstance(accounts, dict):
            non_reg_acct = accounts.get('non_reg')
            if isinstance(non_reg_acct, dict):
                declared = non_reg_acct.get('yield')
                if isinstance(declared, dict):
                    yield_data = declared
    fallback_rate = 0.02 if ctx.config is None else ctx.config.non_reg_yield_rate
    total = 0.0
    for balance in pot_balances:
        if balance > 0:
            breakdown = compute_investment_income(
                balance, yield_data=yield_data,
                default_yield_rate=fallback_rate)
            # Schedule L line 5: capital gains enter at the inclusion rate.
            total += (breakdown['eligible_dividends']
                      + breakdown['non_eligible_dividends']
                      + breakdown['interest']
                      + breakdown['foreign_income']
                      + _CAPITAL_GAINS_INCLUSION * breakdown['capital_gains'])
    return total


_CAPITAL_GAINS_INCLUSION = 0.5  # Schedule L line 5 / ITA s.38 inclusion rate


def _year_split_brackets_for(ctx: RuleContext) -> tuple:
    """``(federal_slice, provincial_slice)`` brackets for this year, or
    ``(None, None)`` when the provider has no split for it -- the same
    warn-and-fallback contract ``simulation._year_brackets_for`` applies to
    the combined list (DP#20). Frozen-bracket runs resolve the split at the
    frozen start year, matching the combined list's year (DP#5).
    """
    if ctx.config is None:
        return None, None
    provider = (ctx.tax_provider if ctx.tax_provider is not None
                else default_tax_provider())
    cal_year = (ctx.config.start_year if ctx.config.frozen_brackets
                else ctx.calendar_year)
    try:
        return provider.get_split_brackets(cal_year,
                                           province=ctx.config.province)
    except ValueError:
        logger.warning(
            "No split tax bracket data for %s/%s; valuing the s.20(1)(c) "
            "deduction on the combined brackets (pre-#1035 behaviour).",
            cal_year, ctx.config.province)
        return None, None


@rule('sm_interest')
def apply_sm_interest(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """The §20(1)(c)-qualifying, QC-carry-forward-limited deductible interest
    on EVERY purpose-traced borrowing the household has. Depends on
    ``sm_readvance`` (new_sm_heloc), ``heloc_tracing`` (the readvance line's
    tracing ratio), and -- issue #850 -- on ``mortgage`` (the advance's
    interest and amortizing balance), ``margin_heloc_interest`` (the drawn
    line's interest and balance) and ``borrowing_purpose`` (both their
    tracings).

    ## The three legs

    Three DISTINCT secured balances can each carry deductible interest, and
    each has its own purpose trace (one trace per borrowing -- that IS the
    s.20(1)(c) tracing rule):

      1. ``new_sm_heloc`` -- the SM READVANCE line, traced by ``new_tracing``.
         The only leg this rule priced before #850.
      2. ``new_mortgage_balance`` -- the mortgage, including any cash-out
         ADVANCE, traced by ``new_advance_tracing`` (#850).
      3. ``new_heloc_balance`` -- the DRAWN revolving margin, traced by
         ``new_margin_tracing`` (#850).

    Legs 2 and 3 are #849's whole question. Before #850 neither was deducted
    anywhere, so "advance vs line" was ranked on the rate gap and interest
    capitalization alone -- a ranking of a different question than the one the
    household asked (DP#32). All three legs share ONE deductibility rule here
    and ONE proportion helper (``compute_heloc_deductible_proportion``, DP#9 --
    the acceptance criterion on #850 is explicitly "not a fourth copy").

    ## The asymmetry this makes visible

    Each leg's deductible proportion is FIXED by its trace (repayments reduce a
    blended borrowing pro rata; a taxpayer cannot pay down the personal half
    first). So the leg's deductible BALANCE is that fixed proportion of a
    balance that moves on its own terms:

      - the ADVANCE's balance amortizes, so its deductible balance -- and the
        deduction it yields -- ERODES year over year (#849's erosion);
      - the drawn LINE is interest-only and capitalizes into the charge, so its
        deductible balance does NOT erode.

    That is the trade-off, priced. Both are surfaced on ``YearResult`` as
    ``advance_deductible_balance`` / ``margin_deductible_balance``.

    ## The QC cap is shared, because the taxpayer is one taxpayer

    Quebec limits investment expenses to investment income, with an indefinite
    carry-forward (TA s.336.0.1). That is ONE limit over ONE taxpayer's whole
    investment position, so all three legs pool into a single
    ``qc_available``/``qc_carry_forward`` -- splitting them into per-leg pools
    would let one household deduct against the same income twice. The savings
    are then split back across the legs pro rata for REPORTING only
    (``readvance_tax_savings`` stays the SM line's own share, so #850 does not
    silently redefine a number other tables already print).
    """
    from simulation_state import compute_heloc_deductible_proportion
    yield_rate = ctx.config.non_reg_yield_rate

    # ── Leg 1: the SM readvance line (gate unchanged from before #850) ──
    sm_active = ctx.use_readvanceable and ws.new_sm_heloc > 0
    readvance_interest = ws.new_sm_heloc * ctx.heloc_rate if sm_active else 0.0
    sm_proportion = compute_heloc_deductible_proportion(
        ws.new_tracing, yield_rate=yield_rate
    ) if sm_active else 0.0
    sm_deductible = readvance_interest * sm_proportion

    # ── Leg 2: the mortgage advance (#850). Its deductible balance amortizes.
    # Issue #1075: when the mortgage carries a declared DEDUCTIBLE tranche
    # (the 3-tranche readvanceable structure's investment mortgage, mapped to
    # ``deductible_mortgage_balance``/``deductible_mortgage_interest`` -- the
    # exact sum of each flagged tranche's balance x ITS OWN rate), the leg is
    # priced at that EXACT interest, not at the blended-rate product the
    # traced proportion would produce. The blended product is only exact when
    # every tranche shares one rate: with a house tranche at 4.5% beside an
    # investment tranche at 6.5%, balance x weighted-rate x proportion
    # understates the s.20(1)(c) deduction the taxpayer can actually claim.
    # The flagged tranches were borrowed for investment, so their interest is
    # deductible in full -- the purpose trace of a NEW year-0 lump sum does
    # not apply to them (and would dilute them with the house tranche's
    # personal purpose). The exact year-0 interest is scaled by the same
    # amortization the traced path applies (the tranches amortize pro rata on
    # the single schedule), so the erosion #849 measures is preserved. A
    # config WITHOUT the keys keeps the pre-#1075 traced path byte-for-byte
    # (the golden household included -- DP#32).
    advance_proportion = compute_heloc_deductible_proportion(
        ws.new_advance_tracing, yield_rate=yield_rate)
    exact_deductible_interest = ctx.config.deductible_mortgage_interest
    exact_deductible_balance = ctx.config.deductible_mortgage_balance
    if exact_deductible_interest > 0 and ctx.config.mortgage_balance > 0:
        amort_scale = ws.opening_mortgage_balance / ctx.config.mortgage_balance
        ws.advance_deductible_interest = exact_deductible_interest * amort_scale
        ws.advance_deductible_balance = exact_deductible_balance * amort_scale
    else:
        ws.advance_deductible_interest = ws.mort.get('total_interest', 0.0) * advance_proportion
        ws.advance_deductible_balance = ws.new_mortgage_balance * advance_proportion

    # ── Leg 3: the drawn revolving margin (#850). Its deductible balance does
    # not amortize -- apply_margin_heloc_interest capitalizes it, never
    # repays it.
    # Issue #1036 D4/N2: deduct only interest that was PAID (serviced from pots)
    # or PAYABLE (capitalized into the balance) -- NOT the `heloc_interest_
    # unfunded` portion that was neither (it evaporated because the pots could
    # not service it and the charge had no room to capitalize it). A s.20(1)(c)
    # deduction requires interest paid or payable; the unfunded is neither, so
    # deducting it was a confident wrong number in the tax computation (and the
    # dollar-exact engine of the D2 inverted incentive). This runs AFTER
    # heloc_interest_servicing (see the rule order), so ws.heloc_interest_unfunded
    # is final here. 0.0 when there is no drawn margin or it is fully paid /
    # capitalized (the byte-identical path, incl. the golden household).
    margin_proportion = compute_heloc_deductible_proportion(
        ws.new_margin_tracing, yield_rate=yield_rate)
    paid_or_payable_margin_interest = ws.margin_heloc_interest - ws.heloc_interest_unfunded
    ws.margin_deductible_interest = paid_or_payable_margin_interest * margin_proportion
    ws.margin_deductible_balance = ws.new_heloc_balance * margin_proportion

    traced_deductible = ws.advance_deductible_interest + ws.margin_deductible_interest
    total_deductible = sm_deductible + traced_deductible

    if not sm_active and traced_deductible <= 0:
        # Nothing traced anywhere: no deduction, and the carry-forward simply
        # carries. Byte-for-byte the pre-#850 `else` branch -- the golden
        # household lands here every year of its 46-year horizon.
        ws.new_qc_carry_forward = ws.opening_qc_carry_forward
        ws.traced_borrowing_tax_savings = 0.0
        return False

    # ── The shared QC investment-expense cap ──
    # The income base is the investment income of the pots the traced
    # borrowings actually bought: the SM leg's proceeds are `new_sm_investment`
    # (its own pot), while the advance's and the drawn line's proceeds were
    # allocated by `fill_room` into the plain non-registered account. The
    # non-reg pot is therefore added ONLY when one of those two legs is present
    # -- an SM-only household's cap must not silently widen because #850
    # landed. Whether QC's pool should ALWAYS have counted the plain non-reg
    # account's income is a real question, and a separate one from this issue.
    #
    # ── The shared QC investment-expense cap (QUEBEC households only) ──
    # The income base is the SCHEDULE L net investment income of the pots the
    # traced borrowings actually bought (#1035): eligible + non-eligible
    # dividends, interest/other, and HALF the realized capital-gain component
    # of the declared yield (Schedule L line 5) -- NOT a flat
    # ``balance * yield_rate`` product, which could not tell a dividend
    # portfolio from a growth one and permanently under-used the cap for
    # growth-tilted households. The SM leg's proceeds are `new_sm_investment`
    # (its own pot), while the advance's and the drawn line's proceeds were
    # allocated by `fill_room` into the plain non-registered account. The
    # non-reg pot is therefore added ONLY when one of those two legs is present
    # -- an SM-only household's cap must not silently widen because #850
    # landed. Whether QC's pool should ALWAYS have counted the plain non-reg
    # account's income is a real question, and a separate one from this issue.
    #
    # The cap itself is QUEBEC-ONLY (TA s.336.0.1): a household whose tax
    # province is not Quebec faces no investment-income limitation in any
    # statute, so it deducts the full traced interest provincially too, and
    # any carry-forward it somehow carries simply carries (#1035).
    qc_income_pots = [ws.new_sm_investment]
    if traced_deductible > 0:
        qc_income_pots.append(ws.new_nonreg_bal)
    from countries.canada.provinces.quebec.quebec_deduction import (
        cap_qc_investment_interest)
    province = '' if ctx.config is None else str(ctx.config.province)
    if province.strip().lower() in ('qc', 'quebec', 'québec'):
        qc_deductible, new_qc_carry_forward = cap_qc_investment_interest(
            total_deductible=total_deductible,
            investment_income=_sm_schedule_l_income(ctx, qc_income_pots),
            opening_carry_forward=ws.opening_qc_carry_forward)
    else:
        qc_deductible = total_deductible
        new_qc_carry_forward = ws.opening_qc_carry_forward

    # Issue #1033: the deduction is routed through taxable income by TWO
    # mechanisms, gated so exactly ONE fires per phase (Blocker 1: the
    # pre-fix code let both fire and paid the same dollar twice):
    #
    #   * STATUTORY side-credit (accumulation only): the deduction's federal+
    #     QC statutory tax saving, valued at bracket-fill via
    #     ``tax_calculator.deduction_value`` -- the SAME ``tax_on_income`` /
    #     year-brackets path the prologue's ``_income_tax_by_adult`` uses for
    #     rental and private-loan s.20(1)(c) interest (DP#9). A deduction
    #     crossing a bracket boundary is worth less than the flat top marginal
    #     rate (the pre-#1033 code used ``amount * primary_marginal_rate``).
    #     Real improvement: a zero-income retiree used to get a bogus
    #     ``qc_deductible * marginal_rate(0)`` = 25.69% side-credit;
    #     ``deduction_value(0, ...)`` now returns 0.
    #   * OAS-CLAWBACK routing (retirement only, in ``apply_retirement_
    #     drawdown``): the FEDERAL ``total_deductible`` (uncapped -- the QC cap
    #     is a provincial limit, TA s.336.0.1, separate) reduces the clawback
    #     base, buying OAS recovery-tax relief + lowering the draw's
    #     progressive base.
    #
    # The gate is ``ctx.primary_retired`` (the deduction is the PRIMARY's;
    # Canada has no joint filing). In retirement ``primary_taxable_income`` is
    # NOT zero -- rental operating income and private-loan interest income
    # survive (``primary_income`` is zeroed at retirement, those are not) --
    # so the side-credit would double-count against the routing. Zeroing it in
    # retirement leaves the routing as the sole mechanism. The cost: the
    # statutory saving on the NON-drawdown income slice (rental/loan) in
    # retirement is not captured (the routing reaches only the cpp+pension+draw
    # base) -- an edge case (retiree with rental/loan income AND a leveraged
    # margin), documented as a known limit, not double-counted.
    #
    # The side-credit is valued as a FEDERAL + QUEBEC pair (#1035): the federal
    # slice on ``total_deductible`` (uncapped -- no federal limit), the Quebec
    # slice on ``qc_deductible``, each on its own bracket set. Pre-#1035 the
    # single capped amount was valued at the blended COMBINED rate, which
    # suppressed the federal deduction whenever the cap bound. That conflation
    # is NOT the same as Major 3's routing leak (routing ``qc_deductible`` into
    # the FEDERAL clawback base), which #1033 fixed by routing
    # ``total_deductible``.
    # Direct unit-test callers that pass ``primary_marginal_rate`` but not
    # ``year_brackets`` keep the pre-#1033 flat-rate valuation byte-for-byte
    # (the live fold always passes ``year_brackets``).
    from tax_calculator import deduction_value
    if ctx.year_brackets is not None:
        fed_brackets, prov_brackets = _year_split_brackets_for(ctx)
        if fed_brackets is not None:
            # Issue #1035: the FEDERAL s.20(1)(c) deduction has no
            # investment-income limit, so its slice is valued on the UNCAPPED
            # ``total_deductible``; only the QUEBEC slice is valued on the
            # capped ``qc_deductible``. One blended combined-rate valuation of
            # the capped amount (the pre-#1035 conflation) suppressed the
            # federal deduction whenever the cap bound.
            tax_savings = (
                deduction_value(
                    ctx.primary_taxable_income, total_deductible, fed_brackets)
                + deduction_value(
                    ctx.primary_taxable_income, qc_deductible, prov_brackets))
        else:
            # Split unavailable for this year (no tax data): value the capped
            # amount at the combined brackets -- the pre-#1035 spelling --
            # rather than fabricate a split (DP#32).
            tax_savings = deduction_value(
                ctx.primary_taxable_income, qc_deductible, ctx.year_brackets)
    else:
        tax_savings = qc_deductible * ctx.primary_marginal_rate
    if ctx.primary_retired:
        # Blocker 1: the routing in apply_retirement_drawdown handles the
        # deduction in retirement; the side-credit would double-count.
        tax_savings = 0.0
    # Report-only split of ONE pooled deduction. When nothing was traced this
    # year the whole (carry-forward-funded) saving stays on the SM leg, exactly
    # where it was reported before #850.
    sm_share = sm_deductible / total_deductible if total_deductible > 0 else 1.0

    ws.readvance_interest = readvance_interest
    ws.deductible_proportion = sm_proportion
    ws.qc_deductible = qc_deductible
    ws.new_qc_carry_forward = new_qc_carry_forward
    # Major 3: route the FEDERAL s.20(1)(c) deduction (``total_deductible``, no
    # investment-income limit) through the OAS clawback base -- NOT the
    # Quebec-capped ``qc_deductible``. The OAS recovery tax is federal; leaking
    # the QC cap into it silently dropped ~61% of the relief in the motivating
    # fixture. ``qc_deductible`` stays the provincial side-credit amount (#1035).
    ws.sm_interest_deduction = total_deductible
    ws.readvance_tax_savings = tax_savings * sm_share
    ws.traced_borrowing_tax_savings = tax_savings * (1.0 - sm_share)
    return True


@rule('sm_investment_growth')
def apply_sm_investment_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Grow the SM (readvanced) investment at the same after-tax rate as
    the plain non-reg account (#576: it is non-registered/taxable by
    construction). Depends on ``non_reg_growth`` (the rate) and
    ``sm_readvance`` (the balance).
    """
    if ctx.use_readvanceable:
        pre = ws.new_sm_investment
        ws.new_sm_investment *= (1 + ws.non_reg_growth_rate)
        return pre > 0 and ws.non_reg_growth_rate != 0
    return False


@rule('margin_heloc_interest')
def apply_margin_heloc_interest(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Interest on the drawn HELOC margin -- capitalized only as far as the
    registered charge has room, serviced in cash beyond that (issue #681).
    Depends on ``mortgage`` (new_mortgage_balance, the other claim on the same
    charge) and ``non_reg_growth`` (the pot the serviced interest is paid
    from). Issue #577: an undrawn margin has a $0 balance here, so no interest
    arises at all.

    Issue #681: this rule used to capitalize interest UNCONDITIONALLY --
    ``new_heloc_balance = opening * (1 + rate)``, forever, with no ceiling. A
    household that drew $400,000 of margin at year 0 compounded it to over
    $1,000,000 of revolving debt by the end of the horizon, straight through
    the $640,000 charge, and the run stayed green. Bounding only the readvance
    (the other half of #681) would have left this channel wide open: the debt
    would have breached the charge anyway, just more slowly.

    A revolving facility cannot capitalize interest past its limit. That is
    not a modelling nicety, it is what the product IS: the lender requires at
    minimum an interest-only payment, and the balance cannot exceed the
    authorized limit. So interest capitalizes only into the room the charge
    actually leaves; the rest must be **serviced in cash** -- booked by the
    ``heloc_interest_servicing`` rule below, which runs once the SM balances
    it is paid out of are final.

    NOTE (issue #1036): the contract declares
    ``liabilities[kind=heloc].capitalize_interest`` and the engine now READS
    it (mapped to ``property.capitalize_interest`` -> ``SimulationConfig
    .capitalize_interest``). When False, the drawn-margin interest is serviced
    in cash -- none of it capitalizes, regardless of charge room. When True
    (the default when the contract omits the field, and the pre-#1036
    behaviour), capitalize up to the charge and service the rest. The
    capitalized-vs-serviced split is honoured below. This closes the
    declaration gap this NOTE used to record.
    """
    margin_heloc_interest = ws.opening_heloc_balance * ctx.heloc_rate

    # Room left under the shared charge, priced against the OTHER claims on
    # it: the amortizing mortgage, plus the already-drawn revolving balances
    # (this margin, and any SM-readvanced line carried in from last year).
    room = charge_room_for_readvance(
        # Issue #963 (epic #956 bite F): the charge is registered against the
        # principal's APPRECIATED value this year, not the static `house_value`
        # (mirrors the SM readvance rule above). Absence-safe: returns the
        # static `house_value` when no rate is declared, byte-identical today.
        house_value=_principal_value_for_year(ctx.config, ctx.calendar_year),
        mortgage_balance=ws.new_mortgage_balance,
        drawn_revolving=ws.opening_heloc_balance + ws.opening_readvance_heloc_balance,
        charge_ltv_limit=ctx.config.charge_ltv_limit,
        heloc_ltv_limit=ctx.config.heloc_ltv_limit,
    )
    capitalized = min(margin_heloc_interest, room)
    # Issue #1036: honour liabilities[kind=heloc].capitalize_interest. When
    # False, the household pays the drawn-margin interest in CASH -- none of
    # it capitalizes into the balance, regardless of how much charge room is
    # left (a retiree servicing their HELOC interest in cash is no longer
    # modelled as capitalizing it up to the charge anyway). When True (the
    # default when the contract omits the field, and the pre-#1036 behaviour),
    # capitalize up to the charge and service the rest in cash. The serviced
    # portion is booked by the ``heloc_interest_servicing`` rule below, which
    # runs once the SM balances it is paid out of are final -- so wiring this
    # here is money-conserving (the interest leaves the balance sheet via the
    # pots, never silently absorbed) and DP#32-honest (when the pots cannot
    # cover it, ``heloc_interest_unfunded`` reports it).
    if not ctx.config.capitalize_interest:
        capitalized = 0.0

    ws.margin_heloc_interest = margin_heloc_interest
    ws.margin_heloc_interest_capitalized = capitalized
    ws.margin_heloc_interest_serviced = margin_heloc_interest - capitalized
    ws.new_heloc_balance = ws.opening_heloc_balance + capitalized
    return ws.opening_heloc_balance > 0


@rule('heloc_interest_servicing')
def apply_heloc_interest_servicing(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Pay, in cash, the HELOC interest the charge had no room to capitalize
    (issue #681). Depends on ``margin_heloc_interest`` (the serviced amount)
    and on ``non_reg_growth`` / ``sm_investment_growth`` (the pots it is paid
    from, which must be final before it draws on them).

    The cash comes out of the household's liquid non-registered savings
    first, then out of the SM investment itself -- selling units to pay the
    interest on the loan that bought them, which is exactly the position an
    over-levered Smith-Manoeuvre household is in.

    That servicing cost is REAL money leaving the balance sheet, and booking
    it is a large part of what stops "borrow the maximum" from looking free:
    before #681 the interest simply inflated the debt, forever, past a charge
    no lender would have granted. What cannot be funded from either pot is
    recorded on ``heloc_interest_unfunded`` -- a household that cannot pay
    the interest on its own facility, reported rather than silently absorbed
    (DP#32).

    Issue #1034: selling either pot to service interest is a real DISPOSITION
    -- it realizes a capital gain (taxed) and reduces the cost basis
    PROPORTIONALLY to the units sold (not clamped to the remaining FMV). Both
    legs (non-reg first, then SM) reuse ``price_sm_unwind`` -- the one spelling
    of the sell-to-raise-net-after-tax arithmetic (DP#9) -- with
    ``sm_heloc=0`` because this sale SERVICES INTEREST (a cash outflow), it
    does not repay HELOC principal. The sale is GROSSED UP so the after-tax
    proceeds cover the interest (the tax is funded from the proceeds, so the
    pot shrinks by MORE than the cash delivered). The gain's taxable slice
    stacks on the year's taxable income for the year-end AMT comparison
    (apply_amt reads ``heloc_servicing_realized_gain`` / ``heloc_servicing_taxable``);
    the tax itself is paid once here (via the gross-up), never re-charged.
    """
    unfunded = ws.margin_heloc_interest_serviced
    if unfunded <= 0:
        ws.heloc_interest_unfunded = 0.0
        ws.heloc_servicing_funded = 0.0
        return False

    # The household's already-recognized taxable income this year: employment
    # (accumulation) + CPP/pension + the drawdown's taxable slice (retirement).
    # This is the SAME base sm_unwind stacks on (``drawdown_other_taxable_income
    # + drawdown_taxable``), extended with employment income for the
    # accumulation phase where this rule fires (sm_unwind runs in retirement,
    # where employment is zero and the two expressions coincide). One spelling
    # of the stacking base for both legs.
    other_income = (ctx.primary_taxable_income + ctx.spouse_taxable_income
                    + ws.drawdown_other_taxable_income + ws.drawdown_taxable)

    def _service_from_pot(fmv_before: float, acb_before: float,
                          net_need: float):
        """Sell ``net_need`` cash out of one capital pot, realizing and taxing
        the gain, reducing ACB proportionally. Returns
        ``(new_fmv, new_acb, realized_gain, tax, net_delivered)``.

        Reuses ``price_sm_unwind`` (DP#9 -- one spelling of the disposition
        arithmetic). Raises if ``ctx.year_brackets`` is None -- a capital gain
        is being priced and a silent 0% flat fallback (DP#32 fabricated zero)
        is refused, mirroring ``property_disposition``'s guard."""
        # Caller invariant: ``net_need > 0`` and ``fmv_before > 0`` (each leg
        # guards ``if from_X > 0.0`` before calling), so price_sm_unwind is
        # reached with a real pot to sell.
        # D6: refuse a silent 0% flat fallback (DP#32 fabricated zero) when
        # ``ctx.year_brackets`` is None, mirroring ``property_disposition``'s
        # guard. Gated on ``fmv_before > acb_before`` because a no-gain
        # disposition (ACB == FMV, e.g. a freshly-funded pot) has nothing to
        # under-tax -- the tax is legitimately 0 there.
        if ctx.year_brackets is None and fmv_before > acb_before:
            raise ValueError(
                "heloc_interest_servicing rule needs ctx.year_brackets to "
                "price the capital gain on a forced disposition; got None (the "
                "prologue resolves these for the marginal rates -- a direct "
                "caller must pass them too).")
        from countries.canada.retirement_transition import price_sm_unwind
        unwind = price_sm_unwind(
            net_need=net_need,
            sm_fmv=fmv_before,
            sm_acb=acb_before,
            sm_heloc=0.0,
            brackets=ctx.year_brackets,
            other_income=other_income,
            inclusion_rate=ctx.config.capital_gains_inclusion,
        )
        f = (unwind.gross_sold / fmv_before if fmv_before > 0 else 0.0)
        new_fmv = max(0.0, fmv_before - unwind.gross_sold)
        new_acb = max(0.0, acb_before - f * acb_before)
        return (new_fmv, new_acb, unwind.realized_gain, unwind.tax,
                unwind.net_delivered)

    inclusion = ctx.config.capital_gains_inclusion

    # Non-reg leg (drains FIRST). Pre-#1034 this clamped ACB (``min(acb, fmv)``)
    # and recognized no gain -- the identical bug the SM leg had. Fixed the
    # same way: realize and tax the gain, reduce ACB proportionally.
    from_nonreg = min(unfunded, max(0.0, ws.new_nonreg_bal))
    if from_nonreg > 0.0:
        ws.new_nonreg_bal, ws.new_nonreg_acb, gain, tax, delivered = (
            _service_from_pot(ws.new_nonreg_bal, ws.new_nonreg_acb, from_nonreg))
        unfunded -= delivered
        ws.heloc_servicing_funded += delivered
        ws.heloc_servicing_realized_gain += gain
        ws.heloc_servicing_tax += tax
        ws.heloc_servicing_taxable += gain * inclusion

    # SM leg (drains SECOND).
    from_sm = min(unfunded, max(0.0, ws.new_sm_investment))
    if from_sm > 0.0:
        ws.new_sm_investment, ws.new_sm_cost_basis, gain, tax, delivered = (
            _service_from_pot(ws.new_sm_investment, ws.new_sm_cost_basis,
                              from_sm))
        unfunded -= delivered
        ws.heloc_servicing_funded += delivered
        ws.heloc_servicing_realized_gain += gain
        ws.heloc_servicing_tax += tax
        ws.heloc_servicing_taxable += gain * inclusion

    ws.heloc_interest_unfunded = max(0.0, unfunded)
    return True


@rule('rrsp_refund_heloc_paydown')
def apply_rrsp_refund_heloc_paydown(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Apply this year's RRSP tax refund to pay down the personal-draw
    HELOC margin. Depends on ``rrsp_deduction`` (the refund amount) and
    ``margin_heloc_interest`` (the balance to pay down).
    """
    if not ctx.deduct_later:
        rrsp_refund = (ws.p_rrsp_actual + ws.s_rrsp_actual) * ctx.primary_marginal_rate + ws.sp_rrsp_actual * ctx.spouse_marginal_rate
    else:
        rrsp_refund = ws.rrsp_deduction_savings + ws.spouse_deduction_savings
    ws.rrsp_refund = rrsp_refund

    heloc_paydown = 0.0
    # Issue #1040: a borrow_to_invest option declared hold_draw=true opts its
    # draw OUT of this sweep (SimulationConfig.hold_borrow_to_invest_draw,
    # set per exploration cell by optimize.run_borrow_to_invest_exploration).
    # The drawn balance is NOT reduced by the refund -- the refund stays in
    # the household's cash and flows to the usual allocation instead -- while
    # apply_margin_heloc_interest still prices/capitalizes (or cash-services,
    # per capitalize_interest) the interest exactly as before. Default False
    # = the pre-#1040 debt-sweep behaviour, byte-identical (DP#32: absence is
    # the fallback, never a coercion).
    if (rrsp_refund > 0 and ws.new_heloc_balance > 0
            and not ctx.config.hold_borrow_to_invest_draw):
        heloc_paydown = min(rrsp_refund, ws.new_heloc_balance)
        ws.new_heloc_balance -= heloc_paydown
    ws.heloc_paydown = heloc_paydown
    ws.new_heloc_rrsp_paydown = ws.opening_heloc_rrsp_paydown + heloc_paydown
    return heloc_paydown > 0


@rule('fhsa')
def apply_fhsa(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """FHSA contribution (clamped to room and lifetime limit) + growth, and
    this year's annual room addition (issue #124, DP#20). Independent of
    every rule above.

    Issue #912: the FHSA grows at the same per-pot rate machinery the rrsp/tfsa
    pots use (``_blended_pot_rate``) so a declared foreign-equity composition
    drags its return via #641's ``registered_wht_drag`` -- an FHSA has no US-
    treaty exemption, so its foreign holdings leak like a TFSA's. Absent a
    declared fhsa composition (and override/fee) the blended rate IS the flat
    ``ctx.investment_return`` (golden no-op, DP#32).
    """
    from simulation_state import _canada_fhsa_limits

    fhsa_lifetime_remaining = max(0, ws.opening_fhsa_lifetime_limit - ws.opening_fhsa_lifetime_used)
    fhsa_actual = min(ctx.fhsa_contribution, max(0, ws.opening_fhsa_room), fhsa_lifetime_remaining)
    fhsa_pre_growth = ws.opening_fhsa_balance + fhsa_actual
    fhsa_rate = _blended_pot_rate(ctx, 'fhsa', fhsa_pre_growth)
    new_fhsa_bal = fhsa_pre_growth * (1 + fhsa_rate)
    new_fhsa_room = max(0, ws.opening_fhsa_room - fhsa_actual)
    new_fhsa_lifetime_used = ws.opening_fhsa_lifetime_used + fhsa_actual

    if ctx.fhsa_annual_limit is not None:
        capped_prior_room = min(new_fhsa_room, _canada_fhsa_limits()[1])
        new_fhsa_room = capped_prior_room + ctx.fhsa_annual_limit

    ws.fhsa_lifetime_remaining = fhsa_lifetime_remaining
    ws.fhsa_actual = fhsa_actual
    # Issue #893: what slot 0 could not absorb is the budget available to the
    # further owners' FHSAs (distributed in rebuild_adult_fhsa). ctx.fhsa_-
    # contribution is now sized against the TOTAL household FHSA room.
    ws.fhsa_overflow = max(0.0, ctx.fhsa_contribution - fhsa_actual)
    ws.new_fhsa_bal = new_fhsa_bal
    ws.new_fhsa_room = new_fhsa_room
    ws.new_fhsa_lifetime_used = new_fhsa_lifetime_used
    return fhsa_actual > 0


@rule('contribution_room')
def apply_contribution_room(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Add this year's annual RRSP/TFSA room (DP#20: year-specific limits
    from the tax data provider). Depends on ``contributions`` for the
    post-contribution room to add onto.

    Issue #674 (ITA s.146(1)): RRSP room accrues on EARNED income, not on
    every taxable dollar -- an Employment Insurance benefit is taxable but
    is NOT earned income, so it must add $0 room. ``simulation.py``'s
    ``_income_components_for_year`` computes ``_primary_earned_income``/
    ``_spouse_earned_income`` (kind-filtered) alongside the taxable
    ``_primary_income``/``_spouse_income`` and every production allocations-
    dict site sets both. The fallback to the taxable total below only fires
    when ``_primary_earned_income`` is truly ABSENT (a caller -- e.g. a unit
    test exercising this rule in isolation -- built an allocations dict that
    never tracked the earned/taxable split at all); an absent key means
    "unknown", not "zero" (DP#32), so it falls back to the pre-#674 taxable-
    income behaviour rather than silently crediting $0 room to a caller that
    was never asked about kinds.
    """
    rrsp_limit = ctx.rrsp_annual_limit if ctx.rrsp_annual_limit is not None else ctx.config.rrsp_annual_max
    tfsa_limit = ctx.tfsa_annual_limit if ctx.tfsa_annual_limit is not None else ctx.config.tfsa_annual_room_per_person
    primary_earned = ctx.allocations.get('_primary_earned_income')
    if primary_earned is None:
        primary_earned = ctx.allocations.get('_primary_income', 130000)
    spouse_earned = ctx.allocations.get('_spouse_earned_income')
    if spouse_earned is None:
        spouse_earned = ctx.allocations.get('_spouse_income', 50000)
    primary_room_added = min(rrsp_limit, ctx.config.rrsp_annual_percent * primary_earned)
    spouse_room_added = min(rrsp_limit, ctx.config.rrsp_annual_percent * spouse_earned)

    ws.new_rrsp_room += primary_room_added
    ws.new_spouse_rrsp_room += spouse_room_added
    ws.new_tfsa_p_room += tfsa_limit
    ws.new_tfsa_sp_room += tfsa_limit
    return (primary_room_added + spouse_room_added + tfsa_limit + tfsa_limit) > 0


@rule('retirement_drawdown')
def apply_retirement_drawdown(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Draw from registered/non-reg accounts (per ``drawdown_order``) to
    cover the NET spending need not met by CPP/OAS/pension
    (issues #294/#363/#579). Depends on every growth/contribution rule
    above -- it prices against POST-growth balances.
    """
    # Issue #1033: route the s.20(1)(c) investment-interest deduction THROUGH
    # the OAS-clawback base and the draw's progressive base. Placed BEFORE the
    # ``drawdown_net_target <= 0`` early return below (Blocker 2: a retiree
    # whose spending is covered without a discretionary draw still has the
    # forced RRIF minimum booking recovery tax in ``apply_rrif_minimum``, which
    # reads ``drawdown_other_taxable_income_primary`` -- so the base reduction
    # must fire even when no discretionary draw is taken). Gated on
    # ``ctx.primary_retired`` (the deduction is the PRIMARY's; Canada has no
    # joint filing) so the primary's deduction never subtracts from the
    # spouse's base in a mixed-phase year (primary working, spouse retired).
    #
    # Major 3: ``sm_interest_deduction`` is the FEDERAL ``total_deductible``
    # (uncapped), not the Quebec-capped ``qc_deductible`` -- the OAS recovery
    # tax is federal and the federal s.20(1)(c) deduction has no investment-
    # income limit. Major 4: the base is FLOORED at 0 -- a deduction larger
    # than the base is a non-capital loss / carry-forward (not modeled here,
    # follow-up), not a silently-forfeited negative base charged at the bottom
    # bracket. Moderate 5: the bracket-fill ceiling sits on the OAS-inclusive
    # base (other_taxable + oas), so it is recomputed consistently from the
    # reduced base + oas (and the ceiling re-derived via ``bracket_ceiling``),
    # so an ``rrsp_bracket_fill`` household prices its draw on the lowered base
    # AND computes headroom on the lowered base -- no D-inconsistency.
    #
    # Exactly one mechanism fires per phase: in retirement the side-credit in
    # ``apply_sm_interest`` is zeroed (its gate), so the routing is the sole
    # capture (clawback relief + draw re-bracketing, both on the balance sheet
    # via ``ws.oas_income`` / retained assets); in accumulation the routing is
    # a no-op (primary not retired -> gate off) and the side-credit handles it.
    # Issue #1083 (the #1033 over-correction): the routing now reaches the
    # PRIMARY's OTHER retirement taxable income too. #1033's known limit (c) --
    # the deduction never offset the rental/loan slice the prologue's
    # ``_income_tax_by_adult`` taxes (employment is zeroed at retirement;
    # rental operating + private-loan interest income survive it), and where
    # the base floored at 0 the whole deduction was stranded -- is fixed here:
    # the share of the deduction the cpp+pension base cannot absorb (the
    # remainder after the floor) is routed against ``ctx.primary_taxable_
    # income`` and its statutory saving booked via
    # ``ws.sm_interest_nondrawdown_tax_saving`` -> ``apply_solvency`` (the
    # tuition_credit precedent: real cash, not an objective side-credit -- the
    # flat side-credit stays gated OFF in retirement, so #1033's double-count
    # does not return). The split is disjoint by construction (absorbed +
    # remainder == D), so no dollar of the deduction is captured twice.
    # Known limits, NOT fixed here: (a) the PRELIMINARY OAS clawback
    # ``member_retirement_income`` books in ``retirement_income`` (BEFORE
    # ``sm_interest`` in RULE_ORDER) on the un-reduced CPP+pension base, so it
    # does not see this deduction; (b) ``drawdown_net_target`` was sized in
    # ``retirement_income`` against the un-reduced base, so the deduction's
    # cash benefit (lower tax -> smaller shortfall) is not fed back into the
    # shortfall; (c) the QC-CAPPED slice (``qc_deductible``) and its
    # carry-forward are still worth $0 once the primary retires -- the
    # provincial cap's release is #1035, out of scope here; (d) the leveraged
    # portfolio's DISTRIBUTED income still never ENTERS the base (the deferred
    # income-flowing half).
    if ctx.primary_retired and ws.sm_interest_deduction > 0.0:
        _D = ws.sm_interest_deduction
        from tax_calculator import bracket_ceiling
        # Issue #1083: how much of D the PRIMARY's cpp+pension base can absorb
        # (read BEFORE the floor below). The excess is the retiree's
        # rental/loan slice's share -- routed further down.
        _base_primary_pre = ws.drawdown_other_taxable_income_primary
        _absorbed = min(_D, _base_primary_pre)
        _remainder = _D - _absorbed
        ws.drawdown_other_taxable_income = max(
            0.0, ws.drawdown_other_taxable_income - _D)
        ws.drawdown_other_taxable_income_primary = max(
            0.0, ws.drawdown_other_taxable_income_primary - _D)
        # Recompute the OAS-inclusive bracket-fill base consistently from the
        # reduced base (the headroom is bracket_target - bracket_fill_base, so a
        # lower base grows the room before the ceiling -- the deduction frees
        # bracket-fill room, correctly). The base is a float (never None) where
        # the routing fires (retirement_income, which runs first, sets it).
        ws.drawdown_bracket_fill_base = (
            ws.drawdown_other_taxable_income + ws.drawdown_oas_gross)
        ws.drawdown_bracket_fill_base_primary = (
            ws.drawdown_other_taxable_income_primary
            + ws.drawdown_oas_gross_primary)
        # NEW-1: re-derive the ceiling ONLY when it was auto-derived. An
        # EXPLICIT retirement.bracket_fill_target (DP#13) is a fixed dollar
        # ceiling the household declared -- overwriting it with a re-derived
        # bracket_ceiling silently discards the election (the reviewer captured
        # a declared $40,000 overridden to $54,345, a 3.8x over-draw). The base
        # still drops (headroom = explicit - reduced_base grows by D), which is
        # the deduction's correct effect on a fixed ceiling.
        if ctx.year_brackets is not None and not ws.drawdown_bracket_target_explicit:
            ws.drawdown_bracket_target = bracket_ceiling(
                ws.drawdown_bracket_fill_base, ctx.year_brackets)
            ws.drawdown_bracket_target_primary = bracket_ceiling(
                ws.drawdown_bracket_fill_base_primary, ctx.year_brackets)
        # Issue #1083: route the REMAINDER against the primary's prologue-taxed
        # slice. ``ctx.primary_taxable_income`` is exactly the base the
        # prologue's ``_income_tax_by_adult`` taxed (employment zeroed at
        # retirement; rental operating + private-loan interest income survive),
        # and its tax already sits inside ``ctx.after_tax_income`` -- so the
        # statutory saving is booked as cash by ``apply_solvency``, the same
        # path the tuition_credit rule's reduction takes (one booking spelling,
        # DP#9). Valued at bracket-fill via ``deduction_value`` -- the SAME
        # ``tax_on_income`` / year-brackets path that taxed the slice, so a
        # deduction crossing a bracket boundary is worth its actual marginal
        # dollars, never a flat top rate (the pre-#1033 mechanism that is NOT
        # returning here: the objective's side-credit fields stay gated off).
        # Disjoint from the drawdown-base capture by construction
        # (``_absorbed + _remainder == _D``): no dollar offsets two incomes.
        # A remainder beyond this slice is a non-capital loss / carry-forward
        # (not modeled -- same follow-up family as Major 4's floor).
        if _remainder > 0.0 and ctx.primary_taxable_income > 0.0:
            from tax_calculator import deduction_value
            if ctx.year_brackets is not None:
                ws.sm_interest_nondrawdown_tax_saving = deduction_value(
                    ctx.primary_taxable_income, _remainder, ctx.year_brackets)
            else:
                # Direct unit-test callers without brackets keep the flat-rate
                # valuation, byte-for-byte the side-credit's own fallback
                # pattern in ``apply_sm_interest`` (the live fold always
                # passes ``year_brackets``).
                ws.sm_interest_nondrawdown_tax_saving = (
                    _remainder * ctx.primary_marginal_rate)
    if ws.drawdown_net_target <= 0:
        return False

    from countries.canada.retirement_transition import plan_drawdown_net

    draw_canada = {
        'tfsa_primary_balance': ws.new_tfsa_p_bal,
        'tfsa_spouse_balance': ws.new_tfsa_sp_bal,
        'rrsp_balance': ws.new_rrsp_bal,
        'spousal_rrsp_balance': ws.new_spousal_rrsp_bal,
        'spouse_rrsp_balance': ws.new_spouse_rrsp_bal,
        'lif_balance': ws.new_lif_balance,
        'lira_balance': ws.new_lira_balance,
        'fhsa_balance': ws.new_fhsa_bal,
    }
    order = ws.drawdown_order or ['tfsa', 'non_reg', 'rrsp']
    # Issue #1009: liquidate-to-target residual sweep. When the household opts
    # into the die-with-(near)-zero mode, append every drawable FINANCIAL token
    # the configured ``order`` did not already name, so the drawdown liquidates
    # ALL drawable savings (across the accounts the two-slot WorkingState
    # prices: both spouses' TFSA/RRSP, the household non-reg, the slot-0
    # FHSA/LIF) to meet the net target before a shortfall is reported -- rather
    # than delivering $0 while a LIF/FHSA the configured order never named sits
    # and compounds. The configured priority is preserved (the tail runs AFTER
    # the user's tokens); only MISSING tokens are appended.
    #
    # 'lira' is deliberately EXCLUDED from the tail: a LIRA is statutorily
    # locked until it converts to a LIF (age 71 / elected earlier), so drawing
    # it pre-conversion would be a statutory over-draw. Post-conversion its
    # balance is 0 anyway, and the LIF (the decumulation vehicle) IS in the tail.
    # If the configured order uses the capped 'rrsp_bracket_fill' token, the
    # uncapped 'rrsp' token is appended after it -- the bracket-fill priority
    # is preserved (the RRSP fills the chosen bracket first), and liquidate-
    # to-target then draws the RRSP remainder, which is exactly what the
    # die-with-zero mode asked for (the two tokens map to the same balance
    # keys, and the second draw sees the balance already reduced by the first,
    # so there is no double-count).
    # The principal residence is NOT a drawable financial account and is absent
    # from the tail by construction (it is not a _DRAWDOWN_SOURCES token); the
    # spouse's slot-1 LIRA/LIF/FHSA are not yet read into this two-slot
    # WorkingState (#901 follow-up).
    #
    # LIF statutory MAXIMUM (#1002): the residual tail appends 'lif' to the
    # order, so #1002's ``lif_discretionary_ceiling`` block below -- which caps
    # the 'lif' token at ``max(0, ws.lif_maximum_withdrawal - ws.lif_withdrawal)``
    # whenever 'lif' is in the order -- AUTOMATICALLY caps the residual LIF draw
    # too. There is ONE LIF-ceiling mechanism (#1002's), reused by the residual
    # sweep by construction; no second spelling (DP#9), no double-cap. Quebec
    # 55+ has no maximum (lif_maximum_withdrawal returns the full balance), so
    # the residual sweep can liquidate the whole LIF; federal respects the
    # factor ceiling and the LIF drains over years instead of in one.
    if ws.liquidate_to_target:
        _RESIDUAL_TAIL = ('tfsa', 'non_reg', 'rrsp', 'fhsa', 'lif')
        _configured = set(order)
        _tail = [t for t in _RESIDUAL_TAIL if t not in _configured]
        if _tail:
            order = list(order) + _tail
    # Issue #363 PR 4: when both spouses are retired, split the taxable draw
    # across their two SEPARATE bracket sets (each spouse's RRSP/RRIF priced
    # against — and clawing back OAS from — that spouse's own income). The net
    # target stays the single pooled `drawdown_net_target` (money conservation
    # unchanged). In a one-retiree year `per_member` stays None and the household
    # schedule prices the whole draw exactly as pre-PR-4.
    per_member = None
    if ws.drawdown_two_member_split:
        per_member = {
            'primary': {
                'other_taxable_income': ws.drawdown_other_taxable_income_primary,
                'oas_gross': ws.drawdown_oas_gross_primary,
                'bracket_target': ws.drawdown_bracket_target_primary,
                'bracket_fill_base': ws.drawdown_bracket_fill_base_primary,
            },
            'spouse': {
                'other_taxable_income': ws.drawdown_other_taxable_income_spouse,
                'oas_gross': ws.drawdown_oas_gross_spouse,
                'bracket_target': ws.drawdown_bracket_target_spouse,
                'bracket_fill_base': ws.drawdown_bracket_fill_base_spouse,
            },
        }
    # Issue #1002: the LIF statutory maximum caps the DISCRETIONARY LIF draw
    # so the forced minimum (apply_lira_lif, already taken and recorded on
    # ws.lif_withdrawal) + this discretionary draw never exceed the annual
    # statutory ceiling (ws.lif_maximum_withdrawal, computed on the opening/
    # converted LIF balance by apply_lira_lif). The ceiling is the RESIDUAL
    # room after the forced slice; ``lif_maximum_withdrawal`` is 0.0 in a year
    # with no LIF activity, so the cap binds at 0 and any 'lif' draw falls
    # through to the next source -- a hard zero, not a fallback (DP#32). Only
    # passed when the drawdown order actually contains the 'lif' token, so a
    # household with no LIF in its order (the default/golden path) never opts
    # into the cap (None disables, DP#13) and the path is byte-identical to
    # pre-#1002.
    lif_discretionary_ceiling = None
    if 'lif' in order:
        lif_discretionary_ceiling = max(
            0.0, ws.lif_maximum_withdrawal - ws.lif_withdrawal)
    plan = plan_drawdown_net(
        ws.drawdown_net_target, order, draw_canada, ws.new_nonreg_bal,
        ws.new_nonreg_acb, ws.retiree_marginal_rate,
        bracket_target=ws.drawdown_bracket_target,
        other_taxable_income=ws.drawdown_other_taxable_income,
        brackets=ctx.year_brackets,
        bracket_fill_base=ws.drawdown_bracket_fill_base,
        oas_gross=ws.drawdown_oas_gross,
        oas_clawback_threshold=ws.drawdown_oas_threshold,
        per_member=per_member,
        lif_max_withdrawal=lif_discretionary_ceiling)

    ws.drawdown_total = plan.total_withdrawn
    ws.drawdown_taxable = plan.taxable_withdrawn
    ws.drawdown_net_delivered = plan.net_delivered
    # Issue #754: surface the raw realized capital gain the non-reg disposition
    # crystallized (proceeds - ACB, 100%). The taxable slice of it is already in
    # plan.taxable_withdrawn at the cg_inclusion rate; this is the pre-inclusion
    # figure the year-end AMT base reads.
    ws.drawdown_realized_capital_gain = plan.realized_capital_gain

    # Issue #825: the per-spouse taxable draw recognized this year, so the
    # forced RRIF minimum (apply_rrif_minimum, later in the fold) can re-bracket
    # its own forced slice on TOP of it (per spouse) instead of on a flat
    # placeholder. In the per-member split each owner's taxable is explicit; in
    # the single 'household' schedule the whole taxable draw belongs to the sole
    # retiree, so attribute it to whichever member is retired.
    tbo = plan.taxable_by_owner or {}
    if 'household' in tbo:
        if ctx.spouse_retired and not ctx.primary_retired:
            ws.drawdown_taxable_spouse = tbo['household']
        else:
            ws.drawdown_taxable_primary = tbo['household']
    else:
        ws.drawdown_taxable_primary = tbo.get('primary', 0.0)
        ws.drawdown_taxable_spouse = tbo.get('spouse', 0.0)

    # Issue #363 PR 2: book the OAS clawback the taxable draw triggered. The
    # draw grossed up to REPLACE the clawed OAS (plan.net_delivered already
    # includes it), so reduce booked OAS income by the recovery tax and raise
    # the net target by the same amount. This is SINGLE-COUNT and cash-flow
    # neutral in the solvency identity (apply_solvency): available adds
    # drawdown_net_delivered (+clawback) and oas_income (-clawback), which
    # cancel — the real effect is the larger gross leaving the RRSP (money
    # conservation: the extra gross is drawn and remitted, and target ==
    # delivered so check_drawdown_meets_net_target still holds to the dollar).
    if plan.oas_clawback > 0:
        ws.oas_income -= plan.oas_clawback
        ws.drawdown_net_target += plan.oas_clawback

    for key, delta in plan.balance_deltas.items():
        if key == 'non_reg_balance':
            ws.new_nonreg_bal += delta
            if ws.new_nonreg_bal >= 0 and ws.new_nonreg_acb > 0:
                ws.new_nonreg_acb = max(0.0, ws.new_nonreg_acb + delta)
        elif key == 'tfsa_primary_balance':
            ws.new_tfsa_p_bal += delta
        elif key == 'tfsa_spouse_balance':
            ws.new_tfsa_sp_bal += delta
        elif key == 'rrsp_balance':
            ws.new_rrsp_bal += delta
            ws.spend_draw_primary_rrsp += -delta
        elif key == 'spousal_rrsp_balance':
            ws.new_spousal_rrsp_bal += delta
            ws.spend_draw_spouse_rrsp += -delta
        elif key == 'spouse_rrsp_balance':
            ws.new_spouse_rrsp_bal += delta
            ws.spend_draw_spouse_rrsp += -delta
        elif key == 'lif_balance':
            ws.new_lif_balance += delta
        elif key == 'lira_balance':
            ws.new_lira_balance += delta
        elif key == 'fhsa_balance':
            ws.new_fhsa_bal += delta

    # Issue #707: surface the decumulation shortfall -- AFTER the deltas are
    # applied, so `remaining` is the true post-drawdown balance. The drawdown
    # plan drew against every account in `order`; if it still delivered less
    # NET than the target, that is only "correct, accounts-empty behaviour"
    # (per check_drawdown_meets_net_target) when nothing was left to draw --
    # and in that case the gap MUST be recorded on the year, not silently
    # swallowed. A year where delivered < target but a meaningful balance
    # remained is a drawdown-sizing bug (the existing invariant flags it);
    # this field is the OTHER branch: accounts exhausted, gap unfunded.
    if ws.drawdown_net_target > 0:
        _gap = ws.drawdown_net_target - ws.drawdown_net_delivered
        if _gap > 1.0:
            _remaining = (
                ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal
                + ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal
                + ws.new_nonreg_bal + ws.new_lif_balance + ws.new_lira_balance
                + ws.new_fhsa_bal
            )
            if _remaining <= 1.0:
                ws.drawdown_shortfall = _gap

    return plan.total_withdrawn > 0


@rule('rrif_minimum')
def apply_rrif_minimum(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Force out the mandatory RRIF minimum (CRA T4040 age factor x the
    OPENING Jan-1 balance) beyond whatever the spending drawdown already
    took. Depends on ``retirement_drawdown`` (spend_draw_*) and every
    growth rule (the forced excess is reinvested into the grown non-reg
    balance) -- runs last because it needs both.
    """
    forced_primary = 0.0
    forced_spouse = 0.0
    if ws.rrif_min_rate_primary > 0 and ws.new_rrsp_bal > 0:
        min_primary = ws.opening_rrsp_balance * ws.rrif_min_rate_primary
        forced = max(0.0, min_primary - ws.spend_draw_primary_rrsp)
        forced = min(forced, ws.new_rrsp_bal)
        ws.new_rrsp_bal -= forced
        forced_primary += forced
    if ws.rrif_min_rate_spouse > 0:
        min_spouse = (ws.opening_spouse_rrsp_balance + ws.opening_spousal_rrsp_balance) * ws.rrif_min_rate_spouse
        forced = max(0.0, min_spouse - ws.spend_draw_spouse_rrsp)
        take_spouse = min(forced, ws.new_spouse_rrsp_bal)
        ws.new_spouse_rrsp_bal -= take_spouse
        take_spousal = min(forced - take_spouse, ws.new_spousal_rrsp_bal)
        ws.new_spousal_rrsp_bal -= take_spousal
        forced_spouse += take_spouse + take_spousal

    forced_rrif_total = forced_primary + forced_spouse
    ws.forced_rrif_total = forced_rrif_total
    if forced_rrif_total <= 0:
        return False

    ws.drawdown_total += forced_rrif_total
    ws.drawdown_taxable += forced_rrif_total

    # Issue #825: price the tax on the forced RRIF minimum through the SAME
    # progressive re-bracketing (#363 PR 1) + OAS-clawback (#363 PR 2) machinery
    # the discretionary drawdown uses, per spouse (#363 PR 4) — not the old flat
    # placeholder rate that skipped the clawback. Each spouse's forced slice
    # re-brackets on top of that spouse's own already-recognized income (CPP /
    # pension + the discretionary taxable draw), and books the INCREMENTAL OAS
    # recovery tax it triggers as reduced OAS income (mirroring the discretionary
    # path's `ws.oas_income -= plan.oas_clawback`). The after-income-tax proceeds
    # are reinvested in non-reg; the clawback is a real reduction in OAS income.
    from countries.canada.retirement_transition import price_forced_rrif_tax
    tax_p, claw_p = price_forced_rrif_tax(
        other_taxable_income=ws.drawdown_other_taxable_income_primary,
        oas_gross=ws.drawdown_oas_gross_primary,
        prior_taxable_draw=ws.drawdown_taxable_primary,
        forced_taxable=forced_primary,
        brackets=ctx.year_brackets,
        oas_clawback_threshold=ws.drawdown_oas_threshold,
        flat_rate=ws.retiree_marginal_rate)
    tax_s, claw_s = price_forced_rrif_tax(
        other_taxable_income=ws.drawdown_other_taxable_income_spouse,
        oas_gross=ws.drawdown_oas_gross_spouse,
        prior_taxable_draw=ws.drawdown_taxable_spouse,
        forced_taxable=forced_spouse,
        brackets=ctx.year_brackets,
        oas_clawback_threshold=ws.drawdown_oas_threshold,
        flat_rate=ws.retiree_marginal_rate)

    income_tax = tax_p + tax_s
    oas_clawback = claw_p + claw_s
    after_tax_cash = max(0.0, forced_rrif_total - income_tax)
    # Issue #1001: the after-tax cash funds the net spending need FIRST (up to
    # the pre-RRIF shortfall priced in apply_retirement_income), and only the
    # EXCESS is reinvested into non-reg. The spending-funded slice flows to the
    # cash-flow identity via ws.rrif_after_tax_to_spending (added to solvency
    # `available`), replacing the discretionary TFSA draw that used to fund it.
    # Money is conserved: the RRIF gross leaves the RRSP, the income tax leaves
    # the household, the spending slice is consumed by spending, and the excess
    # reinvests into non-reg — the discretionary draw is smaller by the spending
    # slice, so TFSA stays higher (the wealth gain the fix produces).
    to_spending = min(after_tax_cash, ws.drawdown_net_target_pre_rrif)
    reinvest = after_tax_cash - to_spending
    ws.rrif_after_tax_to_spending = to_spending
    ws.new_nonreg_bal += reinvest
    ws.new_nonreg_acb += reinvest
    if oas_clawback > 0:
        ws.oas_income -= oas_clawback
    return True


@rule('sm_unwind')
def apply_sm_unwind(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #1017: under ``liquidate_to_target``, UNWIND the Smith-Manoeuvre
    sleeve to fund the spending shortfall the ordinary financial drawdown
    (``retirement_drawdown``) and the forced RRIF minimum (``rrif_minimum``)
    could not cover.

    The SM sleeve is a leveraged non-reg portfolio that compounds untouched by
    the ordinary drawdown (it is not a ``_DRAWDOWN_SOURCES`` token), so on a
    leveraged household the die-with-zero mode delivered $0/yr for ~15 years
    while a $5M SM portfolio compounded and a $520k HELOC rode to death. This
    rule closes the leverage: sell a slice of the sleeve, realize the capital
    gain (taxed through the progressive brackets), repay the SM HELOC
    proportionally from the proceeds, and deliver the NET to the spending
    target. Ordered AFTER ``rrif_minimum`` (so the shortfall reflects the true
    gap once every financial account + the forced minimum have fired) and
    BEFORE ``solvency`` (so the net it delivers counts in the cash-flow
    identity and the waterfall does not force-liquidate for a gap the unwind
    already filled).

    Gated by ``ws.liquidate_to_target`` (the opt-in die-with-zero mode): absent
    or False -> strict no-op -> byte-identical (DP#32). A household with no SM
    sleeve (``new_sm_investment`` == 0) -> nothing to unwind -> no-op. The SM
    gain is priced stacking on the household's already-recognized taxable
    income (``drawdown_other_taxable_income + drawdown_taxable`` -- the
    discretionary draw + forced RRIF + CPP/pension); the OAS clawback the SM
    gain could trigger is NOT modelled here (a noted approximation -- the
    deep-retirement unwind typically lands above the clawback threshold
    already, and the discretionary drawdown's clawback machinery is the one
    spelling of that tax, not duplicated here, DP#9).

    Money conservation (DP#18): the SM balance drops by ``gross_sold``, the
    HELOC drops by ``heloc_repaid``, the tax leaves the household, and
    ``net_delivered`` (``gross_sold - tax - heloc_repaid``) flows to spending
    via ``drawdown_net_delivered`` -- so the balance sheet's net assets drop by
    exactly ``tax + net_delivered`` (the HELOC repayment is an asset-for-debt
    swap within the balance sheet, net-zero on net assets). The proportional
    HELOC repayment couples the asset and its financing so both drain to zero
    together as the sleeve unwinds (terminal SM == 0 -> terminal HELOC == 0).
    """
    if not ws.liquidate_to_target:
        return False
    if ws.new_sm_investment <= 0.0:
        return False
    shortfall = ws.drawdown_net_target - ws.drawdown_net_delivered
    if shortfall <= 1.0:
        return False

    from countries.canada.retirement_transition import price_sm_unwind
    # The household's already-recognized taxable income this year: the
    # discretionary drawdown's taxable slice + the forced RRIF minimum +
    # CPP/pension (all household totals). The SM capital gain stacks on top so
    # it is priced at the marginal band it lands in.
    other_income = ws.drawdown_other_taxable_income + ws.drawdown_taxable
    unwind = price_sm_unwind(
        net_need=shortfall,
        sm_fmv=ws.new_sm_investment,
        sm_acb=ws.new_sm_cost_basis,
        sm_heloc=ws.new_sm_heloc,
        brackets=ctx.year_brackets,
        other_income=other_income,
        inclusion_rate=ctx.config.capital_gains_inclusion,
        flat_rate=ws.retiree_marginal_rate,
    )
    # price_sm_unwind always sells a positive gross when called (sm_fmv > 0
    # and net_need > 0 -- the rule's early returns guarantee both), so
    # gross_sold > 0 here by construction; no defensive zero guard (a dead
    # branch would be exactly the uncovered code DP#32 frowns on).

    # Apply the unwind to the SM sleeve + HELOC. The cost basis retires
    # proportionally to the FMV sold (selling fraction f = gross_sold / fmv;
    # the ACB sold is f * acb), so the remaining sleeve's gain fraction is
    # unchanged -- the unwind does not silently crystallize a different gain
    # ratio than the deemed disposition would (DP#19). Floored at zero for
    # floating-point hygiene (a -1e-15 sleeve would be a silent over-sale,
    # not a value) via ``max`` rather than a guard so there is no uncovered
    # dead branch (DP#32).
    f = unwind.gross_sold / ws.new_sm_investment if ws.new_sm_investment > 0 else 0.0
    ws.new_sm_investment = max(0.0, ws.new_sm_investment - unwind.gross_sold)
    ws.new_sm_cost_basis = max(0.0, ws.new_sm_cost_basis - f * ws.new_sm_cost_basis)
    ws.new_sm_heloc = max(0.0, ws.new_sm_heloc - unwind.heloc_repaid)

    # Deliver the net to the spending target via the drawdown channels, so the
    # solvency cash-flow identity (``available`` adds ``drawdown_net_delivered``)
    # sees the unwind's funding and the shortfall #707 surfaces is the TRUE
    # post-unwind gap. The gross proceeds + realized gain are surfaced for the
    # year-end AMT base (#754) and transparency (DP#32).
    ws.drawdown_net_delivered += unwind.net_delivered
    ws.drawdown_total += unwind.gross_sold
    # The taxable slice of the SM disposition is the realized capital gain at
    # the capital-gains inclusion rate (mirroring the non-reg drawdown's
    # ``taxable_withdrawn`` = cg_inclusion x realized gain).
    ws.drawdown_taxable += unwind.realized_gain * ctx.config.capital_gains_inclusion
    ws.drawdown_realized_capital_gain += unwind.realized_gain
    ws.sm_unwind_proceeds = unwind.gross_sold
    ws.sm_unwind_tax = unwind.tax
    ws.sm_unwind_heloc_repaid = unwind.heloc_repaid
    ws.sm_unwind_net_delivered = unwind.net_delivered
    ws.sm_unwind_realized_gain = unwind.realized_gain
    return True


# Issue #956 bite B (sale-core, DP#10/#26): the pure disposition arithmetic a
# declared mid-horizon property SALE settles in its sale year. Kept as a
# standalone helper so the conservation identity (the spec's crux) is directly
# unit-testable: the household's assets drop by EXACTLY the selling friction
# (selling_costs + disposition tax), the equity converted to investable cash
# less the friction that genuinely left the household. Pure (DP#3): a function
# of the property's appreciated gross value, its secured mortgage, its ACB,
# the PRE designation, the selling costs, the owner's taxable income (for
# gain banding), and the year's brackets -- nothing read off ``self``.
#
# The conservation identity (verified by tests/test_issue_956_bite_b_sale_core):
#   P_gross  = value_share * (1+appreciation_rate)^(sale_year - owned_from)
#             (or value_share when no appreciation_rate -- the SAME value
#             simulation_state._property_equity_for_year's appreciation branch
#             computes, so a property sold at its appreciated value realizes
#             exactly the equity it would have contributed had it been held);
#   E       = P_gross - secured_share      (the equity on the balance sheet);
#   T       = capital-gains tax on (P_gross - acb_share), apportioned by the
#             PRE taxable_fraction, banded against the owner's taxable income;
#   P_net   = P_gross - secured_share - selling_costs - T;
#   Δtotal_assets (sell vs hold, in the sale year) = P_net - E = -(selling_costs + T).
# Money is conserved: the equity converted to investable cash, less the
# friction that genuinely left the household (third-party costs + government
# tax). The money-conservation invariant (trajectory_invariants.py) passes.
def _disposition_gain_tax(
        gain: float, sale: Dict[str, Any], cal_year: int, brackets: list,
        primary_taxable_income: float, spouse_taxable_income: float,
) -> float:
    """The PRE-apportioned, per-owner-banded capital-gains tax on a
    disposition's ``gain`` (proceeds - ACB, 100%) -- the SHARED spine both
    ``_property_disposition_for`` (Bite B, non-principal) and
    ``_principal_disposition_for`` (Bite E, principal) compose (DP#9: one
    spelling of the gain-banding math, not two near-clones).

    The gain is apportioned by the PRE ``taxable_fraction`` (ITA s.40(2)(b)):
    a property designated for its whole ownership -> ~0 tax; a property with
    no designation -> fully taxable (``taxable_fraction = 1.0``). The
    designation is capped at the sale year (a property sold mid-horizon cannot
    designate years after its sale). The denominator is the FAMILY window
    (issue #969) -- the span across ALL the couple's designated properties --
    when the sale carries ``family_pre_window`` (a household with >=2
    designated properties), so a second property's gain is priced against the
    same family horizon the estate path uses, not its own span in isolation
    (which over-shelters it). Absent ``family_pre_window`` (the common
    one-property household, or no designation declared anywhere) the
    property's OWN designation span is the denominator -- byte-identical to
    the single-property path (DP#9: one spelling of the family window, shared
    with ``input_contract._family_pre_window``).

    Canada has no joint filing, so a jointly-owned property's gain is split
    per owner (``owner_roles`` -- each role's fraction is its declared % of
    the property; the fractions sum to ``couple_share``, so weighting the
    couple-share gain by ``role_frac/couple_share`` recovers each owner's
    share), and each share bands against THAT owner's taxable income (the
    marginal rate the gain lands in). Reuses
    ``estate.tax_on_capital_gain_at_death`` exactly (DP#9).

    DP#25: ``countries.canada`` is imported lazily inside this helper (this
    module keeps no jurisdiction import at top level).
    """
    if gain <= 0.0:
        return 0.0
    from countries.canada.pre_designation import (
        designated_years, taxable_gain_fraction)
    periods = sale.get('designated_principal_residence_years', [])
    designated = {y for y in designated_years(periods, cal_year) if y <= cal_year}
    designated_count = len(designated)
    if designated_count <= 0:
        taxable_fraction = 1.0
    else:
        # Issue #969: the FAMILY window (span across all the couple's
        # designated properties) is the denominator when the sale carries it
        # -- the same family horizon `input_contract._family_pre_window`
        # computes and the estate path prices against. Absent it (a single-
        # property household, or no designation anywhere) the property's OWN
        # designation span is the denominator -- byte-identical to the prior
        # single-property path (DP#9/DP#32). An explicit `is None` test, never
        # `or` (DP#32: 0 is a real window value the family-window helper only
        # returns when >=2 properties designate, so a 0 here is genuine).
        family_window = sale.get('family_pre_window')
        if family_window is None:
            window = (max(designated) - min(designated) + 1) if designated else 0
        else:
            window = family_window
        taxable_fraction = taxable_gain_fraction(designated_count, window)
    from countries.canada.estate import tax_on_capital_gain_at_death
    owner_roles = sale.get('owner_roles', {})
    couple_share = sum(owner_roles.values())
    if couple_share <= 0.0:
        return 0.0
    disposition_tax = 0.0
    for role, frac in owner_roles.items():
        if frac <= 0.0:
            continue
        # Each owner's share of the couple's gain: frac/couple_share recovers
        # the owner's share of the property (a couple owning 50/50 -> each
        # owner's gain share is half the couple's gain).
        owner_gain_share = gain * (frac / couple_share)
        other_income = (primary_taxable_income if role == 'primary'
                        else spouse_taxable_income)
        disposition_tax += tax_on_capital_gain_at_death(
            fmv=owner_gain_share, acb=0.0, brackets=brackets,
            other_income=other_income, inclusion_rate=0.5,
            taxable_fraction=taxable_fraction)
    return disposition_tax


def _disposition_cca_recapture_tax(
        prop: Dict[str, Any], p_gross: float, cal_year: int, brackets: list,
        primary_taxable_income: float, spouse_taxable_income: float,
        opening_rental_ucc: Dict[str, float]) -> float:
    """The CCA recapture / terminal-loss tax on a sold rental that elected
    CCA (issue #968, ITA s.13(1) / s.20(16)) -- the disposition-side clawback
    the estate path's ``objective._cca_recapture_for`` assumes this rule prices
    (see issue #964: a sold rental is skipped at the deemed disposition BECAUSE
    its recapture was realized here, at the sale).

    A rental that claimed CCA depreciated a declining-balance UCC below its
    capital cost. On sale, proceeds up to the original capital cost that EXCEED
    the remaining UCC are RECAPTURED as ordinary income (100% inclusion -- taxed
    WORSE than the 50%-inclusion capital gain on the same property); the
    symmetric case -- UCC still above the (cost-capped) proceeds -- is a
    TERMINAL LOSS deductible against ordinary income. Recapture and terminal
    loss are mutually exclusive (at most one is non-zero).

    The recapture is ORDINARY income, so -- unlike the capital gain in
    ``_disposition_gain_tax`` (50% inclusion, banded via
    ``tax_on_capital_gain_at_death``) -- it is taxed at each owner's marginal
    rate stacked on top of their EXISTING taxable income (the incremental
    ``tax_on_income(other_income + share) - tax_on_income(other_income)``, the
    same stacking idiom the gain-band helper uses). A terminal loss is the
    mirror: a tax SAVING at the owner's marginal rate (a negative incremental
    tax, floored at zero overall so a loss in a no-tax year yields no refund
    here -- the engine does not model loss carry-back/carry-forward).

    DP#9: reuses ``countries.canada.cca.recapture_on_disposition`` (the SAME
    primitive the estate path consumes) -- one spelling of the recapture math.
    Consumes ``recapture``/``terminal_loss`` only, NEVER its ``capital_gain``:
    the gain is taxed separately by ``_disposition_gain_tax`` on the (p_gross -
    acb_share) base, and re-adding it here would double-tax (the primitive
    returns ``capital_gain`` only for symmetry).

    Proceeds for the recapture ceiling are ``p_gross`` (the couple's
    appreciated gross value at the sale year -- the SAME figure the estate path
    uses via ``cca['fmv_at_disposition']`` at death, so a mid-horizon sale and
    a deemed disposition share one property valuation). The UCC is the couple's
    OPENING UCC for the sale year (``opening_rental_ucc[prop_id]``, the UCC
    immediately before the mid-year disposition -- the closing UCC of the prior
    year, threaded through ``jurisdiction_state``); a property whose CCA ledger
    has no carried balance falls back to its declared ``opening_ucc`` (the
    first-year case), mirroring ``objective._cca_recapture_for``'s fallback
    exactly (DP#9).

    The couple-level recapture/terminal-loss is split per owner in the SAME
    ``owner_roles`` proportion the gain is split (``frac/couple_share``
    recovers each owner's share of the property), and each share bands against
    THAT owner's taxable income. Returns 0.0 for a non-rental sale or a rental
    without a CCA election (the absence-safe no-op, DP#32 -- a rental that
    never elected CCA has nothing to recapture).
    """
    rental = prop.get('rental')
    cca = rental.get('cca') if rental else None
    if not cca:
        return 0.0
    sale = prop['sale']
    owner_roles = sale.get('owner_roles', {})
    couple_share = sum(owner_roles.values())
    if couple_share <= 0.0:
        return 0.0
    prop_id = prop['id']
    ucc = opening_rental_ucc.get(prop_id, cca['opening_ucc'])
    from countries.canada.cca import recapture_on_disposition
    conseq = recapture_on_disposition(
        p_gross, cca['capital_cost'], ucc)
    recapture = conseq['recapture']
    terminal_loss = conseq['terminal_loss']
    if recapture <= 0.0 and terminal_loss <= 0.0:
        return 0.0
    from tax_calculator import tax_on_income
    tax = 0.0
    for role, frac in owner_roles.items():
        if frac <= 0.0:
            continue
        owner_frac = frac / couple_share
        other_income = (primary_taxable_income if role == 'primary'
                        else spouse_taxable_income)
        if recapture > 0.0:
            # 100%-inclusion ordinary income stacked on the owner's return.
            share = recapture * owner_frac
            tax += (tax_on_income(other_income + share, brackets)
                    - tax_on_income(other_income, brackets))
        if terminal_loss > 0.0:
            # A deductible terminal loss: a marginal tax saving (a negative
            # contribution). Floored per-owner at the tax already on the
            # owner's return so a loss in a no-tax year yields no refund here
            # (the engine does not model loss carry-back/carry-forward).
            share = terminal_loss * owner_frac
            base_tax = tax_on_income(other_income, brackets)
            saving = base_tax - tax_on_income(max(0.0, other_income - share),
                                               brackets)
            tax -= min(saving, base_tax)
    return tax


def _property_disposition_for(
        prop: Dict[str, Any], cal_year: int, start_year: int,
        brackets: list, primary_taxable_income: float, spouse_taxable_income: float,
        opening_rental_ucc: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Compute one sold property's disposition figures for ``cal_year`` (its
    sale year): ``p_gross``, ``p_net``, ``disposition_tax``, ``realized_gain``,
    and the per-owner gain shares. Returns zeros when ``cal_year`` is not this
    property's sale year (a strict no-op for every other year and every
    property held to the horizon -- DP#32)."""
    sale = prop.get('sale')
    if sale is None or sale['year'] != cal_year:
        return {'p_gross': 0.0, 'p_net': 0.0, 'disposition_tax': 0.0,
                'realized_gain': 0.0}
    if opening_rental_ucc is None:
        opening_rental_ucc = {}  # a direct unit-test caller (no UCC ledger)
    # P_gross: the appreciated gross value at the sale year, the SAME value
    # _property_equity_for_year's appreciation branch computes (so a sold
    # property realizes exactly the equity it would have contributed held).
    # owned_from = the purchase year if a dated purchase, else start_year
    # (mirrors _property_equity_for_year exactly -- DP#9, one spelling).
    value_share = prop['value_share']
    rate = prop.get('appreciation_rate')
    purchase = prop.get('purchase')
    owned_from = purchase['year'] if purchase is not None else start_year
    if rate is None or rate == 0.0:
        p_gross = value_share
    else:
        years_held = max(0, cal_year - owned_from)
        p_gross = value_share * ((1.0 + rate) ** years_held)
    acb_share = prop['acb_share']
    secured_share = prop['secured_share']
    selling_costs = sale['selling_costs']

    # PRE-apportioned, per-owner-banded capital-gains tax on the realized
    # gain (shared spine, DP#9 -- one spelling of the gain-banding math).
    realized_gain = max(0.0, p_gross - acb_share)
    disposition_tax = _disposition_gain_tax(
        realized_gain, sale, cal_year, brackets,
        primary_taxable_income, spouse_taxable_income)
    # Issue #968: CCA recapture on a rental SALE. A rental that claimed CCA
    # owes recapture (ITA s.13(1), 100%-inclusion ordinary income) -- real tax
    # the v1 scope skipped for lack of the sale-year UCC. The opening UCC is
    # now threaded onto YearWorkingState (the UCC immediately before the
    # mid-year disposition), so the recapture/terminal-loss is priced here,
    # on top of the capital-gain tax above. Consumes recapture/terminal_loss
    # only, never the primitive's capital_gain (the gain path above already
    # taxes it -- DP#9). 0.0 for a non-rental sale or a rental with no CCA
    # election (absence-safe, DP#32).
    disposition_tax += _disposition_cca_recapture_tax(
        prop, p_gross, cal_year, brackets,
        primary_taxable_income, spouse_taxable_income, opening_rental_ucc)
    p_net = p_gross - secured_share - selling_costs - disposition_tax
    return {'p_gross': p_gross, 'p_net': p_net,
            'disposition_tax': disposition_tax, 'realized_gain': realized_gain}


@rule('property_disposition')
def apply_property_disposition(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #956 bite B (sale-core, DP#10/#26): a declared mid-horizon
    voluntary SALE of a property settles in its sale year.

    When a property declaring ``sale`` reaches its sale calendar year, its net
    proceeds (``P_net`` = gross appreciated value less the secured mortgage
    discharged, the selling costs, and the disposition tax) are INVESTED into
    the non-registered account (establishing fresh ACB), the realized capital
    gain is surfaced for reporting, and the disposition tax is surfaced for
    transparency. The property's equity gated to zero this year
    (``simulation_state._property_equity_for_year``); the proceeds replace it
    in the portfolio.

    Conservation (the spec's crux): ``Δtotal_assets = P_net - E =
    -(selling_costs + T)`` -- the household's assets drop by EXACTLY the
    selling friction (third-party costs + government tax). Money is conserved:
    the equity converted to investable cash, less the friction that genuinely
    left the household. The money-conservation invariant passes (the sale is
    an asset-class swap, cash-flow-neutral -- the P_gross received exactly
    funds the mortgage discharge, the friction, and the invested P_net, so the
    solvency identity is unaffected: no inflow or outflow is needed).

    Wiring: the proceeds are injected POST-GROWTH (added to
    ``ws.new_nonreg_bal`` / ``ws.new_nonreg_acb`` by this rule, which runs
    AFTER ``non_reg_growth`` and BEFORE ``solvency``) rather than via
    ``alloc.non_reg`` (the year-0 free_cash path). The free_cash path adds the
    contribution to the OPENING balance, which then compounds in the sale year
    -- that would make the year-N drop ``P_net*(1+r) - E`` (rate-dependent),
    NOT the exact ``-(selling_costs + T)`` the conservation identity requires.
    Post-growth injection adds P_net at the END of the sale year (after
    compounding), so the year-N drop is exactly the friction and P_net "then
    grows" from the NEXT year on (the spec's "investable non-reg that then
    grows"). Fresh ACB = P_net (the invested cash establishes its own cost
    basis, DP#19). The sale is absence-safe (DP#32): a household with no sale
    (incl. the golden fixture) has every property's ``sale`` absent -> the
    rule is a strict no-op -> the golden invariant is unchanged by
    construction.

    DP#25: ``countries.canada`` is imported lazily inside the helper (this
    module keeps no jurisdiction import at top level). The capital-gains tax
    is computed once, here (in ``T``); it is NOT also added to the year's
    ordinary income-tax base (that would double-tax). The realized gain is
    surfaced on ``YearResult.realized_capital_gains`` (mirroring
    ``realized_capital_gains``, issue #754) for the year-end AMT base (#710)
    and reporting.

    Issue #968: ``T`` also carries the CCA RECAPTURE (and any terminal-loss
    saving) when the sold property is a rental that elected CCA -- 100%-
    inclusion ordinary income (ITA s.13(1)) priced on the opening UCC at the
    sale year (threaded onto ``ws.opening_rental_ucc``), split per owner and
    banded against each owner's taxable income. The estate path skips a sold
    rental's recapture at the deemed disposition (issue #964) BECAUSE this
    rule prices it here; before #968 that skip left the recapture taxed
    nowhere. The recapture is real tax leaving the household, so it enters
    ``T`` and the conservation identity holds exactly as before
    (``Δtotal_assets = -(selling_costs + T)`` -- T now includes the
    recapture). 0.0 for a non-rental sale or a rental with no CCA election
    (absence-safe, DP#32).

    Returns True when any property is sold this year (the rule had an
    observable effect), False for a year with no sale (no-op, all outputs stay
    at their seeded 0.0 defaults).
    """
    config = ctx.config
    cal_year = ctx.calendar_year
    # Absence-safe fast path (DP#32): a household with no property sold this
    # year (every property's `sale` absent or in a different calendar year --
    # incl. the golden fixture and every pre-/post-sale year) is a strict
    # no-op. Brackets are only needed to band a gain, so a no-sale year does
    # not require them -- a direct unit-test caller that did not pass
    # year_brackets still runs cleanly.
    props_in_sale_year = [
        prop for prop in getattr(config, 'properties', [])
        if prop.get('sale') is not None
        and prop['sale']['year'] == cal_year
    ]
    if not props_in_sale_year:
        return False

    brackets = ctx.year_brackets
    if brackets is None:
        # A property IS sold this year but no brackets were passed to band the
        # gain against -- a caller error, not a silent zero (DP#32); raise
        # rather than under-tax the gain. The prologue resolves these for the
        # marginal rates, so the live fold always passes them.
        raise ValueError(
            "property_disposition rule needs ctx.year_brackets to band the "
            "disposition gain; got None (the prologue resolves these for the "
            "marginal rates -- a direct caller must pass them too).")

    total_p_net = 0.0
    total_tax = 0.0
    total_gain = 0.0
    for prop in props_in_sale_year:
        figures = _property_disposition_for(
            prop, cal_year, config.start_year, brackets,
            ctx.primary_taxable_income, ctx.spouse_taxable_income,
            ws.opening_rental_ucc)
        p_net = figures['p_net']
        # Inject the net proceeds into non-reg POST-GROWTH (this rule runs
        # after non_reg_growth), establishing fresh ACB. The proceeds replace
        # the property's equity in the portfolio; they compound from NEXT
        # year on (the conservation identity holds exactly in the sale year).
        ws.new_nonreg_bal += p_net
        ws.new_nonreg_acb += p_net
        total_p_net += p_net
        total_tax += figures['disposition_tax']
        total_gain += figures['realized_gain']

    ws.sale_proceeds_invested = total_p_net
    ws.sale_disposition_tax = total_tax
    ws.sale_realized_gain = total_gain
    return True


def _principal_disposition_for(
        sale: Dict[str, Any], cal_year: int,
        mortgage_balance: float, heloc_balance: float, sm_heloc: float,
        brackets: list, primary_taxable_income: float,
        spouse_taxable_income: float,
        start_year: Optional[int] = None) -> Dict[str, float]:
    """Compute the principal residence's disposition figures for ``cal_year``
    (its sale year): ``discharged_debt``, ``disposition_tax``,
    ``realized_gain``, ``p_net``. Returns all zeros when ``cal_year`` is
    before the sale year (the rule's pre-sale no-op -- the home is held,
    nothing is discharged, no proceeds). When ``cal_year`` is the sale year OR
    any year after it, the debt is discharged (the home + its mortgage + any
    HELOC/SM leave the balance sheet from the sale year on -- the rule
    force-zeros the secured debt every post-sale year so the amortization
    schedule's scheduled balance does not resurrect a paid-off mortgage); the
    proceeds, tax, and gain are priced ONCE, in the sale year only.

    The principal's value is NOT in ``total_assets`` (it flows via
    ``house_value`` / LTV / charge math, not via ``property_equities``), so
    the discharged debt is the LIVE year-N balance read off
    ``YearWorkingState`` (``new_mortgage_balance`` / ``new_heloc_balance`` /
    ``new_sm_heloc``), not a config-time snapshot -- the principal's mortgage
    AMORTIZES in the engine (unlike a non-principal property's static
    ``secured_share``), so a snapshot would under-state the retired debt and
    break the conservation identity. The gain (``value_share - acb_share``)
    is apportioned by the PRE taxable_fraction (ITA s.40(2)(b)) and banded
    against each owner's taxable income, reusing
    ``estate.tax_on_capital_gain_at_death`` exactly (DP#9 -- one spelling of
    the gain-banding math, not a second). ``P_net = value_share -
    discharged_debt - selling_costs - T``.

    Issue #963 (epic #956 bite F): when the sale carries an
    ``appreciation_rate`` (the principal declared one), the sale realizes
    the APPRECIATED gross value at the sale year -- ``value_share`` compounds
    at ``(1 + rate) ** (syear - start_year)`` (the principal is held from
    the projection start, so the ownership year IS ``start_year``). The
    ``acb_share`` stays at cost (appreciation does not change ACB -- DP#19),
    so the realized gain grows by exactly the accrued appreciation.
    ``start_year`` is required ONLY when appreciation is declared; a sale
    with no rate never reads it (absence-safe, DP#32).
    """
    syear = sale['year']
    if cal_year < syear:
        # Pre-sale: the home is held, nothing is discharged, no proceeds.
        return {'discharged_debt': 0.0, 'disposition_tax': 0.0,
                'realized_gain': 0.0, 'p_net': 0.0}
    # The home + its mortgage + any HELOC/SM secured against it leave the
    # balance sheet from the sale year on. The discharged debt is the LIVE
    # year-N balance (the mortgage amortizes; a config snapshot would be
    # wrong). The SM HELOC (readvance line) is secured against the same
    # charge and is discharged alongside the mortgage.
    discharged_debt = mortgage_balance + heloc_balance + sm_heloc
    if cal_year > syear:
        # Post-sale year: the debt is already 0 (discharged last year and
        # carried forward at 0), but the rule force-zeros it again anyway so
        # the amortization schedule's scheduled `end_balance` does not
        # resurrect a paid-off mortgage. No proceeds/tax/gain to price (the
        # sale settled once, in its sale year).
        return {'discharged_debt': discharged_debt, 'disposition_tax': 0.0,
                'realized_gain': 0.0, 'p_net': 0.0}
    # ── The sale year: price the disposition ONCE. ──
    # Issue #963 (epic #956 bite F): when the principal declares an
    # `appreciation_rate`, the sale realizes the APPRECIATED gross value at
    # the sale year, not the static base `value_share` -- a downsize/sell of
    # a grown home realizes the growth. The base compounds at
    # `(1 + rate) ** (syear - start_year)` (the principal is held from the
    # projection start, so the ownership year IS `start_year`). Absence-safe
    # (DP#32): an absent or 0.0 rate returns `value_share` unchanged and never
    # reads the exponent, so a sale with no appreciation is byte-identical to
    # bite E. `acb_share` stays at cost -- appreciation does not change ACB
    # (DP#19), so the realized gain grows by exactly the accrued appreciation.
    value_share = sale['value_share']
    rate = sale.get('appreciation_rate')
    if rate is not None and rate != 0.0:
        # `start_year` is the principal's ownership year (the projection
        # start -- the principal is never a dated mid-horizon purchase). It
        # is required to compound the base; a caller that declares
        # appreciation MUST pass it (the rule does, from ctx.config.start_year).
        if start_year is None:
            raise ValueError(
                "_principal_disposition_for needs start_year to compound the "
                "principal's appreciation_rate (declared on the sale); got None. "
                "The rule passes ctx.config.start_year; a direct caller must "
                "too (DP#32).")
        years = max(0, syear - start_year)
        value_share = value_share * ((1.0 + rate) ** years)
    acb_share = sale['acb_share']
    selling_costs = sale['selling_costs']
    # PRE-apportioned, per-owner-banded capital-gains tax on the realized gain
    # (the SHARED spine, DP#9 -- one spelling of the gain-banding math, the
    # same helper Bite B's _property_disposition_for composes). A principal
    # designated for its whole ownership -> ~0 tax; a principal with no
    # designation -> fully taxable.
    realized_gain = max(0.0, value_share - acb_share)
    disposition_tax = _disposition_gain_tax(
        realized_gain, sale, cal_year, brackets,
        primary_taxable_income, spouse_taxable_income)
    p_net = value_share - discharged_debt - selling_costs - disposition_tax
    return {'discharged_debt': discharged_debt,
            'disposition_tax': disposition_tax,
            'realized_gain': realized_gain, 'p_net': p_net}


@rule('principal_disposition')
def apply_principal_disposition(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #956 bite E (principal-residence disposition, DP#10/#26): a
    declared mid-horizon SALE of the PRINCIPAL residence settles in its sale
    year.

    The principal residence is deliberately excluded from
    ``config.properties`` (its value reaches the annual side via
    ``house_value`` / LTV / charge math, not via ``property_equities`` in
    ``total_assets``), so Bite B's ``property_disposition`` rule cannot sell
    it -- the principal's sale is a separate disposition with its own rule.
    When the principal's declared ``sale`` reaches its sale calendar year,
    the home + its mortgage + any HELOC/SM secured against it LEAVE the
    balance sheet: this rule force-zeros ``new_mortgage_balance``,
    ``new_heloc_balance``, and ``new_sm_heloc`` (so the amortization
    schedule's scheduled `end_balance` does not resurrect a paid-off
    mortgage, and the SM readvance line is retired), and the net proceeds
    ``P_net = value_share - discharged_debt - selling_costs - T`` are
    INVESTED into the non-registered account POST-GROWTH (establishing fresh
    ACB), the realized gain is surfaced for reporting, and the disposition
    tax is surfaced for transparency. The SM INVESTMENT (``new_sm_investment``)
    is NOT zeroed -- it is a real asset that stays (the loan is discharged,
    the asset remains, ACB unchanged); only its financing is retired.

    Conservation (the spec's gate): the principal's value is NOT in
    ``total_assets`` (it flows via ``house_value`` / charge math, off the
    balance sheet), so the conservation identity is on NET_ASSETS, not
    ``total_assets`` -- distinct from Bite B's non-principal identity (the
    non-principal's equity IS in ``total_assets``, so Bite B's
    ``Δtotal_assets = -(selling_costs + T)`` holds). For the principal:

      Δtotal_assets (sell vs hold, sale year) = P_net
        (the proceeds invested -- the hold household has 0 proceeds);
      Δtotal_debt   (sell vs hold, sale year) = -(mortgage + heloc + sm_heloc)
        (the discharged secured debt -- the hold household keeps it);
      Δnet_assets   (sell vs hold, sale year) = P_net + discharged_debt
        = value_share - selling_costs - T
        = V - selling_costs - T

    -- the home's gross value realized, less the friction that genuinely
    left the household (third-party selling costs + government tax). The
    home's equity (V - debt) enters net_assets for the first time (as
    proceeds + debt retired), minus the friction. Money is conserved: the
    off-balance-sheet home converts to on-balance-sheet assets, less the
    friction that genuinely left the household. For a fully-PRE-designated
    principal (the common case), T ≈ 0, so Δnet_assets ≈ V - selling_costs.
    V is the principal's APPRECIATED value at the sale year when the
    household declares an ``appreciation_rate`` (#963 bite F), else the
    static ``value_share`` (byte-identical to bite E).

    Wiring: the proceeds are injected POST-GROWTH (added to
    ``ws.new_nonreg_bal`` / ``ws.new_nonreg_acb`` by this rule, which runs
    AFTER ``non_reg_growth`` -- so P_net does not compound in the sale year,
    and the year-N identity holds exactly) and BEFORE ``solvency`` (so the
    invested non-reg is on the balance sheet the waterfall reads). The rule
    runs AFTER ``mortgage`` / ``margin_heloc_interest`` / ``sm_readvance`` /
    ``sm_investment_growth`` / ``heloc_interest_servicing`` so the
    HELOC/SM interest, readvance, and growth rules have set their ``new_*``
    values and the SM investment (a real asset that STAYS) has grown; it
    runs BEFORE ``rrsp_refund_heloc_paydown`` (no HELOC to pay down
    post-sale). The secured debt is force-zeroed EVERY year from the sale
    year on (the amortization schedule keeps producing a scheduled
    ``end_balance`` -- the rule overrides it every post-sale year so a
    paid-off mortgage stays paid off).

    The sale is absence-safe (DP#32): a household with no principal sale
    (incl. the golden fixture, which builds SimulationConfig.from_dict
    straight from a legacy dict that never carries ``principal_sale``) has
    ``config.principal_sale`` is None -> the rule is a strict no-op -> the
    golden invariant is unchanged by construction.

    DP#25: ``countries.canada`` is imported lazily inside the helper (this
    module keeps no jurisdiction import at top level). The capital-gains tax
    is computed once, here (in ``T``); it is NOT also added to the year's
    ordinary income-tax base (that would double-tax). The realized gain is
    surfaced on ``YearResult.realized_capital_gains`` (mirroring the
    drawdown/solvency/Bite-B realized gains, issue #754) for the year-end
    AMT base (#710) and reporting.

    Returns True when the principal is in or past its sale year (the rule
    had an observable effect -- the debt is force-zeroed), False for a year
    before the sale (no-op, all outputs stay at their seeded 0.0 defaults).
    """
    sale = ctx.config.principal_sale
    # Absence-safe fast path (DP#32): a household with no principal sale
    # (incl. the golden fixture and every pre-sale year) is a strict no-op.
    if sale is None:
        return False
    cal_year = ctx.calendar_year
    if cal_year < sale['year']:
        # Pre-sale: the home is held, nothing is discharged, no proceeds.
        return False

    brackets = ctx.year_brackets
    if brackets is None and cal_year == sale['year']:
        # The principal IS sold this year but no brackets were passed to band
        # the gain -- a caller error, not a silent zero (DP#32); raise rather
        # than under-tax the gain. The no-sale / pre-sale / post-sale paths do
        # NOT need brackets (no gain to band) and do not raise.
        raise ValueError(
            "principal_disposition rule needs ctx.year_brackets to band the "
            "disposition gain in the sale year; got None (the prologue "
            "resolves these for the marginal rates -- a direct caller must "
            "pass them too).")

    figures = _principal_disposition_for(
        sale, cal_year, ws.new_mortgage_balance, ws.new_heloc_balance,
        ws.new_sm_heloc, brackets if brackets is not None else [],
        ctx.primary_taxable_income, ctx.spouse_taxable_income,
        start_year=ctx.config.start_year)
    discharged_debt = figures['discharged_debt']
    # Force-zero the discharged secured debt: the home + its mortgage + any
    # HELOC/SM secured against it leave the balance sheet from the sale year
    # on. The mortgage amortization schedule keeps producing a scheduled
    # `end_balance` -- this override every post-sale year keeps a paid-off
    # mortgage paid off (the carried-forward opening balance is 0, and the
    # schedule's end_balance is overridden to 0 here).
    ws.new_mortgage_balance = 0.0
    ws.new_heloc_balance = 0.0
    ws.new_sm_heloc = 0.0
    # The SM INVESTMENT (new_sm_investment) is NOT zeroed -- it is a real
    # asset that stays (the loan is discharged, the asset remains, ACB
    # unchanged). Its interest deduction stops (sm_interest prices
    # new_sm_heloc * rate = 0 next year on), and it compounds as a regular
    # non-reg holding from the sale year on.
    ws.principal_sale_discharged_debt = discharged_debt
    if cal_year == sale['year']:
        # ── The sale year: invest the net proceeds POST-GROWTH, surface
        # the gain + tax. non_reg_growth already ran (this rule sits after
        # it), so P_net does not compound in the sale year -- the year-N
        # conservation identity holds exactly. Fresh ACB = P_net (DP#19).
        p_net = figures['p_net']
        ws.new_nonreg_bal += p_net
        ws.new_nonreg_acb += p_net
        ws.principal_sale_proceeds_invested = p_net
        ws.principal_sale_disposition_tax = figures['disposition_tax']
        ws.principal_sale_realized_gain = figures['realized_gain']
    # Post-sale years: only the debt-zeroing fires (above) -- no proceeds,
    # tax, or gain to price (the sale settled once, in its sale year).
    return True


# Epic #795 bite 3 (DP#9/#26): the pure carry-forward arithmetic the
# `tuition_credit` rule composes. This used to live as
# ``simulation._apply_tuition_credits_with_carryforward`` and was spelled twice
# in the fold's prologue; it is now a single module-private helper the
# registered rule calls (the orchestration moved to the rule, DP#9 -- one
# spelling, not two). Pure (DP#3): a function of the credit, the opening
# carry-forward, and the tax before the credit. Kept as a standalone helper so
# the carry-forward semantics remain directly unit-testable (#784's own tests
# import it).
def _apply_tuition_credit_with_carryforward(
        this_year: float, carryforward: float, tax_before: float
) -> tuple:
    """Apply a non-refundable tuition credit with carry-forward of the
    unused remainder (issue #784).

    ``credit_available = this_year_credit + carried_forward_from_prior_years``;
    ``applied = min(credit_available, tax_before_credit)`` (the credit is
    non-refundable -- it can never make tax negative, so `applied` never
    exceeds `tax_before`); ``new_carryforward = credit_available - applied``
    (the unused remainder carries to the next year -- CRA / Revenu Québec
    both allow indefinite carry-forward).
    """
    available = this_year + carryforward
    if available <= 0.0:
        return 0.0, max(0.0, carryforward)  # nothing to apply; carry any prior unused
    if tax_before <= 0.0:
        return 0.0, available  # no tax to reduce -> all carries forward
    applied = min(available, tax_before)
    return applied, available - applied


# Issue #785: the federal tuition-credit TRANSFER limit (ITA s.118.8) is on
# the TUITION AMOUNT, not the credit: a student may transfer up to $5,000 of
# eligible tuition fees to a supporting spouse/parent. In credit space, the
# cap is the credit on $5,000 of tuition at the federal lowest rate.
_FEDERAL_TUITION_TRANSFER_LIMIT = 5000.0  # dollars of tuition (ITA s.118.8)


@rule('tuition_credit')
def apply_tuition_credit(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Epic #795 bite 3 (DP#10/#26): the per-year federal (+ QC provincial)
    tuition tax credit.

    A taxed member studying in ``sim_year`` claims their OWN non-refundable
    credit on the eligible tuition they paid that year
    (``member['tuition_by_year'][sim_year]``); the credit is subtracted from
    that member's ``tax_on_income``, floored at 0 (non-refundable -- it
    cannot make tax negative). The unused remainder CARRIES FORWARD to reduce
    a future year's tax (#784, CRA / Revenu Québec indefinite carry-forward).
    A member (or a child, #701) with ``tuition_transfer_to`` declared
    transfers unused credit to a supporting spouse/parent, capped at the
    credit on $5,000 of tuition (ITA s.118.8, #785).

    This used to be computed inline in the fold's prologue -- spelled TWICE,
    once in ``simulate_year`` and once in ``_run_monthly`` (DP#9, two parallel
    implementations of the same wiring, split across three helpers:
    ``simulation._tuition_credits_for`` /
    ``_apply_tuition_credits_with_carryforward`` /
    ``_process_tuition_transfers``). It is now a single registered rule; the
    prologue passes only the primitive inputs the rule needs (each member's
    pre-credit ``tax_on_income`` + the resolved ``tax_provider`` + the province
    + the opening carry-forwards, the latter read off ``ws``), and this rule
    writes its outputs to ``YearWorkingState``: the ACTUAL per-member tax
    reduction (own credit applied + transfers received, after all non-
    refundable flooring) and the new per-member / per-child carry-forwards.
    ``apply_solvency`` (next in ``RULE_ORDER``) reads the applied amounts so
    the cash-flow identity counts the POST-credit after-tax income as
    ``available`` (the credit reduces tax, so it raises after-tax income);
    the epilogue and ``build_year_result`` surface the new carry-forwards.

    DP#25: ``countries.canada`` is imported lazily inside the body (this
    module keeps no jurisdiction import at top level). The body is the
    verbatim computation the three prologue helpers performed -- moved, not
    changed -- so every number is preserved byte-for-byte. Gated on declared
    tuition: absent ``tuition_by_year`` -> ``{}`` -> ``0.0`` credit, so a
    household that declares no tuition (incl. the golden fixture) is
    unaffected (a strict no-op -> the golden invariant is unchanged by
    construction).

    Returns True when any credit was applied or transferred this year (the
    rule had an observable effect), False for a year with no declared tuition
    and no carried-forward remainder (no-op, all outputs stay at their
    seeded 0.0 / [] defaults).
    """
    from countries.canada import tuition_tax_credit as _tuition_tax_credit

    config = ctx.config
    members = config.family_members
    primary_member = next((m for m in members if m.get('role') == 'primary'), {})
    spouse_member = next((m for m in members if m.get('role') == 'spouse'), {})

    sim_year = ctx.calendar_year
    tax_provider = ctx.tax_provider if ctx.tax_provider is not None else default_tax_provider()
    province = config.province

    # ── Per-member own credit (issue #764/#783) ──
    # Federal (+ QC provincial when province is QC) credit on the eligible
    # tuition each taxed member paid this year. 0.0 for a member with no
    # declared tuition that year (DP#32: absent tuition_by_year -> {} -> 0).
    primary_tuition = primary_member.get('tuition_by_year', {}).get(sim_year, 0.0)
    spouse_tuition = spouse_member.get('tuition_by_year', {}).get(sim_year, 0.0)
    primary_tuition_credit = _tuition_tax_credit(
        primary_tuition, sim_year, tax_provider,
        province=province) if primary_tuition else 0.0
    spouse_tuition_credit = _tuition_tax_credit(
        spouse_tuition, sim_year, tax_provider,
        province=province) if spouse_tuition else 0.0

    # ── Apply own credit with carry-forward (issue #784) ──
    primary_tax_before = ctx.primary_tax_before
    spouse_tax_before = ctx.spouse_tax_before
    primary_applied, primary_new_cf = _apply_tuition_credit_with_carryforward(
        primary_tuition_credit, ws.opening_primary_tuition_carryforward,
        primary_tax_before)
    spouse_applied, spouse_new_cf = _apply_tuition_credit_with_carryforward(
        spouse_tuition_credit, ws.opening_spouse_tuition_carryforward,
        spouse_tax_before)

    # ── Transfers to a supporting spouse/parent (issue #785, ITA s.118.8) ──
    # The transfer happens AFTER own-tax application and BEFORE carry-forward:
    # student applies to own tax, then transfers min(remaining, cap) to the
    # supporter, then carries forward the rest (#784). Two cases share the
    # SAME transfer mechanism: a TAXED member transfers their unused credit
    # (the new_cf above, i.e. what remained after applying to own tax); a
    # CHILD transfers their FULL credit (no own tax). Both capped at the
    # credit on $5,000 of tuition (federal only, province=None) and floored at
    # the supporter's REMAINING tax (tax after their own credit, before the
    # transfer) -- non-refundable, no phantom refund.
    transfer_cap = _tuition_tax_credit(
        _FEDERAL_TUITION_TRANSFER_LIMIT, sim_year, tax_provider, province=None)

    primary_id = primary_member.get('id', '')
    spouse_id = spouse_member.get('id', '')

    primary_remaining_tax = max(0.0, primary_tax_before - primary_applied)
    spouse_remaining_tax = max(0.0, spouse_tax_before - spouse_applied)

    def _supporter_tax(transfer_to: str) -> float:
        if transfer_to == primary_id:
            return primary_remaining_tax
        if transfer_to == spouse_id:
            return spouse_remaining_tax
        return 0.0

    to_primary = 0.0
    to_spouse = 0.0

    # Taxed-member transfers (secondary): transfer unused credit after own tax.
    if primary_member.get('tuition_transfer_to') and primary_new_cf > 0.0:
        transfer_to = primary_member['tuition_transfer_to']
        transferable = min(primary_new_cf, transfer_cap)
        supporter_tax = _supporter_tax(transfer_to)
        transferred = min(transferable, max(0.0, supporter_tax))
        primary_new_cf -= transferred
        if transfer_to == primary_id:
            to_primary += transferred
        elif transfer_to == spouse_id:
            to_spouse += transferred
    if spouse_member.get('tuition_transfer_to') and spouse_new_cf > 0.0:
        transfer_to = spouse_member['tuition_transfer_to']
        transferable = min(spouse_new_cf, transfer_cap)
        supporter_tax = _supporter_tax(transfer_to)
        transferred = min(transferable, max(0.0, supporter_tax))
        spouse_new_cf -= transferred
        if transfer_to == primary_id:
            to_primary += transferred
        elif transfer_to == spouse_id:
            to_spouse += transferred

    # Children transfers (the main case, #701): full credit transfers (no own tax).
    children = config.children
    child_cfs = list(ws.opening_child_tuition_carryforwards)
    while len(child_cfs) < len(children):
        child_cfs.append(0.0)
    new_child_cfs = list(child_cfs)

    for i, child in enumerate(children):
        child_tuition = child.get('tuition_by_year', {}).get(sim_year, 0.0)
        transfer_to = child.get('tuition_transfer_to')
        # Federal credit on the child's tuition (federal only for transfer).
        # 0 when no new tuition this year, but the carry-forward from prior
        # years is still available for transfer (#784).
        child_credit = _tuition_tax_credit(
            child_tuition, sim_year, tax_provider, province=None) if child_tuition else 0.0
        available = child_credit + child_cfs[i]
        if available <= 0.0:
            new_child_cfs[i] = 0.0
            continue
        if not transfer_to:
            new_child_cfs[i] = available
            continue
        transferable = min(available, transfer_cap)
        supporter_tax = _supporter_tax(transfer_to)
        transferred = min(transferable, max(0.0, supporter_tax))
        new_child_cfs[i] = available - transferred
        if transferred > 0.0:
            if transfer_to == primary_id:
                to_primary += transferred
            elif transfer_to == spouse_id:
                to_spouse += transferred

    # ── The ACTUAL per-member tax reduction this year ──
    # `primary_tax = max(0, primary_remaining_tax - to_primary)` in the old
    # prologue; the reduction is `tax_before - primary_tax`, i.e. the credit
    # dollars that actually lowered tax (after both non-refundable floors:
    # the own-credit floor and the transfer floor). apply_solvency adds this
    # to `available` so the identity sees the POST-credit after-tax income.
    primary_tax = max(0.0, primary_remaining_tax - to_primary)
    spouse_tax = max(0.0, spouse_remaining_tax - to_spouse)
    primary_reduction = primary_tax_before - primary_tax
    spouse_reduction = spouse_tax_before - spouse_tax

    ws.tuition_credit_applied_primary = primary_reduction
    ws.tuition_credit_applied_spouse = spouse_reduction
    ws.new_primary_tuition_carryforward = primary_new_cf
    ws.new_spouse_tuition_carryforward = spouse_new_cf
    ws.new_child_tuition_carryforwards = new_child_cfs

    return (
        primary_reduction > 0.0 or spouse_reduction > 0.0
        or primary_new_cf > 0.0 or spouse_new_cf > 0.0
        or any(cf > 0.0 for cf in new_child_cfs)
    )


@rule('solvency')
def apply_solvency(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """The cash-flow identity (issue #679), enforced every year, and what
    actually happens when it fails.

    ``after_tax_income + drawdowns >= debt_service + living_costs +
    contributions`` must hold. Every earlier rule in this fold clamps
    contributions to available ROOM (RRSP/TFSA limits) -- never to
    affordability. This rule is the one place that asks whether the money
    for everything already booked this year actually existed, and if it
    did not, forces a real liquidation to cover the difference, in a fixed
    order, each with its real cost (``liquidation_waterfall.py``):
    emergency reserve -> revolving credit facility -> non-registered
    (capital-gains tax, realised losses reported honestly) -> TFSA ->
    registered (fully taxable, at whatever this year's rate is -- the
    worst possible time, not a chosen one).

    Runs LAST in ``RULE_ORDER``: it must see the post-growth,
    post-contribution, post-drawdown state every other rule in this fold
    produced, because it may itself further reduce those very balances to
    fund a shortfall none of them checked for.

    DP#16: a no-op (module not engaged) when ``ctx.living_costs <= 0`` --
    the household's own budget was never supplied. This is NOT the DP#32
    zero-as-fallback trap: no real household spends $0/year to live, so a
    caller that never sets ``household_budget.annual_living_costs`` and one
    that (nonsensically) declared it as exactly 0 are indistinguishable in
    a way that matters -- there is no real "$0 living costs" case this
    engine needs to represent, unlike OAS or a savings rate, which
    genuinely can be zero. Absence of the input is absence of the
    constraint (exactly the LTV explorer's house_value/mortgage_balance/
    margin_available trigger-data pattern), not a value being silently
    coerced.

    If even the full waterfall cannot cover the shortfall, the year is
    marked ``ws.solvency_ruined = True`` -- a hard ruin, reported on
    ``YearResult.ruined``, never silently absorbed (DP#32). This function
    does not decide what a caller does with a ruined year; it only refuses
    to pretend the shortfall did not happen.

    **Issue #689 closed the representability gap** (the waterfall's second
    step used to be structurally $0: ``personal_loan`` forbade a ``limit``
    and required an ``amortization``; ``heloc`` required collateral -- a
    revolving, unsecured, interest-only credit facility could not be written
    into the input contract at all). ``liabilities[kind=line_of_credit]`` now
    maps onto ``SimulationConfig.credit_facility_limit``, and this rule draws
    it -- BEFORE any asset below it -- whenever the household is short. A
    household that has not declared one still sets
    ``ws.solvency_credit_facility_unrepresentable = True`` on a shortfall
    year, so the report can say the household's real resilience MAY be
    understated by an undeclared real-world facility -- that is a fact about
    the INPUT now, not a structural gap in the engine.

    Deliberately NOT the same facility as the mortgage-paired HELOC
    (``margin_available``/``heloc_balance``): a HELOC is a *secured* product
    carved out of the same registered charge as the mortgage (#664/#681),
    and its balance's tax deductibility is TRACED by use (ITA s.20(1)(c),
    ``countries/canada/debt.py``). Drawing it here, for a personal shortfall,
    would poison that tracing and model a different product than the one
    being asked about. The credit facility this rule draws is its own thing
    -- secured or not (``SimulationConfig.credit_facility_secured``), priced
    at its own declared rate, with no investment-purpose tracing at all.

    Interest on whatever is drawn CAPITALIZES onto the balance every
    subsequent year (this rule computes it every year, not only shortfall
    years, so a balance drawn in year N keeps compounding in year N+1 even
    if N+1 is solvent) -- a documented simplification (DP#30): this facility
    is a rarely-touched backstop, not the household's primary borrowing
    instrument, and the engine already services HELOC interest in cash where
    that distinction actually changes strategy rankings (issue #681).
    """
    # issue #763: the cash-flow identity's debt-service term is BOTH the
    # mortgage payment AND the closed-end consumer loans' payment -- a car
    # payment the household must make is the same kind of essential outflow
    # the mortgage payment is, and the #758 reserve/runway target is sized
    # against the same combined figure (SimState.initial sizes the year-0
    # reserve off _annual_debt_service + _annual_consumer_loan_service).
    # issue #759: the installment plan's payment joins the SAME non-
    # compressible debt-service channel -- a contractual payment plan the
    # household is legally bound to is exactly the essential outflow a
    # reserve/runway exists to bridge, and it does NOT compress under an
    # income shock (contrast #761's discretionary split, which compresses
    # annual_living_costs). It is added here, beside the consumer-loan
    # payment, so the identity's debt-service term and the reserve sizing
    # see all three must-pay outflows at once.
    # Issue #967: the second-property mortgage's payment joins the SAME
    # non-compressible debt-service channel -- a mortgage the household must
    # service is exactly the essential outflow the principal mortgage, consumer
    # loans, and installments already are, and the #758 reserve/runway target
    # is sized against the same combined figure.
    consumer_debt_service = (ws.consumer_loan_payment + ws.installment_payment
                             + ws.second_property_mortgage_payment)
    # Issue #760: dated, finite-term living-cost segments layered ON TOP OF the
    # perpetual `living_costs` scalar (a private-school tuition that ENDS when a
    # child ages out, childcare, a term expense that stops). Each active segment
    # contributes its ANNUAL amount prorated by the days of [from, to) falling
    # in this calendar year (day-count blend -- the outflow-side analog of
    # #674's dated income windows; simulation_rules._expense_segment_
    # contribution_in_year), and STOPS after a non-null `to` (reverts to $0,
    # never carried to the horizon). Split by each segment's own
    # non_discretionary flag: a non-discretionary segment (tuition, childcare)
    # is a must-pay essential outflow that sizes the reserve and never
    # compresses; a discretionary one (non_discretionary=false) compresses to
    # zero under an income shock exactly as #761's scalar split does -- so the
    # flag lands on a real decision (DP#18) and its two values are genuinely
    # distinct (DP#32). Empty `expense_segments` -> both totals 0.0, byte-for-
    # byte today's behaviour (the golden invariant does not move).
    seg_nondiscretionary = 0.0
    seg_discretionary = 0.0
    # Issue #882: a per-month total of the NON-discretionary seasonal segments'
    # outflow, so the reserve can be sized on the PEAK month rather than the
    # annual average. Stays all-zero unless a segment declares active_months --
    # a non-seasonal segment has no monthly vector (DP#32 no-op when undeclared).
    seasonal_nondiscretionary_monthly = [0.0] * 12
    for _seg in ctx.config.expense_segments:
        _contribution = _expense_segment_contribution_in_year(_seg, ctx.calendar_year)
        if _contribution <= 0.0:
            continue
        if _seg['non_discretionary']:
            seg_nondiscretionary += _contribution
            if _seg.get('active_months') is not None:
                for _i, _amt in enumerate(
                        _expense_segment_monthly_outflows(_seg, ctx.calendar_year)):
                    seasonal_nondiscretionary_monthly[_i] += _amt
        else:
            seg_discretionary += _contribution
    # A discretionary segment compresses to zero under a dated income shock
    # (income_shock_active is False in retirement -- it is a working-life
    # detection -- so retirement charges every active segment in full).
    seg_discretionary_compressed = seg_discretionary if ctx.income_shock_active else 0.0
    seg_charged = seg_nondiscretionary + (seg_discretionary - seg_discretionary_compressed)
    ws.expense_segment_outflow = seg_charged
    ws.emergency_reserve_target = reserve_target(
        ctx.config.emergency_reserve_target_months,
        # Issue #760: a committed dated expense (tuition, childcare) is an
        # ESSENTIAL outflow the reserve must bridge; the discretionary portion
        # is compressible and does not inflate the reserve.
        annual_living_costs=ctx.living_costs + seg_nondiscretionary,
        annual_debt_service=ws.mort.get('total_payment', 0.0) + consumer_debt_service,
    )
    # Issue #882 (monthly-resolution reserve sizing): reserve_target above spreads
    # each non-discretionary segment's annual amount evenly over 12 months. For a
    # SEASONAL segment the year's cost concentrates into a few months, so the
    # reserve must bridge the PEAK month, not the average. Add the excess of the
    # peak seasonal month over that evenly-spread average, scaled by the same
    # target-months policy. For a non-seasonal household the monthly vector is
    # all zero -> premium 0.0 -> byte-for-byte #760 (DP#32 no-op when undeclared).
    _reserve_months = ctx.config.emergency_reserve_target_months
    if _reserve_months is not None and _reserve_months > 0 and any(
            seasonal_nondiscretionary_monthly):
        _peak = max(seasonal_nondiscretionary_monthly)
        _average = sum(seasonal_nondiscretionary_monthly) / 12.0
        ws.emergency_reserve_target += _reserve_months * (_peak - _average)
    # Issue #689: carries the credit facility's balance forward (with
    # interest capitalized) EVERY year, including years this rule otherwise
    # no-ops below (``living_costs`` undeclared, or no shortfall) -- a
    # balance drawn in an earlier shock year must not evaporate just because
    # a later year is solvent.
    ws.new_credit_facility_balance = ws.opening_credit_facility_balance * (
        1.0 + (ctx.config.credit_facility_rate or 0.0)
    )
    ws.emergency_reserve_months_covered = months_covered(
        ws.new_emergency_reserve,
        # Issue #760: same essential-outflow base as the reserve target above.
        annual_living_costs=ctx.living_costs + seg_nondiscretionary,
        annual_debt_service=ws.mort.get('total_payment', 0.0) + consumer_debt_service,
    )

    if ctx.living_costs <= 0:
        return False

    debt_service = ws.mort.get('total_payment', 0.0) + consumer_debt_service
    contributions = (
        ws.p_rrsp_actual + ws.s_rrsp_actual + ws.sp_rrsp_actual
        + ws.p_tfsa_actual + ws.sp_tfsa_actual + ws.non_reg_alloc
        + ws.resp_alloc + ws.fhsa_actual
    )
    # Issue #696 (epic #690 bite 5): a dated mid-horizon PROPERTY PURCHASE is a
    # cash outflow in its purchase year, funded like every other spend the
    # household cannot cover from income -- through THIS identity's liquidation
    # waterfall (this year's income surplus first, then a real account draw),
    # NOT a silent reduction of that year's contributions (which clamps at zero
    # and would let a down payment larger than the year's savings vanish,
    # inventing net worth). The outflow is the couple's DOWN PAYMENT (net_equity
    # = value less the mortgage originated against it) plus the couple's share
    # of CLOSING COSTS (both mapped by input_contract._map_owned_properties).
    # Money is conserved (DP#18): the whole outflow is charged to the identity
    # and sourced from real inflows/assets, never clamped away -- the same
    # discipline the mortgage payment and #760's dated expense segments already
    # have. The property's equity enters the balance sheet the SAME year
    # (simulation_state._property_equity_for_year): the down-payment dollars come
    # off the asset side (portfolio) and reappear as equity, while the closing
    # costs are a pure expense. It is a non-compressible, non-discretionary
    # one-time outflow (a signed purchase, not a lifestyle choice), so it does
    # not compress under an income shock. Zero in every year but a purchase year,
    # and for every property held from year 0 (no `purchase`) -> byte-for-byte
    # today's behaviour (DP#32).
    purchase_outflow = _property_purchase_outflow_in_year(
        ctx.config, ctx.calendar_year)
    # Issue #1010 (epic #956): a non-principal property's RECURRING carrying
    # costs (property tax, maintenance, insurance) are an annual cash outflow
    # over the ownership window, funded like every other spend the household
    # cannot cover from income -- through THIS identity's liquidation waterfall
    # (this year's income surplus first, then a real account draw), the SAME
    # discipline the #696 purchase outflow and #760 dated expense segments
    # already have. Money is conserved (DP#18): the whole outflow is charged to
    # the identity and sourced from real inflows/assets, never clamped away.
    # It is a non-compressible, non-discretionary recurring outflow (property
    # tax is a must-pay, not a lifestyle choice), so it does not compress under
    # an income shock. Zero in every year outside the ownership window and for
    # every property declaring no carrying costs (incl. the golden fixture) ->
    # byte-for-byte today's behaviour (DP#32).
    carrying_cost_outflow = _total_carrying_cost_in_year(
        ctx.config, ctx.calendar_year, ctx.config.start_year)
    # Borrowed money that was invested this year is an INFLOW as well as an
    # outflow: it arrives from the lender and leaves for the brokerage in the
    # same breath. `contributions` below already counts the outflow, so the
    # inflow must be counted too or the identity invents a shortfall that
    # never existed (see RuleContext.borrowed_investment). The DEBT it created
    # is not forgiven -- it is booked on the balance sheet and serviced, at
    # interest, in `debt_service` and the HELOC-interest rules, every year
    # after this one. That is where a leveraged strategy is made to pay.
    #
    # Issue #758 (the retirement double-count): in WORKING life the identity
    # charges the working-phase `living_costs` and counts after-tax
    # EMPLOYMENT income -- a shortfall there is a shock-induced cash-flow
    # crisis (what runway measures). In RETIREMENT the household funds its
    # spending BY drawing down assets (#707's drawdown), and the drawdown is
    # sized to `retirement_spending_target - (CPP+OAS+pension)`. Charging the
    # working `living_costs` here on top of the drawdown double-counts
    # spending: the household spends `retirement_spending_target` (usually
    # LOWER than the working budget), and CPP/OAS/pension -- netted out of the
    # drawdown's sizing -- are not added back to `available` (ctx.after_tax
    # _income is employment income only). The old form force-sold assets every
    # retirement year to cover spending that does not happen, corrupting the
    # solvency table, #707's decumulation output, and #758's runway.
    #
    # The correct retirement identity: charge `retirement_spending_target`
    # (what the drawdown is sized to) and count CPP+OAS+pension in available.
    # Then available = retirement_income + drawdown = spending_target, so
    # shortfall = debt_service (+ contributions) -- the REAL mortgage gap
    # only, no double-count, money still conserved (the waterfall funds the
    # real gap). #707 owns the "drawdown could not deliver the target" case.
    if ws.any_retired:
        # Issue #760: a dated segment still active in retirement (e.g. a
        # committed expense spanning into it) is a real outflow ON TOP OF the
        # retirement spending target -- charged in full (income_shock_active is
        # False in retirement, so nothing compresses). A segment that already
        # ended (`to` in an earlier year) contributes 0 here by construction.
        spending_outflow = (ws.retirement_spending_target + seg_charged
                            + purchase_outflow + carrying_cost_outflow)
        available = (ctx.after_tax_income + ws.drawdown_net_delivered
                     + ws.cpp_income + ws.oas_income + ws.pension_income
                     + ctx.borrowed_investment + ctx.free_cash_invested
                     # Issue #1001: the forced RRIF minimum's after-tax slice
                     # that funds the net spending target (priced in
                     # apply_retirement_income, split in apply_rrif_minimum).
                     # The discretionary drawdown is now sized to the residual
                     # shortfall AFTER this slice, so available still equals
                     # retirement_spending_target (money conserved). 0.0 in
                     # every pre-RRIF year and any year no forced minimum fires.
                     + ws.rrif_after_tax_to_spending
                     # Issue #967: a mid-horizon mortgage's ORIGINATED
                     # principal is an INFLOW in the purchase year (the
                     # mortgage funds the purchase: it arrives from the lender
                     # and leaves for the seller in the same breath, the same
                     # inflow==outflow discipline borrowed_investment uses).
                     # 0.0 in every year but a financed property's purchase
                     # year, and for a household with no financing (DP#32).
                     + ws.second_property_mortgage_originated
                     # epic #795 bite 3: the tuition_credit rule's per-member
                     # tax reduction (the credit reduces tax, so it raises
                     # after-tax income). 0.0 for a household that declares
                     # no tuition (the golden path) -- a strict no-op.
                     + ws.tuition_credit_applied_primary
                     + ws.tuition_credit_applied_spouse
                     # Issue #1083: the s.20(1)(c) deduction's statutory saving
                     # on the primary's prologue-taxed rental/loan slice -- the
                     # tax the prologue already embedded in
                     # ``ctx.after_tax_income`` is lower by this amount, so it
                     # is added back here (the tuition_credit booking path).
                     # 0.0 unless the primary is retired AND the deduction
                     # exceeds the cpp+pension drawdown base (the golden
                     # household and every accumulation year: strict no-op).
                     + ws.sm_interest_nondrawdown_tax_saving)
        discretionary_compressed = seg_discretionary_compressed
    else:
        # Issue #761: under a dated INCOME SHOCK a household cuts DISCRETIONARY
        # spending first (restaurants, vacations, entertainment) and keeps
        # funding the non-discretionary core (groceries, utilities, insurance,
        # transport, medicine) plus debt service and committed installments
        # (#759). When the household declared household_budget.discretionary_fraction
        # AND this year's income is reduced below baseline by a decisions.income[]
        # override (ctx.income_shock_active), the identity charges only the
        # NON-discretionary portion; the discretionary portion is compressed
        # to ZERO -- a labelled, documented stress assumption (a household in
        # genuine distress stops dining out and taking vacations; it does not
        # stop buying groceries). The compression is reported on YearResult so
        # the assumption is visible, never silently absorbed (DP#32).
        #
        # No split declared (discretionary_fraction is None) OR no shock this
        # year -> spending_outflow is the full scalar, byte-for-byte today's
        # behaviour. The model_fidelity.runway_treats_all_spend_as_rigid
        # approximation fires in that case to name the "all rigid" assumption
        # explicitly; it is suppressed when a split IS declared, so the output
        # always states which assumption it is making.
        discretionary_compressed = 0.0
        frac = ctx.config.discretionary_fraction
        if (frac is not None and ctx.income_shock_active
                and 0.0 <= frac <= 1.0):
            discretionary_compressed = ctx.living_costs * frac
        # Issue #760: the dated segments' charged amount (net of any
        # discretionary segment compressed under this year's shock) is added on
        # top of the scalar spend; the compressed segment dollars join the
        # scalar's discretionary compression so YearResult reports the full
        # stress relief transparently (DP#32).
        spending_outflow = (
            (ctx.living_costs - discretionary_compressed) + seg_charged
            + purchase_outflow + carrying_cost_outflow)
        discretionary_compressed += seg_discretionary_compressed
        available = (ctx.after_tax_income + ws.drawdown_net_delivered
                     + ctx.borrowed_investment + ctx.free_cash_invested
                     # Issue #967: a mid-horizon mortgage's ORIGINATED
                     # principal is an INFLOW in the purchase year (the
                     # mortgage funds the purchase). 0.0 otherwise (DP#32).
                     + ws.second_property_mortgage_originated
                     # epic #795 bite 3: the tuition_credit rule's per-member
                     # tax reduction (0.0 for a no-tuition household).
                     + ws.tuition_credit_applied_primary
                     + ws.tuition_credit_applied_spouse)
    required = debt_service + spending_outflow + contributions

    # epic #795 bite 3: ctx.after_tax_income is the PRE-credit after-tax
    # income the prologue now passes (the credit moved into the registered
    # tuition_credit rule, which writes the per-member tax reduction to ws).
    # Add the reduction back so YearResult.after_tax_income reports the
    # POST-credit figure byte-identical to the pre-refactor prologue output
    # (the characterization test pins it per year). 0.0 for a no-tuition
    # household, so the golden invariant is unchanged.
    ws.solvency_after_tax_income = (
        ctx.after_tax_income
        + ws.tuition_credit_applied_primary
        + ws.tuition_credit_applied_spouse
        # Issue #1083: the s.20(1)(c) nondrawdown routing's saving is a tax
        # reduction on income already inside ``ctx.after_tax_income`` -- report
        # the POST-saving figure, exactly as the tuition credits above report
        # the POST-credit one. 0.0 for every household/phase the routing does
        # not reach (the golden invariant is untouched).
        + ws.sm_interest_nondrawdown_tax_saving)
    # The reported `living_costs` field stays the declared working-phase
    # budget (what the household declared); only the identity's `required`
    # uses `spending_outflow` (the retirement target in retirement years, or
    # the non-discretionary portion under a shock when a split was declared)
    # so the field's semantics don't drift. Issue #761: the spending figure
    # actually charged and the discretionary dollars compressed are stamped
    # separately so the identity is transparent (DP#32).
    ws.solvency_living_costs = ctx.living_costs
    ws.solvency_spending_outflow = spending_outflow
    ws.solvency_discretionary_compressed = discretionary_compressed
    ws.solvency_debt_service = debt_service
    ws.solvency_contributions = contributions

    shortfall = required - available
    if shortfall <= 1e-9:
        return False

    from liquidation_waterfall import (
        LiquidationSource, capital_gains_cost, identity_cost,
        ordinary_income_cost, run_waterfall,
    )

    # Signed, NOT clamped to [0, 1] -- a forced sale while the account sits
    # below cost basis (the correlated job-loss/market-crash case issue
    # #679 is about) must report a genuine realised loss.
    non_reg_gain_frac = (
        (ws.new_nonreg_bal - ws.new_nonreg_acb) / ws.new_nonreg_bal
        if ws.new_nonreg_bal > 0 else 0.0
    )

    # Issue #689: the undrawn ROOM left on the credit facility, AFTER this
    # year's interest capitalization above -- $0 whenever no line_of_credit
    # was declared (credit_facility_limit defaults to 0.0, DP#32) and
    # correctly shrinking in a later shock year if an earlier one already
    # drew part of it. A household that never declared this facility gets
    # a reported, honest $0 here -- and the report says so explicitly below
    # -- rather than a silently invented one.
    revolving_credit_available = max(
        0.0, ctx.config.credit_facility_limit - ws.new_credit_facility_balance)
    ws.solvency_credit_facility_unrepresentable = ctx.config.credit_facility_limit <= 0

    # Issue #823: illiquidity -- balances whose account declared `locked_until`
    # are NOT liquid in any year the owner has not yet reached the unlock age,
    # so they are excluded from the solvency liquidation waterfall (and thus
    # from runway, which composes this verdict). After the owner's unlock age
    # the balance IS liquid. An empty `account_locked` map (no account
    # declared locked_until) excludes nothing -- today's fully-liquid
    # behaviour (golden). DP#1: the owner's age is date-computed from
    # calendar_year - owner_birth_year, never a hardcoded constant.
    locked_non_reg = _still_locked(ctx, 'non_reg')
    locked_tfsa = _still_locked(ctx, 'tfsa')
    locked_registered = _still_locked(ctx, 'rrsp') + _still_locked(ctx, 'spousal_rrsp')

    sources = [
        # Issue #688: the reserve is drawn FIRST, and it is the POST-growth
        # sleeve (apply_emergency_reserve_growth already compounded it at its
        # own declared cash rate this year), not the Jan-1 balance.
        LiquidationSource('emergency_reserve', ws.new_emergency_reserve, identity_cost),
        LiquidationSource('revolving_credit', revolving_credit_available, identity_cost),
        LiquidationSource('non_reg', max(0.0, ws.new_nonreg_bal - locked_non_reg),
                           capital_gains_cost(non_reg_gain_frac,
                                               ctx.config.capital_gains_inclusion,
                                               ctx.primary_marginal_rate)),
        LiquidationSource('tfsa', max(0.0, ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal - locked_tfsa), identity_cost),
        LiquidationSource('registered',
                           max(0.0, ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal - locked_registered),
                           ordinary_income_cost(ctx.primary_marginal_rate)),
    ]

    result = run_waterfall(shortfall, sources)
    drawn = {step.source: step.gross_drawn for step in result.steps}

    ws.new_emergency_reserve = max(
        0.0, ws.new_emergency_reserve - drawn.get('emergency_reserve', 0.0))
    ws.emergency_reserve_months_covered = months_covered(
        ws.new_emergency_reserve,
        annual_living_costs=ctx.living_costs,
        annual_debt_service=debt_service,
    )

    # Issue #689: whatever the waterfall drew from the credit facility this
    # year is now DEBT -- added to the balance already carried forward (with
    # this year's interest) above. It compounds, at credit_facility_rate,
    # every subsequent year via the capitalization step at the top of this
    # rule, exactly like margin_heloc_interest does for the HELOC.
    ws.new_credit_facility_balance += drawn.get('revolving_credit', 0.0)

    non_reg_drawn = drawn.get('non_reg', 0.0)
    if non_reg_drawn > 0:
        ws.new_nonreg_bal -= non_reg_drawn
        # DP#19 ACB bookkeeping mirrors the existing convention used
        # elsewhere in this fold (apply_retirement_drawdown, above): cost
        # basis is reduced dollar-for-dollar with the withdrawal amount,
        # floored at 0 -- not a proportional ACB reduction. The *reported*
        # realized_gain (liquidation_waterfall.capital_gains_cost, folded
        # into ws.solvency_realized_loss below) is still computed honestly
        # from the pre-withdrawal gain fraction; only the account's own
        # go-forward ACB bookkeeping follows the pre-existing convention.
        ws.new_nonreg_acb = max(0.0, ws.new_nonreg_acb - non_reg_drawn)

    tfsa_drawn = drawn.get('tfsa', 0.0)
    if tfsa_drawn > 0:
        tfsa_total = ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal
        if tfsa_total > 0:
            p_share = tfsa_drawn * (ws.new_tfsa_p_bal / tfsa_total)
            ws.new_tfsa_p_bal = max(0.0, ws.new_tfsa_p_bal - p_share)
            ws.new_tfsa_sp_bal = max(0.0, ws.new_tfsa_sp_bal - (tfsa_drawn - p_share))

    registered_drawn = drawn.get('registered', 0.0)
    if registered_drawn > 0:
        reg_total = ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal
        if reg_total > 0:
            p_share = registered_drawn * (ws.new_rrsp_bal / reg_total)
            s_share = registered_drawn * (ws.new_spousal_rrsp_bal / reg_total)
            sp_share = registered_drawn - p_share - s_share
            ws.new_rrsp_bal = max(0.0, ws.new_rrsp_bal - p_share)
            ws.new_spousal_rrsp_bal = max(0.0, ws.new_spousal_rrsp_bal - s_share)
            ws.new_spouse_rrsp_bal = max(0.0, ws.new_spouse_rrsp_bal - sp_share)

    ws.solvency_shortfall = shortfall
    ws.solvency_covered = result.covered
    ws.solvency_tax_paid = sum(step.tax for step in result.steps)
    ws.solvency_realized_loss = sum(min(0.0, step.realized_gain) for step in result.steps)
    # Issue #754: the positive counterpart -- a forced non-reg liquidation above
    # cost base realizes a taxable capital gain, surfaced for the year-end AMT base.
    ws.solvency_realized_gain = sum(max(0.0, step.realized_gain) for step in result.steps)
    ws.solvency_liquidations = [
        {'source': step.source, 'gross_drawn': step.gross_drawn,
         'net_proceeds': step.net_proceeds, 'tax': step.tax,
         'realized_gain': step.realized_gain}
        for step in result.steps
    ]
    ws.solvency_ruined = result.ruined
    return True


@rule('amt')
def apply_amt(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Year-end Alternative Minimum Tax assessment (issue #710; ITA s.127.5-127.6).

    AMT ensures a taxpayer with large preference items pays at least a floor
    tax: the household is charged ``max(regular tax, minimum amount)``, and this
    rule books the SURCHARGE (minimum - regular) on top of the regular tax the
    fold already priced. It runs LAST in ``RULE_ORDER`` because AMT is a year-end
    assessment over ALL of the year's realized income -- after the retirement
    drawdown and every forced solvency liquidation have crystallized their gains.

    This is the wiring #710 asked for and #754 unblocked. ``countries.canada.amt``
    -- fully implemented, unit-tested, and called by NOTHING in production -- is
    invoked here on the year's realized income via ``total_tax_with_amt`` ->
    ``compute_amt`` (the ``max(regular, AMT)`` is handled inside). The regular
    federal tax the minimum is measured against is assembled from tax_calc's
    federal helpers (the same federal side as ``compute_total_tax``); the
    provincial tax and Quebec refundable credits play no part in the federal AMT
    comparison, so they are deliberately not pulled in here (their own wiring is
    #745).

    WHY THE REALIZED-CAPITAL-GAIN GATE IS THE WHOLE STORY (#754). With a correct
    s.127.52(1) base, the ONLY add-back big enough to lift AMTI above regular
    taxable income far enough to clear the basic exemption ABOVE regular tax is
    the 100% capital-gains inclusion (s.127.52(1)(d)): carrying charges are only
    half added back, the RRSP deduction is not an add-back at all, and the fold
    surfaces no stock-option benefit or loss carryover. So in a year the fold
    realizes NO capital gain, AMTI equals regular taxable income and the minimum
    amount can never exceed regular tax -- the surcharge is identically 0. The
    early return on ``realized_gain <= 0`` is that arithmetic, not a silent
    "assume no AMT" default (DP#32): it is the exact set of years in which AMT
    provably cannot bite, and it makes the assessment a strict no-op for every
    household the fold surfaces no realized gain for (e.g. the #581 golden
    household, which realizes 0 gains in all 46 years -> the golden invariant is
    unmoved by construction).

    MODELLED as of #747 (the three #710 deferrals, all cross-year/parallel):
      * the 50%-of-non-refundable-credits reduction to the minimum amount
        (ITA s.127.531) -- the same federal credits the regular tax is net of
        are passed to ``total_tax_with_amt``;
      * the 7-year AMT carry-forward (ITA s.120.2) -- AMT paid becomes a credit
        recovered against regular tax in a later year where regular tax exceeds
        the minimum amount, expiring after 7 years. The credit balance is
        cross-year state carried in ``ctx.amt_credit_opening`` and written out on
        ``ws.amt_credit_closing`` (pure fold, DP#26);
      * Quebec's separate impôt minimum de remplacement (19%, own exemption;
        TP-776.42) -- a SEPARATE surcharge booked on ``ws.qc_imr_surcharge``,
        with its own carry-forward, when the household is a Quebec resident.

    Reads: ``drawdown_realized_capital_gain`` + ``solvency_realized_gain`` (the
    year's 100%-inclusion realized gain, #754), ``drawdown_taxable`` /
    ``lif_withdrawal`` / government retirement income (already on ws), this
    year's grown employment income off ``ctx``, and the opening minimum-tax
    credit balances (``ctx.amt_credit_opening`` / ``ctx.qc_imr_credit_opening``).
    Writes: ``amt_surcharge`` / ``qc_imr_surcharge`` / ``amt_taxable_income``,
    the recovered-credit and closing-balance fields, and charges the NET tax
    (new surcharges minus recovered credits) against the non-registered pot.
    Issue #1082: whatever slice of that net charge the pot cannot fund is
    recorded on ``amt_unfunded`` (surfaced on YearResult alongside the gross
    ``amt_net_charge``) -- reported, never silently absorbed (DP#32).

    Returns True whenever a minimum tax is assessed OR a carried credit is
    recovered, so the #584 coverage sweep sees it fire and stay a no-op for a
    household that neither realizes a gain nor carries a credit (the golden
    household: opening balance empty and 0 realized gain every year, so the
    fast no-op below fires unconditionally and the invariant is unmoved).
    """
    realized_gain = (ws.drawdown_realized_capital_gain
                    + ws.solvency_realized_gain
                    + ws.heloc_servicing_realized_gain)
    # Fast no-op: with no realized gain AND no minimum-tax credit carried in,
    # neither a new minimum-tax assessment nor a recovery is possible (a year
    # with no gain has AMTI == regular taxable income, so the minimum cannot
    # exceed regular tax; with no opening credit there is nothing to recover).
    # The golden household hits this every one of its 46 years (DP#32).
    if realized_gain <= 0 and not ctx.amt_credit_opening and not ctx.qc_imr_credit_opening:
        return False

    # AMT parameters are year-versioned and only defined for a real tax year;
    # a direct unit-test caller that omits calendar_year gets the projection
    # INDEX here (0, 1, ...), which is not a tax year. The live run always
    # supplies the absolute calendar year (start_year + year), so this only
    # skips isolated rule-mechanics tests, never a real assessment.
    if ctx.calendar_year < 2024:
        return False

    from countries.canada.amt import (
        AMTParameters, total_tax_with_amt, carry_forward_amt_credit,
        QuebecIMRParameters, compute_quebec_imr,
    )
    from countries.canada.tax_calc import (
        compute_non_refundable_credits,
        federal_tax_before_abatement,
        quebec_abatement_amount,
        quebec_tax,
    )

    province = ctx.config.province
    year = ctx.calendar_year
    provider = default_tax_provider()

    # This year's ordinary taxable income: actual employment income (grown, and
    # pre-net of deductions -- a conservative overstatement that RAISES regular
    # tax and so can only SHRINK the surcharge, never fabricate one) plus every
    # taxable retirement component. A RETIRED member earns no salary this year,
    # so their (still-populated) pre-retirement grown income is excluded -- else
    # a retiree realizing a large gain would have regular tax computed as if the
    # salary were still coming in, wrongly suppressing the very AMT the gain owes.
    # The taxable (50%-included) slice of the realized gain already lives inside
    # drawdown_taxable (#754), so it is NOT re-added.
    employment_income = 0.0
    if not ctx.primary_retired:
        employment_income += ctx.primary_income_pre
    if not ctx.spouse_retired:
        employment_income += ctx.spouse_income_pre
    taxable_income = (
        employment_income
        + ws.drawdown_taxable
        + ws.heloc_servicing_taxable
        + ws.lif_withdrawal
        + ws.cpp_income
        + ws.oas_income
        + ws.pension_income
    )
    # The regular-inclusion slice of the realized gain -- already in
    # taxable_income -- that total_tax_with_amt grosses up to the AMT's 100%
    # inclusion (s.127.52(1)(d)). Issue #1082: the inclusion rate is the
    # household's declared assumption (ctx.config.capital_gains_inclusion),
    # the same one every other disposition in the fold prices with -- not a
    # hardcoded 0.5 that diverges silently if the input declares otherwise.
    inclusion = ctx.config.capital_gains_inclusion
    taxable_capital_gains = inclusion * realized_gain

    # Regular FEDERAL tax after the Quebec abatement and federal non-refundable
    # credits -- the figure the minimum amount is measured against (CRA T691).
    # This is exactly the federal side of compute_total_tax; the AMT comparison
    # is federal-only, so the provincial tax and Quebec refundable credits
    # (solidarity/QPIP/FSS -- their own deliberate non-wiring, #745) are not part
    # of it and are not computed here.
    gross_fed = federal_tax_before_abatement(taxable_income, year, province, provider)
    abatement = quebec_abatement_amount(taxable_income, year, province, provider)
    nr_credits = compute_non_refundable_credits(
        employment_income, taxable_income, year, province, provider,
    )['total']
    federal_after_credits = max(0.0, gross_fed - abatement - nr_credits)

    # Pass the same federal non-refundable credits the regular tax is net of:
    # 50% of them reduce the minimum amount (ITA s.127.531, #747), so both sides
    # of the max(regular, minimum) comparison carry the credits.
    tax = total_tax_with_amt(
        regular_tax=federal_after_credits,
        taxable_income=taxable_income,
        taxable_capital_gains=taxable_capital_gains,
        capital_gains_inclusion=inclusion,
        nonrefundable_credits=nr_credits,
        params=AMTParameters.for_year(year, provider),
    )
    surcharge = tax['amt_surcharge']
    ws.amt_taxable_income = taxable_income
    ws.amt_surcharge = surcharge

    # ── Federal 7-year carry-forward (ITA s.120.2, #747) ──
    # Recover a carried credit in a year regular tax exceeds the minimum amount,
    # up to that excess; book this year's own surcharge as a fresh 7-year credit.
    fed_room = max(0.0, federal_after_credits - tax['minimum_amount'])
    fed_recovered, fed_closing = carry_forward_amt_credit(
        ctx.amt_credit_opening, year, surcharge, fed_room,
    )
    ws.amt_credit_recovered = fed_recovered
    ws.amt_credit_closing = tuple(fed_closing)

    # ── Quebec impôt minimum de remplacement (TP-776.42, #747) ──
    # A separate provincial minimum tax (19%, own exemption), measured against
    # regular Quebec provincial tax, with its own 7-year carry-forward. Booked
    # on ws.qc_imr_surcharge, kept apart from the federal amt_surcharge because
    # they are two distinct taxes. QC non-refundable credits are not recomputed
    # here (the fold prices them elsewhere), so the QC minimum is not reduced by
    # them -- a conservative overstatement of the QC layer, never a fabrication.
    qc_surcharge = 0.0
    qc_recovered = 0.0
    qc_closing = tuple(ctx.qc_imr_credit_opening)
    if province in ('quebec', 'qc'):
        regular_qc = quebec_tax(taxable_income, year, provider)
        imr = compute_quebec_imr(
            regular_qc_tax=regular_qc,
            adjusted_income=tax['adjusted_income'],
            params=QuebecIMRParameters.for_year(year, provider),
        )
        qc_surcharge = imr['imr_surcharge']
        qc_room = max(0.0, regular_qc - imr['imr_minimum'])
        qc_recovered, qc_closing_list = carry_forward_amt_credit(
            ctx.qc_imr_credit_opening, year, qc_surcharge, qc_room,
        )
        qc_closing = tuple(qc_closing_list)
    ws.qc_imr_surcharge = qc_surcharge
    ws.qc_imr_credit_recovered = qc_recovered
    ws.qc_imr_credit_closing = qc_closing

    # ── Net cash effect on the non-registered pot ──
    # New minimum-tax surcharges (federal + QC) are charged to the non-reg pot
    # whose disposition triggered them; recovered credits are a genuine tax
    # refund credited BACK to it. Net them so a year that only recovers (a later
    # low-minimum year) restores cash, and a year that only pays draws it.
    new_charge = surcharge + qc_surcharge
    recovered = fed_recovered + qc_recovered
    net_charge = new_charge - recovered
    # Issue #1082: surface the assessed net charge and whatever slice of it the
    # non-reg pot cannot fund. A household realizing a large gain while its
    # non-reg pot is already drained (the #1043 servicing rule empties it
    # first) owes a minimum tax on money it no longer holds -- discarding that
    # remainder understated tax and overstated leveraged net worth. Reported,
    # never absorbed (DP#32), mirroring heloc_interest_unfunded (#681).
    ws.amt_net_charge = max(0.0, net_charge)
    ws.amt_unfunded = 0.0
    if net_charge > 0:
        # ACB is floored to the reduced balance so acb <= fmv holds (paying the
        # tax is a cash draw at book value, not a further taxable disposition).
        funded = min(net_charge, ws.new_nonreg_bal)
        ws.amt_unfunded = net_charge - funded
        ws.new_nonreg_bal -= funded
        if ws.new_nonreg_acb > ws.new_nonreg_bal:
            ws.new_nonreg_acb = ws.new_nonreg_bal
    elif net_charge < 0:
        # Net refund: the recovered credit reduced regular tax below what was
        # charged. Reinvest it in the non-reg pot at book value (bal and acb
        # rise together, so acb <= fmv is preserved).
        refund = -net_charge
        ws.new_nonreg_bal += refund
        ws.new_nonreg_acb += refund

    return surcharge > 0 or qc_surcharge > 0 or recovered > 0
