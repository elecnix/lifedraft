#!/usr/bin/env python3
"""Tests for issue #585: approximations that bias a headline result must
declare themselves into the output, not stay buried in a docstring (DP#32).

Covers:
  1. `Approximation` refuses to be constructed without a target figure and a
     direction (DP#32 made structural).
  2. A registered approximation reaches every output surface: TextReport,
     JsonReport, HtmlReport, and the console header (model_fidelity.render_text,
     which optimize.py's main() prints unconditionally).
  3. `active_approximations` is config-driven: a run that routes around the
     approximated path (e.g. drawdown_tax_mode='net') does not carry a caveat
     that doesn't apply to it, but a run that doesn't declare that choice does.
  4. Units disclosure (issue #597, output half): as_of/currency/dollar_basis
     are always present, never silently omitted.
  5. A completeness sweep: known caveat-vocabulary comments in the source
     tree are either covered by a registry entry or explicitly allowlisted as
     non-biasing — the mechanical half of "impossible to add a biasing
     approximation without it appearing in the output."

Run: uv run pytest tests/test_issue_585_model_fidelity.py -q
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model_fidelity
from model_fidelity import Approximation, Direction
from output_plugins import TextReport, JsonReport, HtmlReport


# ── 1. Structural enforcement on Approximation itself ──────────────────────

class TestApproximationConstructionIsGuarded(unittest.TestCase):
    """DP#32: a caveat with no target figure or no direction is not a caveat."""

    def test_missing_biased_figure_rejected(self):
        with self.assertRaises(ValueError):
            Approximation(id='x', summary='does something approximate',
                          biased_figure='', direction=Direction.OVERSTATES)

    def test_missing_summary_rejected(self):
        with self.assertRaises(ValueError):
            Approximation(id='x', summary='', biased_figure='some figure',
                          direction=Direction.OVERSTATES)

    def test_missing_id_rejected(self):
        with self.assertRaises(ValueError):
            Approximation(id='', summary='s', biased_figure='f', direction=Direction.UNKNOWN)

    def test_direction_must_be_enum_member(self):
        with self.assertRaises(ValueError):
            Approximation(id='x', summary='s', biased_figure='f', direction='overstates')  # not the enum

    def test_valid_construction_succeeds_and_direction_unknown_is_legitimate(self):
        # Direction.UNKNOWN is an honest, allowed value — distinct from omitting
        # direction entirely (which is rejected above).
        a = Approximation(id='ok', summary='s', biased_figure='f', direction=Direction.UNKNOWN)
        self.assertEqual(a.direction, Direction.UNKNOWN)


# ── 2. Known approximations reach every output surface ─────────────────────

# A config that activates a known approximation across every output surface.
# The example caveat exercised here is `net_benefit_withdrawal_tax_is_estimated`
# (#580): the forced-RRIF `rrif_forced_excess_tax_rate` caveat that used to ride
# this config was RETIRED by #825 (see TestNoStaleCaveats / the "gone" test),
# and it was the only purely retirement-data-gated caveat, so the plumbing test
# now rides an always-available objective caveat instead.
_CFG_TRIGGERS_DRAWDOWN = {
    'family': {'members': [
        {'role': 'primary', 'gross_income': 120000, 'birth_year': 1960, 'retirement_age': 60},
    ]},
    'property': {'house_value': 500000, 'mortgage_balance': 0, 'margin_available': 0},
    'accounts': {},
    'assumptions': {'investment_return': 0.05, 'start_year': 2026},
    'retirement': {},
}

# Ranked results carry the objective they were scored on; the reports read it
# so objective-specific caveats fire correctly.
_RESULTS = [
    {'label': 'A', 'net_benefit': 100000, 'future_value': 200000, 'total_debt': 0,
     'ltv': 0.0, 'objective_name': 'max_net_benefit'},
]

# The example caveat these output-surface tests assert on.
_EXAMPLE_CAVEAT = 'net_benefit_withdrawal_tax_is_estimated'


