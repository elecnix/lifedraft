"""DP#32 enforcement: "Zero is a value, not a fallback; absence must fail loudly."

Mechanically enforces the ``or``-fallthrough half of DP#32 (DESIGN_PRINCIPLES.md
#32): ``x = cfg.get(k) or DEFAULT`` and ``x = getattr(obj, name) or DEFAULT``
conflate "key absent" with "key present and falsy" (``0``, ``''``, ``[]``,
``{}``, ``False``) whenever the input can legitimately hold that falsy value.
Issue #606 catalogued 7 live sites and a "verified NOT a bug" list found by a
manual sweep; this test makes that sweep automatic and permanent.

## What the scan proves, and what it doesn't

``repo_scan.find_get_or_default()`` is a **syntactic** pattern match: any
``or``-chain whose first operand is a ``.get(...)`` or ``getattr(...)`` call.
It cannot know whether ``0``/``''``/``[]``/``{}`` is a value the field can
legitimately hold -- that requires domain knowledge (a dollar balance can
legitimately be ``$0``; a claim age of ``0`` cannot). So the scan is
deliberately over-inclusive, exactly as #586 asked for: it flags real bugs
*and* the harmless "both sides are the same empty value" idiom side by side,
and every single finding -- both kinds -- must be triaged into one of the two
buckets below by file+snippet, not silently dropped.

Per-finding classification lives in ``_CONFIRMED_VIOLATIONS`` (still broken;
each entry cites the tracking issue) and ``_REVIEWED_HARMLESS`` (a human
checked: the fallback value is the type's own zero/empty identity, applied to
data that is not user-supplied financial input -- e.g. re-reading a value
already computed by the simulation for display, or CLI-tooling metadata).
Marking something "harmless" here does NOT weaken the check: if the flagged
expression changes at all, its snippet no longer matches and the allowlist
entry goes stale (see ``test_dp32_allowlist_has_no_stale_entries``), which
forces a human to look at it again.

The allowlist keys on ``(file, exact unparsed source of the `or` node)``, not
line number, so it survives unrelated line-shift edits elsewhere in the file
-- but note: two textually-identical findings in the same file (e.g. the same
``.get('year_by_year') or []`` idiom repeated at three call sites in
``output_plugins.py``) collapse to one allowlist key. That's an accepted
limitation (see ``repo_scan.diff_against_allowlist`` docstring): it weakens
detection of a *partial* fix among duplicates, not detection of a wholly new
violation shape.
"""
from __future__ import annotations

import repo_scan


# ── #606's 7 catalogued live sites, plus 3 more found by this detector's ────
# broader sweep (filed as #621) and #606's own "cosmetic" 7th site. All still
# broken -- DP#586 doesn't fix them, it detects them (see #586's brief:
# "Don't fix the underlying violations — other agents own those. Detect.").
_CONFIRMED_VIOLATIONS = {
    # Every site originally catalogued here (#606/#621/#622) is now FIXED:
    #
    # - module_registry.py's fhsa_room_accumulated/fhsa_room fallthrough
    #   (#606): the 'fhsa_room' member-level alias is deleted (DP#9). See
    #   tests/test_module_registry.py.
    # - scenario_discovery.py's candidate_ages fallthrough (#606): explicit
    #   None-check, an empty list is honored. See
    #   tests/test_issue_303_retirement_age_scenarios.py.
    # - simulate.py's retirement_age anchor fallthrough (#606): same
    #   explicit None-check. See tests/test_simulate.py.
    # - countries/canada/retirement_transition.py's cpp_start_age/
    #   oas_start_age coercion (#606): explicit None-check. See
    #   tests/test_retirement_transition.py.
    # - output_plugins.py's label/strategy fallthrough (#606): explicit
    #   None-check. See tests/test_output_plugins.py.
    # - model_fidelity.py's describe_units() start_year fallthrough (#622):
    #   explicit None-check. See tests/test_issue_585_model_fidelity.py.
    # - countries/canada/cashout_optimizer.py's readvanceable/heloc_readvance
    #   fallthrough (#621): the undocumented 'property.readvanceable' alias
    #   is deleted (DP#9). See
    #   countries/canada/tests/test_cashout_optimizer.py.
    # - simulation_state.py's _opening() RRSP/TFSA-vs-portfolio fallthrough
    #   (#606, both primary and spouse): explicit `key in primary`/
    #   `key in spouse` presence tests replace the truthiness test.
    # - simulation_state.py's lira_data/member-embedded 'lira' alias (#606)
    #   and fhsa_room_accumulated/fhsa_room alias (#606): both aliases
    #   deleted (DP#9), not hardened.
    # - simulation_state.py's lira.birth_year fallback to the primary's
    #   birth year (#621): explicit `is not None` test.
    # - simulation.py's rrif_conversion_age fallback (#621): explicit
    #   `is None` test.
    #
    # See issue #627's PR for the simulation_state.py/simulation.py fixes
    # and issue #632's PR for the rest. Nothing is currently confirmed-
    # broken; new findings go here as they're triaged.
}

