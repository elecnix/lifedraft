#!/usr/bin/env python3
"""Reusable year-by-year trajectory invariant harness (issue #581, epic #603).

3,900+ tests assert terminal scalars over the default ~10-year horizon. None
of them look at the *shape* of a projection, and none run long enough to
reach the regimes (retirement, RRIF conversion at 71, RESP wind-down, death)
where issues #575-#578 live. Not one of them caught any of it.

This module is the instrument: a registry of small, named, per-year checks
that any simulation result list (``List[YearResult]``) can be run through,
independent of which scenario produced it. ``tests/test_golden_trajectory_581.py``
supplies the scenario(s); this module owns the checks.

## This is LIBRARY code, not test code (issue #681)

It used to live in ``tests/``, and that is exactly how #681 happened: #664
defined ``total_secured_debt_within_charge_limit`` correctly, and then
nothing outside ``tests/`` ever called it. The charge limit was enforced at
document-load time, held at t=0, and was violated by t=1 -- the engine
simulated a household at 382% LTV and printed a confident recommendation off
it, green, because the only thing that would have noticed was a fixture that
did not exercise readvancing hard enough to breach.

An invariant that only runs on a fixture is a test, not an invariant. So this
module now sits in the production tree, and ``assert_run_invariants`` below is
called from the run path itself (``simulation.FamilySimulation.run`` and
``optimizer.Optimizer._run_simulation`` -- the two folds every engine mode
goes through, per DP#26/#627). A breach raises ``InvariantBreachedError``,
loudly, and callers that rank scenarios must surface it as an explicit
infeasibility rather than swallowing it into ``score = -inf`` (#657).

Usage -- running an existing check against a trajectory::

    from trajectory_invariants import assert_invariant
    results = sim.run()
    ctx = {'start_year': 2026, 'primary_birth_year': 1976, ...}
    assert_invariant('no_negative_balances', results, ctx)

Usage -- a later agent adding a new invariant (this is the whole contract)::

    from trajectory_invariants import invariant, Violation

    @invariant('my_new_check')
    def check_my_new_thing(results, ctx):
        violations = []
        for i, r in enumerate(results):
            year = ctx.get('start_year', 0) + i
            if <bad condition on r>:
                violations.append(Violation(year, 'why it is bad', r.some_field))
        return violations

That's it. The function auto-registers under its name; ``all_invariant_names()``
picks it up; any test can call ``assert_invariant('my_new_check', ...)``, or
wrap the call in ``@pytest.mark.xfail(strict=True, reason=...)`` while the bug
it targets is still open. No changes to this file's plumbing are needed --
only new ``@invariant`` functions.

Every check function receives the *entire* trajectory (``results``, a
``List[YearResult]``) and a free-form ``ctx`` dict (birth years, start_year,
gross return, tolerances, ...). A check reads only the ``ctx`` keys it needs
(DP#8: compose through data) and returns a list of ``Violation`` -- one per
offending year -- never raises. An empty list means the invariant holds for
the whole horizon. Checks that need context they were not given return ``[]``
(a no-op) rather than raising, so a generic ``ctx`` can be reused across many
checks without every check needing every key.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
import math


@dataclass
class Violation:
    """One year where an invariant did not hold."""
    year: int
    message: str
    value: Any = None

    def __str__(self) -> str:
        return f"year {self.year}: {self.message} (value={self.value!r})"


CheckFn = Callable[[List[Any], Dict[str, Any]], List[Violation]]

_REGISTRY: Dict[str, CheckFn] = {}


def invariant(name: str) -> Callable[[CheckFn], CheckFn]:
    """Decorator: register a named trajectory-invariant check under ``name``."""
    def _decorator(fn: CheckFn) -> CheckFn:
        if name in _REGISTRY and _REGISTRY[name] is not fn:
            raise ValueError(f"invariant {name!r} already registered")
        _REGISTRY[name] = fn
        return fn
    return _decorator


def all_invariant_names() -> List[str]:
    """Every registered invariant name, sorted (for parametrized discovery)."""
    return sorted(_REGISTRY)


def run_invariant(name: str, results: List[Any], ctx: Optional[Dict[str, Any]] = None) -> List[Violation]:
    """Run one named invariant against a trajectory; returns its violations (possibly empty)."""
    return _REGISTRY[name](results, ctx or {})


def assert_invariant(name: str, results: List[Any], ctx: Optional[Dict[str, Any]] = None) -> None:
    """Run one named invariant; raise with every failing year (capped) if it fails."""
    violations = run_invariant(name, results, ctx)
    if violations:
        raise AssertionError(_format_violations(name, violations, len(results)))


def _format_violations(name: str, violations: List[Violation], n_years: int) -> str:
    """One rendering of "invariant X failed in N/M years, here they are"
    (DP#9), shared by the test-facing ``assert_invariant`` and the
    production-facing ``assert_run_invariants``."""
    shown = violations[:15]
    detail = "\n".join(f"  - {v}" for v in shown)
    more = f"\n  ... and {len(violations) - len(shown)} more" if len(violations) > len(shown) else ""
    return f"invariant {name!r} failed in {len(violations)}/{n_years} year(s):\n{detail}{more}"


class InvariantBreachedError(ValueError):
    """A simulated trajectory violated a structural invariant (issue #681).

    Raised from the RUN PATH, not from a test: a run that breaches the charge
    registered against the property has simulated a household borrowing money
    no lender would advance, and every number downstream of it -- net benefit,
    terminal wealth, the ranking itself -- is computed off a facility that
    does not exist. That must be a loud failure, never a plausible-looking
    row in a ranked table (DP#32).

    Carries the ``violations`` so a ranking caller can report WHICH year and
    by how much, rather than collapsing the scenario to a bare ``-inf``
    (#657: a swallowed raise is a silent failure wearing an exception's
    clothes).
    """

    def __init__(self, name: str, violations: List[Violation], n_years: int):
        self.invariant_name = name
        self.violations = violations
        super().__init__(_format_violations(name, violations, n_years))


# Invariants asserted on EVERY production run (issue #681). Deliberately a
# small, cheap, structural set -- these are properties no correct trajectory
# can violate regardless of scenario, not scenario-specific expectations
# (which stay opt-in, driven by ctx, and are exercised from tests). Adding a
# scenario-dependent check here would make the engine refuse legitimate runs.
RUN_PATH_INVARIANTS: tuple = (
    'total_secured_debt_within_charge_limit',
    'heloc_within_revolving_limit',
    # Issue #94 (DP#18): the mortgage amortization ledger identity -- no money
    # appears or vanishes as the principal is paid down. A pure money-
    # conservation relation every correct run satisfies (a mid-horizon
    # principal DISPOSITION is the one legitimate break, and the check is
    # discharge-aware so a household that sells its home is not false-refused).
    'mortgage_conserves_principal',
)


def run_path_ctx(config) -> Dict[str, Any]:
    """The ``ctx`` the run-path invariants need, read off a
    ``SimulationConfig`` (issue #681).

    Kept here rather than at the two call sites so both folds
    (``FamilySimulation.run`` and ``Optimizer._run_simulation``) cannot drift
    on what they check (DP#9 -- the #619/#627 failure shape).
    """
    return {
        'start_year': config.start_year,
        'house_value': config.house_value,
        'charge_ltv_limit': config.charge_ltv_limit,
        'heloc_ltv_limit': config.heloc_ltv_limit,
        # Issue #689: whether the declared credit facility (if any) is
        # secured against this property -- decides whether
        # 'total_secured_debt_within_charge_limit' folds its drawn balance
        # into the charge check at all.
        'credit_facility_secured': config.credit_facility_secured,
        # Issue #94: the run-path mortgage-conservation ledger identity needs
        # the OPENING mortgage balance (SimState.initial seeds it from the
        # config's mortgage_balance, which a cash-out refinance overlay has
        # already sized) and the principal-sale declaration (so the check can
        # exempt the disposition years rather than false-refuse a home sale).
        # Only supplied when a property is declared (mirroring how
        # 'total_secured_debt_within_charge_limit' no-ops on house_value == 0
        # -- a house_value of 0 means "no property declared", NOT "a $0
        # charge", DP#32 in reverse); without a property there is nothing to
        # amortize, so the check stays a no-op (never invents a break from
        # absent data).
        'opening_mortgage_balance': (config.mortgage_balance
                                     if config.house_value > 0 else None),
        'principal_sale': getattr(config, 'principal_sale', None),
    }


def assert_run_invariants(results: List[Any], config) -> None:
    """Assert every ``RUN_PATH_INVARIANTS`` check against a completed
    trajectory; raise ``InvariantBreachedError`` on the first breach
    (issue #681).

    This is the call that closes the enforcement loop: it runs in the engine,
    on every run, not in a fixture. A household with no property
    (``house_value == 0``) has no charge to breach, and every check here
    no-ops on a missing ``house_value`` anyway, so the guard costs nothing for
    the households it does not apply to.
    """
    ctx = run_path_ctx(config)
    for name in RUN_PATH_INVARIANTS:
        violations = run_invariant(name, results, ctx)
        if violations:
            raise InvariantBreachedError(name, violations, len(results))


# ============================================================================
# Balance-like fields: must never be negative, NaN, or infinite.
# ============================================================================

_BALANCE_FIELDS = [
    'primary_rrsp', 'spousal_rrsp', 'spouse_rrsp', 'total_rrsp',
    'primary_tfsa', 'spouse_tfsa', 'total_tfsa',
    'resp_balance', 'non_reg_balance', 'non_reg_acb', 'total_assets',
    'mortgage_balance', 'heloc_balance', 'total_debt',
    'lira_balance', 'lif_balance',
    'emergency_reserve_balance',  # issue #679
    'credit_facility_balance',  # issue #689
]

_DEBT_FIELDS = ['mortgage_balance', 'heloc_balance', 'total_debt', 'credit_facility_balance']

_ALL_NUMERIC_FIELDS = _BALANCE_FIELDS + [
    'net_benefit', 'primary_income', 'spouse_income', 'total_family_income',
    'annual_savings', 'primary_marginal', 'spouse_marginal', 'bracket_gap',
    'drawdown_income', 'drawdown_taxable', 'retirement_income',
    'employment_income', 'cpp_income', 'oas_income', 'pension_income',
    'non_reg_unrealized_gains', 'mortgage_payment', 'mortgage_interest',
    'mortgage_principal',
    # issue #679
    'after_tax_income', 'living_costs', 'debt_service', 'contributions_total',
    'solvency_shortfall', 'solvency_covered', 'forced_liquidation_tax',
    'forced_liquidation_realized_loss',
]


@invariant('no_nan_or_inf')
def check_no_nan_or_inf(results, ctx):
    """No numeric field on any year's result is NaN or +/-inf."""
    start_year = ctx.get('start_year', 0)
    violations = []
    for i, r in enumerate(results):
        for field_name in _ALL_NUMERIC_FIELDS:
            value = getattr(r, field_name, None)
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                violations.append(Violation(start_year + i, f'{field_name} is NaN/inf', value))
    return violations


@invariant('no_negative_balances')
def check_no_negative_balances(results, ctx):
    """No balance-like field (assets or debts) ever goes negative."""
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1e-6)
    violations = []
    for i, r in enumerate(results):
        for field_name in _BALANCE_FIELDS:
            value = getattr(r, field_name)
            if value < -tol:
                violations.append(Violation(start_year + i, f'{field_name} is negative', value))
    return violations


@invariant('total_secured_debt_within_charge_limit')
def check_total_secured_debt_within_charge(results, ctx):
    """Issue #664: on a readvanceable/all-in-one mortgage, the amortizing
    mortgage and the revolving HELOC are carved out of ONE registered
    charge with ONE combined limit -- NOT independent borrowing sources.
    Every year, ``mortgage_balance + heloc_balance`` (+ ``credit_facility_
    balance`` -- issue #689 -- when ``ctx['credit_facility_secured']`` is
    True) must fit inside ``charge_ltv_limit x house_value`` (OSFI B-20: 80%
    LTV is the legal maximum for an uninsured combined loan plan -- see
    ``simulation_config.OSFI_B20_CHARGE_LTV_MAX``). Requires
    ``ctx['house_value']`` (the property is not tracked on ``YearResult``,
    same limitation as the estate invariant above); a no-op without it.
    ``ctx['charge_ltv_limit']`` defaults to 0.80.

    A ``house_value`` of 0 (or absent) means NO PROPERTY IS DECLARED, and a
    household with no property has no charge to breach. It does NOT mean "a
    charge limit of $0 that every dollar of debt violates" -- reading it that
    way would be DP#32's zero-as-fallback error running in reverse: inventing
    a constraint out of missing data and refusing runs because of it. So this
    no-ops there, and the incoherence of a mortgage against an undeclared
    property is a different question than this invariant's.

    ``ctx['credit_facility_secured']`` defaults to False (DP#13/DP#32): an
    UNSECURED line of credit is genuinely additional capacity OUTSIDE the
    charge (#689) and must not be folded into a check it was never part of
    -- omitting the key, same as declaring it False, correctly excludes it.
    """
    house_value = ctx.get('house_value')
    if not house_value or house_value <= 0:
        return []
    start_year = ctx.get('start_year', 0)
    charge_ltv_limit = ctx.get('charge_ltv_limit', 0.80)
    tol = ctx.get('tolerance', 1.0)
    credit_facility_secured = ctx.get('credit_facility_secured', False)
    limit = house_value * charge_ltv_limit
    violations = []
    for i, r in enumerate(results):
        secured_cf = r.credit_facility_balance if credit_facility_secured else 0.0
        total_secured = r.mortgage_balance + r.heloc_balance + secured_cf
        if total_secured > limit + tol:
            violations.append(Violation(
                start_year + i,
                f'total secured debt (mortgage {r.mortgage_balance:,.2f} + HELOC '
                f'{r.heloc_balance:,.2f}'
                + (f' + secured line of credit {secured_cf:,.2f}' if credit_facility_secured else '')
                + f' = {total_secured:,.2f}) exceeds the charge '
                f'limit ({limit:,.2f} = {charge_ltv_limit:.0%} x house_value '
                f'{house_value:,.2f})',
                total_secured))
    return violations


@invariant('heloc_within_revolving_limit')
def check_heloc_within_revolving_limit(results, ctx):
    """Issue #664: OSFI B-20 caps the revolving/readvanceable portion of a
    combined loan plan at 65% LTV on its own -- independent of the 80%
    combined cap; lending between 65% and 80% must be amortizing and
    non-readvanceable. Requires ``ctx['house_value']``; a no-op without it,
    and equally a no-op at ``house_value <= 0`` (no property declared => no
    charge to breach; see the companion invariant above on why a 0 here is
    absence, not a $0 ceiling).
    ``ctx['heloc_ltv_limit']`` defaults to 0.65.
    """
    house_value = ctx.get('house_value')
    if not house_value or house_value <= 0:
        return []
    start_year = ctx.get('start_year', 0)
    heloc_ltv_limit = ctx.get('heloc_ltv_limit', 0.65)
    tol = ctx.get('tolerance', 1.0)
    limit = house_value * heloc_ltv_limit
    violations = []
    for i, r in enumerate(results):
        if r.heloc_balance > limit + tol:
            violations.append(Violation(
                start_year + i,
                f'HELOC balance ({r.heloc_balance:,.2f}) exceeds the revolving-only '
                f'limit ({limit:,.2f} = {heloc_ltv_limit:.0%} x house_value '
                f'{house_value:,.2f}) -- OSFI B-20 caps the readvanceable portion at '
                f'65% LTV independent of the 80% combined cap',
                r.heloc_balance))
    return violations


@invariant('debt_never_negative')
def check_debt_never_negative(results, ctx):
    """Debt-specific view of the same rule ('debt never goes negative')."""
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1e-6)
    violations = []
    for i, r in enumerate(results):
        for field_name in _DEBT_FIELDS:
            value = getattr(r, field_name)
            if value < -tol:
                violations.append(Violation(start_year + i, f'{field_name} is negative', value))
    return violations


# ============================================================================
# Money conservation.
# ============================================================================

@invariant('mortgage_conserves_principal')
def check_mortgage_conserves_principal(results, ctx):
    """No money appears or vanishes in the mortgage amortization ledger:
    mortgage_balance_t == mortgage_balance_{t-1} - principal_paid_t, every
    year. Requires ``ctx['opening_mortgage_balance']``; a no-op without it.

    Discharge-aware (issue #94). A household that SELLS its principal
    residence mid-horizon legitimately force-zeros the secured debt in the
    sale year and every year after (``apply_principal_disposition``): the
    balance drops to 0 and stays 0, NOT by amortization, while the
    amortization *schedule* keeps producing a scheduled ``principal_paid``
    (and ``principal_sale_discharged_debt`` reports the same scheduled
    principal). A pure-ledger identity would read those post-sale years as a
    conservation break and false-refuse a legitimate run -- so the check
    skips every year from the sale on when ``ctx['principal_sale']`` is set.
    Absence of the key (or a config with no principal sale -- the golden
    fixture) verifies the pure identity every year, exactly as before
    (byte-identical for every non-disposition household).
    """
    opening = ctx.get('opening_mortgage_balance')
    if opening is None:
        return []
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1.0)
    sale = ctx.get('principal_sale')
    sale_year = None
    if isinstance(sale, dict):
        sale_year = sale.get('year')
    violations = []
    prev = opening
    for i, r in enumerate(results):
        this_year = start_year + i
        # Issue #94: a principal disposition force-zeros the balance (and
        # the schedule's principal continues to be booked straight to a paid-
        # off mortgage) -- the ledged identity no longer holds BY DESIGN from
        # the sale year on. Skip those years; they are verified by the
        # disposition's own conservation (P_net + discharged_debt = V - costs).
        if sale_year is not None and this_year >= sale_year:
            prev = r.mortgage_balance
            continue
        expected = prev - r.mortgage_principal
        if abs(r.mortgage_balance - expected) > tol:
            violations.append(Violation(
                start_year + i,
                f'mortgage_balance broke conservation: expected {expected:.2f} '
                f'(prior {prev:.2f} - principal {r.mortgage_principal:.2f})',
                r.mortgage_balance))
        prev = r.mortgage_balance
    return violations


@invariant('undrawn_heloc_margin_not_booked_as_debt')
def check_undrawn_heloc_not_debt(results, ctx):
    """A HELOC margin that is never drawn (no SM readvancing, no personal
    draws) must not appear as debt (DP#18; issue #577: ``margin_available``
    is booked as ``heloc_balance`` from day one and compounds unserviced
    forever).

    Opt-in only: pass ``ctx['margin_never_drawn'] = True`` for a scenario the
    caller has arranged to never draw the margin. Defaults to a no-op so this
    invariant does not misfire against scenarios where drawing is expected.
    """
    if not ctx.get('margin_never_drawn', False):
        return []
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1e-6)
    violations = []
    for i, r in enumerate(results):
        if r.heloc_balance > tol:
            violations.append(Violation(
                start_year + i, 'undrawn HELOC margin booked as debt', r.heloc_balance))
    return violations


# ============================================================================
# ACB <= FMV, and ACB shouldn't be permanently pinned to FMV (DP#19).
# ============================================================================

@invariant('acb_le_fmv')
def check_acb_le_fmv(results, ctx):
    """Cost basis never exceeds fair market value on the non-reg account."""
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1e-6)
    violations = []
    for i, r in enumerate(results):
        if r.non_reg_acb > r.non_reg_balance + tol:
            violations.append(Violation(
                start_year + i,
                f'ACB ({r.non_reg_acb:.2f}) exceeds FMV ({r.non_reg_balance:.2f})',
                r.non_reg_acb - r.non_reg_balance))
    return violations


@invariant('acb_not_pinned_to_fmv')
def check_acb_not_flat(results, ctx):
    """ACB == FMV for many consecutive years means growth isn't accruing.

    A non-reg account that only ever holds exactly what was paid in (no
    unrealized gain, ever) is not earning a return, even though ACB <= FMV
    holds trivially. ``ctx['max_flat_years']`` (default 5) consecutive years
    with balance > 0 and acb == balance is itself a failure.
    """
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1e-6)
    max_flat = ctx.get('max_flat_years', 5)
    violations = []
    streak = 0
    for i, r in enumerate(results):
        flat = r.non_reg_balance > tol and abs(r.non_reg_acb - r.non_reg_balance) < tol
        streak = streak + 1 if flat else 0
        if streak == max_flat:
            violations.append(Violation(
                start_year + i,
                f'non_reg ACB has equalled FMV for {max_flat} consecutive years '
                f'(balance {r.non_reg_balance:.2f}) -- no growth is accruing',
                r.non_reg_balance))
    return violations