class TestActiveApproximationReachesEveryOutputSurface(unittest.TestCase):
    """A headline figure carrying a known approximation cannot be printed bare
    in any of the three report formats."""

    def test_drawdown_caveat_is_active_for_triggering_config(self):
        active_ids = {a.id for a in model_fidelity.active_approximations(_CFG_TRIGGERS_DRAWDOWN)}
        self.assertIn(_EXAMPLE_CAVEAT, active_ids)

    def test_text_report_surfaces_the_caveat(self):
        out = TextReport(_RESULTS, _CFG_TRIGGERS_DRAWDOWN, title="T").render()
        self.assertIn("MODEL FIDELITY", out)
        self.assertIn("net_benefit", out)
        self.assertIn("withdrawal tax", out)

    def test_json_report_surfaces_the_caveat(self):
        out = json.loads(JsonReport(_RESULTS, _CFG_TRIGGERS_DRAWDOWN, title="T").render())
        self.assertIn('model_fidelity', out)
        ids = {a['id'] for a in out['model_fidelity']['approximations']}
        self.assertIn(_EXAMPLE_CAVEAT, ids)
        entry = next(a for a in out['model_fidelity']['approximations']
                    if a['id'] == _EXAMPLE_CAVEAT)
        self.assertEqual(entry['direction'], 'unknown')
        self.assertIn('biased_figure', entry)

    def test_html_report_surfaces_the_caveat(self):
        out = HtmlReport(_RESULTS, _CFG_TRIGGERS_DRAWDOWN, title="T").render()
        self.assertIn("Model Fidelity", out)
        self.assertIn("net_benefit", out)

    def test_console_render_text_surfaces_the_caveat(self):
        # This is exactly what optimize.py's main() prints unconditionally.
        lines = model_fidelity.render_text(_CFG_TRIGGERS_DRAWDOWN)
        joined = "\n".join(lines)
        self.assertIn("net_benefit", joined)
        self.assertIn("withdrawal tax", joined)


# ── 3. A rule that is disabled says so by no longer appearing ──────────────

