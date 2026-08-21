"""Tests for issue #578: the RESP must wind down as EAPs/PSE across the
beneficiary's study window, instead of compounding forever.

DP#17: exercises both sides of the study-window threshold (year just before
the window vs. the first/last year inside it) and both sides of the
"used for education" decision (plan used vs. plan collapsed unused).
Fabricated round numbers, role-based names (DP#4/DP#15) -- no personal data.
"""

import pytest

from countries.canada.adapter import CanadaAdapter
from countries.canada.resp_rules import (
    resp_study_window,
    resp_study_window_for_child,
    resp_annual_withdrawal,
    resp_collapse_aip,
)
from simulation import FamilySimulation
from simulation_config import SimulationConfig


def _in_study(birth_year, year, **kw):
    """The predicate the ENGINE now uses (simulation_rules' RESP wind-down):
    ``first_year <= year <= last_year`` over the child's window.

    #714 collapsed the old ``is_resp_study_year()`` / ``has_aged_out_of_resp_
    study()`` wrappers into this one spelling. They re-derived the very same
    window from the very same inputs -- three spellings of one window, which is
    how a per-child window could be added to the config and silently disagree
    with the one the withdrawal maths actually used -- and they had zero
    production callers once the window was computed once per child (DP#9).
    The threshold coverage below is unchanged; only the spelling is.
    """
    first, last = resp_study_window_for_child({}, birth_year, **kw)
    return first <= year <= last


def _aged_out(birth_year, year, **kw):
    _, last = resp_study_window_for_child({}, birth_year, **kw)
    return year > last


# ============================================================================
# Unit tests for the resp_rules.py wind-down primitives (pure functions).
# ============================================================================

def test_resp_study_window_derived_from_birth_year():
    """The window is birth_year + start_age .. birth_year + start_age + duration - 1."""
    first, last = resp_study_window(2010, study_start_age=18, study_duration_years=4)
    assert (first, last) == (2028, 2031)


def test_is_resp_study_year_just_before_window():
    """DP#17: the calendar year immediately before the window is NOT a study year."""
    assert not _in_study(2010, 2027, study_start_age=18, study_duration_years=4)


def test_is_resp_study_year_first_year_of_window():
    """DP#17: the first calendar year of the window IS a study year."""
    assert _in_study(2010, 2028, study_start_age=18, study_duration_years=4)


def test_is_resp_study_year_last_year_of_window():
    assert _in_study(2010, 2031, study_start_age=18, study_duration_years=4)


def test_is_resp_study_year_just_after_window():
    """DP#17: the calendar year immediately after the window is NOT a study year."""
    assert not _in_study(2010, 2032, study_start_age=18, study_duration_years=4)


def test_has_not_aged_out_during_window():
    assert not _aged_out(2010, 2031, study_start_age=18, study_duration_years=4)


def test_has_aged_out_the_year_after_window():
    assert _aged_out(2010, 2032, study_start_age=18, study_duration_years=4)


def test_annual_withdrawal_drains_evenly_across_remaining_years():
    """Two years remaining -> half of each bucket comes out this year."""
    draw = resp_annual_withdrawal(contributions=10_000, cesg=2_000, qesi=1_000,
                                   earnings=7_000, years_remaining=2)
    assert draw['contributions_withdrawn'] == pytest.approx(5_000)
    assert draw['cesg_withdrawn'] == pytest.approx(1_000)
    assert draw['qesi_withdrawn'] == pytest.approx(500)
    assert draw['earnings_withdrawn'] == pytest.approx(3_500)
    assert draw['pse'] == pytest.approx(5_000)
    assert draw['eap'] == pytest.approx(1_000 + 500 + 3_500)


def test_annual_withdrawal_last_year_drains_everything():
    """One year remaining -> the entire balance comes out (full wind-down)."""
    draw = resp_annual_withdrawal(contributions=4_000, cesg=800, qesi=400,
                                   earnings=2_800, years_remaining=1)
    assert draw['pse'] == pytest.approx(4_000)
    assert draw['eap'] == pytest.approx(800 + 400 + 2_800)