# ============================================================================
# Non-reg growth (#575).
# ============================================================================

@invariant('non_reg_grows_with_positive_return')
def check_non_reg_grows(results, ctx):
    """Non-reg balance must grow by more than its contribution when a
    positive return is configured and there's already money invested.

    Restricted to years with no retirement drawdown (``drawdown_taxable ==
    0``) so the check is not confounded by forced-RRIF reinvestment, which
    is not separately exposed on ``YearResult``. Requires
    ``ctx['gross_return'] > 0``; a no-op otherwise.
    """
    if ctx.get('gross_return', 0) <= 0:
        return []
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1.0)
    violations = []
    prev = None
    for i, r in enumerate(results):
        if prev is not None and prev.non_reg_balance > tol and r.drawdown_taxable == 0:
            contribution = r.contributions.get('non_reg', 0)
            expected_floor = prev.non_reg_balance + contribution
            if r.non_reg_balance <= expected_floor + tol:
                violations.append(Violation(
                    start_year + i,
                    f'non_reg_balance did not grow beyond its contribution '
                    f'(prior {prev.non_reg_balance:.2f} + contribution {contribution:.2f} '
                    f'= {expected_floor:.2f})',
                    r.non_reg_balance))
        prev = r
    return violations


# ============================================================================
# RRIF minimum withdrawal fires from age 71 (should hold on main today).
# ============================================================================