class TestApproximationIsConfigDriven(unittest.TestCase):
    """DP#32 extended: an approximation whose code path this run avoided must
    not keep claiming to bias output; one this run DOES hit must not be
    silently absent either."""

    def test_flat_rate_caveats_are_gone_after_the_progressive_rebracketing(self):
        """The retirement flat-rate tax approximations are now fully closed and
        DELETED, not merely inactive — printing either would be a false
        disclosure. #363 (#614 deleted the over-drawing gross path, PR 1 added
        progressive re-bracketing, PR 2 folded in the OAS recovery tax, PR 4
        split the draw across the spouses) closed `drawdown_flat_marginal_rate`;
        #825 routed the forced RRIF minimum through the same progressive +
        clawback machinery, closing `rrif_forced_excess_tax_rate`."""
        active_ids = {a.id for a in model_fidelity.active_approximations(_CFG_TRIGGERS_DRAWDOWN)}
        self.assertNotIn('drawdown_flat_marginal_rate', active_ids)
        self.assertNotIn('rrif_forced_excess_tax_rate', active_ids)

    def test_no_retirement_data_means_no_retirement_caveats(self):
        cfg = {'family': {'members': [{'role': 'primary', 'gross_income': 80000}]},
               'assumptions': {'start_year': 2026}}
        active_ids = {a.id for a in model_fidelity.active_approximations(cfg)}
        self.assertNotIn('drawdown_flat_marginal_rate', active_ids)
        self.assertNotIn('rrif_forced_excess_tax_rate', active_ids)

    def test_undeclared_estate_elections_are_disclosed(self):
        """epic #603 Track C Phase 2c (#600): the five estate elections are now
        INPUTS. A config that declares them gets real numbers and NO caveat; a
        config that declares none of them is silently running on
        objective._UNDECLARED_ESTATE_DEFAULTS, and THAT must be disclosed
        (DP#32: absence is reported, never passed off as a decision).

        This is the replacement for the old, now-false
        `after_tax_estate_defaulted_assumptions` (which claimed the schema
        "cannot yet express" these — it can)."""
        no_estate = {'assumptions': {'start_year': 2026}}
        ids = {a.id for a in model_fidelity.active_approximations(
            no_estate, objective_name='max_after_tax_estate')}
        self.assertIn('estate_elections_not_declared', ids)

        # Declared elections → the figure is no longer a guess → no caveat.
        declared = {'assumptions': {'start_year': 2026},
                    'estate': {'spousal_rollover': False,
                               'tfsa_successor_holder': False,
                               'non_reg_primary_share': 0.7}}
        ids_declared = {a.id for a in model_fidelity.active_approximations(
            declared, objective_name='max_after_tax_estate')}
        self.assertNotIn('estate_elections_not_declared', ids_declared)

        # ...and it only ever fires for the objective whose figure it shapes.
        other = {a.id for a in model_fidelity.active_approximations(
            no_estate, objective_name='max_net_benefit')}
        self.assertNotIn('estate_elections_not_declared', other)

    def test_point_in_time_estate_gap_is_declared_even_when_fully_configured(self):
        """The residual gap Phase 2c SURFACED rather than closed: the estate is
        valued at one date, so the first-death→second-death interval (and the
        post-death TFSA growth a beneficiary designation would expose) is not
        modelled. This must be disclosed even for a fully-declared estate —
        otherwise declaring the elections would buy false confidence."""
        declared = {'assumptions': {'start_year': 2026},
                    'estate': {'spousal_rollover': True,
                               'tfsa_successor_holder': True,
                               'non_reg_primary_share': 0.5}}
        ids = {a.id for a in model_fidelity.active_approximations(
            declared, objective_name='max_after_tax_estate')}
        self.assertIn('estate_is_a_point_in_time_valuation', ids)

    def test_the_false_estate_caveat_is_gone(self):
        """DP#9 + TestNoStaleCaveats' own thesis: the old caveat asserted the
        input schema "cannot yet express" the estate elections. Phase 2c made
        that sentence false, so the caveat is DELETED, not reworded."""
        ids = {a.id for a in model_fidelity.all_approximations()}
        self.assertNotIn('after_tax_estate_defaulted_assumptions', ids)

    def test_pretax_wealth_caveat_fires_only_for_pretax_objectives(self):
        """#580, narrowed: max_terminal_wealth genuinely sums balances pre-tax
        ('Does NOT deduct future tax on RRSP withdrawals'), so it carries the
        caveat. An after-tax estate objective must NOT — otherwise the caveat
        is a false disclosure the moment PR #616 lands."""
        cfg = {'assumptions': {'start_year': 2026}}
        pretax = {a.id for a in model_fidelity.active_approximations(
            cfg, objective_name='max_terminal_wealth')}
        self.assertIn('terminal_wealth_is_pretax', pretax)

        after_tax = {a.id for a in model_fidelity.active_approximations(
            cfg, objective_name='max_after_tax_estate')}
        self.assertNotIn('terminal_wealth_is_pretax', after_tax)

    def test_net_benefit_carries_its_own_estimated_tax_caveat_not_the_pretax_one(self):
        """max_net_benefit is NOT a raw pre-tax sum — it deducts an estimated
        withdrawal tax. It gets its own, accurate caveat rather than the
        terminal-wealth one."""
        cfg = {'assumptions': {'start_year': 2026}}
        ids = {a.id for a in model_fidelity.active_approximations(
            cfg, objective_name='max_net_benefit')}
        self.assertIn('net_benefit_withdrawal_tax_is_estimated', ids)
        self.assertNotIn('terminal_wealth_is_pretax', ids)

    def test_unknown_objective_reports_rather_than_hides(self):
        """DP#32: an objective the caller didn't name is 'unknown', which must
        fail loud (report the caveat), never fail silent (suppress it)."""
        cfg = {'assumptions': {'start_year': 2026}}
        ids = {a.id for a in model_fidelity.active_approximations(cfg, objective_name=None)}
        self.assertIn('terminal_wealth_is_pretax', ids)
        self.assertIn('net_benefit_withdrawal_tax_is_estimated', ids)
        self.assertIn('net_benefit_omits_estate_elections', ids)

    def test_net_benefit_estate_blindness_caveat_names_the_objective_that_prices_it(self):
        """Issue #672: this caveat must be sharper than 'estimated' -- it must
        say the estate levers net_benefit does NOT price remain inert (the
        residual blindness after #1034 closed the SM sleeve's deemed
        disposition), and it must NAME max_after_tax_estate as the objective
        that does price the full estate (so a reader isn't just told 'this is
        approximate' with nowhere to go)."""
        cfg = {'assumptions': {'start_year': 2026}}
        active = {a.id: a for a in model_fidelity.active_approximations(
            cfg, objective_name='max_net_benefit')}
        self.assertIn('net_benefit_omits_estate_elections', active)
        approx = active['net_benefit_omits_estate_elections']
        self.assertIn('max_after_tax_estate', approx.detail)
        self.assertIn('inert', approx.summary.lower())

        # And it must NOT fire for the objective that actually prices the
        # estate -- a caveat that fires everywhere is not a caveat.
        after_tax = {a.id for a in model_fidelity.active_approximations(
            cfg, objective_name='max_after_tax_estate')}
        self.assertNotIn('net_benefit_omits_estate_elections', after_tax)

    def test_declaring_dollar_basis_silences_the_unlabeled_units_caveat(self):
        cfg_undeclared = {'assumptions': {'start_year': 2026}}
        cfg_declared = {'assumptions': {'start_year': 2026, 'dollar_basis': 'nominal'}}
        undeclared_ids = {a.id for a in model_fidelity.active_approximations(cfg_undeclared)}
        declared_ids = {a.id for a in model_fidelity.active_approximations(cfg_declared)}
        self.assertIn('unlabeled_dollar_basis', undeclared_ids)
        self.assertNotIn('unlabeled_dollar_basis', declared_ids)


