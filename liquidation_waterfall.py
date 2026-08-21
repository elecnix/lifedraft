#!/usr/bin/env python3
"""Liquidation waterfall (issue #679): what actually happens when a
household's required outflows exceed its available inflows in a given year.

## The gap this closes

Before this module existed, the simulator applied a `savings_rate` to gross
income and booked the resulting contributions unconditionally -- it never
asked whether the money existed after debt service and living costs. A
household could be cash-flow insolvent for a decade while the engine kept
recording new investment contributions, and the terminal wealth it reported
was only reachable on the assumption that the shortfall never happened.

## The fix

`simulation_rules.apply_solvency` (the `'solvency'` rule, last in
`RULE_ORDER`) computes the year's cash-flow identity
(`after_tax_income + drawdowns >= debt_service + living_costs +
contributions`) and, when it fails, calls `run_waterfall` here to fund the
difference from real sources, IN ORDER, each with its real cost:
emergency reserve -> revolving credit facility -> non-registered
(capital-gains tax, realised honestly) -> TFSA -> registered (fully
taxable). The order and the cost are the answer the household needs -- this
module makes both explicit and auditable rather than assuming the shortfall
resolves itself.

DP#25/DP#3: this module is jurisdiction-agnostic and holds no state. It
knows nothing about RRSPs, TFSAs, or Canadian tax law -- it folds over an
ordered list of `LiquidationSource` objects the caller supplies, each
carrying its own available balance and a pure `cost_fn` that prices a gross
draw. `simulation_rules.apply_solvency` is the one place that builds the
Canada-specific pools (non-reg ACB-aware capital gains, RRSP ordinary
income) and hands them to `run_waterfall` as data (DP#8).

## What this does NOT model

Capital losses are reported honestly (a forced sale below cost basis
surfaces a negative `realized_gain`, not a floor at zero -- issue #679
insists on this), but this engine does not yet track a capital-loss
carryforward that could shelter a *future* gain with it. That is a materially
larger feature (ACB-ledger-level loss tracking across years) and is out of
this PR's scope; the loss is surfaced in the report, not silently absorbed
or double-counted as a tax benefit that isn't modeled elsewhere.

**A revolving credit facility cannot be declared in the contract at all**
(issue #689): `personal_loan` forbids a `limit` and requires an
`amortization`; `heloc` requires collateral. There is no way to write down an
unsecured, interest-only, revolving line of credit. The waterfall's second
step therefore exists, is drawn in the right place, and can only ever find
$0 there. It is NOT faked, and it is NOT quietly dropped: every ruin the
waterfall reports while that step is unrepresentable carries
`credit_facility_unrepresentable=True`, so the report can say that the
household's real resilience is being **understated**. A pessimistic number
presented as the truth is still a wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# A cost function prices a GROSS draw from one source: given the gross
# dollar amount drawn, it returns (net_proceeds, tax, realized_gain).
# realized_gain is signed -- negative means a realised loss (DP#679:
# reported honestly, never clamped to zero).
CostFn = Callable[[float], Tuple[float, float, float]]


@dataclass(frozen=True)
class LiquidationStep:
    """One source actually drawn against to cover a cash-flow shortfall."""
    source: str
    gross_drawn: float
    net_proceeds: float
    tax: float = 0.0
    realized_gain: float = 0.0

    @property
    def cost(self) -> float:
        """What this step cost beyond the dollar it delivered -- almost
        always the tax remitted (gross_drawn - net_proceeds)."""
        return self.gross_drawn - self.net_proceeds


@dataclass(frozen=True)
class WaterfallResult:
    """Outcome of running the waterfall against one year's shortfall."""
    shortfall: float
    steps: Tuple[LiquidationStep, ...]
    covered: float
    remaining_shortfall: float
    ruined: bool


@dataclass(frozen=True)
class LiquidationSource:
    """One pool of money the waterfall may draw from, in the order supplied
    by the caller. ``available`` is the gross balance/room this source can
    contribute *before* ``cost_fn`` prices what actually gets delivered
    (DP#3: pure -- no hidden state, no mutation)."""
    name: str
    available: float
    cost_fn: CostFn


# ── Emergency reserve sizing (issue #688) ───────────────────────────────────
# Pure arithmetic (DP#3), and deliberately NOT jurisdiction-specific: "how
# many months of essential outflows am I holding" is not a tax rule.

def essential_annual_outflows(annual_living_costs: float, annual_debt_service: float) -> float:
    """What the household must pay every year no matter what: the measured
    living-cost budget (``household_budget.annual_living_costs``) plus this
    year's debt service. Contributions are deliberately EXCLUDED -- an
    investment contribution is precisely the thing a household stops doing
    when money is tight, so counting it as essential would size the reserve
    against a need that evaporates in the emergency it exists for."""
    return max(0.0, annual_living_costs) + max(0.0, annual_debt_service)