def test_collapse_aip_repays_grants_and_taxes_earnings_plus_penalty():
    """AIP: grants are fully repaid; earnings taxed at subscriber MTR + 20%."""
    result = resp_collapse_aip(cesg=1_000, qesi=500, earnings=10_000,
                                subscriber_marginal_rate=0.30)
    assert result['grant_repayment'] == pytest.approx(1_500)
    assert result['aip_tax'] == pytest.approx(10_000 * 0.50)  # 30% MTR + 20% penalty
    assert result['net_aip'] == pytest.approx(10_000 - 5_000)


# ============================================================================
# Integration tests: the study window threshold inside the year-by-year fold.
# ============================================================================

START_YEAR = 2026
PRIMARY_BIRTH = 1985
CHILD_BIRTH = 2008  # age 18 in START_YEAR + 0 => study window starts immediately


def _one_child_config(resp_used_for_education: bool = True, resp_balance: float = 60_000) -> dict:
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': PRIMARY_BIRTH, 'gross_income': 100_000,
                 'retirement_age': 65, 'rrsp_room_accumulated': 20_000,
                 'tfsa_room_accumulated': 10_000},
            ],
            'children': [{'name': 'child_a', 'birth_year': CHILD_BIRTH}],
        },
        'accounts': {
            'resp_current_balance': resp_balance,
            'resp_used_for_education': resp_used_for_education,
        },
        'assumptions': {
            'start_year': START_YEAR,
            'projection_years': 8,  # covers ages 18-25
            'investment_return': 0.05,
            'salary_growth': 0.0,
            'frozen_brackets': True,
        },
        'property': {
            'house_value': 400_000, 'mortgage_balance': 200_000, 'mortgage_rate': 0.05,
            'amortization_years': 20, 'margin_available': 0, 'ltv_max': 0.80,
        },
        'savings': {'rate': 0.15},
        'tax': {'province': 'qc'},
    }


def _run(cfg: dict):
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                            use_readvanceable=False, deduct_later=False)
    return sim.run()


def test_resp_used_for_education_pays_eap_during_study_window_and_drains_to_zero():
    """DP#17: EAPs fire while the child is in the study window, and the RESP
    is (near) empty by the year after the window closes -- not compounding."""
    results = _run(_one_child_config(resp_used_for_education=True))

    # Child is 18 at START_YEAR (first study year) -> ages 18-21 are in-window
    # (projection_years=8 covers indices 0..7 -> calendar years 2026..2033).
    in_window = results[0:4]     # calendar years 2026-2029 (ages 18-21)
    after_window = results[4:]   # calendar years 2030+ (ages 22+)

    assert all(r.resp_eap_paid > 0 for r in in_window), \
        "EAP must be paid every year the beneficiary is in the study window"
    assert all(r.resp_pse_paid > 0 for r in in_window), \
        "PSE (contributions) must be returned every year in the study window"
    assert after_window[0].resp_balance < 1.0, \
        "RESP must be wound down by the year after the study window closes"
    assert all(r.resp_balance < 1.0 for r in after_window), \
        "RESP must stay wound down, not resume compounding"
    # EAP is the beneficiary's income, not the subscriber's -- no AIP penalty
    # tax should ever be charged on a plan that IS being used for education.
    assert all(r.resp_aip_tax == 0 for r in results)


def test_resp_not_used_for_education_collapses_via_aip_at_study_start():
    """DP#17: the opposite path -- resp_used_for_education=False means the
    plan collapses (grant repayment + AIP tax) instead of paying EAPs."""
    results = _run(_one_child_config(resp_used_for_education=False))

    # Collapse happens the very first year (child is already 18 at START_YEAR).
    assert results[0].resp_balance < 1.0
    assert results[0].resp_aip_tax > 0, \
        "collapsing an unused RESP must charge the AIP tax (grants repaid + earnings taxed)"
    assert results[0].resp_eap_paid == 0, \
        "a plan collapsed as unused must never pay EAP (that's the used-for-education path)"
    assert results[0].resp_pse_paid > 0, \
        "contributions are still returned tax-free to the subscriber on collapse"
    # Stays wound down for the rest of the horizon.
    assert all(r.resp_balance < 1.0 for r in results[1:])