# ── 4. Units disclosure (issue #597, output half) ──────────────────────────

class TestUnitsDisclosure(unittest.TestCase):
    def test_units_always_present_even_when_unspecified(self):
        u = model_fidelity.describe_units({})
        self.assertEqual(u['as_of'], 'unspecified')
        self.assertEqual(u['currency'], 'CAD')  # DP#13: documented default, not an opinion overriding input
        self.assertEqual(u['dollar_basis'], 'nominal')

    def test_explicit_zero_start_year_not_silently_replaced_by_tax_year(self):
        """DP#32 (#606): an explicit assumptions.start_year=0 is erroneous
        data (never a legitimate calendar year), not absence. It must not
        silently read as tax.year -- reporting a confident but WRONG as_of
        year is worse than reporting 'unspecified'."""
        cfg = {'assumptions': {'start_year': 0}, 'tax': {'year': 2027}}
        u = model_fidelity.describe_units(cfg)
        self.assertNotEqual(u['as_of'], '2027')
        self.assertEqual(u['as_of'], 'unspecified')

    def test_real_dollars_reports_base_year(self):
        cfg = {'assumptions': {'start_year': 2026, 'dollar_basis': 'real', 'base_year': 2026}}
        u = model_fidelity.describe_units(cfg)
        self.assertEqual(u['dollar_basis'], 'real')
        self.assertEqual(u['base_year'], '2026')

    def test_units_line_distinguishes_nominal_and_real(self):
        nominal = model_fidelity.units_line({'assumptions': {'start_year': 2026}})
        real = model_fidelity.units_line(
            {'assumptions': {'start_year': 2026, 'dollar_basis': 'real', 'base_year': 2026}})
        self.assertIn('nominal', nominal)
        self.assertIn('real', real)
        self.assertNotEqual(nominal, real)

    def test_make_retirement_input_declares_real_dollar_basis(self):
        """Issue #597: the real-dollar recast used to be recorded ONLY in a
        Python comment in make_retirement_input.py. It must now be in the
        generated config itself."""
        import make_retirement_input as mri
        base = {
            'assumptions': {'start_year': 2026, 'projection_years': 10},
            'family': {'members': [{'role': 'primary', 'birth_year': 1979,
                                     'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0}]},
            'property': {'house_value': 500000, 'mortgage_balance': 0},
            'heloc': {},
            'accounts': {},
        }
        out = mri.build(base)
        self.assertEqual(out['assumptions']['dollar_basis'], 'real')
        self.assertEqual(out['assumptions']['base_year'], 2026)
        # And that declaration is exactly what silences the unlabeled-basis caveat.
        active_ids = {a.id for a in model_fidelity.active_approximations(out)}
        self.assertNotIn('unlabeled_dollar_basis', active_ids)


# ── 5. Completeness sweep: caveat vocabulary must be covered or allowlisted ─

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Regex tuned to the caveat vocabulary this codebase actually uses for
# approximations that bias a *headline* figure (issue #585's inventory).
# Deliberately narrower than a bare "approximat|simplif" grep (which also
# matches unrelated prose like "compose through data" or test names) — it
# looks for the words directly describing a numeric approximation.
_CAVEAT_PATTERN = re.compile(
    r'\bconservative\s*\(never|is an \*?approximation\*?|documented approximation'
    r'|conservative approximation|is APPROXIMAT',
    re.IGNORECASE,
)

# Known sites where the caveat vocabulary appears in a code comment/docstring
# and IS a real headline-biasing approximation. Every one of these has a
# model_fidelity.Approximation registered above (or is a duplicate mention of
# the same underlying approximation, listed here so the sweep doesn't demand
# one registry entry per comment occurrence).
_COVERED_BY_REGISTRY = {
    'countries/canada/retirement_transition.py',  # gross_up_for_tax — #363, registered
    'simulation.py',                               # monthly-compounding 'is approximated' prose (not a registered headline-biasing approx; covered by the blanket since the forced_excess vocabulary used to live here too)
    # epic #795 bite 1: the rrif_forced_excess_tax_rate 'documented
    # approximation' vocabulary moved with the code from simulation.py's
    # _retirement_transition_for into the registered retirement_income
    # rule; still registered (id='rrif_forced_excess_tax_rate').
    'simulation_rules.py',
    # Issue #758: runway.py documents the labelled-interpolation approximation
    # inline (the point estimate IS a headline-biasing estimate, registered as
    # runway_months_is_linear_interpolation); the vocabulary is covered.
    'runway.py',
}

# Known sites where the vocabulary appears but the approximation does NOT
# bias a headline output figure users read (diagnostic-only comparison
# helpers, internal eligibility checks, etc.) — reviewed and intentionally
# left out of the registry, with a one-line reason each. Adding a NEW file
# here requires a human to say why it's not headline-biasing; a bare grep
# hit that lands in neither set fails this test.
_KNOWN_NON_BIASING = {
    'countries/canada/fhsa.py': 'eligibility gate errs toward denying eligibility, not toward a wrong dollar figure',
    'countries/canada/provinces/quebec/quebec_lif.py': 'historical QMFR schedule constant, not a per-run headline figure',
    'countries/canada/cpp_sharing.py': ("credit_split_pension_impact() already self-discloses via a 'note' key "
                                       "in its own return dict at the call site — a different, already-DP#32-"
                                       "compliant disclosure mechanism, not routed through this registry"),
}

# The mechanism's own file necessarily documents the caveat vocabulary (it's
# explaining what the vocabulary means) — that is not an undisclosed
# approximation, it's the disclosure system itself.
_SELF_FILE = 'model_fidelity.py'


class TestCaveatVocabularyIsCoveredOrAllowlisted(unittest.TestCase):
    """Mechanical half of DP#32 for this issue: grep the source tree for the
    caveat vocabulary and require every hit to be either registered in
    model_fidelity.py or explicitly allowlisted as non-biasing. A new
    "conservative (never under-draws)"-style comment with neither entry
    fails CI — that is what makes a silent approximation hard to add."""

    def test_every_caveat_comment_is_registered_or_allowlisted(self):
        offenders = []
        for root, dirs, files in os.walk(_REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in
                      ('.venv', '.git', 'node_modules', '__pycache__', 'tests')
                      and not d.endswith('.egg-info')
                      and d not in ('build', 'dist')]
            for fname in files:
                if not fname.endswith('.py'):
                    continue
                path = os.path.join(root, fname)
                rel = os.path.relpath(path, _REPO_ROOT)
                try:
                    with open(path, encoding='utf-8') as f:
                        text = f.read()
                except (OSError, UnicodeDecodeError):
                    continue
                if rel == _SELF_FILE:
                    continue
                if _CAVEAT_PATTERN.search(text):
                    if rel not in _COVERED_BY_REGISTRY and rel not in _KNOWN_NON_BIASING:
                        offenders.append(rel)
        self.assertEqual(
            offenders, [],
            f"Files using approximation/caveat vocabulary with no model_fidelity "
            f"registry entry and no allowlist reason: {offenders}. Register the "
            f"approximation in model_fidelity.py (preferred) or add it to "
            f"_KNOWN_NON_BIASING in this test with a reason.",
        )


# ── 6. Staleness: a caveat for an approximation that no longer exists ───────
#
# The other half of the question the registry must be able to answer
# mechanically. #5 catches "an approximation exists but isn't registered".
# This catches the inverse — "an approximation is registered but no longer
# exists" — which is the failure that just bit us: #613 fixed the SM/non-reg
# return asymmetry, and the caveat for it would have gone on printing forever.
#
# Each entry is pinned to a CODE ANCHOR: a (file, snippet) pair that must
# still be present for the approximation to be real. Fix the approximation
# (delete or change the anchored code) and this test goes red until the
# registry entry is removed too.
_CODE_ANCHORS = {
    'terminal_wealth_is_pretax': (
        'objective.py', 'def _terminal_wealth('),
    # Issue #170: the refused-contribution caveat is anchored to the clamp
    # site that records the refusal -- if the clip (or its disclosure) is
    # ever removed, this anchor goes stale and the caveat must go with it.
    'rrsp_contribution_refused': (
        'rules_contributions.py', 'ws.rrsp_refused_own'),
    'net_benefit_withdrawal_tax_is_estimated': (
        'optimize.py', 'def compute_net_benefit('),
    # Issue #672: sibling caveat, same anchor -- both describe
    # compute_net_benefit(), which never models a death event.
    'net_benefit_omits_estate_elections': (
        'optimize.py', 'def compute_net_benefit('),
    # Issue #1034: the residual cross-pot basis inconsistency -- the SM sleeve
    # is priced via compute_estate (estate path) while the non-reg pot keeps
    # compute_net_benefit's own marginal_rate formula. Anchored to the SM
    # sleeve's estate-path call site.
    'net_benefit_sm_sleeve_cheaper_than_non_reg': (
        'optimize.py', 'compute_estate(**_sm_estate_args).sm_investment_tax'),
    # epic #603 Track C Phase 2c (#600): after_tax_estate_defaulted_assumptions
    # is GONE (the schema CAN now express all five elections -- the caveat's own
    # claim became false). Two narrower, still-true caveats replace it.
    'estate_elections_not_declared': (
        'objective.py', '_UNDECLARED_ESTATE_DEFAULTS'),
    'estate_is_a_point_in_time_valuation': (
        'countries/canada/estate.py', 'def compute_estate('),
    # Issue #685: unlike its neighbours, this caveat does not describe a
    # simplification the CODE makes -- it reports a contradiction the INPUT
    # contains (a rate_paths belief disagreeing with a signed liability rate).
    # It is anchored all the same: delete the reconciliation and the caveat
    # can never fire, which is exactly the staleness this test exists to catch.
    'rate_path_contradicts_signed_rate': (
        'contract_liabilities.py', 'def _reconcile_rate_paths('),
    # Issue #707: a runtime ruin, not a code approximation -- but it is
    # produced by real code (the per-year shortfall recorded in
    # apply_retirement_drawdown), and this anchor proves that code still
    # exists. If the shortfall recording is removed, this caveat is stale.
    'decumulation_shortfall': (
        'rules_drawdown.py', 'ws.drawdown_shortfall = _gap'),
    # Issue #766: an input contradiction (two unreconciled spending figures),
    # same class as #685. Anchored to the reconciliation that produces the
    # records; delete it and the caveat can never fire.
    'spending_figures_unreconciled': (
        'contract_assumptions.py', 'def _reconcile_spending_figures('),
    # Issue #758: runway caveats. The two structural biases (all-spend-rigid,
    # contributions-counted) and the credit-line/waterfall-order findings are

    # produced by runway.compute_runway reading the #679 solvency verdict; the
    # interpolation caveat is the linear point estimate itself. All four are
    # anchored to the fold that produces them.
    'runway_treats_all_spend_as_rigid': (
        'runway.py', 'def compute_runway('),
    'runway_compresses_discretionary_under_shock': (
        'rules_solvency.py', 'frac = ctx.config.discretionary_fraction'),
    'runway_counts_contributions_as_committed': (
        'runway.py', 'def compute_runway('),
    'runway_relies_on_uncollateralized_credit': (
        'runway.py', 'def worst_runway_summary('),
    'runway_waterfall_order': (
        'runway.py', 'def compute_runway('),
    'runway_months_is_linear_interpolation': (
        'runway.py', 'frac = max(0.0, min(1.0, covered_r / shortfall_r))'),
    # unlabeled_dollar_basis is a config-shape gap, not a code path — it has
    # no anchor and is exempt below.
    # Issue #141: the declarable-window disclosure is anchored to the rule
    # that books the denial -- delete the rule and the caveat can never fire.
    'superficial_loss_window_is_declarable': (
        'rules_superficial_loss.py', 'ws.superficial_loss_denied'),
    # Issue #143: the step-scale friction disclosure is anchored to the annual
    # deployment seam -- delete the seam and no friction is ever charged, so
    # the caveat would be a false disclosure.
    'trading_friction_annual_step': (
        'simulation.py', "Issue #143: turning the year's savings over is not free"),
}

_NO_CODE_ANCHOR = {'unlabeled_dollar_basis'}


class TestSpendingFiguresReconciliation(unittest.TestCase):
    """Issue #766: a guessed retirement.spending_target that materially
    outranks a measured annual_living_costs must be surfaced on every output
    surface, not silently used to size the decumulation."""

    @staticmethod
    def _cfg(living_costs, spending_target, lc_conf=None, st_conf=None):
        record = {
            'living_costs': living_costs,
            'spending_target': spending_target,
            'ratio': (spending_target / living_costs) if living_costs else 0,
            'winner': 'spending_target',
            'living_costs_confidence': lc_conf,
            'spending_target_confidence': st_conf,
        }
        return {'assumptions': {'spending_figure_conflicts': [record]}}

    def test_no_conflict_when_figures_agree(self):
        self.assertEqual(model_fidelity.spending_figure_conflicts(
            {'assumptions': {}}), [])
        self.assertEqual(model_fidelity.spending_figure_conflicts(
            {'assumptions': {'spending_figure_conflicts': None}}), [])
        self.assertEqual(model_fidelity.spending_figure_conflicts(None), [])

    def test_conflict_fires_and_names_both_values_provenance_and_ratio(self):
        cfg = self._cfg(80000, 125000, lc_conf='derived', st_conf='assumed')
        active = [a for a in model_fidelity.all_approximations()
                  if a.id == 'spending_figures_unreconciled']
        self.assertTrue(active, 'spending_figures_unreconciled must be registered')
        ctx = model_fidelity.FidelityContext(cfg=cfg)
        self.assertTrue(active[0].is_active(ctx))
        findings = active[0].findings_for(ctx)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertIn('80000', f)                  # living costs value named
        self.assertIn('125000', f)                    # spending target value named
        self.assertIn('1.56', f)                      # ratio named (125000/80000)
        self.assertIn('derived', f)                   # living-costs provenance named
        self.assertIn('assumed', f)                   # spending-target provenance named

    def test_conflict_text_report_surfaces_the_finding(self):
        cfg = self._cfg(80000, 125000, lc_conf='derived', st_conf='assumed')
        lines = model_fidelity.render_text(cfg)
        joined = '\n'.join(lines)
        self.assertIn('#766', joined)
        self.assertIn('annual_living_costs', joined)
        self.assertIn('spending_target', joined)
        self.assertIn('1.56x', joined)

    def test_no_conflict_for_benign_small_gap(self):
        # A retirement target 10% above working-life spend is legitimate; the
        # reconciliation band is +/-25%, so this must NOT be recorded.
        from contract_assumptions import _reconcile_spending_figures
        self.assertEqual(
            _reconcile_spending_figures({}, 80000, 88000), [])
        # 80k vs 100k = 1.25x -- at the band edge, not outside it.
        self.assertEqual(
            _reconcile_spending_figures({}, 80000, 100000), [])

    def test_conflict_recorded_for_material_gap(self):
        from contract_assumptions import _reconcile_spending_figures
        rec = _reconcile_spending_figures({}, 80000, 125000)
        self.assertEqual(len(rec), 1)
        self.assertAlmostEqual(rec[0]['ratio'], 125000 / 80000)
        self.assertEqual(rec[0]['winner'], 'spending_target')

    def test_no_record_when_either_figure_absent(self):
        from contract_assumptions import _reconcile_spending_figures
        self.assertEqual(_reconcile_spending_figures({}, None, 125000), [])
        self.assertEqual(_reconcile_spending_figures({}, 80000, None), [])

    def test_provenance_confidences_resolved_from_sidecar(self):
        from contract_assumptions import _reconcile_spending_figures
        doc = {'provenance': {
            '/household_budget/annual_living_costs': {'confidence': 'derived'},
            '/assumptions/retirement/spending_target': {'confidence': 'assumed'},
        }}
        rec = _reconcile_spending_figures(doc, 80000, 125000)
        self.assertEqual(rec[0]['living_costs_confidence'], 'derived')
        self.assertEqual(rec[0]['spending_target_confidence'], 'assumed')


class TestNoStaleCaveats(unittest.TestCase):
    """A caveat for an approximation that has since been FIXED is a false
    disclosure — as corrosive as a missing one, and harder to notice, because
    it trains the reader to skim the fidelity section. Every registered
    approximation must still be anchored to code that exists."""

    def test_every_registered_approximation_is_anchored_to_live_code(self):
        stale = []
        for approx in model_fidelity.all_approximations():
            if approx.id in _NO_CODE_ANCHOR:
                continue
            anchor = _CODE_ANCHORS.get(approx.id)
            self.assertIsNotNone(
                anchor,
                f"Approximation {approx.id!r} has no code anchor in _CODE_ANCHORS. "
                f"Add one (file, snippet) so this test can prove the approximation "
                f"still exists, or add it to _NO_CODE_ANCHOR if it isn't a code path.",
            )
            rel, snippet = anchor
            path = os.path.join(_REPO_ROOT, rel)
            with open(path, encoding='utf-8') as f:
                text = f.read()
            if snippet not in text:
                stale.append(f"{approx.id} (anchor {rel!r} no longer contains {snippet!r})")
        self.assertEqual(
            stale, [],
            f"Registered approximations whose underlying code is GONE — the "
            f"approximation was fixed but the caveat is still being printed: {stale}. "
            f"Delete the entry from model_fidelity.py.",
        )

    def test_caveats_for_approximations_that_were_fixed_are_gone(self):
        """Caveats this project registered and later made false by a merged fix
        must be GONE, not merely inactive:

        - #576 / PR #613: SM investments now compound at the same after-tax
          rate as non-reg (one model of taxable investing).
        - #363 / PR #614: the over-drawing gross path (gross_up_for_tax and the
          drawdown_tax_mode switch) was deleted; the net path is the only one.
        - #618 / #363 PR 3: the bracket-fill headroom now fills against an
          OAS-inclusive taxable base, so it no longer excludes OAS.
        - #363 PR 4: the drawdown (and its OAS clawback) is now split across the
          two spouses' separate bracket sets, so the last clause of
          `drawdown_flat_marginal_rate` is closed and the whole entry is gone.
        - #825: the forced RRIF minimum's tax is now priced through the same
          progressive re-bracketing + per-spouse OAS-clawback machinery as the
          discretionary draw, so the flat-placeholder `rrif_forced_excess_tax_rate`
          caveat is gone.
        """
        ids = {a.id for a in model_fidelity.all_approximations()}
        self.assertNotIn('sm_vs_nonreg_return_asymmetry', ids)
        self.assertNotIn('retirement_drawdown_gross_up', ids)
        self.assertNotIn('drawdown_bracket_fill_target_excludes_oas', ids)
        self.assertNotIn('drawdown_flat_marginal_rate', ids)
        self.assertNotIn('rrif_forced_excess_tax_rate', ids)


if __name__ == '__main__':
    unittest.main()