def reserve_target(target_months: Optional[float], annual_living_costs: float,
                    annual_debt_service: float) -> float:
    """The dollar target implied by a ``target_months`` policy (#688).

    ``target_months is None`` (the household declared no reserve block at
    all) yields ``0.0`` -- a household with no declared reserve holds
    nothing. That is not a silent default masking a missing input: it is the
    honest reading of "you told us nothing about a reserve, so we are not
    inventing one" (DP#32), and the caller reports it as such rather than
    quietly assuming a comfortable cushion into existence.
    """
    if target_months is None or target_months <= 0:
        return 0.0
    annual = essential_annual_outflows(annual_living_costs, annual_debt_service)
    return (target_months / 12.0) * annual


def months_covered(reserve_balance: float, annual_living_costs: float,
                    annual_debt_service: float) -> float:
    """How many months of essential outflows the reserve actually covers --
    the number the household needs to hear ("you have 2 months; you said you
    wanted 12"). Returns 0.0 when there is nothing to burn against (a
    household with no essential outflows at all has no runway question)."""
    annual = essential_annual_outflows(annual_living_costs, annual_debt_service)
    if annual <= 0:
        return 0.0
    return max(0.0, reserve_balance) / (annual / 12.0)


def identity_cost(gross: float) -> Tuple[float, float, float]:
    """A dollar-for-dollar source: no tax, no realized gain/loss. Used for
    the emergency reserve (already after-tax cash), the revolving credit
    facility (borrowed cash is not a taxable event), and TFSA withdrawals
    (tax-free by statute)."""
    return gross, 0.0, 0.0


def capital_gains_cost(gain_frac: float, inclusion_rate: float, marginal_rate: float) -> CostFn:
    """Cost function for a non-registered/taxable account: only the
    accrued-gain fraction of a withdrawal is taxable, at
    ``inclusion_rate x marginal_rate`` (matches the rest of this engine's
    capital-gains treatment, e.g. ``countries.canada.retirement_transition
    .plan_drawdown_net``).

    ``gain_frac`` is SIGNED and NOT clamped to [0, 1] here -- a forced sale
    while the account sits below its cost basis (a market-crash scenario,
    exactly the correlated case issue #679 is about) must report a genuine
    realised loss, not a phantom zero. Only a positive gain is taxed; a
    negative one costs nothing in tax (this engine does not yet carry
    capital losses forward to shelter a later gain -- see module docstring).
    """
    def _cost(gross: float) -> Tuple[float, float, float]:
        realized_gain = gross * gain_frac
        taxable_gain = max(0.0, realized_gain) * inclusion_rate
        tax = taxable_gain * max(0.0, min(0.95, marginal_rate))
        return gross - tax, tax, realized_gain
    return _cost


def ordinary_income_cost(marginal_rate: float) -> CostFn:
    """Cost function for a fully-taxable registered withdrawal (RRSP/RRIF):
    every gross dollar drawn is ordinary income at ``marginal_rate`` --
    forced out at whatever this year's rate is, not a rate the household
    would have chosen for a planned drawdown."""
    def _cost(gross: float) -> Tuple[float, float, float]:
        tax = gross * max(0.0, min(0.95, marginal_rate))
        return gross - tax, tax, 0.0
    return _cost


def run_waterfall(shortfall: float, sources: Sequence[LiquidationSource]) -> WaterfallResult:
    """Draw against ``sources`` IN ORDER until ``shortfall`` (a NET dollar
    amount the household needs in hand this year) is covered, or every
    source is exhausted.

    Each source is asked to deliver up to ``remaining`` NET dollars, capped
    by its own ``available`` gross balance/room. Because every ``cost_fn``
    used in this engine is linear in the gross draw (a fixed tax rate or
    fixed gain fraction, not a function of the amount itself), the gross
    needed to net a given amount is computed from one probe call --
    ``cost_fn(probe)`` -- rather than requiring callers to supply an
    inverse function. A ``cost_fn`` that is genuinely non-linear in gross
    would need a different solver; none used by this engine is.

    ``ruined`` is True only if every source is exhausted and the household
    is still short (DP#32: a shortfall that survives every real source must
    be reported, never silently absorbed).
    """
    if shortfall <= 0:
        return WaterfallResult(shortfall=shortfall, steps=(), covered=0.0,
                                remaining_shortfall=0.0, ruined=False)

    remaining = shortfall
    steps: List[LiquidationStep] = []
    for source in sources:
        if remaining <= 1e-9:
            break
        available = max(0.0, source.available)
        if available <= 0:
            continue

        probe = min(1.0, available)
        probe_net, _, _ = source.cost_fn(probe)
        net_per_dollar = probe_net / probe if probe > 0 else 0.0
        if net_per_dollar <= 0:
            continue

        gross_needed = remaining / net_per_dollar
        gross_draw = min(gross_needed, available)
        net, tax, gain = source.cost_fn(gross_draw)
        if gross_draw <= 1e-9:
            continue

        steps.append(LiquidationStep(source.name, gross_draw, net, tax, gain))
        remaining -= net

    remaining = max(0.0, remaining)
    covered = shortfall - remaining
    return WaterfallResult(shortfall=shortfall, steps=tuple(steps), covered=covered,
                            remaining_shortfall=remaining, ruined=remaining > 1e-6)