@invariant('rrif_minimum_fires_from_71')
def check_rrif_minimum(results, ctx):
    """From age 71, the mandatory RRIF minimum (CRA age-factor x Jan-1
    balance) is drawn whether or not it is needed for spending.

    Needs ``start_year`` and ``primary_birth_year`` in ctx (``spouse_birth_year``
    optional, 0 disables the spouse leg); uses ``TaxDataProvider`` for the
    year-specific age-factor table (DP#12/DP#20) -- the same data source the
    engine itself uses, so this is not a re-implementation of the rule, just
    an independent read of the published factor table.
    """
    start_year = ctx.get('start_year')
    primary_birth = ctx.get('primary_birth_year')
    if start_year is None or primary_birth is None:
        return []
    spouse_birth = ctx.get('spouse_birth_year', 0)
    tol = ctx.get('tolerance', 1.0)

    from tax_data import TaxDataProvider
    provider = TaxDataProvider()

    violations = []
    prev = None
    for i, r in enumerate(results):
        cal_year = start_year + i
        if prev is not None:
            rates = provider.get_rrif_min_withdrawal_rates(cal_year)
            expected_min = 0.0
            age_p = cal_year - primary_birth
            if age_p >= 71:
                expected_min += prev.primary_rrsp * rates.get(age_p, 0.20)
            if spouse_birth:
                age_s = cal_year - spouse_birth
                if age_s >= 71:
                    expected_min += (prev.spouse_rrsp + prev.spousal_rrsp) * rates.get(age_s, 0.20)
            if expected_min > tol and r.drawdown_taxable < expected_min - tol:
                violations.append(Violation(
                    cal_year,
                    f'RRIF minimum not met: drawdown_taxable {r.drawdown_taxable:.2f} '
                    f'< statutory minimum {expected_min:.2f}',
                    r.drawdown_taxable))
        prev = r
    return violations