def test_child_not_yet_in_study_window_keeps_accumulating():
    """DP#17: the other side of the age threshold -- a child who has not yet
    reached study_start_age sees the RESP keep growing normally, untouched."""
    cfg = _one_child_config(resp_used_for_education=True)
    cfg['family']['children'][0]['birth_year'] = START_YEAR - 10  # age 10, far from 18
    cfg['assumptions']['projection_years'] = 3
    results = _run(cfg)

    assert all(r.resp_eap_paid == 0 for r in results)
    assert all(r.resp_pse_paid == 0 for r in results)
    assert all(r.resp_aip_tax == 0 for r in results)
    # Balance should grow (contribution + return), never shrink, while
    # accumulating pre-study.
    balances = [r.resp_balance for r in results]
    assert all(b2 >= b1 for b1, b2 in zip(balances, balances[1:]))


# ============================================================================
# The new config keys must be real: declared in the schema, and load-bearing
# (an override must change engine OUTPUT, not evaporate onto a dead key --
# DP#18/#594/#593, the failure mode this epic exists to end).
# ============================================================================

def test_winddown_keys_are_declared_in_the_canada_input_schema():
    """#594: a load-bearing key that appears in no schema file is undocumented
    config. Epic #603 Track C Phase 2b: the legacy schema this test used to
    read (countries/canada/input_schema.json) is deleted (DP#9) -- the
    contract's home for these is assumptions.resp (a family/study BELIEF,
    applied uniformly to every RESP beneficiary -- #598 follow-up), not
    accounts.*. Before this PR these three had NO home anywhere in the
    contract at all; input_contract.to_internal_config now maps them onto
    the internal accounts.resp_study_start_age/etc. SimulationConfig.
    from_dict reads (resp_composition has no contract equivalent -- it is
    DERIVED by the adapter from accounts[kind=resp].resp's contributions/
    cesg/qesi totals, never authored directly, so it is not a schema key)."""
    import json
    from pathlib import Path
    schema = json.loads(
        (Path(__file__).resolve().parent.parent
         / 'schema' / 'countries' / 'canada' / 'input_schema.json').read_text())
    resp_beliefs = schema['$defs']['assumptions']['properties']['resp']['properties']
    for key in ('study_start_age', 'study_duration_years', 'used_for_education'):
        assert key in resp_beliefs, f"assumptions.resp.{key} is read by the engine but absent from the schema"


# test_schema_default_composition_is_empty_not_zeroed deleted (epic #603
# Track C Phase 2b): it guarded against module_registry._deep_merge
# injecting an all-zero resp_composition default into every config -- that
# whole mechanism (deep-merging a country overlay of DEFAULT VALUES from an
# example-instance file into an unvalidated dict) is deleted along with
# input_schema.json/countries/canada/input_schema.json (DP#9). The contract
# schema has no equivalent "example default" to be zeroed: resp_composition
# is derived by the adapter, never schema-declared, so the near-miss this
# guarded against is structurally impossible now, not merely fixed.


def test_study_start_age_override_changes_engine_output():
    """DP#18: an overlay must modify a key the ENGINE reads. Deferring the
    study start by 2 years must delay the first EAP by exactly 2 years."""
    base = _one_child_config()
    base['assumptions']['projection_years'] = 10

    early = _run(base)
    late_cfg = dict(base)
    late_cfg['accounts'] = dict(base['accounts'])
    late_cfg['accounts']['resp_study_start_age'] = 20
    late = _run(late_cfg)

    first_eap_early = next(i for i, r in enumerate(early) if r.resp_eap_paid > 0)
    first_eap_late = next(i for i, r in enumerate(late) if r.resp_eap_paid > 0)
    assert first_eap_late == first_eap_early + 2


