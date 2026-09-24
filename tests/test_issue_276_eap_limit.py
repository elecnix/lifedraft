"""Issue #276, slice 1: the statutory EAP limit is enforced in the RESP payout.

``RESPCalculator.eap_payment_limit()`` existed, pro-rated a cap by weeks/13
(a rule in no version of the statute), and had no production caller: the
engine paid any EAP its even spread produced -- a silent overstatement.

ITA s.146.1(2)(g.1)(ii)(A): a full-time (qualifying program) student may be
paid at most the qualifying amount over the trailing 12 months UNTIL they have
been enrolled for 13 consecutive weeks in that period; after that there is no
statutory cap. So the engine caps the first year of the study window (and the
first year of a declared restart after a gap), defers the excess to later
study years, and refuses loudly any schedule that would still pay over it.

Pure functions are tested directly; engine behaviour is tested by driving
``FamilySimulation.run()`` (DP#11). The limit figures are asserted as LITERALS
from the cited statute, never read back from the table (a tautology).
Fabricated round numbers, role-based names (DP#4/DP#15).
"""

import inspect

import pytest

import countries.canada.resp_rules as resp_rules
from countries.canada.resp_rules import (
    EAPLimitExceeded,
    assert_eap_within_limit,
    eap_limited_years,
    eap_payment_limit,
    resp_annual_withdrawal,
)
from test_issue_578_resp_winddown import _one_child_config, _run

START_YEAR = 2026

# Measured on main @ 9c7ae4f (before this change), _one_child_config() with a
# 60,000 plan: the even-spread EAP for 2026-2029, and its 4-year total.
UNCAPPED_EAP_60K = [8250.0, 9037.5, 9864.375, 10732.59375]
UNCAPPED_EAP_60K_TOTAL = sum(UNCAPPED_EAP_60K)


# ============================================================================
# Pure functions
# ============================================================================

def test_eap_payment_limit_reads_year_versioned_table():
    """Both sides of the 2023-03-28 amendment and of the earliest sourced row
    (DP#17/DP#20). Sources: S.C. 2023, c. 26, s. 39 ($8,000 / $4,000, deemed in
    force 2023-03-28); S.C. 2007, c. 29, s. 18 ($5,000 / $2,500, 2007+)."""
    # After the latest row: the latest law stays in force (not indexed).
    assert eap_payment_limit(2099, 'qualifying') == 8000.0
    assert eap_payment_limit(2099, 'specified') == 4000.0
    assert eap_payment_limit(2024, 'qualifying') == 8000.0
    assert eap_payment_limit(2024, 'specified') == 4000.0
    # The amendment year had both regimes in force: the LOWER one wins.
    assert eap_payment_limit(2023, 'qualifying') == 5000.0
    assert eap_payment_limit(2023, 'specified') == 2500.0
    # The prior regime.
    assert eap_payment_limit(2022, 'qualifying') == 5000.0
    assert eap_payment_limit(2007, 'qualifying') == 5000.0
    assert eap_payment_limit(2007, 'specified') == 2500.0
    # Before the earliest sourced row: refuse, never borrow a later law.
    for program in ('qualifying', 'specified'):
        with pytest.raises(ValueError, match=r'2006.*2007-01-01'):
            eap_payment_limit(2006, program)
    # No default program.
    for bad in ('bogus', '', None, 'full_time'):
        with pytest.raises(ValueError, match='program'):
            eap_payment_limit(2030, bad)


def test_eap_limits_table_rows_cite_a_primary_source():
    """DP#12: every row of the table says where its figures come from."""
    rows = resp_rules.EAP_LIMITS
    assert rows == sorted(rows, key=lambda r: r['effective_date'])
    for row in rows:
        assert 'https://laws-lois.justice.gc.ca/' in row['source']
        assert '146.1' in row['source']
        assert 'retrieved' in row['source']


def test_resp_annual_withdrawal_requires_eap_limit():
    """The limit has no default: a caller that forgets it must not silently
    get an uncapped schedule (DP#32)."""
    param = inspect.signature(resp_annual_withdrawal).parameters['eap_limit']
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        resp_annual_withdrawal(1, 1, 1, 1, 1)  # pylint: disable=missing-kwoa