# ============================================================================
# RESP winds down (#578).
# ============================================================================

@invariant('resp_winds_down_after_children_age_out')
def check_resp_winds_down(results, ctx):
    """After every child ages past the study window, the RESP should be
    ~empty (EAPs paid out and/or the plan collapsed) -- not still compounding
    (DP#28).

    ``ctx['children_birth_years']`` (list[int]) and ``ctx['study_end_age']``
    (default 25, a generous upper bound past a normal undergrad + grad
    program) drive the expected wind-down year. ``ctx['resp_near_zero_threshold']``
    (default $5,000) allows a small administrative residual. A no-op without
    ``children_birth_years``.
    """
    children = ctx.get('children_birth_years')
    if not children:
        return []
    start_year = ctx.get('start_year', 0)
    study_end_age = ctx.get('study_end_age', 25)
    threshold = ctx.get('resp_near_zero_threshold', 5_000)
    wind_down_year = max(children) + study_end_age

    violations = []
    for i, r in enumerate(results):
        cal_year = start_year + i
        if cal_year > wind_down_year and r.resp_balance > threshold:
            violations.append(Violation(
                cal_year, f'RESP should have wound down by {wind_down_year} '
                f'(all children past age {study_end_age})', r.resp_balance))
    return violations


# ============================================================================
# Drawdown nets the target spend, tax-exact (#363/#579).
# ============================================================================