def test_study_duration_override_changes_engine_output():
    """DP#18: a longer study window spreads the same plan over more years, so
    each year's EAP is smaller and the drain finishes later."""
    base = _one_child_config()
    base['assumptions']['projection_years'] = 10

    short = _run(base)                                # 4-year window
    long_cfg = dict(base)
    long_cfg['accounts'] = dict(base['accounts'])
    long_cfg['accounts']['resp_study_duration_years'] = 6
    long = _run(long_cfg)

    eap_years_short = sum(1 for r in short if r.resp_eap_paid > 0)
    eap_years_long = sum(1 for r in long if r.resp_eap_paid > 0)
    assert eap_years_long == 6
    assert eap_years_short == 4
    # Same plan spread thinner: the first year's EAP must be smaller.
    assert long[0].resp_eap_paid < short[0].resp_eap_paid


def test_supplied_composition_beats_the_default_split():
    """DP#13: real composition data wins over the resp_rules default 50/10/5/35.

    A plan that is almost entirely contributions has very little to pay out as
    (taxable) EAP; the default split would invent ~$0.5 of EAP per $1 of plan.
    """
    cfg = _one_child_config(resp_balance=40_000)
    cfg['accounts']['resp_composition'] = {
        'total_contributions': 38_000,
        'total_cesg_received': 1_000,
        'total_qesi_received': 500,
        'investment_earnings': 500,
    }
    results = _run(cfg)
    default_results = _run(_one_child_config(resp_balance=40_000))

    total_eap = sum(r.resp_eap_paid for r in results)
    total_default_eap = sum(r.resp_eap_paid for r in default_results)
    assert total_eap < total_default_eap, \
        "a contribution-heavy plan must pay out less taxable EAP than the default split assumes"


def test_zeroed_composition_against_a_real_balance_fails_loudly():
    """DP#32: absence must fail loudly, never silently default.

    A composition supplied with every bucket zero, against a real balance, is
    incoherent -- left alone it would classify the whole balance as earnings
    and over-tax every dollar of it. The run must refuse, not answer.
    """
    cfg = _one_child_config(resp_balance=40_000)
    cfg['accounts']['resp_composition'] = {
        'total_contributions': 0,
        'total_cesg_received': 0,
        'total_qesi_received': 0,
        'investment_earnings': 0,
    }
    with pytest.raises(ValueError, match='cannot be composed of nothing'):
        _run(cfg)


def test_resp_approximations_are_declared_not_buried():
    """#585/DP#32: an approximation that biases a headline figure must declare
    itself. Both RESP wind-down approximations (the student's EAP tax is not
    computed; the returned contributions are assumed consumed by education)
    are stated in structured, auditable form, ready to register with the
    model-fidelity registry when #585 lands."""
    from countries.canada.resp_rules import RESP_MODEL_APPROXIMATIONS

    ids = {a['id'] for a in RESP_MODEL_APPROXIMATIONS}
    assert ids == {'resp_eap_student_tax_not_computed',
                   'resp_pse_consumed_by_education'}
    for a in RESP_MODEL_APPROXIMATIONS:
        # Each must say what it biases and in which direction -- a note that
        # does not name the direction of its bias is not a disclosure.
        assert a['affects'] and a['direction'] and a['description']


# ============================================================================
# Issue #714: the per-child DECLARED study window (people[].study_periods[])
# must beat the household-wide age assumption.
#
# Before this, study_periods was parsed into child['study_periods'] and read by
# nobody -- every beneficiary wound down on the GLOBAL
# assumptions.resp.study_start_age, so a child who actually starts at 19, or
# studies for six years, had their EAP/AIP schedule computed against a window
# they never declared. DP#1 (store dates, not derived values), DP#28 (programs
# enter and exit on a schedule), DP#13 (a default is a fallback for ABSENT
# input, never a way to overrule a supplied one).
# ============================================================================




def test_window_falls_back_to_age_when_no_periods_declared():
    """DP#13: absent input -> the household-wide age assumption, unchanged."""
    assert resp_study_window_for_child(
        {}, 2010, study_start_age=18, study_duration_years=4) == (2028, 2031)