# ── Reviewed and confirmed harmless. Two shapes qualify: (a) the final
# fallback is a literal equal to the field's own zero/empty identity (0, '',
# [], {}, False) AND the expression reads simulation *output* (already-
# computed report data), not user-supplied input -- so "absent" and
# "explicit zero" are the same displayable answer and nothing is lost; or
# (b) the expression is developer-tooling metadata (CLI args, git commit,
# session bookkeeping), not financial data DP#32 is about.
_REVIEWED_HARMLESS = {
    # simulation_state.py: deduction_marginal_rate -- 0% deduction is
    # legitimately both "not specified" and "specified as 0%"; #606 confirms.
    ("simulation_state.py", "e.get('deduction_marginal_rate') or 0"): {
        "reason": "#606 verified-not-a-bug: default already equals fallback (0 or 0); a 0% rate reads the same whether absent or explicit.",
    },
    # simulation_state.py:434 -- part of the SAME _opening() function as the
    # confirmed violation above, but this specific expression (the portfolio
    # fallback path itself) is symmetric: 0 or 0.
    ("simulation_state.py",
     "_pf_accounts.get(pf_key, {}).get('balance', 0) or 0"): {
        "reason": "Symmetric 0-or-0; the lossy step in _opening() is the p_val-or-s_val truthiness test upstream, not this expression.",
    },
    # output_plugins.py: display/report layer reading already-computed
    # result dicts (yr/r/top), never raw user input. #606 confirms this
    # whole file is the "display layer" bucket.
    ("output_plugins.py", "yr.get('total_assets', 0) or 0"): {"reason": "#606 verified-not-a-bug: display layer, symmetric 0-or-0."},
    ("output_plugins.py", "yr.get('total_debt', 0) or 0"): {"reason": "#606 verified-not-a-bug: display layer, symmetric 0-or-0."},
    ("output_plugins.py", "nested.get(tail, 0) or 0"): {"reason": "Same _year_get() helper as total_assets/total_debt above; display layer, symmetric 0-or-0."},
    ("output_plugins.py", "yr.get(dotted_key, 0) or 0"): {"reason": "Same _year_get() helper; display layer, symmetric 0-or-0."},
    ("output_plugins.py", "top.get('year_by_year') or []"): {"reason": "#606 verified-not-a-bug: display layer, symmetric []-or-[]."},
    ("output_plugins.py", "r.get('ltv', 0) or 0"): {"reason": "#606 verified-not-a-bug: display layer, symmetric 0-or-0."},
    ("output_plugins.py", "r.get('future_value', 0) or 0"): {"reason": "#606 verified-not-a-bug: display layer, symmetric 0-or-0."},
    ("output_plugins.py", "r.get('total_debt', 0) or 0"): {"reason": "#606 verified-not-a-bug: display layer, symmetric 0-or-0."},
    ("output_plugins.py", "r.get('year_by_year') or []"): {"reason": "#606 verified-not-a-bug: display layer, symmetric []-or-[] (repeated at 3 call sites; see module docstring re: duplicate-snippet collapse)."},
    # scenario_discovery.py / the config layer (config_access.py,
    # config_serde.py, scenario_overlay.py -- all three carved out of the old
    # simulation_config.py): dict-section fallbacks where absence of the whole
    # section and an explicitly empty section produce the same {}.
    #
    # NOTE these are the SAME already-triaged sites the entries below used to
    # name under "simulation_config.py"; the split re-filed them, it did not add
    # any. `cfg.get('assumptions', {}) or {}` needs two keys now only because
    # the two call sites landed in two different modules.
    ("scenario_discovery.py", "cfg.get('retirement', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty retirement section reads the same as an absent one."},
    ("scenario_discovery.py", "cfg.get('assumptions', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty assumptions section reads the same as an absent one."},
    ("config_access.py", "cfg.get('assumptions', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty assumptions section reads the same as an absent one."},
    ("config_access.py", "cfg.get('return_model') or {}"): {"reason": "Fallback is the dict type's own empty identity {}; an explicitly empty return_model reads the same as an absent one."},
    ("config_serde.py", "cfg.get('lira', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty lira section reads the same as an absent one."},
    ("scenario_overlay.py", "cfg.get('retirement', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty retirement section reads the same as an absent one."},
    ("scenario_overlay.py", "cfg.get('assumptions', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty assumptions section reads the same as an absent one."},
    # session_log.py / loop.py / optimize.py: developer tooling and session
    # bookkeeping, not financial input DP#32 is about.
    ("session_log.py", "getattr(scenario, 'config_overrides', {}) or {}"): {"reason": "Symmetric {}-or-{}; session-logging metadata, not financial input."},
    ("loop.py", "getattr(a, f'{prefix}_provider') or a.provider"): {"reason": "CLI-tooling (pi loop runner) argparse Namespace default, not financial data; argparse default is None, never a meaningful empty-string provider."},
    ("loop.py", "getattr(a, f'{prefix}_model') or a.model"): {"reason": "Same CLI-tooling default as *_provider above."},
    ("optimize.py", "record.get('git_commit') or 'unknown'"): {"reason": "Session-log metadata (git commit hash); an empty-string commit sha is never legitimate user-supplied data."},
    # retirement_transition.py: numeric amounts/counts where an explicit 0
    # is the objectively correct value (no CPP estimate, no deferral months,
    # no pension income, no account balance) -- unlike cpp_start_age/
    # oas_start_age (a *claim age* of 0 is never valid), these fields'
    # legitimate zero and their fallback zero are the same number.
    ("countries/canada/retirement_transition.py", "member.get('cpp_monthly_estimated', 0) or 0"): {"reason": "Symmetric 0-or-0: no CPP estimate supplied reads the same as an explicit $0 estimate."},
    ("countries/canada/retirement_transition.py", "member.get('oas_defer_months', 0) or 0"): {"reason": "Symmetric 0-or-0: no deferral is 0 months either way."},
    ("countries/canada/retirement_transition.py", "member.get('pension_income_annual', 0) or 0"): {"reason": "Symmetric 0-or-0: no pension is $0 either way."},
    ("countries/canada/retirement_transition.py", "canada.get(key, 0) or 0"): {"reason": "Symmetric 0-or-0 (drawdown source balance lookup, repeated at 2 call sites in plan_drawdown/plan_drawdown_net)."},
    # Found on re-scan after rebasing onto #585/#580/#578/#577.
    ("model_fidelity.py", "cfg.get('assumptions', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty assumptions section reads the same as an absent one (describe_units)."},
    ("model_fidelity.py", "ctx.cfg.get('assumptions', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty assumptions section reads the same as an absent one (_dollar_basis_unlabeled)."},
    ("model_fidelity.py", "cfg.get('tax', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty tax section reads the same as an absent one."},
    # #825 removed `_has_retirement_data()` (dead after its only caveat was
    # deleted), so its symmetric []-or-[] snippet is gone; the allowlist entry
    # for it leaves too (else test_dp32_allowlist_has_no_stale_entries fires).
    ("objective.py", "cfg.get('property', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty property section reads the same as an absent one."},
    ("objective.py", "cfg.get('tax', {}) or {}"): {"reason": "Symmetric {}-or-{}: an empty tax section reads the same as an absent one."},
    # epic #841 bite 4: the family objective reads the terminal YearResult's
    # child-account snapshot. Symmetric []-or-[]: a household with no
    # child-savers carries an empty child_accounts list, which reads
    # identically to the field being absent (a YearResult constructed by a
    # direct unit-test caller before bite 4). Both mean "no child wealth to
    # add" and sum to exactly 0.0 -- no data can be lost by the fallback.
    ("objective.py", "getattr(results[-1], 'child_accounts', None) or []"): {"reason": "Symmetric []-or-[]: no child-savers carries an empty list, identical to the field being absent; both add exactly $0.0 to the family total."},
    ("objective.py", "getattr(results[-1], 'extra_adult_accounts', None) or []"): {"reason": "Symmetric []-or-[] (#899): no extra accumulating adults carries an empty list, identical to the field being absent; both add exactly $0.0 to the family total."},
    ("output_plugins.py", "_sort_results(results)[0].get('objective_name') or None"): {"reason": "Fallback is None, the canonical absence sentinel itself; the caller's own docstring says an ambiguous objective_name is treated as unknown either way (report the caveat), so nothing distinguishable is lost."},
    ("simulation_state.py", "getattr(config, 'resp_composition', None) or {}"): {"reason": "Symmetric {}-or-{}, and the adjacent comment documents the intent explicitly: an explicitly empty composition falls back to the default split exactly like an absent one."},
}

