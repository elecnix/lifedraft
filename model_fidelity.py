#!/usr/bin/env python3
"""Model Fidelity Registry — approximations declare themselves into the output.

DP#32: absence must fail loudly; a rule that is disabled must say so in the
output. This module extends that principle to approximations: a computation
that biases a headline figure (over- or under-states it, in a known or
unknown direction) must not rely on a docstring or a `# NOTE:` comment for
disclosure. It must register itself here, and every output surface
(console header, TextReport, JsonReport, HtmlReport — see output_plugins.py)
renders whatever is registered as *active for this run's config*.

Why a registry and not per-call-site printing (issue #585):

- One place to look ("is this run's headline number exact?") instead of
  grepping docstrings.
- `Approximation.__post_init__` refuses to construct an entry that has no
  `biased_figure` or `direction` — DP#32's "a caveat with no target figure
  is not a caveat" made structural, not a review nit.
- `active_approximations(cfg, objective_name)` is a pure function of the run
  context (DP#3): whether an approximation actually bites is itself
  data-driven (e.g. the gross-drawdown approximation only fires when
  `retirement.drawdown_tax_mode` is left at its default 'gross'; the pre-tax
  terminal-wealth caveat only fires for objectives that really are pre-tax),
  so a run that avoids an approximated code path does not carry a caveat that
  does not apply to it — while a run that hits it cannot omit the caveat.
- **The registry is only trustworthy if entries LEAVE it when the underlying
  approximation is fixed.** A caveat for an approximation that no longer
  exists is a false disclosure; it teaches the reader to ignore the section,
  which destroys the value of the entries that are still true. When you fix
  an approximation, delete its entry in the same PR (see the "NOTE ON REMOVED
  ENTRIES" comment below for a worked example).
- `tests/test_issue_585_model_fidelity.py::test_known_caveat_language_is_registered_or_allowlisted`
  greps the source tree for the caveat vocabulary ("approximate",
  "conservative (never", "simplified", "overstate", "understate") near
  headline computations and fails CI if a new hit is neither registered
  here nor added to `_KNOWN_NON_BIASING` with a reason. That is the
  mechanical half of "impossible to add a biasing approximation without it
  appearing in the output": you cannot introduce the vocabulary without the
  test forcing a decision.

This module is deliberately import-light (stdlib only) so it can be
imported from `output_plugins.py`, `optimize.py`, `simulation.py` and tests
without pulling in a heavier dependency cycle (DP#25: dependencies point
inward).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional


class Direction(Enum):
    """Which way a known approximation pushes the figure it biases."""
    OVERSTATES = "overstates"    # the reported figure is too high
    UNDERSTATES = "understates"  # the reported figure is too low
    UNKNOWN = "unknown"          # biases it, but the sign isn't established

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class FidelityContext:
    """What an ``applies`` predicate gets to look at.

    The resolved config, plus the *objective the run is actually ranking on*.
    The objective matters because several approximations bite only for some
    objectives: ``max_terminal_wealth`` sums balances pre-tax, while
    ``max_net_benefit`` deducts an (approximate) withdrawal tax. A caveat that
    fires for both would be wrong for one of them.

    ``objective_name`` is None when the surface has no single objective in view
    (e.g. the pre-run console header). Predicates must treat None as "unknown,
    so report the caveat" — DP#32: fail loud, not silent.
    """
    cfg: dict = field(default_factory=dict)
    objective_name: Optional[str] = None


@dataclass(frozen=True)
class Approximation:
    """A single declared modelling approximation.

    Attributes:
        id: Stable identifier (used in JSON output and tests).
        summary: One line — what is approximated. Shown in every report.
        biased_figure: Which headline output figure this approximation
            biases (e.g. "retirement withdrawal amount", "terminal net
            worth"). DP#32: an approximation with no named target is not
            a real disclosure.
        direction: Direction the figure is pushed. Direction.UNKNOWN is a
            legitimate, honestly-reported value — it is NOT the same as
            omitting direction entirely (which __post_init__ rejects).
        detail: Longer explanation for the JSON/HTML reports.
        issue: Tracking issue reference, e.g. "#363". Optional but expected.
        applies: Optional predicate over a FidelityContext. If None, the
            approximation is active whenever it is registered (it is
            unconditionally on the code path). If provided, the
            approximation is only surfaced for runs where it actually
            bites — a rule that has been fixed, disabled, or routed around
            for this run must NOT keep claiming to bias output. A caveat
            for an approximation that no longer exists is as corrosive to
            trust as a missing one, and harder to notice.
        findings: Optional function from a FidelityContext to a list of
            one-line strings naming THIS RUN's specific figures. `summary`
            and `detail` are static — they can say "a belief contradicts a
            signed rate", but they cannot say *which* liability, or *which
            two rates*, because the registry is built at import time and an
            Approximation is frozen. A caveat that depends on the run's own
            numbers (#685's input contradiction, #707's trajectory shortfall)
            supplies them here and every surface (console, TXT, JSON, HTML)
            renders them alongside the summary.
    """
    id: str
    summary: str
    biased_figure: str
    direction: Direction
    detail: str = ""
    issue: str = ""
    applies: Optional[Callable[['FidelityContext'], bool]] = field(
        default=None, repr=False, compare=False)
    findings: Optional[Callable[['FidelityContext'], List[str]]] = field(
        default=None, repr=False, compare=False)

    def __post_init__(self):
        if not self.id:
            raise ValueError("Approximation requires a non-empty id")
        if not self.summary:
            raise ValueError(f"Approximation {self.id!r}: summary is required — "
                              "a caveat that doesn't say what is approximated is not a caveat")
        if not self.biased_figure:
            raise ValueError(f"Approximation {self.id!r}: biased_figure is required (DP#32) — "
                              "an approximation that doesn't name the headline figure it "
                              "affects cannot be surfaced meaningfully")
        if not isinstance(self.direction, Direction):
            raise ValueError(f"Approximation {self.id!r}: direction must be a model_fidelity.Direction "
                              "member (OVERSTATES/UNDERSTATES/UNKNOWN), not a free-text guess")

    def is_active(self, ctx: 'FidelityContext') -> bool:
        """Whether this approximation actually bites for the given run context."""
        if self.applies is None:
            return True
        try:
            return bool(self.applies(ctx))
        except Exception:
            # DP#32: a predicate that fails to evaluate must not silently
            # hide the caveat — fail open (report it) rather than fail closed.
            return True

    def findings_for(self, ctx: 'FidelityContext') -> List[str]:
        """This run's specific, named figures. Empty when the caveat carries
        no run-specific detail (the common case)."""
        if self.findings is None:
            return []
        return list(self.findings(ctx))

    def to_dict(self, ctx: Optional['FidelityContext'] = None) -> Dict:
        d = {
            'id': self.id,
            'summary': self.summary,
            'biased_figure': self.biased_figure,
            'direction': self.direction.value,
        }
        if self.detail:
            d['detail'] = self.detail
        if self.issue:
            d['issue'] = self.issue
        if ctx is not None:
            found = self.findings_for(ctx)
            if found:
                d['findings'] = found
        return d


# ── Registry ─────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, Approximation] = {}


def register(approx: Approximation) -> Approximation:
    """Register an approximation. Returns it so a module-level constant can
    bind the same object, e.g. ``GROSS_UP = register(Approximation(...))``.

    This is the only sanctioned way to declare that a computation biases a
    headline figure (DP#32). See the module docstring for the enforcement
    story around comments that use the caveat vocabulary without a matching
    entry here.
    """
    existing = _REGISTRY.get(approx.id)
    if existing is not None and existing != approx:
        raise ValueError(f"Approximation id {approx.id!r} already registered with different content")
    _REGISTRY[approx.id] = approx
    return approx


def all_approximations() -> List[Approximation]:
    """Every known approximation, regardless of whether it applies to any particular run."""
    return list(_REGISTRY.values())


def active_approximations(cfg: dict,
                          objective_name: Optional[str] = None) -> List[Approximation]:
    """Approximations that actually bias headline output for this run.

    Pure function of (cfg, objective_name) (DP#3) — no simulation run
    required. ``objective_name`` narrows the objective-sensitive caveats
    (a pre-tax terminal-wealth caveat must not fire for a run ranking on an
    after-tax estate objective). Passing None means "objective unknown", and
    the objective-sensitive caveats then report themselves rather than
    silently disappear.

    Callers that additionally want to show "known but not active in this run"
    can diff this against ``all_approximations()``.
    """
    ctx = FidelityContext(cfg=cfg or {}, objective_name=objective_name)
    return [a for a in _REGISTRY.values() if a.is_active(ctx)]


def reset_registry_for_tests() -> None:
    """Test-only: clear the registry so a test can register a throwaway
    Approximation without colliding with the real ones. Not called by
    production code."""
    _REGISTRY.clear()


# ── Units / dollar-basis disclosure (issue #597, output half) ─────────────
#
# The full fix (dated, unitless input contract) is Track C (#602). What can
# be done now, from today's config shape, is: state plainly what IS known
# (as_of year, currency, whether the run is nominal or real dollars) rather
# than let the reader guess. `assumptions.dollar_basis` / `assumptions.currency`
# / `assumptions.base_year` are new OPTIONAL input keys (default preserves
# today's behaviour — nominal dollars, CAD) added alongside this mechanism;
# see input_schema.json and make_retirement_input.py.

def describe_units(cfg: dict) -> Dict[str, str]:
    """Best-effort unit disclosure for the current config.

    Returns a dict with as_of, currency, dollar_basis, and (if real) base_year.
    Every key is always present — DP#32: a field that cannot be determined is
    reported as 'unspecified', never silently omitted.
    """
    cfg = cfg or {}
    assumptions = cfg.get('assumptions', {}) or {}
    tax = cfg.get('tax', {}) or {}

    # DP#32 (#606): an explicit start_year=0 is erroneous data (never a
    # legitimate calendar year), not absence -- it must surface, not
    # silently fall through to tax.get('year').
    as_of = assumptions.get('start_year')
    as_of = tax.get('year') if as_of is None else as_of
    dollar_basis = assumptions.get('dollar_basis', 'nominal')
    currency = assumptions.get('currency', 'CAD')

    out = {
        'as_of': str(as_of) if as_of else 'unspecified',
        'currency': currency,
        'dollar_basis': dollar_basis,
    }
    if dollar_basis == 'real':
        base_year = assumptions.get('base_year', as_of)
        out['base_year'] = str(base_year) if base_year else 'unspecified'
    return out


def units_line(cfg: dict) -> str:
    """One-line rendering of describe_units(), for console/TXT headers."""
    u = describe_units(cfg)
    basis = f"real (base year {u['base_year']})" if u['dollar_basis'] == 'real' else 'nominal'
    return f"As of {u['as_of']} | {u['currency']} | {basis} dollars"


# ── Rendering helpers shared by output_plugins.py ──────────────────────────

def render_text(cfg: dict, objective_name: Optional[str] = None) -> List[str]:
    """Lines for the TXT/console 'MODEL FIDELITY' section. Empty caveats list
    still renders the units line — units are stated unconditionally."""
    lines = [units_line(cfg)]
    ctx = FidelityContext(cfg=cfg or {}, objective_name=objective_name)
    active = active_approximations(cfg, objective_name)
    if active:
        lines.append("Known approximations affecting this run's headline figures:")
        for a in active:
            issue = f" ({a.issue})" if a.issue else ""
            lines.append(f"  - {a.summary} -> {a.biased_figure} [{a.direction.value}]{issue}")
            # #685/#707: the run's OWN figures, not just the static caveat.
            for finding in a.findings_for(ctx):
                lines.append(f"      * {finding}")
    else:
        lines.append("No registered approximations are active for this configuration.")
    return lines


def to_dict(cfg: dict, objective_name: Optional[str] = None) -> Dict:
    """Structured 'model_fidelity' block for JSON/HTML consumption."""
    ctx = FidelityContext(cfg=cfg or {}, objective_name=objective_name)
    return {
        'units': describe_units(cfg),
        'objective': objective_name or 'unspecified',
        'approximations': [a.to_dict(ctx)
                           for a in active_approximations(cfg, objective_name)],
    }


# ── Known approximations (the inventory, wired live) ────────────────────────
#
# Each entry below corresponds to a real approximation in this codebase today
# (file:line noted in `detail`). Predicates are deliberately conservative:
# when in doubt, report the caveat rather than suppress it (DP#32 applied to
# the caveat mechanism itself). Fixing any of these is out of scope for
# #585 — surfacing them is the deliverable; see the referenced issues for
# the fix.

#
# NOTE ON REMOVED ENTRIES — a stale caveat is a bug, and this file has already
# had to eat its own dog food twice.
#
# `sm_vs_nonreg_return_asymmetry` (#576) — DELETED, not merely disabled. PR #613
#   unified the two models of taxable investing, so SM/readvanceable investments
#   now compound at the SAME after-tax rate as non-registered ones (see
#   simulation_state.py, "Grow SM investment").
#
# `retirement_drawdown_gross_up` (#363/#579) — DELETED. PR #614 made the
#   tax-exact net drawdown the ONLY path and removed `gross_up_for_tax` and the
#   `drawdown_tax_mode` switch entirely. The over-draw it described cannot occur
#   any more. Its residual — the flat-rate pricing that #614 did NOT close —
#   survived as `drawdown_flat_marginal_rate`, itself now DELETED by #363 PR 4
#   (see the per-spouse-split note below).
#
# `drawdown_bracket_fill_target_excludes_oas` (#618) — DELETED. #363 PR 3 made
#   the 'rrsp_bracket_fill' headroom fill against an OAS-INCLUSIVE taxable base
#   (CPP + pension + full OAS): OAS is taxable income for bracket purposes even
#   though it is received net of the recovery tax, so the ceiling no longer
#   omits it and the bracket-fill order no longer over-draws by the OAS slice
#   (simulation_rules.apply_retirement_income: `bracket_fill_base = ... + oas`;
#   plan_drawdown_net's `bracket_fill_base` headroom). The entry's SECONDARY
#   claim — that the bracket-fill draw is not split across two spouses' separate
#   brackets — was the SAME single-household simplification carried by
#   `drawdown_flat_marginal_rate` clause (c); #363 PR 4 has since split the draw
#   (and the bracket-fill headroom) per spouse, so that simplification is now
#   closed too, not merely relocated.
#
# Both were caught by TestNoStaleCaveats, which pins every entry to a code
# anchor; the second was caught *in flight*, when #614 merged mid-review and CI
# went red on a branch that had been green locally an hour earlier. That is the
# mechanism working: continuing to print a caveat for an approximation somebody
# has since fixed is a FALSE disclosure. It corrodes trust exactly the way a
# missing disclosure does, and it is harder to notice, because it teaches the
# reader to skim the section. When an approximation is fixed, its entry leaves
# this file in the same PR.

# `_has_retirement_data()` was removed with #825: it existed ONLY to gate the
#   `rrif_forced_excess_tax_rate` approximation (its `applies=` predicate). #825
#   deleted that approximation (the forced RRIF minimum is now priced through the
#   real progressive + OAS-clawback path), leaving the helper with zero callers.
#   Per this file's own rule above — "when an approximation is fixed, its entry
#   leaves this file in the same PR" — its supporting predicate leaves too (DP#9).


# `drawdown_flat_marginal_rate` (#363/#579) — DELETED, not merely disabled. This
#   entry outlived its three clauses one at a time: (a) no re-bracketing of the
#   incremental draw — retired by #363 PR 1 (progressive bracket walk); (b) the
#   OAS 15% recovery tax not folded into the rate — retired by #363 PR 2 (the
#   clawback fixpoint); and finally (c) a couple's draw and its clawback priced
#   against ONE household schedule rather than split across two spouses' separate
#   bracket sets — retired by #363 PR 4. plan_drawdown_net now takes a
#   `per_member` split: each spouse's RRSP/RRIF slice re-brackets against, and
#   claws back OAS from, that spouse's OWN income
#   (retirement_transition.py:_KEY_OWNER; simulation_rules.apply_retirement_income
#   computes the per-member bases; apply_retirement_drawdown passes them when both
#   spouses are retired). With all three clauses gone the approximation no longer
#   exists, so its entry leaves this file in the same PR (TestNoStaleCaveats: a
#   caveat for a fixed approximation is a FALSE disclosure). The per-spouse
#   marginal rates this unlocked are what #711 (CPP sharing) and #712 (pension
#   split) needed in the live drawdown.


# `rrif_forced_excess_tax_rate` (#294/#301/#363) — DELETED, not merely disabled.
#   The forced RRIF minimum's tax used to be a flat placeholder marginal rate
#   evaluated at ``cpp + pension + max(net_shortfall, 40000)`` and applied to the
#   whole forced slice, with the OAS 15% recovery tax the minimum triggers
#   omitted entirely. Issue #825 routes it through the SAME machinery the
#   discretionary drawdown already uses: apply_rrif_minimum now prices each
#   spouse's forced slice via retirement_transition.price_forced_rrif_tax —
#   progressive re-bracketing (#363 PR 1) on top of that spouse's own recognized
#   income, the incremental OAS clawback (#363 PR 2) it triggers booked as
#   reduced OAS income, per spouse (#363 PR 4). With the flat placeholder gone
#   the approximation no longer exists, so its entry leaves this file in the same
#   PR (TestNoStaleCaveats: a caveat for a fixed approximation is a FALSE
#   disclosure).


# Objectives that sum balances with NO deduction for the tax owed on
# registered dollars. `max_terminal_wealth` says so in its own docstring
# ("Does NOT deduct future tax on RRSP withdrawals"). The after-tax
# objectives `max_after_tax_estate` (PR #616) and `min_after_tax_estate`
# (issue #1009, the die-with-near-zero mirror) must NOT carry this caveat —
# hence the predicate rather than `applies=None`.
_PRETAX_WEALTH_OBJECTIVES = {'max_terminal_wealth'}

# The objectives whose figure is derived from the after-tax estate under the
# SAME deemed-disposition math (compute_after_tax_estate): the maximising
# `max_after_tax_estate` and `min_after_tax_estate` (issues #1009/#1065/#1081:
# since #1081 it ranks the SPEND-DOWN surface -- drawable_after_tax -- with
# terminal debt priced as a pure penalty, no longer the plain negated
# estate). Every estate-figure caveat below applies equally to
# both — a household optimising toward die-with-zero runs the same estate
# defaults and the same point-in-time valuation, so suppressing the
# caveat under the min objective would be a false disclosure gap.
_AFTER_TAX_ESTATE_OBJECTIVES = {'max_after_tax_estate', 'min_after_tax_estate'}


def _pretax_objective_active(ctx: FidelityContext) -> bool:
    # DP#32: an unknown objective is reported, not silently exonerated.
    if ctx.objective_name is None:
        return True
    return ctx.objective_name in _PRETAX_WEALTH_OBJECTIVES


register(Approximation(
    id='terminal_wealth_is_pretax',
    summary=("The terminal-wealth ranking figure sums account balances at face value: "
             "a dollar inside an RRSP/RRIF counts the same as a dollar inside a TFSA, "
             "though the registered dollar still owes income tax on withdrawal or at "
             "death and the TFSA dollar does not"),
    biased_figure="terminal wealth (max_terminal_wealth objective), which over-values registered assets",
    direction=Direction.OVERSTATES,
    detail=("objective.py:_terminal_wealth() returns total_assets - total_debt with no "
             "tax deducted, over a simulation_state.py total_assets that is a gross sum "
             "of RRSP+TFSA+non-reg+... PR #616 added the max_after_tax_estate objective, "
             "which applies the deemed disposition properly; rank on that one to avoid "
             "this approximation entirely. This caveat is scoped to the objectives that "
             "remain pre-tax BY DESIGN, so it does not fire for the after-tax one."),
    issue='#580',
    applies=_pretax_objective_active,
))


def _after_tax_estate_objective_active(ctx: FidelityContext) -> bool:
    # Only fires for the objectives whose figure these defaults actually
    # shape -- the after-tax-estate family (max + its #1009 min mirror).
    return ctx.objective_name in _AFTER_TAX_ESTATE_OBJECTIVES


# ── epic #603 Track C Phase 2c (#600): `after_tax_estate_defaulted_assumptions`
# is GONE from this registry, not merely edited.
#
# It said the five estate assumptions rest on things "the input schema cannot
# yet express". That sentence is now FALSE — the contract's `estate` namespace
# expresses every one of them (spousal_rollover, per-account
# beneficiary/successor_holder, the real non-registered ownership split, the
# per-property principal-residence designation, life_insurance[], per-person
# mortality), `contract_estate._map_estate` maps them, and
# `estate.EstatePlan`/`compute_estate` consume them. A caveat that describes a
# limitation that no longer exists is a FALSE disclosure — exactly what
# TestNoStaleCaveats exists to catch, and exactly as corrosive as a missing one.
#
# Two NARROWER, still-true caveats replace it below. Neither is the old one
# renamed: the first fires only when a config declines to declare its elections
# at all (a real DP#32 absence, and a real possibility for an in-memory config,
# though never for a contract-sourced run), and the second declares a genuinely
# NEW, residual modelling gap that this phase surfaced rather than closed.


def _estate_elections_undeclared(ctx: FidelityContext) -> bool:
    """Fires only when the after-tax-estate figure is being reported for a config
    that never declared its estate elections — so the numbers are running on
    objective._UNDECLARED_ESTATE_DEFAULTS. A contract-sourced run always
    declares them (`estate` is a required key of the schema), so this is the
    in-memory/ad-hoc-config case."""
    if ctx.objective_name not in _AFTER_TAX_ESTATE_OBJECTIVES:
        return False
    from objective import estate_is_declared
    return not estate_is_declared(ctx.cfg or {})


register(Approximation(
    id='estate_elections_not_declared',
    summary=("This config declares no `estate` block, so the after-tax estate figure is "
             "running on defaulted elections: no spousal rollover, the TFSA treated as "
             "passing to a successor holder, the non-registered gain split 50/50 across "
             "the two terminal returns, and the residence assumed designated as the "
             "principal residence"),
    biased_figure=('after-tax estate / legacy to heirs (max_after_tax_estate objective), '
                    'and net_benefit for a leveraged household (post-#1034 it prices the SM '
                    'sleeve via the same estate path, so defaulted elections bias it too)'),
    direction=Direction.OVERSTATES,
    detail=("Every one of these is now an INPUT (epic #603 Track C Phase 2c / #600): the "
            "contract's `estate` namespace carries default_spousal_rollover, per-account "
            "rollover_overrides and beneficiary/successor_holder designations, "
            "per-property designated_principal_residence_years, life_insurance[], and "
            "assumptions.mortality. This caveat fires only because THIS config supplied "
            "none of them, so objective._UNDECLARED_ESTATE_DEFAULTS were used. Direction "
            "is OVERSTATES because the defaults are the favourable branch of each choice: "
            "the residence is assumed to attract the s.40(2)(b) exemption (worth ~$213k of "
            "tax on the #581 golden household if it does NOT), and the TFSA is assumed to "
            "keep its shelter. Declare the estate block and this caveat disappears — "
            "because the numbers stop being a guess."),
    issue='#600',
    applies=_estate_elections_undeclared,
))


register(Approximation(
    id='estate_is_a_point_in_time_valuation',
    summary=("The estate is valued at ONE date — the projection horizon — so the interval "
             "between the first and second death is not modelled: assets that roll to a "
             "survivor are not grown (or drawn down, or taxed) over the years they would "
             "actually be held, and a TFSA left to a beneficiary rather than a successor "
             "holder loses its shelter at a death this model never reaches"),
    biased_figure=('after-tax estate / legacy to heirs (max_after_tax_estate objective), '
                    'and net_benefit for a leveraged household (post-#1034 it prices the SM '
                    'sleeve via the same estate path, so the point-in-time valuation biases it too)'),
    direction=Direction.UNKNOWN,
    detail=("Phase 2c (#600) made the estate ELECTIONS real inputs, and the rollover "
            "election's bracket-compression effect is now priced exactly (on the #581 "
            "golden household: electing the rollover costs $78,525 MORE tax than declining "
            "it, because one combined terminal return runs the progressive brackets once "
            "instead of twice). What it does NOT price is the TIMING: countries/canada/"
            "estate.py values everything at a single horizon date, so it cannot model the "
            "years between the two deaths — during which rolled-over assets keep "
            "compounding (which would RAISE the eventual tax base) while a declined "
            "rollover has already paid its tax and compounds a smaller, after-tax pot "
            "(which would LOWER it). The two effects push in opposite directions, so the "
            "direction of the residual bias is genuinely UNKNOWN rather than conveniently "
            "favourable. The same gap is why EstatePlan.tfsa_successor_holder is consumed "
            "and reported (EstateResult.tfsa_shelter_ends) but moves no dollars: under the "
            "ITA a TFSA is tax-free at its death-date VALUE under BOTH designations, and "
            "the difference is entirely in the post-death growth this snapshot does not "
            "reach. Closing it needs a two-date (first-death / second-death) estate model, "
            "which is a real modelling extension, not a config gap."),
    issue='#600',
    applies=_after_tax_estate_objective_active,
))


def _net_benefit_objective_active(ctx: FidelityContext) -> bool:
    if ctx.objective_name is None:
        return True
    return ctx.objective_name == 'max_net_benefit'


register(Approximation(
    id='net_benefit_withdrawal_tax_is_estimated',
    summary=("net_benefit does deduct a future withdrawal tax, but an ESTIMATED one: it "
             "prices the terminal RRSP against a single assumed retirement scenario, and "
             "falls back to a flat 30% withdrawal tax whenever the member has no "
             "birth_year — neither is the deemed disposition that actually occurs"),
    biased_figure='net_benefit ranking figure (the default objective, and the console headline)',
    direction=Direction.UNKNOWN,
    detail=("optimize.py:compute_net_benefit() — the RRSP withdrawal tax is computed from "
             "a projected retirement state when birth_year is present, and from a flat "
             "30% assumption when it is not (a round-number placeholder, not this "
             "household's rate). Distinct from 'terminal_wealth_is_pretax': net_benefit "
             "is not a raw pre-tax sum, it is an approximately-taxed one."),
    issue='#580',
    applies=_net_benefit_objective_active,
))


# Issue #672: the sibling caveat above ("an ESTIMATED withdrawal tax") is
# TRUE but not sharp enough — it reads as ordinary imprecision, when what
# #661's VOI sweep actually measured is stronger: net_benefit's withdrawal-tax
# estimate is a PRE-DEATH retirement drawdown that never models a death at
# all, so the estate election levers move it by EXACTLY $0, not "somewhat
# imprecisely". This is a distinct, sharper claim ("no sensitivity", not "an
# approximation") and needs its own entry rather than a reworded one, per
# DP#32's caveat-vocabulary discipline: "these five inputs have zero effect
# on the number you are reading" is a different fact than "this number is
# estimated".
register(Approximation(
    id='net_benefit_omits_estate_elections',
    summary=("net_benefit prices the SM sleeve's terminal deemed disposition via "
             "the estate code path (issue #1034), so the spousal-rollover election "
             "MOVES it for a leveraged household; but it still prices the non-reg "
             "pot with its own marginal_rate (not the estate's progressive "
             "stacking + rollover), and it does not price TFSA / principal-residence "
             "/ life-insurance at death at all -- so those estate elections remain "
             "inert; rank on max_after_tax_estate to see the FULL estate priced"),
    biased_figure=('net_benefit ranking figure (the default objective, and the console '
                    'headline) -- its partial blindness to the /estate election levers specifically'),
    direction=Direction.UNKNOWN,
    detail=("#661's VOI sweep (voi.py) measured /estate/default_spousal_rollover at $0 "
             "VOI under max_net_benefit and $84,998 under max_after_tax_estate on a "
             "reference household -- proof, not inference, that compute_net_benefit() "
             "(optimize.py) never routed through objective.compute_after_tax_estate() / "
             "countries/canada/estate.py. Issue #1034 closed the largest piece: "
             "compute_net_benefit() now prices the SM sleeve's terminal deemed "
             "disposition by calling compute_estate(**_estate_call_args(...)) -- the "
             "SAME estate code path max_after_tax_estate uses (DP#9, one spelling) -- "
             "so the spousal-rollover election (which the SM sleeve mirrors, via the "
             "non-reg pot's rollover) now moves net_benefit for a leveraged household. "
             "The blindness that remains is the NON-REG pot: net_benefit still prices "
             "it with its own marginal_rate (a pre-death retirement drawdown estimate), "
             "not the estate's progressive stacking + rollover, so the rollover's "
             "effect on the non-reg pot's deemed disposition still does not move it; and "
             "TFSA (tax-free), principal-residence designation, and life-insurance death "
             "benefits are not priced at death at all. Rank on max_after_tax_estate to "
             "see the full estate priced, or read both: optimize.py prints "
             "them side by side whenever `estate` is declared (issue #672)."),
    issue='#672',
    applies=_net_benefit_objective_active,
))


register(Approximation(
    id='net_benefit_sm_sleeve_cheaper_than_non_reg',
    summary=("#1034 closed the SM sleeve's UNTAXED terminal gain but left a residual "
             "pro-leverage bias: net_benefit prices the SM sleeve's deemed disposition "
             "via the estate path (split across two terminal returns, progressive "
             "terminal-YEAR indexed brackets) but the non-reg pot via its own flat "
             "marginal_rate on ONE household return against START-year brackets -- so an "
             "IDENTICAL dollar of accrued gain is worth roughly 5% more inside the SM "
             "sleeve than outside it (measured: a $700k/$500k pot scores ~$674k as the "
             "sleeve vs ~$664k as non-reg, a ~$10k gap on a $200k gain). The two pots use "
             "different bases, not one; the inconsistency is not conservatively biased -- "
             "it favours leverage"),
    biased_figure=('net_benefit ranking figure (the default objective) -- the residual '
                    'cross-pot basis inconsistency that still tilts it toward leverage'),
    direction=Direction.UNKNOWN,
    detail=("compute_net_benefit charges the non-reg pot gain x inclusion x "
             "marginal_rate(retirement_income + ..., brackets_2026) (optimize.py:410), but "
             "the SM sleeve via compute_estate(**_estate_call_args(...)).sm_investment_tax "
             "(optimize.py:455-457) -- two terminal returns running progressive brackets "
             "from $0 on terminal-YEAR indexed brackets, with the rollover election. The "
             "estate path is the SAME spelling max_after_tax_estate uses (DP#9 for the SM "
             "sleeve), but the non-reg pot kept net_benefit's pre-#1034 formula, so the two "
             "pots are priced on inconsistent bases. Fixing it (routing the non-reg pot "
             "through the estate path too) would change net_benefit for every household "
             "with non-reg, not just leveraged ones, and is out of #1034's scope; tracked "
             "as a follow-up. Read max_after_tax_estate for a single consistent estate "
             "valuation."),
    issue='#1034',
    applies=_net_benefit_objective_active,
))


def _dollar_basis_unlabeled(ctx: FidelityContext) -> bool:
    assumptions = ctx.cfg.get('assumptions', {}) or {}
    return 'dollar_basis' not in assumptions


register(Approximation(
    id='unlabeled_dollar_basis',
    summary=("The config does not declare assumptions.dollar_basis, so it is not "
             "possible to state from the input alone whether headline dollar "
             "figures are nominal or real (today's) dollars"),
    biased_figure='every dollar-denominated headline figure (ambiguous units, not a numeric bias)',
    direction=Direction.UNKNOWN,
    detail=('See #597: retirement_analysis.py / make_retirement_input.py recast the '
            'retirement scenario into real dollars (real return, 0% real salary '
            'growth, frozen brackets) but previously the only record of that fact '
            'was a Python comment. Set assumptions.dollar_basis to "nominal" or '
            '"real" (and assumptions.base_year when real) to silence this caveat.'),
    issue='#597',
    applies=_dollar_basis_unlabeled,
))


# ── Issue #685: a belief that contradicts a signed rate ───────────────────
#
# NOT an approximation the CODE makes — a contradiction the INPUT contains.
# It is surfaced through this mechanism anyway, and deliberately: the
# household reads a rate off a signed mortgage, types a different one into
# `assumptions.rate_paths`, and gets a confident 44-year projection. Before
# the fix the belief silently won; now the signed rate wins, and the fact
# that the two disagreed is stated on every surface that prints a number
# derived from it. "The right one happened to win" is only half a fix —
# the next person to write a conflicting rate_path deserves to be told.

def rate_path_conflicts(cfg: dict) -> List[Dict]:
    """Contradictions between a declared (signed) liability rate and the
    `assumptions.rate_paths` belief for the same borrowing at year zero.

    Recorded by `input_contract.to_internal_config` (which is the only place
    that can see both the contract's `liabilities[]` and its
    `assumptions.rate_paths` at once); read here so every output surface can
    name the same liability and the same two rates the load-time warning did.
    """
    # DP#32: explicit absence-testing, never truthiness. `x or {}` here would
    # be the very idiom this fix exists to punish -- and the guard in
    # tests/architecture/test_dp32_zero_fallback.py caught it when this
    # function was first written that way.
    cfg = {} if cfg is None else cfg
    assumptions = cfg.get('assumptions')
    if assumptions is None:
        return []
    conflicts = assumptions.get('rate_path_conflicts')
    return [] if conflicts is None else list(conflicts)


def _describe_rate_path_conflicts(ctx: FidelityContext) -> List[str]:
    return [
        (f"{c['liability_kind']} {c['liability_id']!r}: SIGNED rate "
         f"{c['declared_rate']:.2%} (liabilities[].rate) vs. BELIEF "
         f"{c['believed_rate']:.2%} (assumptions.rate_paths.{c['liability_kind']}) "
         f"-- the SIGNED {c['declared_rate']:.2%} is what this run charges")
        for c in rate_path_conflicts(ctx.cfg)
    ]


register(Approximation(
    id='rate_path_contradicts_signed_rate',
    summary=("A declared assumptions.rate_paths belief contradicts the SIGNED rate on "
             "the liability it describes; the signed rate WINS at year zero (a rate "
             "path describes what the borrowing costs AFTER the current term, and "
             "cannot reprice a rate the contract has already pinned)"),
    biased_figure=("the cost of debt on the named liability -- mortgage/HELOC interest, "
                   "the after-tax Smith-Manoeuvre spread, and every ranking that "
                   "depends on them"),
    direction=Direction.UNKNOWN,
    detail=("contract_liabilities.py, _reconcile_rate_paths(): the run uses the rate declared "
            "on the liability, so the figures are computed from the FACT, not the "
            "belief. Direction is UNKNOWN because it names an inconsistency in the "
            "input, not a bias in the code: the belief may sit either side of the "
            "signed rate. Nothing is silently substituted -- but the document "
            "contains two different answers to the same question, and only one of "
            "them can be right. Reconcile them: set the rate path to the signed rate "
            "(or delete it) unless you meant it as a post-renewal belief, which it "
            "cannot be while it disagrees with the contract at year zero."),
    issue='#685',
    applies=lambda ctx: bool(rate_path_conflicts(ctx.cfg)),
    findings=_describe_rate_path_conflicts,
))


# ── Issue #766: two unreconciled spending figures ────────────────────────────────
#
# A contract can declare TWO answers to "how much does this household spend"
# and nothing reconciles them: `household_budget.annual_living_costs` (the
# working-life figure, MEASURED) and `assumptions.retirement.spending_target`
# (the retirement figure, often a GUESS). They are consumed by different
# subsystems (solvency #679 vs. decumulation #707) and never compared, so a
# guessed retirement target silently outranks a measured living-cost figure
# and the decumulation shortfall it produces is an artifact of the guess. Same
# defect class as #685 (a belief silently outranking a fact) in a different
# costume. Recorded by contract_assumptions._reconcile_spending_figures() (the only
# place both figures are visible at once) and read here so every surface that
# prints a decumulation number also names the two spending figures that
# disagree.

def spending_figure_conflicts(cfg: dict) -> List[Dict]:
    """A material disagreement between `household_budget.annual_living_costs`
    (working-life, measured) and `assumptions.retirement.spending_target`
    (retirement, often guessed). Recorded by `input_contract.to_internal_config`;
    read here so every output surface names the same two figures the load-time
    warning did (#685's same bridge for input contradictions)."""
    cfg = {} if cfg is None else cfg
    assumptions = cfg.get('assumptions')
    if assumptions is None:
        return []
    conflicts = assumptions.get('spending_figure_conflicts')
    return [] if conflicts is None else list(conflicts)


def _describe_spending_figure_conflicts(ctx: 'FidelityContext') -> List[str]:
    out = []
    for c in spending_figure_conflicts(ctx.cfg):
        lc = c.get('living_costs_confidence')
        lc_conf = 'no provenance entry' if lc is None else lc
        st = c.get('spending_target_confidence')
        st_conf = 'no provenance entry' if st is None else st
        out.append(
            f"household_budget.annual_living_costs = {c['living_costs']:.0f} "
            f"(provenance: {lc_conf}) vs. assumptions.retirement.spending_target "
            f"= {c['spending_target']:.0f} (provenance: {st_conf}) -- a "
            f"{c['ratio']:.2f}x gap. The decumulation sizes to the retirement "
            f"target; if it is a guess outranking a measured living cost, the "
            f"shortfall it produces is an artifact of the guess."
        )
    return out


register(Approximation(
    id='spending_figures_unreconciled',
    summary=("The contract declares two unreconciled spending figures -- a "
             "MEASURED working-life `household_budget.annual_living_costs` and a "
             "retirement `assumptions.retirement.spending_target` that is often a "
             "guess -- and a guessed target silently outranks the measured figure "
             "in the decumulation"),
    biased_figure=("the decumulation drawdown amount, the shortfall year, and the "
                  "total net spending reported unfunded"),
    direction=Direction.UNKNOWN,
    detail=("contract_assumptions.py, _reconcile_spending_figures(): the two figures "
            "are consumed by different subsystems (solvency #679 vs. decumulation "
            "#707) and never compared. They are not the same quantity (working-life "
            "vs. retirement), so a small gap is legitimate; the defect is a MATERIAL "
            "gap (ratio outside [0.75, 1.25]) where a guessed retirement target "
            "outranks a measured living-cost figure. Direction is UNKNOWN because it "
            "names an input inconsistency, not a code bias: the gap may sit either "
            "side. Reconcile them -- derive the retirement target from the measured "
            "living cost, or confirm the gap is intended."),
    issue='#766',
    applies=lambda ctx: bool(spending_figure_conflicts(ctx.cfg)),
    findings=_describe_spending_figure_conflicts,
))


# ── Issue #707: a decumulation plan that runs out of money before the horizon ─
#
# NOT an approximation the CODE makes -- a RUNTIME RUIN the trajectory
# produced: the retirement drawdown could not deliver the net spending target
# because every drawable account was exhausted, and the run nonetheless
# completed and printed a confident terminal number. This is the repo's
# founding defect class (the engine silently substitutes zero), and it is
# surfaced through this mechanism deliberately, for the same reason #685's
# input contradiction is: every output surface that prints a number derived
# from the trajectory must also print the fact that the trajectory went
# bankrupt, or the number is a lie of omission. "The right terminal figure
# happened to print" is only half a fix -- the household needs to be told the
# money ran out in year N, $G short, not handed a number and left to infer it.
#
# Unlike the config-driven caveats above, this one's content depends on the
# TRAJECTORY, not the input alone: which year the shortfall first occurred and
# the size of the gap. The run's caller (optimize.main) records the
# worst-case (earliest-exhausting) summary onto assumptions.decumulation_shortfall
# after the run and before any surface renders, so the same finding reaches
# the console header, TXT, JSON, and HTML -- one spelling (DP#9), every
# surface (DP#32).

def _run_recorded_summary(cfg: dict, key: str, all_clear: Dict) -> Dict:
    """The ONE reader behind every run-recorded fidelity caveat (the #685/
    #707 bridge): the optimize caller writes a findings summary onto the
    cfg's ``assumptions`` block; the registry is built at import time and an
    Approximation is frozen, so a caveat can only READ what the run wrote.
    This helper is that read -- key and all-clear defaults supplied by each
    delegate (``decumulation_shortfall_summary`` / ``runway_summary`` /
    ``rrsp_refusal_summary``), which exist to give the shared mechanics a
    named, documented home per finding.

    Returns a FRESH copy of ``all_clear`` when nothing was recorded, so a
    caller mutating the returned dict cannot corrupt another reader or a
    later call (DP#3); a recorded summary is returned as recorded, never
    merged with or overwritten by the defaults."""
    assumptions = cfg.get('assumptions') if isinstance(cfg, dict) else None
    if assumptions is None:
        assumptions = {}
    recorded = assumptions.get(key)
    if recorded is None:
        return dict(all_clear)
    return recorded


def decumulation_shortfall_summary(cfg: dict) -> Dict:
    """The worst-case decumulation shortfall recorded on the run's config by
    the caller (``optimize.main``), or an all-False summary when no scenario
    exhausted its assets. Recorded as data, not recomputed here -- see
    ``_run_recorded_summary`` for the bridge; this lives at
    ``assumptions.decumulation_shortfall`` -- one spelling."""
    return _run_recorded_summary(cfg, 'decumulation_shortfall', {
        'engaged': False, 'exhausted': False,
        'first_shortfall_year': None, 'first_shortfall_gap': 0.0,
        'shortfall_years': 0, 'total_unmet': 0.0,
    })


def _describe_decumulation_shortfall(ctx: FidelityContext) -> List[str]:
    s = decumulation_shortfall_summary(ctx.cfg)
    if not s.get('exhausted'):
        return []
    year = s.get('first_shortfall_year')
    gap = s.get('first_shortfall_gap', 0.0)
    yrs = s.get('shortfall_years', 0)
    unmet = s.get('total_unmet', 0.0)
    return [
        (f"first shortfall in year {year}: the retirement drawdown could not "
         f"deliver ${gap:,.0f} of the net spending target -- every drawable "
         f"account was exhausted"),
        (f"{yrs} shortfall year(s) across the trajectory; ${unmet:,.0f} of net "
         f"spending went unfunded in total"),
    ]


def _has_decumulation_shortfall(ctx: FidelityContext) -> bool:
    return bool(decumulation_shortfall_summary(ctx.cfg).get('exhausted'))


register(Approximation(
    id='decumulation_shortfall',
    summary=("The retirement drawdown exhausted every drawable account before the "
             "projection horizon ended -- a year arrived where the net spending "
             "target could not be met, and the run nonetheless completed and "
             "printed a terminal figure. That figure is the ledger's truth "
             "(what the balances say), NOT an achievable retirement: the "
             "household ran out of money"),
    biased_figure=("terminal net worth / retirement income -- the headline figure "
                   "a bankrupt plan still prints with confidence, and any ranking "
                   "that scores it side-by-side with a non-bankrupt plan"),
    direction=Direction.UNKNOWN,
    detail=("YearResult.drawdown_shortfall (simulation_rules.apply_retirement_drawdown) "
            "records the per-year NET gap left unfunded when every drawable account "
            "is exhausted; decumulation.summarize_drawdown_shortfall folds a "
            "trajectory into {exhausted, first_shortfall_year, first_shortfall_gap, "
            "shortfall_years, total_unmet}; optimize.main records the worst case "
            "(earliest-exhausting scenario) onto assumptions.decumulation_shortfall so "
            "this caveat can name the year and the gap on every surface. Direction "
            "is UNKNOWN because this names a runtime ruin, not a code approximation: "
            "the terminal figure may be high (an illiquid house, an estate the "
            "bankrupt household will not live to spend) or zero, and the bias is "
            "that the figure is reported at all as if reachable. Distinct from "
            "solvency ruin (#679, the cash-flow identity against declared "
            "household_budget.annual_living_costs): a household whose drawdown ran "
            "out may also fail the cash-flow identity, and both are surfaced."),
    issue='#707',
    applies=_has_decumulation_shortfall,
    findings=_describe_decumulation_shortfall,
))


# ── Issue #758: runway caveats ────────────────────────────────────────────
#
# Runway (months-to-ruin after a dated income shock) reuses the #679 solvency
# verdict as-is, which by construction counts the year's booked contributions
# as a committed outflow and treats ALL of household_budget.annual_living_costs
# as rigid. Both UNDERSTATE runway (the household would in reality stop saving
# and compress discretionary spend under a shock). The safe direction -- a
# runway that errs short -- but a number that flatters is the defect class this
# repo exists to kill, so the bias is surfaced here, not hidden. Two further
# caveats name the liquidation waterfall's real-world fragility: an unsecured
# credit line a lender can cut, and an ordering that liquidates non-reg before
# TFSA (the engine's #679 order differs from the issue's suggested order).

def runway_summary(cfg: dict) -> Dict:
    """The worst-case (shortest-runway) scenario's verdict recorded on the run's
    config by optimize.main (runway.worst_runway_summary), or an all-False
    summary when no scenario engaged. Recorded as data, not recomputed here
    -- see ``_run_recorded_summary`` for the bridge; this lives at
    ``assumptions.runway`` -- one spelling."""
    return _run_recorded_summary(cfg, 'runway', {
        'engaged': False, 'runway_months': None,
        'relies_on_credit_facility': False, 'drew_registered': False,
        'scenario_label': None,
    })


def _has_engaged_runway(ctx: FidelityContext) -> bool:
    return bool(runway_summary(ctx.cfg).get('engaged'))


def _runway_discretionary_split_declared(ctx: FidelityContext) -> bool:
    """Issue #761: True when the contract declared
    household_budget.discretionary_fraction -- the split that lets the
    solvency/runway identity compress discretionary spending under a shock.
    Absent (None) means "no split" -- the whole scalar is treated as rigid.
    """
    hb = (ctx.cfg.get('household_budget') if isinstance(ctx.cfg, dict) else None) or {}
    return hb.get('discretionary_fraction') is not None


def _runway_relies_on_credit(ctx: FidelityContext) -> bool:
    return bool(runway_summary(ctx.cfg).get('relies_on_credit_facility'))


register(Approximation(
    id='runway_treats_all_spend_as_rigid',
    summary=("Runway treats every dollar of household_budget.annual_living_costs "
             "as non-discretionary -- the contract declares no discretionary/"
             "non-discretionary split, so a household that would compress dining "
             "out, vacations and entertainment under a shock is modelled as keeping "
             "them. This UNDERSTATES runway (the true runway is longer)"),
    biased_figure='runway (months to insolvency after an income shock)',
    direction=Direction.UNDERSTATES,
    detail=("The contract's household_budget.annual_living_costs is a single scalar "
            "(schema/input_schema.json $defs/household_budget); the household "
            "declared no discretionary_fraction, so there is no field marking part "
            "of it discretionary. runway.compute_runway reuses the #679 solvency "
            "identity, which reads the whole scalar as the committed outflow. "
            "Declare household_budget.discretionary_fraction (issue #761) to engage "
            "the discretionary-compression path under a shock; this caveat is "
            "suppressed once a split is declared, so the output always states which "
            "assumption it is making (DP#32)."),
    issue='#758',
    applies=lambda ctx: _has_engaged_runway(ctx) and not _runway_discretionary_split_declared(ctx),
))


# Issue #761: the counterpart caveat -- when a discretionary split IS
# declared, the identity compresses the discretionary portion to ZERO under a
# dated income shock. That is a LABELLED stress assumption (a household in
# genuine distress stops dining out and taking vacations; it does not stop
# buying groceries or paying insurance), not a silent default. Surfaced so a
# reader knows the runway figure assumes full discretionary compression, and
# can judge whether a partial cut is more realistic for this household.
register(Approximation(
    id='runway_compresses_discretionary_under_shock',
    summary=("Under a dated income shock the runway identity compresses the "
             "declared discretionary portion of household_budget.annual_living_costs "
             "to ZERO (restaurants, vacations, entertainment cut entirely) while the "
             "non-discretionary core (groceries, utilities, insurance, debt service) "
             "stays rigid. This OVERSTATES runway vs the all-rigid case -- a real "
             "household may keep some discretionary spend"),
    biased_figure='runway (months to insolvency after an income shock)',
    direction=Direction.OVERSTATES,
    detail=("simulation_rules.apply_solvency charges living_costs * (1 - "
            "discretionary_fraction) as the spending outflow in any working-life "
            "year where income_shock_active is True (a decisions.income[] override "
            "reduced income below baseline). The compression-to-zero rule is a "
            "documented stress assumption (issue #761), surfaced here rather than "
            "silently absorbed; a household that would keep some discretionary spend "
            "under a shock should read the runway as an upper bound on the "
            "compressible benefit. The non-discretionary portion, debt service, and "
            "committed installments (#759) are never compressed."),
    issue='#761',
    applies=lambda ctx: _has_engaged_runway(ctx) and _runway_discretionary_split_declared(ctx),
))


register(Approximation(
    id='runway_counts_contributions_as_committed',
    summary=("Runway counts the year's booked RRSP/TFSA/non-reg contributions as a "
             "committed outflow -- the engine keeps booking them (clamped to room, "
             "not affordability) and the #679 waterfall liquidates to cover. A "
             "household in real distress stops contributing; modelling it as "
             "committed UNDERSTATES runway (and adds the tax friction of "
             "contribute-then-liquidate)"),
    biased_figure='runway (months to insolvency after an income shock)',
    direction=Direction.UNDERSTATES,
    detail=("simulation_rules.apply_solvency's cash-flow identity includes "
            "contributions_total on the required side; runway.compute_runway reads "
            "the resulting solvency_shortfall as-is rather than re-simulating a "
            "contributions-off path (DP#9: one spelling of the identity). The "
            "double-handling costs only the liquidation tax on the contributed "
            "dollars, a small pessimism on top of the larger one above."),
    issue='#758',
    applies=_has_engaged_runway,
))


register(Approximation(
    id='runway_relies_on_uncollateralized_credit',
    summary=("A runway figure that drew a revolving credit facility assumes the "
             "lender does not reduce or cancel it -- a lender can cut an unsecured "
             "line exactly when the borrower most needs it. The runway is contingent "
             "on a credit line that may not be there"),
    biased_figure='runway (months to insolvency after an income shock)',
    direction=Direction.OVERSTATES,
    detail=("The #679 waterfall's second rung draws liabilities[kind=line_of_credit] "
            "(issue #689). The draw is real debt at the declared rate, but the "
            "modelling assumes the facility stays available at its declared limit "
            "through the shock -- no lender-behaviour model exists. Direction is "
            "OVERSTATES: a runway leaning on a cuttable line is shorter than reported."),
    issue='#758',
    applies=_runway_relies_on_credit,
    findings=lambda ctx: [
        (f"the worst-case scenario "
         f"{'(' + repr(s.get('scenario_label')) + ') ' if s.get('scenario_label') else ''}"
         f"drew its revolving credit facility to fund the gap")
        for s in [runway_summary(ctx.cfg)]
        if s.get('relies_on_credit_facility')
    ],
))


register(Approximation(
    id='runway_waterfall_order',
    summary=("The liquidation waterfall draws non-registered BEFORE TFSA; the "
             "conventional emergency order is TFSA first (tax-free, instantly "
             "liquid, room restored next January). Runway reflects the engine's "
             "actual order, not the conventional one"),
    biased_figure='runway (months to insolvency after an income shock) and the tax paid to fund the gap',
    direction=Direction.UNKNOWN,
    detail=("simulation_rules.apply_solvency builds the sources list as "
            "emergency_reserve -> revolving_credit -> non_reg -> tfsa -> registered. "
            "Reordering the shared #679 waterfall is out of scope for #758 (it would "
            "move the golden invariant and the ruin model is shared), so the runway "
            "number uses the engine's order and the difference is disclosed here. "
            "The counter-intuitive RRSP point -- a withdrawal in a job-loss year is "
            "taxed at an unusually LOW marginal rate, because that year's income is "
            "low -- the engine ALREADY gets right: ordinary_income_cost(ctx."
            "primary_marginal_rate) prices the registered draw at this year's rate, "
            "not a chosen one, and a job-loss year's marginal rate is low by "
            "construction. Direction UNKNOWN: liquidating non-reg first crystallises "
            "a capital gain (worse for tax) but preserves the TFSA's tax-free "
            "compounding (better for the long run) -- the net depends on the gain "
            "fraction and the horizon, which the run's own numbers already reflect."),
    issue='#758',
    applies=_has_engaged_runway,
))


def _runway_is_interpolated(ctx: FidelityContext) -> bool:
    s = runway_summary(ctx.cfg)
    return bool(s.get('engaged') and s.get('runway_months') is not None)


register(Approximation(
    id='runway_months_is_linear_interpolation',
    summary=("The runway headline is a LINEAR interpolation inside an honest "
             "bracket: the engine steps in years, and the months figure assumes "
             "uniform monthly burn within the ruin year. The bracket "
             "[start-of-ruin-year, end-of-ruin-year] is structural and always "
             "co-reported; the point estimate can be off by up to half a year"),
    biased_figure='runway (months to insolvency after an income shock) -- the point estimate',
    direction=Direction.UNKNOWN,
    detail=("runway.compute_runway interpolates as months_to_start_of_ruin_year + "
            "(covered/shortfall) * 12, where covered/shortfall is the #679 waterfall's "
            "own measurement of how far the liquid stock stretched in the ruin year. "
            "The uniform-burn assumption ignores that the shock year's income is "
            "day-count-blended by #674, so the true exhaustion month is within the "
            "bracket but not necessarily at the linear point. The bracket is always "
            "shown beside the point (DP#32: an estimate must be labelled as one). "
            "Direction UNKNOWN: uniform burn can land early or late depending on the "
            "blended-income shape within the ruin year."),
    issue='#758',
    applies=_runway_is_interpolated,
))


# ── Issue #170: refused RRSP contributions (a runtime fact, not a code
# approximation). When a household's declared RRSP contributions exceed the
# contributor's room, ``apply_contributions`` clips the excess -- and before
# #170 the clipped slice simply VANISHED: booked $0, no warning, no redirect,
# no disclosure. The per-year refusal amounts now ride every YearResult
# (rrsp_contribution_refused_own / _spousal), and the optimize caller records
# the trajectory summary onto assumptions.rrsp_contribution_refused (the same
# bridge #707's decumulation_shortfall uses), so this caveat can name the
# first refused year and the refused totals on every surface. Direction is
# UNKNOWN because the bias runs both ways: the modeled balances UNDERSTATE the
# household's real position (the budgeted money exists off-model) while any
# plan derived from the output OVERSTATES the savings the household believes
# it deployed -- and in reality the money would either sit unsheltered or
# incur the CRA's 1%/month excess-contribution tax (T1-OVP).

def rrsp_refusal_summary(cfg: dict) -> Dict:
    """The run-recorded RRSP-refusal summary (written by the optimize caller
    onto ``assumptions.rrsp_contribution_refused``), or an all-clear summary
    when no contribution was refused. Recorded as data, not recomputed here
    -- see ``_run_recorded_summary`` for the bridge (#685/#707)."""
    return _run_recorded_summary(cfg, 'rrsp_contribution_refused', {
        'engaged': False, 'first_refused_year': None,
        'refused_own_total': 0.0, 'refused_spousal_total': 0.0,
    })


def _has_rrsp_refusal(ctx: FidelityContext) -> bool:
    return bool(rrsp_refusal_summary(ctx.cfg).get('engaged'))


def _describe_rrsp_refusal(ctx: FidelityContext) -> List[str]:
    s = rrsp_refusal_summary(ctx.cfg)
    if not s.get('engaged'):
        return []
    year = s.get('first_refused_year')
    own = s.get('refused_own_total', 0.0)
    spousal = s.get('refused_spousal_total', 0.0)
    parts = []
    if year is not None:
        parts.append(f"first refused contribution in year {year}")
    if own > 0:
        parts.append(f"${own:,.0f} of declared OWN RRSP contributions were "
                     f"refused (above the contributor's room)")
    if spousal > 0:
        parts.append(f"${spousal:,.0f} of declared SPOUSAL RRSP contributions "
                     f"were refused (the contributor's pool was exhausted)")
    parts.append("the refused amounts were NOT redirected -- they entered no "
                 "account; a plan reading these contributions as made is wrong")
    return parts


register(Approximation(
    id='rrsp_contribution_refused',
    summary=("Declared RRSP contributions exceeded the contributor's room and "
             "the engine REFUSED the excess: the money booked $0 -- it entered "
             "no account, spilled nowhere, and stayed in no cash line. The "
             "output shows a plan whose contributions were not all made"),
    biased_figure=("any plan or ranking that assumes the declared RRSP "
                   "contributions were made; the household's real-world "
                   "contribution would either sit unsheltered or trigger the "
                   "CRA's 1%-per-month excess-contribution tax (T1-OVP)"),
    direction=Direction.UNKNOWN,
    detail=("apply_contributions clamps each declared contribution to the "
            "room remaining (own first, spousal from the remainder of the "
            "contributor's pool). Before #170 the clipped slice was dropped "
            "by the bare min() with no record. Now the per-year refusals are "
            "on YearResult.rrsp_contribution_refused_own/_spousal, and the "
            "run-wide totals are recorded onto assumptions."
            "rrsp_contribution_refused so this caveat fires identically in "
            "TXT/JSON/HTML. The optimizer rarely trips this (it ranks splits "
            "by net benefit and prefers fully-deployed ones); it bites when a "
            "household declares its contribution split manually."),
    issue='#170',
    applies=_has_rrsp_refusal,
    findings=_describe_rrsp_refusal,
))