def test_resp_annual_withdrawal_scales_eap_pro_rata_when_limit_binds():
    """Even spread of 40k contrib / 8k CESG / 4k QESI / 28k earnings over 2
    years is PSE 20,000 and EAP 4,000 + 2,000 + 14,000 = 20,000. A 5,000 limit
    scales each EAP bucket by 1/4 and leaves PSE alone."""
    uncapped = resp_annual_withdrawal(40_000, 8_000, 4_000, 28_000, 2, eap_limit=None)
    assert uncapped == {
        'pse': 20_000.0, 'eap': 20_000.0, 'contributions_withdrawn': 20_000.0,
        'cesg_withdrawn': 4_000.0, 'qesi_withdrawn': 2_000.0, 'earnings_withdrawn': 14_000.0,
    }
    capped = resp_annual_withdrawal(40_000, 8_000, 4_000, 28_000, 2, eap_limit=5_000.0)
    assert capped['eap'] == pytest.approx(5_000.0, abs=1e-9)
    assert capped['cesg_withdrawn'] == pytest.approx(1_000.0, abs=1e-9)
    assert capped['qesi_withdrawn'] == pytest.approx(500.0, abs=1e-9)
    assert capped['earnings_withdrawn'] == pytest.approx(3_500.0, abs=1e-9)
    assert capped['pse'] == 20_000.0
    assert capped['contributions_withdrawn'] == 20_000.0
    # The other side of the threshold: a limit at or above the even spread,
    # and a zero-EAP plan, return exactly the even spread.
    assert resp_annual_withdrawal(40_000, 8_000, 4_000, 28_000, 2, eap_limit=20_000.0) == uncapped
    assert resp_annual_withdrawal(40_000, 8_000, 4_000, 28_000, 2, eap_limit=25_000.0) == uncapped
    zero_eap = resp_annual_withdrawal(10_000, 0, 0, 0, 2, eap_limit=5_000.0)
    assert zero_eap == resp_annual_withdrawal(10_000, 0, 0, 0, 2, eap_limit=None)
    assert zero_eap['eap'] == 0.0 and zero_eap['pse'] == 5_000.0


def test_assert_eap_within_limit_boundaries():
    """The guard raises above the limit (beyond half a cent of float noise),
    never clamps, and never fires when no cap applies."""
    assert assert_eap_within_limit(8_000.0, 8_000.0, child_index=0, calendar_year=2026) is None
    assert assert_eap_within_limit(8_000.004, 8_000.0, child_index=0, calendar_year=2026) is None
    assert assert_eap_within_limit(1e9, None, child_index=0, calendar_year=2026) is None
    for over in (8_000.01, 8_001.0):
        with pytest.raises(EAPLimitExceeded) as exc:
            assert_eap_within_limit(over, 8_000.0, child_index=1, calendar_year=2031)
        msg = str(exc.value)
        assert 'child index 1' in msg and '2031' in msg
        assert '8,000.00' in msg and '146.1' in msg and 'Minister' in msg
    assert issubclass(EAPLimitExceeded, ValueError)


def test_eap_limited_years_first_year_and_gap_restart():
    """The first year of the window is capped; a declared period restarting
    after a gap is capped in its start year; a contiguous one is not."""
    assert eap_limited_years({}, 2028, 4) == frozenset({2028})
    assert eap_limited_years({'study_periods': []}, 2028, 4) == frozenset({2028})
    contiguous = {'study_periods': [{'start_year': 2028, 'end_year': 2030},
                                    {'start_year': 2030, 'end_year': 2032}]}
    assert eap_limited_years(contiguous, 2028, 4) == frozenset({2028})
    gapped = {'study_periods': [{'start_year': 2031, 'end_year': 2032},
                                {'start_year': 2028, 'end_year': 2029}]}
    assert eap_limited_years(gapped, 2028, 4) == frozenset({2028, 2031})
    # A period nested inside an earlier, longer one is not a restart.
    nested = {'study_periods': [{'start_year': 2028, 'end_year': 2033},
                                {'start_year': 2030, 'end_year': 2031}]}
    assert eap_limited_years(nested, 2028, 4) == frozenset({2028})
    # An open-ended period resolves its end as start + duration - 1 (the same
    # rule as the study window): 2028 + 2 - 1 = 2029, so 2031 is a restart...
    open_ended = {'study_periods': [{'start_year': 2028, 'end_year': None},
                                    {'start_year': 2031, 'end_year': 2032}]}
    assert eap_limited_years(open_ended, 2028, 2) == frozenset({2028, 2031})
    # ...but with a 4-year duration it ends 2031 and 2031 is contiguous.
    assert eap_limited_years(open_ended, 2028, 4) == frozenset({2028})


# ============================================================================
# Engine: FamilySimulation.run()
# ============================================================================

