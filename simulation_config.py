#!/usr/bin/env python3
"""``SimulationConfig`` -- the engine's input: one household, fully declared.

This module used to hold the whole config layer (3,353 lines / 184 KB, which no
reviewer and no review tool could read in one piece). It now holds exactly the
dataclass and its accessors; the rest moved to modules named for what they do,
with NO re-export shim left behind (DP#9 -- callers import from the new home):

  - ``charge_limits``      OSFI B-20 charge geometry + its typed refusals
  - ``config_access``      readers/resolvers/shape guard for the raw config dict
  - ``config_serde``       DP#24's ``from_dict``/``to_dict`` round-trip bodies
  - ``year_result``        ``YearResult``, the engine's per-year OUTPUT record
  - ``scenario_overlay``   ``ScenarioOverlay`` and every way of applying one
  - ``property_structure`` mortgage-structure / sourcing / funding overlays
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from charge_limits import OSFI_B20_CHARGE_LTV_MAX, OSFI_B20_REVOLVING_LTV_MAX
from config_access import _dict_to_json
from config_serde import config_fields_from_dict, config_to_dict
# DP#25 (issue #998): find_member_by_role / adult_members / projection_span
# are pure config-reading helpers (they read the raw family.members list and
# derive structural facts with NO simulation machinery). They belong in the
# data layer (member_config), not here -- keeping them here forced the scenario
# layer (scenario_discovery) to import from this simulation-layer module to
# reach them, a DP#25 outward dependency. Relocated to member_config; imported
# back here (simulation -> data, inward) so SimulationConfig.member_by_role /
# .adult_members keep their single spelling (DP#9 -- no shim, no re-export:
# callers that used to import these from simulation_config now import them
# from member_config directly).
from member_config import find_member_by_role, adult_members

@dataclass
class SimulationConfig:
    """Configuration for a simulation run.

    Can be created from input.json or manually constructed.
    """
    projection_years: int = 10
    # DP#1: the horizon as a date-computed rule ("plan to age N"). When set it
    # derives projection_years via projection_span(); kept so to_dict() can
    # re-emit it for round-trip (DP#24).
    horizon_age: Optional[int] = None
    investment_return: float = 0.07  # DP#21 DEPRECATED: Use return_model_data instead. Remove in v2.0.
    salary_growth: float = 0.02
    inflation: float = 0.025  # DP#13: round-number placeholder; read from config
    tfsa_growth: float = 0.02
    savings_rate: float = 0.0  # DP#13: personal data - set from config (0 = not set)
    capital_gains_inclusion: float = 0.50
    resp_eap_tax_rate: float = 0.15
    resp_eap_taxable_portion: float = 0.60

    # RESP wind-down (issue #578): the study window is computed from each
    # beneficiary's birth_year + resp_study_start_age, for
    # resp_study_duration_years. DP#28: date-computed, configurable -- not a
    # magic age hardcoded into the simulation fold. resp_used_for_education
    # is a family decision (DP#30), not a government rule: when False, the
    # plan collapses (grant repayment + AIP) once the beneficiary reaches
    # study_start_age instead of paying out EAPs.
    resp_study_start_age: int = 18
    resp_study_duration_years: int = 4
    resp_used_for_education: bool = True
    # Real contributions/CESG/QESI/earnings breakdown of resp_current_balance,
    # when known (DP#13: real data always wins over the resp_rules default
    # 50/10/5/35 split used when this is absent).
    resp_composition: Dict = field(default_factory=dict)

    # Property
    house_value: float = 0  # DP#13: set from input.json, not hardcoded
    # Issue #963 (epic #956 bite F): the principal residence's REAL annual
    # appreciation rate (dimensionless, e.g. 0.03 for 3%/yr; negative allowed),
    # mirroring Bite A's `appreciation_rate` on a non-principal property.
    # None = a static value (byte-identical to today, DP#32): the consumers'
    # absence-test returns `house_value` and never reads this field, so the
    # golden fixture (whose legacy `property` dict never carries this key)
    # round-trips unchanged. When declared, the principal's value compounds at
    # `house_value * (1 + rate) ** (cal_year - start_year)` everywhere the
    # home's value is read: the LTV/charge math (annual), the estate's
    # deemed-disposition FMV (terminal), and Bite E's principal sale gross
    # (sale year). A sweepable numeric leaf (sensitivity.sweeps).
    appreciation_rate: Optional[float] = None
    mortgage_balance: float = 0
    mortgage_rate: float = 0.05
    # Issue #1075 (data-model half): the sum of the balances of every
    # kind=mortgage tranche whose `deductible` flag is set -- their interest
    # is deductible under ITA s.20(1)(c) (borrowed for an income-producing
    # non-registered investment) -- and, alongside it, those tranches' EXACT
    # annual interest (each balance * its OWN rate). Mapped by
    # input_contract.py off property.deductible_mortgage_balance /
    # property.deductible_mortgage_interest, emitted only when > 0 so a
    # household with no deductible tranche (the golden fixture and every
    # pre-#1075 contract) round-trips byte-identical (DP#32: absence of the
    # flag is "not deductible", never a fabricated zero key). Surfaced HERE
    # so the s.20(1)(c) interest pricing (issue #850) can price the declared
    # tranches' TRUE interest without re-deriving the tracing from the
    # contract. The interest is NOT deductible_mortgage_balance * rate: the
    # rate is the balance-weighted average of ALL tranches, which equals the
    # deductible tranches' own-rate sum only when every tranche shares one
    # rate (DP#32: never price deductible interest off a blended rate).
    deductible_mortgage_balance: float = 0.0
    deductible_mortgage_interest: float = 0.0
    ltv_max: float = 0.80
    amortization_years: int = 25
    margin_available: float = 200000
    # issue #663/DP#32: whether this contract declares a readvanceable HELOC
    # facility AT ALL -- a first-class state, distinct from margin_available
    # being 0. Set in from_dict() from the presence of property.margin_available
    # in the source cfg (has_readvanceable_facility()); NOT derived from
    # margin_available > 0, which cannot tell "no facility" apart from "a
    # facility with zero undrawn room". Consumers that decide whether to rank
    # the readvanceable-mortgage strategy, or quote a HELOC rate/after-tax
    # cost, must check this instead of margin_available's truthiness.
    #
    # Defaults True (matching margin_available's own legacy default of
    # 200000, DP#13) so the ~4,000 tests that build SimulationConfig
    # directly -- not through from_dict()/a contract -- keep their existing
    # "assume a HELOC exists" behaviour unchanged. from_dict() is the one
    # place that computes the real answer from contract-mapped data.
    #
    # NOTE: to_dict() does not conditionally re-emit margin_available from
    # this flag -- it always writes the field, same as every other release
    # of this dataclass, so existing "mutate a field, then export/reload"
    # callers (see tests/test_new_features.py's round-trip suite) keep
    # working. The dict-level crash/misreporting this issue actually fixes
    # lives entirely upstream of here, in apply_overlay() and the raw-dict
    # cfg passed around optimize.py/simulate.py -- those never fabricate
    # margin_available, so a contract with no facility stays that way
    # through every overlay. has_heloc exists for callers that hold a
    # SimulationConfig built straight from a real contract (from_dict) and
    # need the true answer without re-deriving it from margin_available.
    has_heloc: bool = True
    # issue #654: the household's OWN declared HELOC rate --
    # liabilities[kind=heloc].rate, mapped by from_dict() off
    # property.heloc_rate. A HELOC is a different credit product from the
    # mortgage it may sit alongside (a cheap legacy fixed mortgage plus a
    # prime-linked revolving HELOC is the ordinary case, not an edge case)
    # and the two rates must never be conflated (DP#32). None means "no
    # HELOC rate was ever declared" -- distinct from a declared 0. The
    # engine (FamilySimulation) honours a declared value outright; it only
    # falls back to a mortgage-derived approximation (DP#13 placeholder,
    # not a fallback that overrides supplied input) when this is None.
    heloc_rate: Optional[float] = None
    # issue #654: liabilities[kind=heloc].rate_type ("fixed"/"variable"),
    # mapped alongside heloc_rate for round-trip completeness (DP#24) and
    # future rate-path modelling. Not yet consumed by the engine beyond
    # that: a declared heloc_rate is a single current-year scalar either
    # way (see FamilySimulation.__init__'s heloc_path construction) --
    # there is no year-over-year HELOC rate PATH in this schema/engine yet
    # for "variable" to switch on (that would be assumptions.rate_paths.
    # heloc's job, which is unwired end-to-end; see #654's PR notes).
    heloc_rate_type: Optional[str] = None
    # issue #257: refinance cash-out (mortgage increase whose proceeds are
    # invested). Recorded as debt once (via mortgage_balance); the proceeds are
    # added to the invested lump sum. margin_available is NOT inflated by this.
    cash_out: float = 0.0

    # issue #1039: the OPENING DRAWN position of the mortgage-paired HELOC
    # (liabilities[kind=heloc].balance.amount), mapped by from_dict() off
    # property.heloc_opening_balance. 0.0 is #577's documented undrawn state.
    # input_contract.py writes the key only when the contract declares a drawn
    # balance WITH its deductibility, so absence here means "no opening draw"
    # -- never a coerced zero (DP#32). Seeded into SimState.heloc_balance by
    # SimState.initial() and carried by the fold; margin_available has already
    # been reduced by the same draw upstream (undrawn room = limit - drawn).
    heloc_opening_balance: float = 0.0
    # issue #1039: the declared deductible proportion
    # (deductibility.investment_portion) of that OPENING drawn balance. The
    # original borrowing's purpose is a historical fact that predates the
    # snapshot, so it is carried in as a declared ratio -- not re-derived from
    # a simulation decision (#577 governs draws the engine makes). Consumed by
    # SimState.initial() to seed canada.margin_tracing; only meaningful with
    # heloc_opening_balance > 0.
    heloc_opening_investment_portion: float = 0.0

    # issue #735: what FRACTION of margin_available is drawn and invested at
    # year 0. 0.0 is the DP#32-correct default -- a declared facility that is
    # simply left UNDRAWN, not a fabricated draw the household never made.
    # Before this fix, every caller sized the year-0 lump sum as
    # `margin_available + cash_out` unconditionally (simulation_state.
    # margin_draw_for_lump_sum), so a declared facility was ALWAYS drawn in
    # full and invested -- charging its cost (interest on a fully drawn
    # balance) while denying its benefit (standby liquidity in a #679
    # shortfall, which can only ever find room there if the room was never
    # spent). This is a SEARCH DIMENSION (DP#22/DP#31: the optimizer ranks,
    # it doesn't choose), not a SimulationConfig field: a household does not
    # "declare" what fraction of a facility it will draw at time zero of a
    # hypothetical projection, so there is no fact for a config field to
    # hold. Each caller that sizes a year-0 lump sum computes
    # `margin_available * draw_fraction + cash_out` with its OWN draw
    # fraction, explicit at the call site -- ``GridOptimizer.optimize()``'s
    # ``draw_fraction_options`` parameter (default ``[0.0]``, sweepable),
    # ``optimize.run_optimization()``'s parameter of the same name, and
    # ``ScenarioOverlay.draw_fraction`` (``simulate.py``'s path) -- rather
    # than a config field that would sit unread by two of those three call
    # sites (DP#9/DP#18: a value nothing reads is not a feature).

    # issue #689: a revolving, interest-only credit facility DISTINCT from
    # the mortgage-paired HELOC above -- ``liabilities[kind=line_of_credit]``.
    # Kept as its own facility, deliberately not merged into
    # margin_available/heloc_balance: a HELOC is carved out of the same
    # registered charge as the mortgage and its balance's tax deductibility
    # is TRACED by use (ITA s.20(1)(c), countries/canada/debt.py) -- treating
    # a household's investment-loan room as if it were emergency liquidity
    # would poison that tracing and model a different product than the one
    # asked about (see simulation_rules.apply_solvency's docstring). This
    # facility exists for exactly one purpose today: it is the #679
    # liquidation waterfall's second rung (emergency reserve -> THIS ->
    # non-registered -> TFSA -> registered) -- available capital in a
    # shortfall year that costs nothing while undrawn (DP#32: 0.0 is the
    # correct default, not a fallback masking a missing input; nothing has
    # been borrowed until a real shortfall draws it).
    credit_facility_limit: float = 0.0
    # None means "no line_of_credit liability was ever declared" -- distinct
    # from a declared 0 (DP#32), mirroring heloc_rate's own None-vs-0
    # convention above.
    credit_facility_rate: Optional[float] = None
    credit_facility_rate_type: Optional[str] = None
    # issue #689: whether this facility is registered against the property
    # (liabilities[kind=line_of_credit].collateral is non-null and matches
    # the principal residence) -- decides whether its DRAWN balance counts
    # toward the OSFI B-20 charge limit (#664/#681) at all. An unsecured
    # facility is genuinely ADDITIONAL capacity outside that charge, and
    # survives a sale of the property; a secured one does not (#689).
    credit_facility_secured: bool = False

    # issue #664: the LTV ceiling of the ONE registered charge against the
    # property -- mortgage balance + drawn/limit revolving HELOC must fit
    # inside charge_limit(house_value, charge_ltv_limit). DP#13 fallback:
    # OSFI B-20's legal maximum for an uninsured combined loan plan (see
    # ``charge_limits.OSFI_B20_CHARGE_LTV_MAX``); a contract that declares its
    # own charge terms should set this explicitly.
    charge_ltv_limit: float = OSFI_B20_CHARGE_LTV_MAX
    # issue #664: OSFI B-20's revolving-only ceiling -- independent of
    # charge_ltv_limit, the readvanceable/revolving segment alone may not
    # exceed this fraction of house_value.
    heloc_ltv_limit: float = OSFI_B20_REVOLVING_LTV_MAX
    # issue #655: the amortization a cash-out refinance is repaid over. NOT
    # like amortization_years above -- deliberately has NO numeric default
    # (DP#32). A refinance is a new loan; apply_ltv_overlay/apply_overlay
    # refuse to book a cash-out when this is None (and no explicit override
    # is supplied) rather than silently inheriting the incumbent mortgage's
    # remaining amortization_years. Sourced from
    # decisions.mortgage.refinance_options[].amortization_years when a
    # contract declares one.
    refinance_amortization_years: Optional[int] = None

    # Issue #792: the dollar amount of the refinance advance the household
    # DECLARES it will route into the DEDUCTIBLE non-reg account first, before
    # filling registered room. Sourced from the first declared
    # decisions.mortgage.refinance_options[].advance_split.deductible_non_reg.
    # None means "no declared split" -- the engine keeps today's internal
    # optimization (fill registered first, non-reg gets the remainder); this is
    # the default-off state that preserves current behaviour (DP#13/DP#32).
    # A declared 0 is a real choice (route nothing to deductible non-reg) and
    # is carried as 0.0, distinct from absence -- both are honoured by
    # StrategyEngine.fill_room.
    refinance_advance_deductible_non_reg: Optional[float] = None

    # Family
    family_members: List[Dict] = field(default_factory=list)
    children: List[Dict] = field(default_factory=list)
    private_loans: List[Dict] = field(default_factory=list)  # issue #813: private loans (lender income + borrower s.20(1)(c) deduction)
    gifts: List[Dict] = field(default_factory=list)  # epic #841 bite 3: parent->child gifts funding a child's registered room
    first_home_purchases: List[Dict] = field(default_factory=list)  # issue #704: a member's first-home purchase -> FHSA qualifying withdrawal + HBP
    # Dated ZEV acquisitions -> federal iZEV + Quebec Roulez vert incentive inflow.
    zev_purchases: List[Dict] = field(default_factory=list)

    # Accounts
    rrsp_annual_percent: float = 0.18
    rrsp_annual_max: float = 0  # DP#13: set from TaxDataProvider; 2026 value: 33810
    tfsa_annual_room_per_person: float = 7000

    # DP#45: deduct-later bracket target - income threshold below which
    # RRSP deductions are not claimed (they stay undeducted until income
    # rises above this bracket). Default: federal 20.5%/26% boundary for 2026.
    deduct_later_bracket_target: float = 0  # DP#13: set from tax brackets; 2026 federal 26% boundary: 117045

    # RESP (from resp_rules module)
    resp_current_balance: float = 0.0

    # DP#8/DP#10 (#241): jurisdiction is config data, not a hardcoded literal.
    # Core modules read these instead of defaulting to 'canada'/'quebec' in code.
    # province accepts either the long form ('quebec') or the postal code ('qc');
    # TaxDataProvider resolves both via PROVINCES aliases. Default preserves the
    # historical Quebec behaviour for configs that omit a tax.province.
    country: str = 'canada'
    province: str = 'quebec'

    # DP#20: year-versioned simulation
    start_year: int = 2026
    frozen_brackets: bool = False  # If True, use start_year brackets for all years (sensitivity isolation per DP#5)
    time_step: str = 'yearly'     # 'yearly' or 'monthly' (monthly enables intra-year events)

    # DP#18: CashFlow events - irregular income, planned expenses, lifecycle events
    cash_flows: List[Dict] = field(default_factory=list)

    # DP#27: Portfolio composition - per-account investment income types
    portfolio_data: Dict = field(default_factory=dict)

    # DP#16: Readvanceable mortgage trigger - derived from mortgage config data,
    # not a standalone boolean flag. When heloc_readvance=True in input.json
    # AND margin_available > 0, the simulation auto-enables the readvanceable
    # mortgage investment-loan strategy.
    # The use_readvanceable parameter in FamilySimulation overrides this when
    # explicitly set (for optimizer comparisons); None means auto-detect.
    heloc_readvance: bool = False

    # Issue #956 bite E (principal-residence disposition): a declared
    # mid-horizon SALE of the principal residence, mapped by
    # `input_contract._map_principal_sale` onto `cfg['property']['principal_sale']`.
    # None / absent = the principal is held to the horizon -> a strict no-op
    # (DP#32): the golden invariant is unchanged by construction (the golden
    # household builds SimulationConfig.from_dict straight from a legacy dict
    # that never carries `principal_sale`, so this stays None there). When
    # present, the `principal_disposition` rule fires in the sale year: the
    # home + its mortgage + any HELOC/SM secured against it leave the balance
    # sheet from the sale year, and the net proceeds (gross value less the
    # discharged debt, selling costs, and the PRE-apportioned disposition tax)
    # are invested into non-reg POST-GROWTH. The shape is the SAME Bite B
    # carries on a non-principal `sale` (year/selling_costs/owner_roles/
    # designated_principal_residence_years/value_share/acb_share) -- one
    # consistent contract the disposition rules read.
    principal_sale: Optional[Dict] = None

    # DP#9 (epic #603 Track C Phase 2): heloc_data / rental_data /
    # rate_scenario_data / employer_benefits_data / life_events_data used to
    # live here. Each was stashed straight from the raw config dict
    # (`cfg.get('heloc', {})` etc.) and read by NOTHING outside this loader
    # and tests -- HELOCConfig/RentalProperty/LifeEvent/RateScenarioConfig
    # (the from_dict() parsers that would have consumed them) had zero
    # production callers (#593). Deleted along with their schema leaves
    # rather than kept as unwired features (DP#9: a feature that has never
    # run is not a feature, it's a liability). The HELOC rate the engine
    # actually uses is derived from property.mortgage_rate
    # (simulation.py's build_heloc_path); rate_type is hardcoded 'variable'.

    # DP#28: Retirement extended data
    retirement_data: Dict = field(default_factory=dict)

    # epic #603 Track C Phase 2c (#600): the DECLARED estate elections
    # (spousal rollover, TFSA successor holder vs beneficiary, the real
    # non-registered ownership split, the principal-residence designation,
    # life insurance, per-person mortality). Every one of these used to be a
    # silent assumption inside countries/canada/estate.py, and every one
    # resolved in the favourable direction. Consumed by
    # objective.plan_from_config -> estate.EstatePlan.
    estate_data: Dict = field(default_factory=dict)

    # DP#2/DP#13: Non-reg investment yield rate - configurable, not hardcoded.
    # Default 2% is a conservative fallback; actual yield depends on portfolio
    # composition (dividend ETFs ~3-5%, growth stocks ~0.5%, bonds ~4%).
    # Used by Quebec interest deduction limit calculation (DP#10/DP#27).
    non_reg_yield_rate: float = 0.02

    # DP#21: Return model configuration
    return_model_data: Dict = field(default_factory=dict)

    # DP#16/issue #293: CRI/LIRA (locked-in pension) data. Lives at the top
    # level of input.json (separate from the RRSP balance held inside the
    # family member), so it must be captured here and threaded into
    # SimState.initial. Without this the $52,837 locked-in account is silently
    # dropped and never grows or appears in total_assets.
    lira_data: Dict = field(default_factory=dict)

    # Issue #679: the household's own MEASURED working-phase living costs (a
    # fact -- 12 months of bank/credit statements), separate from the
    # assumptions.savings_rate BELIEF. None means "not supplied"
    # (DP#32/DP#16): the cash-flow solvency invariant
    # (simulation_rules.apply_solvency) does not run without it, exactly as
    # the LTV explorer needs house_value+mortgage_balance+margin_available
    # together. A household legitimately never states living_costs=0 --
    # nobody spends nothing to live -- so treating None/absent as "off" is
    # not the DP#32 zero-as-fallback trap; it is the trigger-data pattern.
    living_costs: Optional[float] = None

    # Issue #761: the DISCRETIONARY fraction (0..1) of `living_costs` -- the
    # share a household cuts FIRST under an income shock (restaurants,
    # vacations, entertainment). None means "no split declared" (DP#16):
    # the solvency/runway identity treats the whole scalar as rigid and the
    # output says so explicitly (model_fidelity.runway_treats_all_spend_as_rigid
    # fires), never inventing a fraction (DP#32). 0.0 and 1.0 are real,
    # declarable values ("all rigid" / "all discretionary"), distinct from
    # None. Under a dated income shock the identity compresses this portion
    # to zero -- a labelled stress assumption, see apply_solvency.
    discretionary_fraction: Optional[float] = None

    # Issue #760: dated, finite-term living-cost segments layered ON TOP OF the
    # perpetual `living_costs` scalar -- a private-school tuition that ENDS when
    # a child ages out, childcare, a term expense that stops on a date. Each is
    # {description, amount (annual), from, to (nullable = perpetual),
    # non_discretionary}; simulation_rules.apply_solvency prorates each active
    # segment by the days of [from, to) falling in the calendar year (the
    # outflow-side analog of #674's dated income windows) and folds it into the
    # solvency identity's spending outflow, a non-null `to` STOPPING the expense
    # after that date (never carried to the horizon the way `living_costs` is).
    # Empty by default -- a household with no dated segments, never a fabricated
    # entry (DP#24/DP#32). Only present when the contract declared some.
    expense_segments: List[Dict] = field(default_factory=list)

    # Issue #688: the emergency-reserve POLICY -- how many months of
    # essential outflows to hold, WHERE, and held as WHAT.
    #
    # `emergency_reserve_target_months` is None when the household declared
    # no reserve block at all, and 0.0 when it declared one with a zero
    # target. Both hold a $0 reserve, but they are NOT the same statement --
    # the first is an absence, the second a decision -- and the ruin report
    # says which (DP#32). This is why the field is Optional rather than a
    # plain 0.0 default.
    #
    # `emergency_reserve_rate` is the return the reserve's declared
    # instrument actually earns. The reserve grows at THIS rate, never at
    # return_model's (a reserve compounding at 7% is not a reserve). It is
    # a required field of the schema block rather than inferred from
    # `instrument`, because "cash means 2%" is exactly the opinion-in-code
    # DP#2 forbids.
    #
    # `emergency_reserve_held_in` is the account kind the cash sleeve is
    # carved out of ('tfsa'/'non_reg'/'rrsp'/...), resolved by
    # input_contract.py from the declared account id. WHERE is most of the
    # answer: cash inside a TFSA grows tax-free, withdraws with no tax and
    # no penalty, and its room is restored the following January; the same
    # dollars in a non-registered account are taxed on their yield, and in
    # an RRSP are effectively unavailable. None = held outside every
    # declared account (an ordinary chequing balance the contract does not
    # otherwise model), in which case the sleeve is NOT carved out of any
    # account balance.
    emergency_reserve_target_months: Optional[float] = None
    emergency_reserve_rate: Optional[float] = None
    emergency_reserve_held_in: Optional[str] = None
    emergency_reserve_instrument: Optional[str] = None

    # Issue #763: closed-end consumer liabilities (car_loan, student_loan,
    # personal_loan) -- amortizing, unsecured, non-revolving debt the
    # household services alongside the mortgage. Each entry is a dict of the
    # contract's own static facts: {id, kind, balance (opening), rate,
    # payment_monthly, amortization_years}. The engine amortizes this list
    # year by year (simulation_rules.apply_consumer_loans), folds the
    # annual payment into the solvency identity's debt-service term
    # (apply_solvency) and the reserve/runway sizing (#758), and carries the
    # declining balance on SimState / YearResult.total_debt.
    #
    # A car/student/personal loan is NOT the mortgage (DP#7): it is a
    # separate unsecured amortizing liability, never aliased onto
    # mortgage_balance. Interest is NOT deductible (consumption debt); the
    # #656 default-to-deductible guard lives at the contract boundary
    # (input_contract refuses investment_portion > 0 loudly).
    consumer_loans: List[Dict] = field(default_factory=list)

    # Issue #692 (epic #690 bite 1): the couple's NON-principal real properties
    # -- a cottage, a rental -- each a dict of static facts {id, kind,
    # net_equity} produced by input_contract._map_owned_properties. Their net
    # equity (value - mortgage secured against that property, at the couple's
    # ownership share) is added to the annual balance sheet: SimState.initial
    # seeds SimState.property_equities from this list and total_assets() sums
    # them in. Before this seam only the single principal residence's value was
    # carried (prop_cfg -> house_value), so a declared cottage/rental was absent
    # from every annual metric and surfaced only at the terminal estate (#692).
    # Absence-safe: an empty list is a household with no such property (the
    # golden path), and the run is byte-identical -- the golden invariant must
    # not move (DP#32). This bite carries a STATIC equity figure; rental income
    # (#693), CCA (#694), the PRE allocation (#695), a purchase event (#696) and
    # STR (#697) are later bites that give the property dynamics.
    properties: List[Dict] = field(default_factory=list)

    # Issue #759: fixed-term, zero-interest installment obligations -- a
    # medical/dental/education payment plan (up-front lump already paid, then
    # N equal monthly payments + optional final balloon, 0% interest, finite
    # term). Each entry is a dict of the contract's own static facts:
    # {id, owner, description, start_date, monthly_amount, number_of_payments,
    # final_payment, rate, non_discretionary}. The engine services this list
    # year by year (simulation_rules.apply_installments), folds the annual
    # payment into the solvency identity's debt-service term (apply_solvency)
    # and the reserve/runway sizing (#758), and carries the declining
    # remaining-payment balance on SimState / YearResult -- but NOT in
    # total_debt: an installment plan is a committed payment schedule for
    # services already received, not a callable borrowing against the estate
    # (contrast SimulationConfig.consumer_loans, which IS real debt). The
    # payment STOPS the year after the final payment date -- it is never
    # carried to the horizon the way household_budget.annual_living_costs is.
    # Absence-safe: an empty list is a household with no such plan, and the
    # run is unchanged (the golden invariant must not move, DP#32).
    installments: List[Dict] = field(default_factory=list)

    # Issue #768: private-company equity grants / stock options the household
    # holds. A RECORD, not an asset: no simulation rule reads this list, so
    # every declared grant contributes $0 to all solvency / runway /
    # decumulation metrics by construction -- it is never counted as a liquid
    # resource. The output surfaces each grant as 'recorded, valued $0' so
    # the household knows it was not silently dropped (DP#32: an
    # absence-of-record is not a labelled $0). Absence-safe: an empty list is
    # a household with no such grants, and the run is unchanged (the golden
    # invariant must not move). Each entry is the contract's own static facts:
    # {id, owner, grantor, grant_date, vesting, strike (nullable), liquidity,
    # shares, fully_diluted_pct, notes}.
    equity_grants: List[Dict] = field(default_factory=list)

    # Issue #936: deposit products -- a plain HISA, a term/GIC, a promotional
    # teaser, expressed by ONE generic mechanism (different rate_schedule/cap
    # field values, not different concepts).
    #
    # `deposit_products` is the DECLARED list (the QUESTIONS the household is
    # deciding between). Like `strategies`/`objective`, it is read by the
    # OPTIMIZER layer (scenario_discovery._discover_deposit_products ->
    # simulate.enumerate_overlays), which ranks each product + the implicit
    # "leave it" baseline take-vs-leave -- the simulator does not read it
    # (DP#22: the optimizer ranks, it doesn't choose).
    #
    # `deposit_product` is the SINGLE product a given SCENARIO takes -- apply_overlay
    # writes exactly one declared product here (or leaves it None for the "leave
    # it" baseline). This IS the engine-facing lever: SimState.initial carves
    # `fund_amount` out of `funding_source` (money-conserving, #936 capability
    # #5) and seeds the deposit balance, and simulation_rules
    # .apply_deposit_product_growth grows it at the rate its `rate_schedule`
    # prescribes for the elapsed time since funding, on the portion up to any
    # `rate_eligible_cap` (#936 capabilities #2/#3), taxing the yield as ordinary
    # interest -- 100% taxable at the marginal rate each year (#936 capability
    # #1), not a deferred capital return.
    #
    # Both absence-safe: a household that declares no product gets today's
    # behaviour byte-for-byte (the golden invariant must not move, DP#32: an
    # absent product is not a $0 product). Each entry is the contract's own static
    # facts: {id, label, account_kind, fund_amount, funding_source,
    # rate_schedule:[{rate, duration_days|duration_years?}, ...] (final step
    # open-ended), rate_eligible_cap (optional), tax_character}. Sequence-of-
    # returns / staggered-deployment scoring is the companion issue #937.
    deposit_products: List[Dict] = field(default_factory=list)
    deposit_product: Optional[Dict] = None

    # Issue #1036: `capitalize_interest` is the HELOC's declared interest-
    # handling mode, mapped from liabilities[kind=heloc].capitalize_interest by
    # input_contract. True (the default when the key is absent, e.g. every
    # internal-config test built directly) = capitalize the drawn-margin
    # interest up to the charge, servicing the rest in cash (the pre-#1036
    # behaviour, byte-identical). False = service ALL the drawn-margin interest
    # in cash (a retiree paying HELOC interest in cash is no longer modelled as
    # capitalizing it). Read by simulation_rules.apply_margin_heloc_interest.
    # The internal-config default (absent key) is True so every test that
    # builds the internal dict directly stays byte-identical (DP#32: absence is
    # the fallback, never a coercion).
    #
    # NOTE: `capitalize_interest` is a FACILITY-level fact wired only to the
    # drawn-MARGIN leg (apply_margin_heloc_interest / new_heloc_balance). The
    # SM readvance leg (new_sm_heloc) is untouched -- its interest is priced
    # and deducted by apply_sm_interest, never capitalized into the balance
    # (the readvance grows the line by principal paydown, not by capitalized
    # interest). Wiring it there too is defensible but out of scope here.
    capitalize_interest: bool = True

    # Issue #1040: a declared decisions.borrow_to_invest[] option with
    # hold_draw=true opts its draw OUT of the RRSP-refund HELOC paydown sweep
    # (simulation_rules.apply_rrsp_refund_heloc_paydown): the drawn balance is
    # NOT reduced by the year's RRSP refund -- the refund stays in the
    # household's cash and flows to the usual allocation instead -- while the
    # interest is still priced, deducted, and serviced/capitalized per
    # capitalize_interest. Mapped from property.borrow_to_invest_hold_draw
    # (set per exploration cell by optimize.run_borrow_to_invest_exploration
    # for options that declare hold_draw). The internal-config default (absent
    # key, e.g. every test that builds the internal dict directly, and the
    # golden fixture) is False -- the pre-#1040 debt-sweep behaviour,
    # byte-identical (DP#32: absence is the fallback, never a coercion).
    hold_borrow_to_invest_draw: bool = False

    # Issue #823: per-account expected_return / locked_until overrides,
    # pot-keyed (rrsp/tfsa/non_reg/lira/lif/fhsa). Both default to empty --
    # a household that declares neither gets today's global-rate, fully-
    # liquid behaviour, which is what keeps the golden invariant unchanged
    # (DP#32: an absent override is not a zero override).
    #   return_overrides[kind] = {'override_balance': float,
    #                             'weighted_rate_sum': float}  # sum(bal*rate)
    #   locked[kind] = [{'balance': float, 'unlock_age': int,
    #                    'owner_birth_year': int}, ...]
    account_return_overrides: Dict = field(default_factory=dict)
    account_locked: Dict = field(default_factory=dict)

    # Issue #691/#136: per-account MER fees, pot-keyed. Defaults to empty -- a
    # household that declares no `mer` grows at the fee-free global rate
    # (today's behaviour; keeps the golden invariant unchanged, DP#32: an
    # absent fee is not a zero fee). The growth rule subtracts a FIXED DRAG
    # RATE from the pot's gross rate (net = gross - fee_share * fee_rate) so
    # the fee neither dilutes as the pot grows nor freezes at a zero-opening
    # account's $0 contribution (issue #136).
    #   mer_drag[kind] = {'mer_balance': float,     # declared fee-account balances
    #                     'weighted_mer_sum': float}  # sum(bal*mer), declared
    #                     'fee_share': float,      # fee acs' share of declared pot
    #                     'fee_rate': float}       # balance-weighted avg fee
    account_mer_drag: Dict = field(default_factory=dict)

    def non_reg_management_fees(self) -> Dict[str, float]:
        """Issue #142: ``{role: annual s.20(1)(e)-deductible fee}`` -- each
        member's DECLARED total of separately-charged non-registered
        management/counsel fees (attributed pro rata to joint owners by the
        contract adapter, which rides the totals onto the member records;
        family.members round-trips wholesale, DP#24). One spelling (DP#9):
        every consumer -- the working-phase prologue's taxable-income
        reduction, the retirement drawdown base's OAS-clawback reduction,
        and rules_amt's s.127.52(1)(j) half-add-back -- reads this map, so a
        household declaring no fee gets an empty map everywhere and is
        byte-identical to before (DP#32).
        """
        fees: Dict[str, float] = {}
        for m in self.family_members:
            f = m.get('mgmt_fee_non_reg_annual')
            if f is not None:
                fees[m['role']] = float(f)
        return fees

    def superficial_loss_events(self) -> List[Dict]:
        """Issue #141: every DECLARED ITA s.53(1)(c)/s.54 superficial-loss
        disposition across the household, each tagged with its seller's
        member id. The events ride family.members (round-trips wholesale,
        DP#24 -- config_serde untouched); this is the one read seam the
        registered `superficial_loss` rule consumes, so a household
        declaring none gets an empty list everywhere and is byte-identical
        to before (DP#32).
        """
        events: List[Dict] = []
        for m in self.family_members:
            declared = m.get('superficial_losses')
            if declared is None:
                continue
            for e in declared:
                tagged = dict(e)
                tagged['seller'] = m.get('id', m.get('role'))
                events.append(tagged)
        return events

    @classmethod
    def from_json(cls, path: str) -> 'SimulationConfig':
        """Load configuration from an on-disk input contract document.

        Epic #603 Track C Phase 2b: this is the SOLE loading boundary --
        ``path`` must be a document conforming to the input contract
        (``schema/input_schema.json`` + ``schema/countries/canada/
        input_schema.json``, composed and validated by
        ``input_contract.validate_contract``). ``input_contract.load_and_map``
        does the load + validate + map-to-internal-shape in one mandatory
        step (DP#32: not a bypassable adapter a caller could skip) and hands
        the result to the UNCHANGED ``from_dict`` below. There is no more
        "merge a country overlay of legacy defaults into whatever the user
        typed" step -- the contract requires every field explicit; a
        document that omits a required key is a validation error, not a
        silent default (DP#32).
        """
        import input_contract
        cfg = input_contract.load_and_map(path)
        return cls.from_dict(cfg)

    @classmethod
    def from_dict(cls, cfg: Dict) -> 'SimulationConfig':
        """Build a SimulationConfig from its internal dict shape.

        NOT the input-contract wire format (see ``from_json``) -- this is
        the shape ``to_dict()``/``apply_overlay()``/``ScenarioOverlay`` round
        -trip through internally for scenario generation (DP#18/DP#24), and
        what ~4,169 tests construct directly. ``input_contract.py`` is the
        only place that builds this shape FROM a contract document; nothing
        else authors it as an on-disk file.

        The field-by-field mapping lives in ``config_serde`` (DP#24's read
        half); this stays the single public entry point.
        """
        return cls(**config_fields_from_dict(cfg))

    def member_by_role(self, role: str, default=None):
        """Issue #699: resolve one of this config's members by role label.

        Delegates to the module-level :func:`find_member_by_role` seam so there
        is a single definition of the role->member resolution. ``default`` is
        returned when no member has that role (explicit, not ``or DEFAULT`` --
        DP#32).
        """
        return find_member_by_role(self.family_members, role, default)

    def member_by_id(self, member_id: str, default=None):
        """Issue #699: resolve one of this config's members by stable entity id.

        This is the lookup the multi-generation rewrite (#643) keys off -- an
        id survives a household that has more than two adults, where a role
        string cannot. ``default`` is returned when no member has that id.
        """
        return next((m for m in self.family_members
                     if m.get('id') == member_id), default)

    def adults(self) -> List[Dict]:
        """Issue #699: this config's adult members, primary then spouse.

        The seam every "iterate the taxable adults" site routes through
        (accounts, tax, estate) so Steps 2-8 generalize it to N adults in one
        place instead of ~20 inline role lookups.
        """
        return adult_members(self.family_members)

    @property
    def is_readvanceable(self) -> bool:
        """DP#16: Auto-detect whether the readvanceable mortgage strategy applies.

        Derived from mortgage config data, not a standalone flag.
        Returns True when all conditions hold:
        1. has_heloc (issue #663: a readvanceable facility exists on this
           contract at all -- distinct from margin_available being 0)
        2. heloc_readvance=True (the mortgage product is readvanceable)
        3. margin_available > 0 (there is available HELOC room to readvance into)

        The optimizer can override this by passing use_readvanceable=True/False
        explicitly; None means auto-detect from this property.
        """
        return self.has_heloc and self.heloc_readvance and self.margin_available > 0

    @property
    def should_deduct_later(self) -> bool:
        """DP#16: Auto-detect whether RRSP deductions should be deferred (deduct_later).

        Derived from bracket target config data, not a standalone flag.
        Returns True when deduct_later_bracket_target > 0, meaning the user
        has configured a specific tax bracket target for RRSP deduction timing.
        When deduct_later_bracket_target is 0 (not set), the deduction-later
        module is not activated.

        The optimizer can override this by passing deduct_later=True/False
        explicitly; None means auto-detect from this property.
        """
        return self.deduct_later_bracket_target > 0

    def to_dict(self) -> Dict:
        """Export configuration as a dict matching the input.json schema.

        The field-by-field mapping lives in ``config_serde`` (DP#24's write
        half); this stays the single public entry point.
        """
        return config_to_dict(self)

    def to_json(self, path: str = None, indent: int = 2) -> str:
        """Serialize config to a JSON string, optionally writing it to disk."""
        return _dict_to_json(self.to_dict(), path=path, indent=indent)

    @classmethod
    def overlay_diff(cls, base: 'SimulationConfig', modified: 'SimulationConfig') -> Dict:
        """Compute the diff between a base config and a modified config (DP#18)."""
        base_dict = base.to_dict()
        mod_dict = modified.to_dict()
        overlays = {}

        def _deep_diff(base_d: Dict, mod_d: Dict, path: str = '') -> None:
            for key in set(list(base_d.keys()) + list(mod_d.keys())):
                current_path = f"{path}.{key}" if path else key
                bval = base_d.get(key)
                mval = mod_d.get(key)

                if isinstance(bval, dict) and isinstance(mval, dict):
                    _deep_diff(bval, mval, current_path)
                elif bval != mval:
                    overlays[current_path] = {
                        'from': bval,
                        'to': mval,
                    }

        _deep_diff(base_dict, mod_dict)

        return {
            'base_fields': len(base_dict),
            'overlays': overlays,
            'n_changes': len(overlays),
        }