@invariant('drawdown_meets_net_target')
def check_drawdown_meets_net_target(results, ctx):
    """The retirement drawdown delivers the requested NET (after-tax) spending
    target every year, within a dollar -- plan_drawdown_net (#363/#579) grosses
    up only the taxable portion of each source so the after-tax proceeds equal
    ``drawdown_net_target`` exactly, rather than the old blended-rate ``gross``
    path's approximate (and systematically over-drawing) single-rate gross-up.

    A year where delivered < target is only a violation if account balances
    were not yet exhausted -- running out of money and stopping short is
    correct behavior, not a drawdown-sizing bug. Requires
    ``drawdown_net_target``/``drawdown_net_delivered`` on ``YearResult``; a
    no-op on result objects that predate those fields.
    """
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1.0)
    violations = []
    for i, r in enumerate(results):
        target = getattr(r, 'drawdown_net_target', 0.0)
        if target <= tol:
            continue
        delivered = getattr(r, 'drawdown_net_delivered', 0.0)
        shortfall = target - delivered
        if shortfall > tol:
            remaining_balance = (
                r.total_rrsp + r.total_tfsa + r.non_reg_balance
                + r.lif_balance + r.lira_balance
            )
            if remaining_balance > tol:
                violations.append(Violation(
                    start_year + i,
                    f'drawdown delivered {delivered:.2f} net, short of target '
                    f'{target:.2f} by {shortfall:.2f}, though {remaining_balance:.2f} '
                    f'of account balance remained undrawn',
                    delivered))
    return violations


# ============================================================================
# Decumulation shortfall is surfaced, not silently swallowed (#707).
# ============================================================================

