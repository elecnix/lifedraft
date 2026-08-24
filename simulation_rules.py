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

## Where the rules live

The rule IMPLEMENTATIONS are no longer in this file. This module is the FOLD:
it owns ``RULE_ORDER`` (the sequence, declared as data), ``run_rules`` (the
dispatcher) and ``trace_firing`` (the coverage-sweep instrumentation), and it
imports every domain rule module so that importing ``simulation_rules`` yields
a COMPLETE registry -- a partially-populated ``RULES`` would be exactly the
silent no-op DP#32 forbids, so completeness is by construction, not by
convention.

``rule_registry`` holds what a rule IS (``RuleContext``, ``YearWorkingState``,
``RULES``, ``@rule``); each ``rules_*`` module holds one domain's rules:

    rules_retirement_income   the retirement transition (CPP/OAS/pension onset)
    rules_contributions       money into the registered plans, and the room
    rules_growth              every pot compounds at the rate that pot earns
    rules_registered_plans    LIRA/LIF and RESP -- programs, not plain growth
    rules_debt                amortizing debt: mortgage, loans, installments
    rules_leverage            borrowed money, its s.20(1)(c) purpose, its cost
    rules_drawdown            the discretionary draw and the forced RRIF minimum
    rules_disposition         a home leaves the balance sheet; the tax settles
    rules_tuition_credit      ITA s.118.5/118.61/118.8 and Quebec's
    rules_solvency            was it affordable, and what had to be sold
    rules_amt                 the year-end minimum-tax assessment

Splitting the file moved NO rule and reordered NOTHING: ``RULE_ORDER`` below is
byte-identical to the one on ``main``, and it remains the single declaration of
the sequence rules fire in. (The one reorder it carries -- #1036 moving
'sm_interest' after 'heloc_interest_servicing' -- came from ``main``, not from
the split.)
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Dict

# ``RULES`` is read by ``run_rules`` / ``trace_firing`` below; ``RuleContext``
# and ``YearWorkingState`` are ``run_rules``' own signature. Nothing here is a
# re-export for old callers' benefit (DP#9) -- every caller imports the rule
# vocabulary from ``rule_registry`` directly.
from rule_registry import RULES, RuleContext, YearWorkingState

# Importing every rule module is what POPULATES ``RULES`` (each module's
# ``@rule(...)`` decorators run at import). ``run_rules`` raises if any name in
# ``RULE_ORDER`` is unregistered, so a module missing from this list is a loud
# failure, never a silently skipped rule (DP#32).
import rules_amt              # noqa: F401
import rules_contributions    # noqa: F401
import rules_debt             # noqa: F401
import rules_disposition      # noqa: F401
import rules_drawdown         # noqa: F401
import rules_growth           # noqa: F401
import rules_leverage         # noqa: F401
import rules_registered_plans  # noqa: F401
import rules_retirement_income  # noqa: F401
import rules_solvency         # noqa: F401
import rules_tuition_credit   # noqa: F401


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
