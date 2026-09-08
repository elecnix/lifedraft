"""The ``management_fee`` rule: the s.20(1)(e) management-fee carrying charge.

Issue #142. An account-level declared ``management_fee`` RATE (distinct from
``mer``, DP#8 -- the MER is a growth drag netted out of the pot's return,
whereas the management fee is a REAL annual cash outflow the household pays
the brokerage) is charged here on every account-kind pot that declares one.

The fee is ``rate x pot balance`` computed on the balance at the START of the
fold step (the same ``opening_*`` reference the ``mer`` drag's aggregation and
the interest rules use). Modelling decision, stated for review: we do not
average opening and closing balances -- consistency with the existing fee and
interest paths matters more than a refinement no input can currently
distinguish, and a stated choice is reviewable in a way a silent convention
is not (DP#32).

Tax treatment by registration (ITA s.20(1)(e)):
  * NON-REGISTERED (``non_reg``): the fee is a deductible carrying charge --
    it pools into the SAME s.20(1)(c) machinery the SM interest uses
    (bracket-fill valuation, the TA s.336.0.1 QC investment-income cap,
    retirement gating via ``ws.sm_interest_deduction``) by writing
    ``ws.management_fee_deductible``, which ``apply_sm_interest`` adds to its
    ``total_deductible``. Wholesale reuse -- the fee is not a second
    deduction engine.
  * REGISTERED (``rrsp`` / ``tfsa`` / ``lira`` / ``lif`` / ``fhsa``): the fee
    is a real cash outflow with NO deduction. The shelter means the expense
    is not a deductible charge against other income -- deducting it would
    double-count the shelter's benefit.

The total cash fee (``ws.management_fee``) joins ``apply_solvency``'s
``spending_outflow`` -- a non-compressible, waterfall-funded outflow, money
conserved (DP#18), the same discipline the #1010 property carrying costs use.

Runs BEFORE ``sm_interest`` in ``RULE_ORDER`` (the pooling consumer) and after
the prologue has stamped the ``opening_*`` balances; it reads nothing any
growth rule writes.

One module per government program (DP#10). Absent declared fees -> both
outputs 0.0 and the rule is a strict no-op, so the golden invariant is
unmoved by construction (DP#32).
"""

from __future__ import annotations

from rule_registry import RuleContext, YearWorkingState, rule

# Registered account kinds: the fee is real cash but NEVER deductible --
# the shelter means the expense is not a charge against other income.
_REGISTERED_KINDS = frozenset(
    {'rrsp', 'tfsa', 'lira', 'lif', 'fhsa'})


def _opening_pot_balance(ws: YearWorkingState, kind: str) -> float:
    """The kind pot's balance at the START of the fold step (Jan-1 values).

    Every pot key the adapter can emit must map to a real opening field --
    an unknown kind is a loud failure, never a silently unfunded pot (DP#32).
    """
    if kind == 'non_reg':
        return ws.opening_non_reg_balance
    if kind == 'rrsp':
        # All three RRSP pots: the primary's, the spousal-in-name and the
        # spouse's own -- one s.20(1)(e) rate per kind, charged on the money.
        return (ws.opening_rrsp_balance
                + ws.opening_spousal_rrsp_balance
                + ws.opening_spouse_rrsp_balance)
    if kind == 'tfsa':
        return (ws.opening_tfsa_primary_balance
                + ws.opening_tfsa_spouse_balance)
    if kind == 'lira':
        return ws.opening_lira_balance
    if kind == 'lif':
        return ws.opening_lif_balance
    if kind == 'fhsa':
        return ws.opening_fhsa_balance
    raise ValueError(
        f"management_fee: unknown account kind {kind!r} declares a fee -- "
        "no opening balance field models it (loud failure, DP#32)")


@rule('management_fee')
def apply_management_fee(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Charge each declared kind's management fee on its OPENING pot balance.

    Writes ``ws.management_fee`` (the total cash outflow, every kind) and
    ``ws.management_fee_deductible`` (the non-registered slice only -- the
    s.20(1)(e) carrying charge ``apply_sm_interest`` pools into its
    ``total_deductible``). Returns True when any fee was charged.
    """
    fee_rates = ctx.config.account_management_fee_rate
    if not fee_rates:
        # No fee declared anywhere: no cash, no deduction (DP#32 -- absence
        # is absence, never a zeroed fee silently applied).
        return False

    total = 0.0
    deductible = 0.0
    for kind, entry in fee_rates.items():
        rate = entry.get('management_fee_rate', 0.0) if entry else 0.0
        if not rate:
            # An explicit 0.0 (or an empty entry) is a declared fee-free fact.
            continue
        fee = rate * _opening_pot_balance(ws, kind)
        total += fee
        if kind not in _REGISTERED_KINDS:
            deductible += fee
    ws.management_fee = total
    ws.management_fee_deductible = deductible
    return total > 0.0