@invariant('drawdown_shortfall_surfaced')
def check_drawdown_shortfall_surfaced(results, ctx):
    """A year where the retirement drawdown exhausted every drawable account
    and STILL delivered less than the net spending target MUST record the gap
    on ``YearResult.drawdown_shortfall`` -- the founding defect this repo
    exists to kill is the engine silently substituting zero, and a bankrupt
    year becoming just another number is exactly that defect (issue #707).

    The complement of ``drawdown_meets_net_target``: that invariant flags a
    year where delivered < target AND a balance remained undrawn (a
    drawdown-sizing bug); THIS invariant flags a year where delivered <
    target AND nothing remained (a genuine, accounts-exhausted shortfall) but
    the gap was not recorded -- the very class of silent zero #707 is about.

    A year where the drawdown did not exhaust the accounts but the cash-flow
    identity (``apply_solvency``, #679) subsequently drew the rest is NOT a
    false positive here: the solvency system surfaces its own shortfall
    (``solvency_shortfall``/``ruined``), and the decumulation field
    correctly stays 0 (the drawdown itself did not run out). Such a year is
    skipped via ``solvency_engaged`` -- the final balances are post-solvency,
    so they cannot tell us whether the DRAWDOWN exhausted, and we refuse to
    guess (DP#32).

    Requires ``drawdown_net_target``/``drawdown_net_delivered``/
    ``drawdown_shortfall`` on ``YearResult``; a no-op on result objects that
    predate those fields.
    """
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1.0)
    violations = []
    for i, r in enumerate(results):
        target = getattr(r, 'drawdown_net_target', 0.0)
        if target <= tol:
            continue
        delivered = getattr(r, 'drawdown_net_delivered', 0.0)
        gap = target - delivered
        if gap <= tol:
            continue
        if getattr(r, 'drawdown_shortfall', 0.0) > tol:
            # The field was recorded -- the shortfall is surfaced. Verify it
            # matches the gap (a wrongly-sized field is also a silent-zero
            # flavour: a field that names the wrong number is no better than
            # one that names none).
            if abs(getattr(r, 'drawdown_shortfall', 0.0) - gap) > tol:
                violations.append(Violation(
                    start_year + i,
                    f'drawdown_shortfall {getattr(r, "drawdown_shortfall", 0.0):.2f} '
                    f'does not match the actual gap {gap:.2f} (target {target:.2f} - '
                    f'delivered {delivered:.2f})',
                    getattr(r, 'drawdown_shortfall', 0.0)))
            continue
        # Field is 0 despite delivered < target. That is only acceptable if
        # the drawdown did NOT exhaust the accounts (a sizing bug the sibling
        # invariant flags) OR the solvency waterfall drew the rest afterwards
        # (in which case final balances cannot tell us whether the drawdown
        # exhausted, and we skip rather than guess).
        solvency_engaged = (
            getattr(r, 'solvency_shortfall', 0.0) > tol
            or bool(getattr(r, 'forced_liquidation_events', None))
            or getattr(r, 'ruined', False))
        if solvency_engaged:
            continue
        remaining = (
            getattr(r, 'total_rrsp', 0.0) + getattr(r, 'total_tfsa', 0.0)
            + getattr(r, 'non_reg_balance', 0.0) + getattr(r, 'lif_balance', 0.0)
            + getattr(r, 'lira_balance', 0.0))
        if remaining <= tol:
            violations.append(Violation(
                start_year + i,
                f'drawdown delivered {delivered:.2f} net, short of target '
                f'{target:.2f} by {gap:.2f}, every drawable account exhausted, '
                f'but drawdown_shortfall was 0 -- the shortfall was silently '
                f'swallowed (DP#32)',
                0.0))
    return violations


# ============================================================================
# Smith-Manoeuvre investment tax drag (#576).
# ============================================================================

@invariant('sm_investment_has_tax_drag')
def check_sm_investment_tax_drag(results, ctx):
    """SM (readvanced) investments are legally non-registered and taxable
    (that is the whole basis of the s.20(1)(c) interest deduction); they
    must not compound at the raw gross return with zero tax drag.

    Only checks years with no new readvance (``mortgage_principal == 0``,
    i.e. post mortgage-payoff), where the SM balance's only driver should be
    growth (net of tax), not new contributions.

    ``sm_investment_balance`` is not a field on ``YearResult`` -- the caller
    must derive it (e.g. from ``total_assets`` minus every other named
    account, on a scenario with no FHSA/LIRA/LIF to keep that subtraction
    exact) and pass it as a parallel list via ``ctx['sm_investment_balances']``,
    plus ``ctx['gross_return']``. A no-op without both.
    """
    sm_balances = ctx.get('sm_investment_balances')
    gross_return = ctx.get('gross_return')
    if not sm_balances or gross_return is None:
        return []
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1e-4)

    violations = []
    for i in range(1, len(results)):
        prev_bal = sm_balances[i - 1]
        bal = sm_balances[i]
        if prev_bal <= 0 or results[i].mortgage_principal > tol:
            continue
        ratio = bal / prev_bal
        if ratio >= (1 + gross_return) - tol:
            violations.append(Violation(
                start_year + i,
                f'SM investment grew at the raw gross return ({ratio - 1:.4f}) '
                f'with no tax drag applied (gross_return={gross_return:.4f})',
                bal))
    return violations


# ============================================================================
# After-tax estate never exceeds the pre-tax balance sheet it's derived from
# (issue #580, epic #603). Death tax can only ever shrink what heirs
# receive, never grow it -- this is the one property that has to hold
# regardless of drawdown order, retirement age, or the (currently defaulted,
# issue #600) estate-model inputs.
# ============================================================================