def test_empty_periods_list_is_not_a_declared_window():
    """An empty list declares nothing -- it must not read as a zero-length
    window that instantly collapses the RESP (DP#32: absence is not a value)."""
    assert resp_study_window_for_child(
        {'study_periods': []}, 2010, study_start_age=18, study_duration_years=4) == (2028, 2031)


def test_declared_window_beats_the_global_age_assumption():
    """#714's headline: this child starts at 19 and studies 3 years. The global
    assumption (18 + 4y => 2028-2031) must NOT win."""
    child = {'study_periods': [{'start_year': 2029, 'end_year': 2031}]}
    assert resp_study_window_for_child(
        child, 2010, study_start_age=18, study_duration_years=4) == (2029, 2031)


def test_multiple_periods_span_earliest_start_to_latest_end():
    """CEGEP then university: the beneficiary is in study for the whole span."""
    child = {'study_periods': [
        {'start_year': 2028, 'end_year': 2029},   # CEGEP
        {'start_year': 2030, 'end_year': 2033},   # university
    ]}
    assert resp_study_window_for_child(
        child, 2010, study_start_age=18, study_duration_years=4) == (2028, 2033)


def test_open_ended_period_does_not_study_forever():
    """DP#32, and the whole point of this repo: `end_date: null` means UNKNOWN,
    not INFINITE. Reading it as 'still studying' would keep the RESP in its
    sheltered EAP window for good and the AIP collapse -- grant repayment,
    subscriber tax, 20% penalty -- would never fire. That is a silent
    substitution in the FAVOURABLE direction. The unknown end is priced at the
    declared start + the declared duration instead: both are inputs.
    """
    child = {'study_periods': [{'start_year': 2029, 'end_year': None}]}
    first, last = resp_study_window_for_child(
        child, 2010, study_start_age=18, study_duration_years=4)
    assert (first, last) == (2029, 2032)


def test_declared_window_changes_the_simulated_resp_winddown():
    """The DP#18 standard: not "the config changed" -- the ENGINE's output
    changed. Two identical households whose ONLY difference is the child's
    declared study window must not produce the same RESP trajectory.
    """
    def _cfg(study_periods):
        child = {'birth_year': 2010, 'name': 'child_a'}
        if study_periods is not None:
            child['study_periods'] = study_periods
        return {
            'family': {
                'members': [
                    {'role': 'primary', 'birth_year': 1980, 'gross_income': 150000},
                    {'role': 'spouse', 'birth_year': 1982, 'gross_income': 90000},
                ],
                'children': [child],
            },
            'property': {'house_value': 700000, 'mortgage_balance': 300000,
                         'mortgage_rate': 0.05},
            'accounts': {'resp_current_balance': 60000},
            'savings': {'rate': 0.20},
            'assumptions': {
                'start_year': 2026, 'projection_years': 20, 'inflation': 0.02,
                'investment_return': 0.06, 'resp_study_start_age': 18,
                'resp_study_duration_years': 4, 'resp_used_for_education': True,
            },
            'tax': {'province': 'qc'},
        }

    START_YEAR = 2026

    def _eap_by_year(cfg):
        sim_cfg = SimulationConfig.from_dict(cfg)
        sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg))
        # YearResult.year is a 1-based step index, not a calendar year.
        return [(START_YEAR + r.year - 1, round(r.resp_eap_paid, 2)) for r in sim.run()]

    # Global assumption alone => window 2028-2031.
    default_window = _eap_by_year(_cfg(None))
    # Declared: this child starts LATER and studies LONGER => 2032-2037.
    declared_window = _eap_by_year(_cfg([{'start_year': 2032, 'end_year': 2037}]))

    assert default_window != declared_window, (
        "the child's declared study_periods produced an IDENTICAL RESP "
        "wind-down to the global age assumption -- study_periods is still a "
        "dead write (#714)"
    )

    def _first_eap_year(rows):
        return next((y for y, eap in rows if eap > 0), None)

    assert _first_eap_year(default_window) == 2028
    assert _first_eap_year(declared_window) == 2032, (
        "the first EAP must be paid in the year the beneficiary says they START "
        "studying, not the year the household-wide age assumption guesses"
    )