# ── Trajectory-level solvency summary (issue #679, reporting layer) ──────────

def summarize_solvency(results: Sequence) -> Dict:
    """Fold a whole trajectory's ``YearResult`` list into the facts a
    household actually asked for when it declared a job-loss scenario.

    Issue #679's complaint is not that the engine computed the wrong terminal
    number -- it is that **terminal wealth is the wrong question**. "You
    finish with $4.4M" is not an answer to "what happens if I lose my job";
    the answer is *how long the money lasts, when it runs out, and what you
    were forced to sell.* This function produces exactly those three facts so
    the reporting layer can print them beside (and ahead of) any ranking.

    It exists as a pure fold (DP#3) rather than as printing code inside
    ``optimize.py`` for one reason: **"does this trajectory report ruin" must
    be a directly testable fact, not something only observable by parsing
    stdout.** That is the property tests/test_issue_679_solvency.py asserts,
    and it is what stops the reporting layer from silently regressing the
    moment someone reformats a table.

    Duck-typed over the ``YearResult`` attributes rather than importing the
    dataclass, so this module stays free of any dependency on
    ``simulation_config`` (DP#25: dependencies point inward -- the waterfall
    is the inner layer here, and it must not reach back out to the config).

    ``engaged`` is False when the household never supplied
    ``household_budget.annual_living_costs``: the solvency module never ran,
    and every zero below is an ABSENCE, not a measurement of safety. A caller
    that prints "0 shortfall years" for an un-engaged run would be reporting
    the most dangerous falsehood this codebase exists to prevent (DP#32), so
    the flag is returned first and the caller must branch on it.
    """
    if not results:
        return {
            'engaged': False, 'ruined': False, 'first_ruin_year': None,
            'first_shortfall_year': None, 'shortfall_years': 0,
            'runway_months_at_start': 0.0, 'reserve_months_at_first_shortfall': 0.0,
            'reserve_target_at_first_shortfall': 0.0,
            'forced_liquidation_gross_by_source': {}, 'forced_liquidation_tax': 0.0,
            'forced_liquidation_realized_loss': 0.0, 'uncovered_shortfall': 0.0,
            'credit_facility_unrepresentable': False,
        }

    engaged = any(getattr(r, 'living_costs', 0.0) > 0 for r in results)
    shortfall_rows = [r for r in results if getattr(r, 'solvency_shortfall', 0.0) > 0]
    ruined_rows = [r for r in results if getattr(r, 'ruined', False)]

    by_source: Dict[str, float] = {}
    for r in shortfall_rows:
        # DP#32: no `or []`. YearResult.forced_liquidation_events is a
        # default_factory=list field -- an empty list is a real value (a
        # shortfall funded entirely from the reserve draws no other source),
        # and it must not be conflated with a missing attribute.
        for ev in getattr(r, 'forced_liquidation_events', ()):
            src = ev.get('source', '?')
            by_source[src] = by_source.get(src, 0.0) + ev.get('gross_drawn', 0.0)

    first_short = shortfall_rows[0] if shortfall_rows else None

    return {
        'engaged': engaged,
        'ruined': bool(ruined_rows),
        # `year` is the 1-indexed relative offset the engine stamps on each
        # YearResult, not a calendar year -- the caller labels it as such.
        'first_ruin_year': getattr(ruined_rows[0], 'year', None) if ruined_rows else None,
        'first_shortfall_year': getattr(first_short, 'year', None) if first_short else None,
        'shortfall_years': len(shortfall_rows),
        # The declared reserve, measured against YEAR ONE's essential outflows:
        # the household's liquid runway before anything goes wrong. 0.0 when no
        # reserve was declared -- a stated zero (#688), not a comfortable default.
        'runway_months_at_start': getattr(results[0], 'emergency_reserve_months_covered', 0.0),
        # What was left in the reserve AFTER the first shortfall year drew on it.
        'reserve_months_at_first_shortfall': (
            getattr(first_short, 'emergency_reserve_months_covered', 0.0) if first_short else 0.0),
        'reserve_target_at_first_shortfall': (
            getattr(first_short, 'emergency_reserve_target', 0.0) if first_short else 0.0),
        'forced_liquidation_gross_by_source': by_source,
        'forced_liquidation_tax': sum(
            getattr(r, 'forced_liquidation_tax', 0.0) for r in results),
        # Negative (or 0.0). A forced sale below cost basis is a real loss and is
        # reported as one -- never floored at zero (issue #679).
        'forced_liquidation_realized_loss': sum(
            getattr(r, 'forced_liquidation_realized_loss', 0.0) for r in results),
        'uncovered_shortfall': sum(
            max(0.0, getattr(r, 'solvency_shortfall', 0.0) - getattr(r, 'solvency_covered', 0.0))
            for r in results),
        # Issue #689: true whenever a shortfall was priced while the waterfall's
        # revolving-credit step was structurally empty -- the household's real
        # resilience is being UNDERSTATED, and the report must say so.
        'credit_facility_unrepresentable': any(
            getattr(r, 'credit_facility_unrepresentable', False) for r in results),
    }