_ALLOWLIST_KEYS = set(_CONFIRMED_VIOLATIONS) | set(_REVIEWED_HARMLESS)


def _assert_disjoint():
    overlap = set(_CONFIRMED_VIOLATIONS) & set(_REVIEWED_HARMLESS)
    assert not overlap, f"Entries classified as BOTH violation and harmless: {overlap}"


def test_dp32_allowlist_is_internally_consistent():
    """The two buckets must not overlap -- a finding is either a tracked
    violation or a reviewed-harmless site, never both."""
    _assert_disjoint()


def test_dp32_no_unlisted_or_fallback():
    """Every `.get()/getattr() or DEFAULT` site in first-party source must be
    triaged: either a tracked violation (cites an issue) or reviewed-harmless
    (cites why). A NEW site that matches neither is a build failure -- that
    is the mechanism that makes DP#32 load-bearing instead of decorative.
    """
    findings = repo_scan.find_get_or_default()
    unlisted, _ = repo_scan.diff_against_allowlist(findings, {k: {} for k in _ALLOWLIST_KEYS})
    if unlisted:
        lines = "\n".join(
            f"  {f.file}:{f.line}: {f.snippet}" for f in sorted(unlisted, key=lambda f: (f.file, f.line))
        )
        raise AssertionError(
            "DP#32: found `.get()/getattr() or DEFAULT` site(s) not triaged in "
            "tests/architecture/test_dp32_zero_fallback.py. Add each to "
            "_CONFIRMED_VIOLATIONS (citing a tracking issue) or "
            "_REVIEWED_HARMLESS (citing why the fallback cannot lose data):\n"
            f"{lines}"
        )


