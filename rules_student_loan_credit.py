"""The ``student_loan_credit`` rule: ITA s.118.62 (federal) and TP-1 line 385
(Quebec) non-refundable credits on interest paid on a QUALIFYING government
student loan, with the interest carry-forward both regimes use.

One module per government program (DP#10). The arithmetic lives in
``countries/canada/student_loan_credit``; this rule is the wiring -- it reads
which loans qualify and who owns them, attributes the year's interest to that
member's return, applies the credit against that member's tax, and threads the
per-member interest ledger.

Why a rule rather than the prologue
-----------------------------------
The prologue prices the year's tax ONCE, before ``run_rules``. A credit applied
there would have to be recomputed on the retirement path (``plan_drawdown_net``)
and again at the estate, which is how the tuition credit was spelled twice
before epic #795 bite 3 moved it into a rule. This follows that seam: the rule
writes the per-member reduction to ``YearWorkingState``, and ``apply_solvency``
(next in ``RULE_ORDER``) counts it so the cash-flow identity sees the
POST-credit after-tax income (the credit lowers tax, so it raises after-tax
income).

What it reads
-------------
* ``ctx.config.consumer_loans`` -- each loan's ``owner`` and, for a
  ``student_loan``, ``qualifying_government_loan`` (schema-required and
  mapper-refused when absent, so a non-qualifying loan is a real ``False``
  rather than a guess).
* ``ws.consumer_loan_interest_by_loan`` -- the year's interest per loan, which
  ``apply_consumer_loans`` computes and previously only summed.
* ``ctx.primary_tax_before`` / ``ctx.spouse_tax_before`` -- the pre-credit tax
  each member's credit is applied against (non-refundable: it can never make
  tax negative).
* The OPENING per-member interest ledger off ``ws``, threaded through
  ``jurisdiction_state['canada']['student_loan_interest']``.

Absence-safety (DP#32): a household with no qualifying student loan has no
interest, so every member's credit is ``0.0`` and the ledger stays empty -- a
strict no-op, which is why the golden invariant cannot move.
"""
from __future__ import annotations

from tax_data import default_tax_provider

from rule_registry import RuleContext, YearWorkingState, rule


def _interest_by_role(config, sim_year: int, interest_by_loan: dict) -> dict:
    """This year's QUALIFYING student-loan interest per role.

    ``{role: dollars}``, with the roles of the household's members as keys (both
    present, 0.0 when they hold no qualifying loan). A loan whose owner is a
    person the engine does not tax as a member contributes nothing -- the credit
    belongs on the return of the person who pays the interest, and there is no
    such return to price it on.
    """
    by_role = {}
    for member in config.family_members:
        role = member.get('role')
        if role:
            by_role[role] = 0.0
    loans = getattr(config, 'consumer_loans', None)
    for loan in (() if loans is None else loans):
        if loan.get('kind') != 'student_loan':
            continue
        if loan.get('qualifying_government_loan') is not True:
            continue  # interest on a non-qualifying loan earns no credit
        interest = interest_by_loan.get(loan['id'], 0.0)
        if interest <= 0.0:
            continue
        owner = loan.get('owner')
        for member in config.family_members:
            if member.get('id') == owner and member.get('role') in by_role:
                by_role[member['role']] += interest
                break
    return by_role


@rule('student_loan_credit')
def apply_student_loan_credit(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Claim each member's student-loan-interest credits for the year, carrying
    forward the interest that produced none.

    Returns True when any credit was applied or any interest carried forward (the
    rule had an observable effect), False for a household with no qualifying
    student loan -- a no-op, every output left at its seeded default.
    """
    # Single-line on purpose: the unreached-rule-modules guard resolves the
    # caller by matching the import statement, and a multi-line one reads as no
    # caller at all (which would fail that guard, not this rule).
    from countries.canada.student_loan_credit import consume_interest_fifo, eligible_interest, student_loan_interest_credit  # noqa: E501

    config = ctx.config
    sim_year = ctx.calendar_year
    province = config.province
    provider = ctx.tax_provider if ctx.tax_provider is not None else default_tax_provider()

    interest_by_role = _interest_by_role(
        config, sim_year, ws.consumer_loan_interest_by_loan or {})

    opening = ws.opening_student_loan_interest or {}
    new_ledger = {}
    applied_by_role = {}
    carried = False
    for role, tax_before in (('primary', ctx.primary_tax_before),
                             ('spouse', ctx.spouse_tax_before)):
        opening_ledger = opening.get(role)
        role_ledger = dict(opening_ledger) if opening_ledger is not None else {}
        this_year = interest_by_role.get(role, 0.0)
        if this_year > 0.0:
            role_ledger[sim_year] = role_ledger.get(sim_year, 0.0) + this_year
        # The two regimes have different windows: federal is the year and the
        # five before it, Quebec's is open-ended. The credit is computed on the
        # larger (Quebec) base and the smaller is the federal slice; the helper
        # returns both from one interest figure, so claim against the base that
        # actually applies to this household's province.
        federal_eligible = eligible_interest(role_ledger, sim_year, federal=True)
        quebec_eligible = eligible_interest(role_ledger, sim_year, federal=False)
        eligible = quebec_eligible if province.lower() in ('quebec', 'qc') else federal_eligible
        federal, quebec = student_loan_interest_credit(
            eligible, sim_year, provider, province=province)
        credit = federal + quebec
        applied = min(credit, max(0.0, tax_before))  # non-refundable
        applied_by_role[role] = applied
        if applied > 0.0 and eligible > 0.0:
            # The credit is recomputed from the interest that remains, so what
            # is consumed is INTEREST: convert the applied dollars back at the
            # same combined per-dollar rate the credit was computed at (one
            # rate for the whole eligible base, so the conversion is exact).
            consumed = applied / (credit / eligible)
            role_ledger = consume_interest_fifo(role_ledger, consumed)
        if role_ledger:
            carried = True
        new_ledger[role] = role_ledger

    ws.new_student_loan_interest = new_ledger
    ws.student_loan_credit_applied_primary = applied_by_role.get('primary', 0.0)
    ws.student_loan_credit_applied_spouse = applied_by_role.get('spouse', 0.0)
    return (applied_by_role.get('primary', 0.0)
            + applied_by_role.get('spouse', 0.0)) > 0.0 or carried
