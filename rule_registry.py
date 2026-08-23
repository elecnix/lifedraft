"""The rule interface: the explicit per-year state, and the registry (DP#26).

Split out of ``simulation_rules.py`` (issue: split the 5,654-line rule layer)
so that each domain rule module can import *what a rule is* -- the frozen
per-call ``RuleContext`` it reads, the mutable ``YearWorkingState`` it writes,
and the ``@rule(name)`` decorator it registers itself with -- WITHOUT importing
the fold that orders and runs them. ``simulation_rules`` holds ``RULE_ORDER`` /
``run_rules`` and imports every rule module; every rule module imports this one.
Dependencies point one way (DP#25) and there is no import cycle.

Nothing else moved: ``RuleContext``, ``YearWorkingState``, ``RuleFn``,
``RULES``, ``rule`` and ``all_rule_names`` are the same definitions, byte for
byte, that lived in ``simulation_rules.py``.

Per DP#26/#583: no rule function reads ``self`` or any instance -- every input
arrives explicitly via ``ws``/``ctx``. Per DP#25: this module makes no
module-level ``countries.canada`` import; jurisdiction-specific helpers are
imported inside each rule function body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from simulation_config import SimulationConfig


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