def test_dp32_allowlist_has_no_stale_entries():
    """Every allowlisted site must still exist verbatim in the source. A
    stale entry means the code changed (fixed, refactored, or deleted)
    without anyone updating this file -- which is exactly the silent-growth
    (and silent-shrink) failure mode #586 asks the allowlist to be immune to.
    """
    findings = repo_scan.find_get_or_default()
    _, stale = repo_scan.diff_against_allowlist(findings, {k: {} for k in _ALLOWLIST_KEYS})
    if stale:
        lines = "\n".join(f"  {file}: {snippet!r}" for file, snippet in stale)
        raise AssertionError(
            "DP#32: allowlist entries no longer match any source (the "
            "underlying code changed). Remove or update these entries in "
            f"tests/architecture/test_dp32_zero_fallback.py:\n{lines}"
        )


def test_dp32_confirmed_violations_cite_a_tracking_issue():
    """Guard against an entry being added to _CONFIRMED_VIOLATIONS without a
    real issue reference -- the whole point of the bucket is traceability."""
    for key, meta in _CONFIRMED_VIOLATIONS.items():
        issue = meta.get("issue", "")
        assert issue.startswith("#") and issue[1:].isdigit(), (
            f"{key}: _CONFIRMED_VIOLATIONS entry must cite a '#NNN' issue, got {issue!r}"
        )


def test_dp32_reviewed_harmless_entries_cite_a_reason():
    for key, meta in _REVIEWED_HARMLESS.items():
        reason = meta.get("reason", "")
        assert len(reason) >= 20, (
            f"{key}: _REVIEWED_HARMLESS entry must explain why the fallback cannot lose data, got {reason!r}"
        )