def test_engine_caps_first_study_year_eap_and_defers_the_rest():
    """child_a is 18 in 2026, the first year of a 4-year window. The even
    spread would pay 8,250 of EAP in 2026; the engine pays the 8,000 limit and
    pays the deferred 250 (plus its growth) in later years. Years 2-4 are NOT
    capped: the 13-consecutive-weeks condition is met by then."""
    results = _run(_one_child_config())
    eap = [r.resp_eap_paid for r in results]

    assert eap[0] == pytest.approx(8_000.0, abs=0.01)
    assert results[0].resp_pse_paid == pytest.approx(7_500.0, abs=0.01)  # PSE never capped
    for capped_run, uncapped in zip(eap[1:4], UNCAPPED_EAP_60K[1:]):
        assert capped_run > uncapped
    assert max(eap[1:4]) > 8_000.0, "later years must not be capped at the limit"

    # Conservation: the deferred EAP is paid later with growth, not lost and
    # not duplicated. 250 deferred, growing at most 3 years at 5%.
    deferred = UNCAPPED_EAP_60K[0] - 8_000.0
    total = sum(eap[:4])
    assert UNCAPPED_EAP_60K_TOTAL < total <= UNCAPPED_EAP_60K_TOTAL + deferred * 1.05 ** 3 + 0.01

    assert all(r.resp_aip_tax == 0 for r in results)
    assert all(r.resp_balance < 1.0 for r in results[4:])


def test_engine_below_limit_is_unchanged(monkeypatch):
    """The other side of the threshold (DP#17): a 20,000 plan pays about 2,750
    of EAP in 2026, below the limit, so the schedule is exactly the even spread
    -- identical to a run in which no cap could ever bind."""
    capped_run = _run(_one_child_config(resp_balance=20_000))

    monkeypatch.setattr(resp_rules, 'eap_payment_limit', lambda year, program: float('inf'))
    never_binding = _run(_one_child_config(resp_balance=20_000))

    def _rows(results):
        return [(r.resp_eap_paid, r.resp_pse_paid, r.resp_aip_tax, r.resp_balance)
                for r in results]
    assert _rows(capped_run) == _rows(never_binding)
    # Measured on main @ 9c7ae4f, before the cap existed.
    assert [r.resp_eap_paid for r in capped_run[:4]] == pytest.approx(
        [2_750.0, 3_012.5, 3_288.125, 3_577.53125], abs=1e-9)


def test_engine_one_year_window_prices_undrawn_eap_via_collapse():
    """A one-year window: the plan can pay only the 8,000 limit as EAP in its
    only study year, so the undrawn EAP must go through the AIP collapse the
    year after -- priced (grants repaid, earnings taxed), never vanished."""
    cfg = _one_child_config()
    cfg['accounts']['resp_study_duration_years'] = 1
    cfg['assumptions']['projection_years'] = 3
    results = _run(cfg)

    assert results[0].resp_eap_paid == pytest.approx(8_000.0, abs=0.01)
    assert results[0].resp_balance > 1.0
    assert results[1].resp_eap_paid == 0
    assert results[1].resp_aip_tax > 0
    assert results[1].resp_balance < 1.0
    assert results[2].resp_aip_tax == 0


def test_engine_refuses_schedule_that_ignores_eap_limit(monkeypatch):
    """The detector: a schedule that ignores the limit (today's even spread, or
    a future declared plan) must crash the run, never be paid."""
    original = resp_rules.resp_annual_withdrawal

    def _ignores_the_limit(*args, eap_limit, **kwargs):
        return original(*args, eap_limit=None, **kwargs)

    monkeypatch.setattr(resp_rules, 'resp_annual_withdrawal', _ignores_the_limit)
    with pytest.raises(EAPLimitExceeded) as exc:
        _run(_one_child_config())
    msg = str(exc.value)
    assert 'child index 0' in msg and 'in 2026' in msg
    assert '$8,250.00' in msg and '$8,000.00' in msg


def test_engine_caps_restart_year_after_declared_gap():
    """child_a declares 2026-2027 then, after a gap year, 2029-2030. The first
    year and the restart year are capped at the limit; the contiguous second
    year of each period is not."""
    cfg = _one_child_config(resp_balance=150_000)
    cfg['family']['children'][0]['study_periods'] = [
        {'start_year': 2026, 'end_year': 2027},
        {'start_year': 2029, 'end_year': 2030},
    ]
    results = _run(cfg)
    by_year = {START_YEAR + i: r for i, r in enumerate(results)}

    assert by_year[2026].resp_eap_paid == pytest.approx(8_000.0, abs=0.01)
    assert by_year[2029].resp_eap_paid == pytest.approx(8_000.0, abs=0.01)
    assert by_year[2027].resp_eap_paid > 8_000.0
    assert by_year[2030].resp_eap_paid > 8_000.0
    assert all(r.resp_aip_tax == 0 for r in results)
    assert by_year[2031].resp_balance < 1.0


def test_engine_mid_window_entry_is_not_capped():
    """The cap keys on the study WINDOW, not on the simulation's first year: a
    child whose window began in 2025 has met the 13 weeks by 2026, so the
    first simulated year pays the full even spread even above the limit."""
    cfg = _one_child_config()
    cfg['family']['children'][0]['birth_year'] = 2007  # window 2025-2028
    results = _run(cfg)

    assert results[0].resp_eap_paid > 8_000.0
    assert all(r.resp_aip_tax == 0 for r in results)
    assert all(r.resp_balance < 1.0 for r in results[3:])
