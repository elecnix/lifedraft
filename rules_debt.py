"""Amortizing-debt servicing: the payments ``solvency`` later reads as debt service.

``mortgage`` (the principal residence's own amortization), ``consumer_loans``
(#763 closed-end car/student/personal loans), ``installments`` (#759 dated
fixed-term obligations) and ``second_property_mortgage`` (#967 mid-horizon
mortgages originated by a property's ``purchase.financing``).

All four publish a payment ``apply_solvency`` folds into its debt-service term
and reserve sizing, and all four therefore sit BEFORE ``solvency`` in
``RULE_ORDER`` -- their relative positions are unchanged.

``_installment_payment_in_year`` is the ONE spelling of the installment payment
schedule (DP#9): ``simulation_state._annual_installment_service`` imports it
from here so a year-0 reserve can never disagree with what the engine charges.

Split out of ``simulation_rules.py``; the rule bodies are unchanged.
"""

from __future__ import annotations

from typing import Dict

from rule_registry import RuleContext, YearWorkingState, rule


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