@invariant('estate_value_never_exceeds_pretax_balance_sheet')
def check_estate_le_pretax_balance_sheet(results, ctx):
    """``max_after_tax_estate`` (objective.py, issue #580) must never report
    more than the pre-tax balance sheet (``total_assets - total_debt +
    house_value``) it's computed from -- the deemed-disposition tax at death
    (ITA s.70(5)/s.146(8.8)) only ever removes value, it cannot add any.

    Needs ``ctx['house_value']`` (the property is not tracked in
    ``total_assets``, see ``simulation_state.SimState.total_assets``) and
    ``ctx['start_year']``; a no-op without ``house_value`` since the pre-tax
    balance sheet can't be assembled without it. ``ctx['tax_province']``
    defaults to ``'quebec'``.
    """
    house_value = ctx.get('house_value')
    if house_value is None:
        return []
    start_year = ctx.get('start_year', 0)
    province = ctx.get('tax_province', 'quebec')
    tol = ctx.get('tolerance', 1.0)

    from objective import compute_after_tax_estate

    violations = []
    for i, r in enumerate(results):
        cfg = {
            'property': {'house_value': house_value},
            'tax': {'province': province, 'year': start_year + i},
        }
        est = compute_after_tax_estate([r], cfg)
        pretax_balance_sheet = r.total_assets - r.total_debt + house_value
        if est.net_estate > pretax_balance_sheet + tol:
            violations.append(Violation(
                start_year + i,
                f'after-tax estate ({est.net_estate:.2f}) exceeds the '
                f'pre-tax balance sheet ({pretax_balance_sheet:.2f}) it was '
                f'computed from',
                est.net_estate))
    return violations


# ============================================================================
# Money conservation for a year-0 lump-sum draw (issues #257, #577, #619;
# DP#18's core identity: invested capital == new debt taken on).
# ============================================================================

@invariant('invested_capital_equals_new_debt')
def check_invested_capital_equals_new_debt(results, ctx):
    """No dollar is invested that was neither borrowed nor already present.

    DP#18's conservation identity, checked directly rather than by pinning
    two code paths to each other: the new debt actually DRAWN in year 0 must
    equal ``ctx['expected_year0_new_debt']`` -- a figure the caller computes
    *independently* of the overlay/optimizer code under test (e.g.
    ``base_margin_available + cash_out``, the #257 rule applied by hand to
    the raw input numbers).

    This is deliberately NOT "does the optimizer path match the simulate.py
    path" -- #619 shipped with exactly that cross-engine check in place
    (``test_apply_ltv_overlay_matches_grid_optimizer``) and it passed, because
    both optimizer copies inflated ``margin_available`` by ``cash_out``
    *identically* and then derived their own "expected" new-debt figure from
    that same inflated number: self-consistent, and wrong. A check that reads
    its expectation back off the code path it is checking cannot catch a bug
    both branches share (#257's original sin, repeated at #577 and #619).
    Here the expectation is supplied by the caller from first principles, so
    a bug in the overlay/draw-booking logic under test cannot also corrupt
    the yardstick.

    "New debt drawn in year 0" is isolated from two same-year confounds that
    would otherwise corrupt the comparison, since both events land in the
    same ``YearResult``:

    - Mortgage amortization pays principal down in the same year the
      cash-out increases it, so the draw is ``mortgage_balance +
      mortgage_principal - opening_mortgage`` (add back what was paid),
      not the raw end-of-year balance delta.
    - The HELOC draw capitalizes interest in the same year it is booked
      (``margin_heloc_interest`` in ``simulate_year_pure``), so the draw is
      ``heloc_balance / (1 + heloc_rate) - opening_heloc``, not the raw
      end-of-year balance delta either. This assumes no other same-year
      HELOC paydown (e.g. an RRSP-refund-funded paydown, see
      ``simulate_year_pure``'s ``heloc_paydown``) -- a fixture built to
      exercise this invariant cleanly should not also generate one in year 0
      (zero RRSP room, or ``deduct_later=True`` with no first-year claim, is
      enough).

    Opt-in via ``ctx['expected_year0_new_debt']``; a no-op without it (most
    scenarios draw no year-0 lump sum, so there is nothing to check). Also
    reads ``ctx['opening_mortgage_balance']`` and
    ``ctx['opening_heloc_balance']`` (both default to 0.0 -- SimState.initial
    always opens ``heloc_balance`` at 0.0 per #577, so the default is correct
    for every scenario that does not pre-seed a drawn HELOC balance).
    """
    expected = ctx.get('expected_year0_new_debt')
    if expected is None or not results:
        return []
    opening_mortgage = ctx.get('opening_mortgage_balance', 0.0)
    opening_heloc = ctx.get('opening_heloc_balance', 0.0)
    tol = ctx.get('tolerance', 1.0)
    start_year = ctx.get('start_year', 0)

    r0 = results[0]
    mortgage_drawn = (r0.mortgage_balance + r0.mortgage_principal) - opening_mortgage
    heloc_rate = getattr(r0, 'heloc_rate', 0.0)
    heloc_drawn = (r0.heloc_balance / (1 + heloc_rate) if heloc_rate else r0.heloc_balance) - opening_heloc
    actual_new_debt = mortgage_drawn + heloc_drawn
    violations = []
    if abs(actual_new_debt - expected) > tol:
        violations.append(Violation(
            start_year,
            f'year-0 new debt (${actual_new_debt:,.2f}) does not equal the '
            f'independently-computed correct figure (${expected:,.2f}); '
            f'${actual_new_debt - expected:+,.2f} of invested capital was '
            f'invested but never borrowed',
            actual_new_debt))
    return violations


