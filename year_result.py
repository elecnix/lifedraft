#!/usr/bin/env python3
"""``YearResult`` -- one simulated year's output record.

Split out of ``simulation_config.py``. This is the engine's per-year OUTPUT
(what ``simulate_year`` returns and ``run`` folds into a list), as distinct from
``SimulationConfig``, which is its INPUT. It is a pure field dataclass with no
behaviour and no dependencies on anything else in the config layer.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class YearResult:
    """Snapshot of family finances for one year of simulation."""
    year: int = 0

    # Rates
    mortgage_rate: float = 0.0
    heloc_rate: float = 0.0

    # Income
    primary_income: float = 0.0
    spouse_income: float = 0.0
    total_family_income: float = 0.0
    annual_savings: float = 0.0

    # Contributions this year
    contributions: Dict[str, float] = field(default_factory=dict)

    # Account balances (end of year)
    primary_rrsp: float = 0.0
    spousal_rrsp: float = 0.0
    spouse_rrsp: float = 0.0
    total_rrsp: float = 0.0
    primary_tfsa: float = 0.0
    spouse_tfsa: float = 0.0
    total_tfsa: float = 0.0
    resp_balance: float = 0.0
    non_reg_balance: float = 0.0
    total_assets: float = 0.0

    # RESP wind-down (issue #578): EAP (taxable to the student, not the
    # household) and PSE (contributions returned tax-free to the subscriber)
    # paid out this year, plus the AIP tax cost if the plan collapsed unused.
    resp_eap_paid: float = 0.0
    resp_pse_paid: float = 0.0
    resp_aip_tax: float = 0.0

    # Debt
    mortgage_balance: float = 0.0
    heloc_balance: float = 0.0
    total_debt: float = 0.0

    # Tax
    primary_marginal: float = 0.0
    spouse_marginal: float = 0.0
    bracket_gap: float = 0.0
    rrsp_tax_savings: float = 0.0
    # Issue #546: per-year deduct-later claim slices, each
    # {'year': contribution_year, 'amount': claimed, 'rate': bracket-fill rate}.
    deduction_claims: List[Dict] = field(default_factory=list)
    # Issue #546: cumulative advantage of the staggered deduct-later schedule
    # over deducting the same total amount all in a single (first-claim) year.
    # Defined as (sum of staggered bracket-fill savings across claim years)
    # minus (bracket-fill value of deducting that whole total in the first
    # claim year, where later dollars of the lump sink into low brackets).
    # >= 0 always; > 0 exactly when spreading the deduction keeps slices in
    # higher brackets year over year instead of wasting room on low brackets.
    deduction_advantage_vs_now: float = 0.0
    readvance_interest: float = 0.0
    readvance_tax_savings: float = 0.0
    # Issue #1083: the s.20(1)(c) deduction's statutory (bracket-fill) tax
    # saving on the retired primary's PROLOGUE-taxed slice (rental/loan
    # income), for the share of the deduction the cpp+pension drawdown base
    # could not absorb. Booked as cash by ``apply_solvency`` (the
    # tuition_credit path); 0.0 in every accumulation year and for any
    # household whose deduction fits inside the drawdown base. The flat
    # objective side-credit (``readvance_tax_savings``) stays 0 in retirement
    # -- this is the taxable-income routing, not its return.
    sm_interest_nondrawdown_tax_saving: float = 0.0
    sm_qc_deductible: float = 0.0
    sm_qc_carry_forward: float = 0.0
    sm_deductible_proportion: float = 0.0  # From HELOC tracing

    # Issue #850: the s.20(1)(c) position of the OTHER two borrowings a year-0
    # leveraged lump sum creates -- the mortgage ADVANCE (cash-out) and the
    # DRAWN revolving margin. Both were previously deducted NOWHERE, so
    # advance-vs-line was ranked on the rate gap and interest capitalization
    # alone, with the deductibility asymmetry that motivates the choice (#849)
    # set to zero on both sides.
    #
    # The two *_deductible_balance fields are the instrument that measures that
    # asymmetry directly: the ADVANCE's falls year over year as the mortgage
    # amortizes (a fixed deductible proportion of a shrinking balance -- the
    # erosion #849 names), while the LINE's does not (it is interest-only and
    # capitalizes into the charge). All 0.0 for a household that borrowed no
    # lump sum -- e.g. the golden household (DP#32).
    advance_deductible_balance: float = 0.0
    advance_deductible_interest: float = 0.0
    margin_deductible_balance: float = 0.0
    margin_deductible_interest: float = 0.0
    # The tax actually saved by the two legs above, AFTER the shared QC
    # investment-expense cap. Distinct from readvance_tax_savings (the SM
    # readvance line's own share of that same cap).
    traced_borrowing_tax_savings: float = 0.0

    # Issue #693 (epic #690 bite 2): a declared rental property's income for the
    # year. `net_rental_income` is gross rent minus operating expenses minus the
    # deductible mortgage interest (ITA s.20(1)(c)) -- the ordinary-income figure
    # added to the owner's taxable income and to household after-tax cash (CRA
    # form T776). `rental_interest_deductible` is the s.20(1)(c) mortgage-interest
    # deduction embedded in it, surfaced separately so the deductibility is
    # observable. Both 0.0 for a household that declares no couple-owned rental
    # (the golden path) -- a no-op there (DP#32).
    #
    # Issue #694 (epic #690 bite 3): a rental may also elect Capital Cost
    # Allowance -- non-cash depreciation of the building. `cca_claimed` is the
    # year's CCA (already netted OUT of `net_rental_income` above, since CCA is a
    # T776 deduction line, but NOT out of after-tax cash: it is non-cash);
    # `rental_ucc` is the running per-property undepreciated capital cost, threaded
    # to next year and read by the estate to recapture the CCA at the deemed
    # disposition (ITA s.13(1)). Both inert (0.0 / {}) with no CCA election.
    net_rental_income: float = 0.0
    rental_interest_deductible: float = 0.0
    cca_claimed: float = 0.0
    rental_ucc: Dict[str, float] = field(default_factory=dict)

    # Issue #697 (epic #690 bite 6): a SHORT-TERM rental (Airbnb-style) is
    # ACTIVE business income (ITA s.9), not passive property income. Its net
    # income is included in net_rental_income above (business income is ordinary
    # income too), but surfaced HERE distinctly as `str_business_income` -- the
    # subset of net_rental_income earned by declared STR properties. Its gross
    # revenue is tested against the $30k GST/HST small-supplier threshold (ETA
    # s.148): `gst_hst_registration_required` is True when any STR's gross rent
    # exceeds it. Both inert (0.0 / False) for a household with no STR (the
    # golden path -- a long-term rental never triggers the flag, DP#32).
    str_business_income: float = 0.0
    gst_hst_registration_required: bool = False

    # Issue #702: the year's attribution-rule DETECTION over the declared
    # private loans -- ITA s.74.1 (spousal property transfer), s.74.2 (minor
    # child) and s.74.5(1) (below-market / prescribed-rate-loan escape) -- one
    # dict per loan, computed by simulation._attribution_checks_for from the
    # rule functions in countries.canada.attribution (DP#10). DETECTION ONLY:
    # it changes no tax number (the one attribution flow the engine PRICES --
    # a minor LENDER's interest attributed to the borrower under s.74.2 -- is
    # #929's wiring in private_loan_interest.classify_private_loan_interest).
    # Empty for a household that declares no private loans (the golden path,
    # DP#32: no trigger data -> no entries, zero overhead).
    attribution_summary: List[Dict] = field(default_factory=list)

    # Mortgage details
    mortgage_payment: float = 0.0
    mortgage_interest: float = 0.0
    mortgage_principal: float = 0.0
    # issue #681: sm_readvanced is what the CHARGE allowed to be re-borrowed
    # this year -- NOT simply mortgage_principal (which is what it used to
    # be, when the readvance was unbounded and a household could re-borrow
    # its way to 382% LTV). readvance_room is the room the shared charge
    # left at the moment of the draw; readvance_blocked is principal repaid
    # that could NOT be re-borrowed because the charge was full. A blocked
    # readvance is reported, never silently truncated (DP#32).
    sm_readvanced: float = 0.0
    readvance_room: float = 0.0
    readvance_blocked: float = 0.0
    # issue #681: a revolving facility cannot capitalize interest past the
    # charge (it used to, unboundedly -- a $400k margin draw compounded to
    # $1M+ of revolving debt straight through a $720k charge, and the run
    # stayed green). What the charge had room for is capitalized; the rest is
    # SERVICED IN CASH out of non-registered savings, then out of the SM
    # investment itself; anything the household cannot fund at all is
    # reported here rather than silently absorbed (DP#32).
    heloc_interest_capitalized: float = 0.0
    heloc_interest_serviced: float = 0.0
    heloc_interest_unfunded: float = 0.0
    # Issue #1069: the year's TOTAL margin interest charged (opening drawn
    # balance x rate), surfaced so the split below is CHECKABLE rather than
    # taken on faith: capitalized + serviced must equal it, and serviced must
    # equal funded + unfunded -- the identity
    # 'heloc_interest_fully_accounted' asserts every year in the fold (DP#18).
    heloc_interest_charged: float = 0.0
    # Issue #1034: forced dispositions of the non-reg and SM pots to service
    # HELOC interest realize a taxed capital gain and reduce the cost basis
    # proportionally (matching sm_unwind, which reuses price_sm_unwind).
    # ``heloc_servicing_realized_gain`` is the year's total pre-inclusion gain
    # from both legs; it is wired into apply_amt's realized_gain base (the
    # AMT minimum-tax side) and surfaced here for transparency (DP#32).
    # ``heloc_servicing_tax`` is the tax paid (funded by grossing the sale up).
    # The taxable slice (``heloc_servicing_taxable``) is an internal AMT
    # accumulator on YearWorkingState, not surfaced here. 0.0 in every year no
    # pot is sold to service interest (incl. the golden household) -- inert,
    # byte-identical (DP#32).
    heloc_servicing_realized_gain: float = 0.0
    heloc_servicing_tax: float = 0.0
    # Issue #1069: what the pots actually DELIVERED toward the serviced
    # interest (net of the gross-up tax), accumulated leg by leg. With
    # ``heloc_interest_unfunded`` it closes the serviced-slice identity:
    # heloc_interest_serviced == heloc_servicing_funded +
    # heloc_interest_unfunded, asserted every year by the run-path invariant.
    heloc_servicing_funded: float = 0.0
    # Issue #1031: the Smith-Manoeuvre investment SLEEVE -- the leveraged
    # non-registered portfolio that lives in jurisdiction_state['canada']
    # (NOT the top-level non_reg_balance), tracked separately because it was
    # financed by the readvanceable HELOC. Surfaced here so the estate's deemed
    # disposition (#1031) can read the terminal FMV / cost basis / HELOC debt
    # without reaching into the jurisdiction dict, and so a test/output surface
    # can observe them directly (DP#32). All 0.0 for a household with no SM
    # sleeve (the golden household) -- inert, byte-identical (DP#32).
    sm_investment_balance: float = 0.0      # SM portfolio FMV (separate sleeve)
    sm_investment_cost_basis: float = 0.0   # SM portfolio ACB (DP#19)
    sm_heloc_balance: float = 0.0           # SM readvance line debt financing it
    # Issue #1017: the liquidate-to-target SM unwind -- when the ordinary
    # financial drawdown exhausts every drawable account and a shortfall
    # remains, a slice of the SM sleeve is sold (realizing a capital gain), the
    # proceeds repay the SM HELOC proportionally, the capital-gains tax is paid,
    # and the NET funds the spending target. Surfaced so a test/output surface
    # can observe the unwind directly (DP#32). All 0.0 in every year no unwind
    # fires (no SM sleeve, or liquidate_to_target off, or no shortfall) -- the
    # golden household -> inert, byte-identical (DP#32).
    sm_unwind_proceeds: float = 0.0         # SM portfolio FMV sold this year
    sm_unwind_tax: float = 0.0              # capital-gains tax on the realized gain
    sm_unwind_heloc_repaid: float = 0.0     # SM HELOC principal repaid
    sm_unwind_net_delivered: float = 0.0    # net to the spending target

    # ACB tracking (DP#19: track cost basis from day one)
    non_reg_acb: float = 0.0           # Adjusted cost base for non-reg
    non_reg_unrealized_gains: float = 0.0  # balance - acb
    # Issue #754: the capital gain REALIZED this year by disposing of non-reg
    # assets -- proceeds minus ACB, at 100% (NOT the 50%-included taxable slice,
    # which is already folded into drawdown_taxable). Sums the retirement-drawdown
    # disposition and any forced solvency-waterfall liquidation. 0.0 in every year
    # the non-reg pot is only accumulated (never disposed) -- e.g. every
    # pre-retirement year (DP#32). Surfaced so a year-end AMT assessment (#710,
    # ITA s.127.52(1)(d): 100% inclusion) and other consumers read a real realized
    # base instead of re-deriving it from the bundled taxable total.
    # Issue #956 bite B (sale-core): also folds in the realized gain from a
    # declared mid-horizon property SALE (ws.sale_realized_gain), so the AMT
    # base sees a real realized base from the property disposition too.
    realized_capital_gains: float = 0.0
    # Issue #956 bite B (sale-core): the net proceeds (P_net) invested into
    # non-reg this year from declared property SALES, and the capital-gains
    # tax (T) those sales crystallized (net of the PRE apportionment). The tax
    # is already netted out of P_net (the household receives the after-tax
    # proceeds); surfaced here for transparency (DP#32), NOT added to the
    # year's ordinary income-tax base (the tax is computed once, in T). Both
    # 0.0 in every year no property is sold (the golden household -- DP#32).
    sale_proceeds_invested: float = 0.0
    sale_disposition_tax: float = 0.0
    # Issue #956 bite E (principal-residence disposition): the net proceeds
    # (P_net) invested into non-reg this year from a declared SALE of the
    # PRINCIPAL residence, the PRE-apportioned capital-gains tax (T, already
    # netted out of P_net), the pre-inclusion realized gain (for the AMT
    # base), and the secured debt discharged at the sale (mortgage + HELOC +
    # SM-HELOC -- the debt leg of the net_assets conservation identity). The
    # principal's value is NOT in total_assets (it flows via house_value /
    # charge math), so the conservation identity is on NET_ASSETS
    # (Δnet_assets = V - selling_costs - T), not total_assets. Surfaced for
    # transparency (DP#32); NOT added to the ordinary income-tax base (the
    # tax is computed once, in T). All 0.0 in every year no principal is sold
    # (the golden household -- DP#32).
    principal_sale_proceeds_invested: float = 0.0
    principal_sale_disposition_tax: float = 0.0
    principal_sale_realized_gain: float = 0.0
    principal_sale_discharged_debt: float = 0.0
    # Issue #710: the Alternative Minimum Tax surcharge assessed this year --
    # max(0, minimum amount - regular federal tax after credits), booked on top
    # of the regular tax the fold already priced (the household is charged
    # max(regular, AMT)). 0.0 in every year the fold realizes no capital gain,
    # which is every year AMT cannot bite in this engine (#754) -- so 0.0 for
    # the golden household in all 46 years (DP#32).
    amt_surcharge: float = 0.0
    # Issue #747: the Quebec impôt minimum de remplacement surcharge (a separate
    # provincial minimum tax, TP-776.42), the minimum-tax credit recovered this
    # year against regular tax (ITA s.120.2), and the closing federal credit
    # balance carried forward. All 0.0 for a household that never pays a minimum
    # tax (the golden household, all 46 years -- DP#32).
    qc_imr_surcharge: float = 0.0
    amt_credit_recovered: float = 0.0
    qc_imr_credit_recovered: float = 0.0
    amt_credit_balance: float = 0.0
    # Issue #1082: the net minimum-tax charge assessed this year (new
    # surcharges minus recovered credits, floored at 0) and the slice of it the
    # non-registered pot could not fund -- reported, never silently absorbed
    # (DP#32; before #1082 an unfunded remainder simply vanished, understating
    # tax by up to $380k in the issue's reported case -- fabricated round
    # figures, DP#4/DP#15). Both 0.0 whenever no minimum
    # tax is assessed or the pot fully funds it (the golden household).
    amt_net_charge: float = 0.0
    amt_unfunded: float = 0.0

    # CRI/LIRA and LIF (issue #230)
    lira_balance: float = 0.0          # CRI/LIRA balance (accumulation phase)
    lif_balance: float = 0.0          # LIF balance (decumulation phase)
    lif_withdrawal: float = 0.0       # LIF withdrawal this year (taxable income)

    # Retirement transition (issue #294): government income + drawdown that
    # engage once a member crosses retirement_age within the projection.
    cpp_income: float = 0.0           # Combined CPP/QPP for the family this year
    oas_income: float = 0.0           # Combined OAS (net of clawback) this year
    # Issue #1033: the OAS 15% recovery-tax (clawback) the drawdown + forced
    # RRIF minimum triggered this year -- the drawdown+RRIF slice (the
    # preliminary recovery tax ``member_retirement_income`` booked on the
    # CPP+pension base, before ``sm_interest`` runs, is bundled into
    # ``oas_income`` above and NOT broken out here). Surfaced for TEST /
    # OPTIMIZER observability: the s.20(1)(c) investment-interest deduction
    # (routed through ``drawdown_other_taxable_income`` in
    # ``apply_retirement_drawdown``) REDUCES this, and the deferred
    # income-flowing half will later RAISE it. NOTE: this field is NOT in
    # ``output_plugins.YEAR_COLUMNS`` -- it is read by tests/optimizer, not by
    # the report renderer (NEW-4: the docstring no longer overstates "observable
    # on YearResults" as if reports surface it). 0.0 in every pre-retirement year
    # (no OAS) and for a household below the recovery-tax threshold.
    oas_clawback: float = 0.0
    pension_income: float = 0.0       # Combined defined-benefit pension this year
    # Issue #1020 (S04 Step 1): GIS (Guaranteed Income Supplement) paid to
    # the family this retirement year. The simulation fold now calls the
    # existing ``countries.canada.retirement.gis_benefit`` (DP#9: reused, not
    # re-spelled) from ``apply_retirement_income`` using the PRIOR year's
    # GIS-countable income (CRA's prior-year income test — OAS is excluded
    # from the test base, per the helper's documented ``net_income`` contract).
    # Folded into ``retirement_income`` / ``total_family_income`` / the
    # drawdown ``covered_net`` (GIS is cash that covers spending, so it
    # reduces the discretionary drawdown shortfall). 0.0 in every pre-retirement
    # year and for every GIS-ineligible household (high income -> GIS=0,
    # DP#32). The golden household is GIS-ineligible, so this stays 0.0 across
    # its whole 46-year horizon and the golden invariant is byte-unchanged.
    gis_income: float = 0.0           # GIS paid this year (prior-year income test)
    drawdown_income: float = 0.0      # Registered/non-reg drawdown this year (gross)
    drawdown_taxable: float = 0.0     # Portion of drawdown taxable as ordinary income
    # Issues #363/#579: the requested NET (after-tax) spending target for the
    # year's discretionary drawdown, and what plan_drawdown_net actually
    # delivered net of tax. Equal within tolerance unless account balances ran
    # out (delivered < target). 0.0 in every pre-retirement / non-drawdown year.
    drawdown_net_target: float = 0.0
    drawdown_net_delivered: float = 0.0
    # Issue #707: the NET spending gap the household could NOT fund from
    # drawdown because every drawable account was exhausted -- target minus
    # delivered, only in a year the target was positive AND the post-drawdown
    # balance across every drawable account is ~0. 0.0 in every year the
    # target was met (or no drawdown was requested). Distinct from
    # ``solvency_shortfall`` (#679), which is the cash-flow identity gap
    # against declared ``household_budget.annual_living_costs`` and fires
    # whether or not the drawdown itself fell short. The two can co-exist:
    # a household whose drawdown already ran out may ALSO fail the cash-flow
    # identity. DP#32: a bankrupt year must not be reported as just another
    # number -- this field is what makes the shortfall a directly testable
    # fact rather than something only observable by re-deriving target -
    # delivered from two other fields.
    drawdown_shortfall: float = 0.0
    retirement_income: float = 0.0    # CPP + OAS + pension + drawdown (total)
    employment_income: float = 0.0    # Family employment income (post-retirement stop)
    # Issue #758: True when any member is past retirement_age this year (the
    # retirement drawdown model is active). The #679 solvency rule uses the
    # RETIREMENT spending target in these years (not the working-phase
    # living_costs); #758's runway metric uses this to scope itself to
    # WORKING life -- a retirement-year solvency event (e.g. a mortgage not
    # fully paid off by retirement) is a retirement-plan question (#707's
    # domain), not a shock-induced working-life runway event.
    any_retired: bool = False

    # Net benefit calculation
    net_benefit: float = 0.0

    # ── Solvency (issue #679) ──────────────────────────────────────────
    # The cash-flow identity checked every year by simulation_rules
    # .apply_solvency: after_tax_income + drawdown_net_delivered >=
    # debt_service + living_costs + contributions. All zero/empty/False in
    # every year the household's own living-cost budget was never supplied
    # (household_budget.annual_living_costs absent -- DP#16, the module
    # does not run without its trigger data).
    after_tax_income: float = 0.0       # Employment income net of tax this year
    living_costs: float = 0.0           # This year's declared working-phase budget
    # Issue #761: the spending figure actually CHARGED in the cash-flow
    # identity's `required` term this year. Equals `living_costs` in every
    # year except a working-life income-shock year where the household
    # declared a discretionary split -- then it is the NON-discretionary
    # portion (living_costs * (1 - discretionary_fraction)), the
    # discretionary portion having been compressed to zero. 0.0 in every
    # year the solvency rule did not run (no budget declared). Reported so
    # the identity is transparent about which figure it charged, never a
    # silent substitution (DP#32).
    solvency_spending_outflow: float = 0.0
    # Issue #761: discretionary dollars compressed to zero this year under
    # an income shock (living_costs * discretionary_fraction, only in a
    # working-life shock year where a split was declared). 0.0 otherwise --
    # including when no split was declared, which is the "all rigid"
    # assumption the output names explicitly via model_fidelity. A positive
    # value is the labelled stress assumption (discretionary cuts to zero
    # under a shock), not a silent default.
    solvency_discretionary_compressed: float = 0.0
    # Issue #760: this year's dated living-cost segment outflow actually CHARGED
    # in the solvency identity's spending term (on top of `living_costs`) --
    # each active segment's annual amount prorated by the days of [from, to)
    # falling in this calendar year, MINUS any discretionary segment compressed
    # to zero by an income shock. 0.0 in every year no segment is active (before
    # a segment's `from`, after its `to` -- it ENDS, never carried to the
    # horizon) and in every year no segments were declared. The discretionary
    # segment dollars compressed under a shock are folded into
    # solvency_discretionary_compressed above, so the identity stays transparent.
    expense_segment_outflow: float = 0.0
    debt_service: float = 0.0           # Mortgage payment this year (the identity's debt-service term)
    contributions_total: float = 0.0    # This year's ACTUAL booked contributions (post room-clamping), the identity's contributions term
    solvency_shortfall: float = 0.0     # required - available, before any liquidation (0 if solvent)
    solvency_covered: float = 0.0       # Net dollars the liquidation waterfall actually delivered
    forced_liquidation_tax: float = 0.0        # Total tax paid across every waterfall step this year
    forced_liquidation_realized_loss: float = 0.0  # Sum of negative realized_gain across steps (issue #679: reported honestly, never floored at 0)
    # Per-step detail: [{'source', 'gross_drawn', 'net_proceeds', 'tax', 'realized_gain'}, ...],
    # in the order actually drawn (emergency_reserve -> revolving_credit ->
    # non_reg -> tfsa -> registered). Empty in every solvent year.
    forced_liquidation_events: List[Dict] = field(default_factory=list)

    # ── Emergency reserve (issue #688) ─────────────────────────────────
    # The reserve is a CASH SLEEVE CARVED OUT of the account named in
    # assumptions.emergency_reserve.held_in -- NOT extra money on top of
    # it. It grows at its own declared instrument rate, never the
    # portfolio's (a reserve compounding at the equity return is not a
    # reserve), and the #679 waterfall draws it FIRST.
    emergency_reserve_balance: float = 0.0   # End-of-year reserve (cash sleeve)
    emergency_reserve_target: float = 0.0    # target_months x essential outflows, this year
    # Months of THIS YEAR's essential outflows the reserve actually covers.
    # The number the household needs to hear ("you have 2 months, you said
    # you wanted 12"); 0.0 when no reserve is declared -- a hard, stated
    # zero, never an assumed-away one (DP#32).
    emergency_reserve_months_covered: float = 0.0

    # ── Revolving credit facility (issue #689) ──────────────────────────
    # `liabilities[kind=line_of_credit]` -- a revolving, interest-only
    # facility distinct from the mortgage-paired HELOC (`heloc_balance`
    # above), secured or not (SimulationConfig.credit_facility_secured).
    # 0.0 at year 0 and in every year no shortfall reached it -- an undrawn
    # facility costs nothing (DP#32: this is the correct value, not a
    # fallback). Rises only when the #679 waterfall draws it in a shortfall
    # year, at which point it is real debt, accruing interest at
    # credit_facility_rate every year after, folded into total_debt.
    credit_facility_balance: float = 0.0
    # True when a shortfall was reported (or would have been) while no
    # line_of_credit was declared at all (credit_facility_limit <= 0) -- the
    # household's real resilience may be understated in that year, because a
    # real facility the household holds simply was not stated as input. This
    # is now a genuine, representable absence (fixed by declaring the
    # facility), not a structural gap in the engine (which #689 closed).
    credit_facility_unrepresentable: bool = False

    # Issue #763: closed-end consumer loans (car_loan/student_loan/
    # personal_loan). The total DRAWN balance (folded into total_debt above)
    # and this year's payment / interest -- the payment is the
    # consumer-debt half of the solvency identity's debt-service term
    # (apply_solvency), the interest is its non-deductible (consumption)
    # component. Both 0 for a household with no consumer debt.
    consumer_loan_balance: float = 0.0
    consumer_loan_payment: float = 0.0
    consumer_loan_interest: float = 0.0

    # Issue #759: fixed-term, zero-interest installment obligations. The
    # remaining-payment balance (the sum of monthly payments + final balloon
    # still owed forward -- a REPORTING figure, deliberately NOT folded into
    # total_debt above: an installment plan is a committed payment schedule,
    # not a callable borrowing against the estate) and this year's payment --
    # the installment half of the solvency identity's debt-service term
    # (apply_solvency), at 0% interest so there is no interest component. The
    # payment drops to 0 the year after the final payment date (the plan
    # ENDS -- it is not carried to the horizon the way annual_living_costs
    # is). Both 0 for a household with no installment plan.
    installment_balance: float = 0.0
    installment_payment: float = 0.0

    # Issue #967: mid-horizon mortgages originated by properties'
    # `purchase.financing`. The outstanding balance (the sum of each financed
    # property's end-of-year mortgage balance -- folded into total_debt above)
    # and this year's payment / interest -- the payment is the
    # second-property-mortgage half of the solvency identity's debt-service
    # term (apply_solvency), the interest is the portion DEDUCTIBLE when the
    # property is a rental (ITA s.20(1)(c), claimed by the rental fold) and
    # NON-deductible for a recreational/personal property. All 0 for a
    # household with no financed property (the golden path) -- byte-identical
    # (DP#32).
    second_property_mortgage_balance: float = 0.0
    second_property_mortgage_payment: float = 0.0
    second_property_mortgage_interest: float = 0.0
    # Issue #967: the principal ORIGINATED this year (non-zero only in a
    # financed property's purchase year). An INFLOW to the solvency identity
    # that funds the purchase (only the down payment leaves the portfolio),
    # surfaced for transparency + money-conservation checks. 0 in every year
    # but a purchase year, and for a household with no financing (DP#32).
    second_property_mortgage_originated: float = 0.0

    # True only when the waterfall exhausted every source and the household
    # is STILL short this year -- a hard ruin, not a modeled cost (DP#32:
    # this must be checked explicitly by any caller reporting a terminal
    # net_benefit; net_benefit itself is NOT zeroed here, since a truthful
    # report needs both "what the ledger says" and "whether that ledger was
    # ever actually achievable" -- see tests/test_issue_679_solvency.py).
    ruined: bool = False
    # Issue #784: per-member unused tuition-tax-credit carry-forward at the
    # END of this year (the remainder carried to the next year). 0.0 for a
    # household that declares no tuition. Surfaced for transparency so a
    # reader can see the credit was carried, not discarded.
    primary_tuition_carryforward: float = 0.0
    spouse_tuition_carryforward: float = 0.0

    # Epic #841 bite 4: end-of-year snapshot of each child's OWN accounts (the
    # bite-2 child_accounts list -- one dict per child with rrsp/tfsa/fhsa/
    # non_reg balances and their room/acb). Threaded here as REPORTING data so
    # the family objective (max_family_after_tax_networth) can value every
    # member's wealth, NOT summed into total_assets(): a household with no
    # child-savers (the golden household -- its children are RESP-only) carries
    # an empty-or-all-zero list here, so total_assets() and every existing
    # objective are bit-identical (DP#32). Copied from the terminal
    # jurisdiction_state['canada']['child_accounts'] each year.
    child_accounts: List[Dict] = field(default_factory=list)

    # Issue #899 (part a): end-of-year snapshot of each ADDITIONAL accumulating
    # adult's OWN RRSP/TFSA (the adults beyond the primary couple -- slots >= 2
    # of the per-adult stores). One dict per extra adult, keyed by the same
    # stable entity id the storage layer uses. Threaded here as REPORTING data,
    # mirroring child_accounts: NOT summed into the two-slot total_assets (so a
    # two-adult household carries an empty list here and every existing objective
    # + the golden invariant are bit-identical, DP#32), but read back by the
    # family objective so an extra adult's wealth is not silently dropped.
    extra_adult_accounts: List[Dict] = field(default_factory=list)