# ============================================================================
# Cash-flow solvency identity (issue #679, #581's trajectory-invariant
# pattern): after_tax_income + drawdowns >= debt_service + living_costs +
# contributions must hold every year -- and when it does not, the year must
# be marked ruined rather than left to silently proceed as if the
# contributions booked that year were achievable.
# ============================================================================

@invariant('cash_flow_solvency_identity')
def check_cash_flow_solvency_identity(results, ctx):
    """The forced-liquidation waterfall must actually close the gap it
    computed, and the ``ruined`` flag must agree with whether it did.

    A no-op in every year ``living_costs <= 0`` (simulation_rules
    .apply_solvency's own DP#16 "not engaged" state -- see that rule's
    docstring). In every year it IS engaged, this checks, purely from
    ``YearResult``'s own reported fields (no re-derivation of contributions
    from allocations, which would duplicate ``apply_solvency``'s own
    arithmetic rather than independently checking its output):

      1. ``solvency_shortfall`` is never negative (it is defined as
         ``max(0, required - available)`` by construction).
      2. The waterfall never reports covering MORE than the shortfall it
         was asked to close.
      3. ``ruined`` is True if and only if the waterfall's ``covered``
         fell short of the ``solvency_shortfall`` by more than a dollar --
         DP#32: a shortfall that survives every real source must be
         reported, and a shortfall that WAS fully covered must not be
         reported as ruin either (a false ruin is exactly as dishonest as
         a hidden one).
      4. Every per-step liquidation event's tax/loss sums to the year's
         reported ``forced_liquidation_tax`` / ``forced_liquidation_realized_loss``
         totals (internal consistency of the report).
    """
    start_year = ctx.get('start_year', 0)
    tol = ctx.get('tolerance', 1.0)
    violations = []
    for i, r in enumerate(results):
        # Direct attribute reads, not `getattr(r, ..., default)`: every one of
        # these is a real YearResult field. A defaulted read here would make a
        # RESULT OBJECT THAT NEVER RAN THE SOLVENCY RULE indistinguishable from
        # one that ran it and found the household solvent -- and this invariant
        # would then pass by reading its own default back (DP#32). If a caller
        # hands this check a result type without these fields, it must crash.
        living_costs = r.living_costs
        if living_costs <= 0:
            continue
        year = start_year + i
        shortfall = r.solvency_shortfall
        covered = r.solvency_covered

        if shortfall < -tol:
            violations.append(Violation(
                year, f'solvency_shortfall is negative ({shortfall:.2f})', shortfall))
        if covered > shortfall + tol:
            violations.append(Violation(
                year,
                f'forced-liquidation waterfall covered {covered:.2f}, more than '
                f'the {shortfall:.2f} shortfall it was asked to close',
                covered))

        expected_ruined = (shortfall - covered) > 1.0
        if r.ruined != expected_ruined:
            violations.append(Violation(
                year,
                f'ruined={r.ruined} but shortfall {shortfall:.2f} minus '
                f'covered {covered:.2f} = {shortfall - covered:.2f} '
                f'{"exceeds" if expected_ruined else "does not exceed"} $1 -- '
                f'the flag disagrees with the waterfall\'s own numbers',
                r.ruined))

        # `forced_liquidation_events` is a real YearResult field with a
        # default_factory=list, so it is ALWAYS a list -- never None. A
        # `getattr(..., None) or []` here would be exactly the DP#32 shape
        # this repo's own architecture guard rejects (and did reject, when
        # this line was first written that way): it would silently launder a
        # missing/None field into "no liquidations happened", which is the
        # single most dangerous wrong answer this invariant could give.
        events = r.forced_liquidation_events
        events_tax = sum(e['tax'] for e in events)
        events_loss = sum(min(0.0, e['realized_gain']) for e in events)
        if abs(events_tax - r.forced_liquidation_tax) > tol:
            violations.append(Violation(
                year,
                f'forced_liquidation_tax ({r.forced_liquidation_tax:.2f}) does '
                f'not match the sum of per-step tax in forced_liquidation_events '
                f'({events_tax:.2f})',
                r.forced_liquidation_tax))
        if abs(events_loss - r.forced_liquidation_realized_loss) > tol:
            violations.append(Violation(
                year,
                f'forced_liquidation_realized_loss ({r.forced_liquidation_realized_loss:.2f}) '
                f'does not match the sum of per-step realized losses in '
                f'forced_liquidation_events ({events_loss:.2f})',
                r.forced_liquidation_realized_loss))
    return violations


@invariant('ruin_reported_when_shortfall_survives_waterfall')
def check_ruin_marks_terminal_figures(results, ctx):
    """Once a year is ruined, every subsequent year's ``ruined`` flag (or
    the same year) must be checkable by a caller BEFORE it reports a
    terminal net_benefit as achievable -- this invariant does not itself
    forbid a positive ``net_benefit`` in a ruined trajectory (the ledger
    value is still reported honestly, see ``YearResult.ruined``'s
    docstring), but it asserts the discoverability property the whole
    mechanism exists for: if any year in the trajectory is ruined, at
    least one ``YearResult`` in the list reports it, so no caller reading
    the full trajectory can miss it. A no-op if no year ever goes insolvent
    (``ctx['expect_ruin']`` unset or False) -- most scenarios never draw
    the waterfall at all.
    """
    if not ctx.get('expect_ruin', False):
        return []
    if not any(r.ruined for r in results):
        return [Violation(
            ctx.get('start_year', 0),
            'ctx declared this scenario expects ruin, but no year in the '
            'trajectory reports ruined=True',
            None)]
    return []
