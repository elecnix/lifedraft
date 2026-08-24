#!/usr/bin/env python3
"""Issue #708: LIRA->LIF conversion must be event-driven, not hardcoded 71.

Root cause (the bug): ``apply_lira_lif`` gated conversion on
``must_convert_by_year(birth_year)`` == ``birth_year + 71`` and nothing else,
so a member retiring before 71 could never convert locked-in money at
retirement -- it compounded untouched for years while non-locked accounts
were over-depleted. Quebec expressly permits a CRI/LIRA->FRV/LIF transfer at
any age ("Il n'y a pas d'âge minimum pour faire un tel transfert", Retraite
Québec), with conversion mandatory only by 31 December of the year the owner
turns 71.

Fix (DP#1/DP#2/DP#28): the conversion year is the EARLIER of an elected
``lira.conversion_date`` and the mandatory age-71 backstop. An absent
election reproduces the pre-#708 path byte-for-byte (so no existing
trajectory can move). Early elections are honoured down to the
jurisdiction's earliest-permitted conversion age -- sourced for Quebec
(none) and REJECTED (raise) for federal/Ontario, whose earliest age is not
sourced here (flagged, not guessed -- DP#32).

These tests cover:
1. ``lif_conversion_year`` pure function: backstop, early election (Quebec),
   election clamped to the backstop, unsourced-jurisdiction refusal, bad input.
2. The provider delegates ``lif_conversion_year``.
3. End-to-end via ``simulate_year_pure``: an early Quebec election converts
   before 71 and LIF withdrawals begin; with no election the age-71 backstop
   still fires (regression); a federal early election raises loudly.
4. The adapter threads ``lira.conversion_date`` -> ``conversion_year`` and
   refuses to blend two LIRA accounts that disagree on the election.
5. The schema accepts the optional ``conversion_date`` leaf.

Run: uv run pytest tests/test_issue_708_lira_early_conversion.py -v
"""

import copy
import sys

import pytest

from countries.canada.locked_in_account import (
    EARLIEST_LIF_CONVERSION_AGE,
    MANDATORY_CONVERSION_AGE,
    CanadaLIFConversionProvider,
    lif_conversion_year,
    lif_maximum_withdrawal,
    must_convert_by_year,
)
from simulation_config import SimulationConfig
from simulation_state import (
    SimState, _default_canada_state, simulate_year_pure,
    adult_lira_slot, adult_lif_slot,
)


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_lira_wiring.py's legacy simulate_year_pure path)
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    defaults = {
        'projection_years': 10,
        'investment_return': 0.07,
        'house_value': 500000,
        'mortgage_balance': 300000,
        'mortgage_rate': 0.05,
        'margin_available': 100000,
        'family_members': [
            {'role': 'primary', 'gross_income': 130000, 'birth_year': 1979,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 70000},
        ],
        'children': [],
    }
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def _make_state_with_lira(lira_balance=100000, lira_birth_year=1979,
                           lira_jurisdiction='quebec',
                           lira_conversion_year=0,
                           lif_balance=0, lif_birth_year=0):
    """Create a SimState with CRI/LIRA data in jurisdiction_state.

    ``lira_conversion_year`` is the elected early-conversion calendar year
    (0 = no early election -> age-71 backstop applies).
    """
    # Issue #700/#643 (Step 4): LIRA/LIF are per-adult stores (single
    # primary-keyed slot today), not flat canada scalars.
    canada = _default_canada_state()
    canada['adult_lira'] = {'primary': {
        'balance': lira_balance, 'birth_year': lira_birth_year,
        'jurisdiction': lira_jurisdiction, 'reference_rate': 0.06,
        'conversion_year': lira_conversion_year,
    }}
    canada['adult_lif'] = {'primary': {
        'balance': lif_balance, 'birth_year': lif_birth_year,
        'jurisdiction': lira_jurisdiction, 'reference_rate': 0.06,
    }}
    return SimState(
        non_reg_balance=0,
        non_reg_acb=0,
        mortgage_balance=300000,
        heloc_balance=0,
        jurisdiction_state={'canada': canada},
    )


def _run_year(state, year, config, investment_return=0.07):
    return simulate_year_pure(
        state=state, year=year,
        allocations={'_primary_income': 130000, '_annual_savings': 0},
        config=config, investment_return=investment_return,
        primary_marginal_rate=0.40,
    )


# A two-generation contract fixture (the example doc, trimmed to one couple +
# children) -- reuse the helpers from test_input_contract so the adapter
# tests drive the exact same ingestion path real contracts do.
sys.path.insert(0, 'tests')
from test_input_contract import _load_example, _two_generation_subset  # noqa: E402
import input_contract as ic  # noqa: E402
import contract_errors
import contract_schema


def _base_doc():
    """A fresh, valid two-generation contract with one LIRA account (p2_lira)."""
    return _two_generation_subset(_load_example())


# ---------------------------------------------------------------------------
# 1. Pure function: lif_conversion_year
# ---------------------------------------------------------------------------

class TestLifConversionYearPure:
    """Unit tests for the event-driven conversion-year gate."""

    def test_no_election_returns_the_age_71_backstop(self):
        # birth_year 1979 -> turns 71 in 2050. No election -> backstop.
        assert lif_conversion_year(1979, 'quebec', None) == 2050
        assert lif_conversion_year(1979, 'federal', None) == 2050
        assert lif_conversion_year(1979, 'quebec', None) == must_convert_by_year(1979)

    def test_election_equal_to_backstop_returns_backstop(self):
        assert lif_conversion_year(1979, 'quebec', 2050) == 2050

    def test_election_after_backstop_is_clamped_to_backstop(self):
        # An election cannot DELAY conversion past the mandatory backstop.
        assert lif_conversion_year(1979, 'quebec', 2060) == 2050

    def test_quebec_early_election_at_retirement_age_65_is_honoured(self):
        # The issue's scenario: retire at 65 (year 2044 for birth_year 1979),
        # convert the locked-in money at retirement.
        assert lif_conversion_year(1979, 'quebec', 2044) == 2044

    def test_quebec_early_election_below_55_is_honoured_no_minimum(self):
        # Sourced (Retraite Québec): no minimum age. Age 50 and even 30 convert.
        assert lif_conversion_year(1979, 'quebec', 2029) == 2029  # age 50
        assert lif_conversion_year(1979, 'quebec', 2009) == 2009  # age 30

    def test_federal_early_election_is_rejected_not_guessed(self):
        # The earliest-permitted age for federal/Ontario is NOT sourced -- an
        # early election must raise (DP#32) rather than silently guess.
        with pytest.raises(ValueError, match="not sourced"):
            lif_conversion_year(1979, 'federal', 2044)
        with pytest.raises(ValueError, match="not sourced"):
            lif_conversion_year(1979, 'ontario', 2044)

    def test_federal_without_an_early_election_uses_the_backstop(self):
        # No early election -> the sourced age-71 backstop applies for every
        # jurisdiction; nothing is guessed.
        assert lif_conversion_year(1979, 'federal', None) == 2050

    def test_quebec_is_the_only_sourced_jurisdiction(self):
        assert 'quebec' in EARLIEST_LIF_CONVERSION_AGE
        assert EARLIEST_LIF_CONVERSION_AGE['quebec'] is None  # no minimum
        # federal/ontario are deliberately absent (unsourced -> rejected).
        assert 'federal' not in EARLIEST_LIF_CONVERSION_AGE
        assert 'ontario' not in EARLIEST_LIF_CONVERSION_AGE

    def test_backstop_is_end_of_year_turns_71(self):
        assert MANDATORY_CONVERSION_AGE == 71
        assert must_convert_by_year(1979) == 1979 + 71

    def test_invalid_birth_year_raises(self):
        with pytest.raises(ValueError, match="birth_year"):
            lif_conversion_year(0, 'quebec', None)
        with pytest.raises(ValueError, match="birth_year"):
            lif_conversion_year(-1, 'quebec', 2044)

    def test_invalid_election_year_raises(self):
        # 0 / negative is not a calendar year; pass None for "no election".
        with pytest.raises(ValueError, match="election_year"):
            lif_conversion_year(1979, 'quebec', 0)
        with pytest.raises(ValueError, match="election_year"):
            lif_conversion_year(1979, 'quebec', -5)


# ---------------------------------------------------------------------------
# 2. Provider delegation
# ---------------------------------------------------------------------------

class TestProviderDelegates:
    def test_provider_lif_conversion_year_matches_pure_function(self):
        p = CanadaLIFConversionProvider
        assert p.lif_conversion_year(1979, 'quebec', None) == 2050
        assert p.lif_conversion_year(1979, 'quebec', 2044) == 2044
        with pytest.raises(ValueError, match="not sourced"):
            p.lif_conversion_year(1979, 'federal', 2044)


class TestNumericEarliestGuardMechanism:
    """The numeric-earliest guard is the general 'permitted earlier'
    machinery (e.g. 'typically from age 55 in most jurisdictions'). No
    jurisdiction is sourced with a numeric minimum today (Quebec is None;
    federal/Ontario are unsourced), so these tests INJECT a hypothetical
    sourced minimum via monkeypatch (auto-reverted) to exercise the branch
    -- they test the MECHANISM, they do NOT assert any real jurisdiction's
    rule (DP#15/DP#32: never guess a rule)."""

    def test_election_below_numeric_earliest_is_rejected(self, monkeypatch):
        # Pretend Ontario's earliest LIF-conversion age were sourced at 55.
        monkeypatch.setitem(EARLIEST_LIF_CONVERSION_AGE, 'ontario', 55)
        # birth_year 1979 -> age 54 in 2033, below 55 -> rejected.
        with pytest.raises(ValueError, match="earliest permitted"):
            lif_conversion_year(1979, 'ontario', 2033)

    def test_election_at_numeric_earliest_is_honoured(self, monkeypatch):
        monkeypatch.setitem(EARLIEST_LIF_CONVERSION_AGE, 'ontario', 55)
        # age 55 in 2034 == earliest -> honoured (convert at 2034).
        assert lif_conversion_year(1979, 'ontario', 2034) == 2034

    def test_election_above_numeric_earliest_but_below_backstop_is_honoured(self, monkeypatch):
        monkeypatch.setitem(EARLIEST_LIF_CONVERSION_AGE, 'ontario', 55)
        # age 60 (2039) -> between earliest (55) and backstop (71) -> honoured.
        assert lif_conversion_year(1979, 'ontario', 2039) == 2039


# ---------------------------------------------------------------------------
# 3. End-to-end via simulate_year_pure
# ---------------------------------------------------------------------------

class TestEarlyConversionOnSimulationPath:
    """The bug: conversion forced at 71 regardless of an earlier election.
    The fix: an early Quebec election converts before 71."""

    def test_reproduction_no_election_compounds_until_age_71(self):
        """Pre-#708 behaviour preserved: with NO early election the LIRA
        compounds untouched through age 62 and has not converted."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000, lira_birth_year=1979,
            lira_jurisdiction='quebec', lira_conversion_year=0,  # no election
        )
        # Age 62 (year 2041) -- well before the age-71 backstop (2050).
        result, new_state = _run_year(state, 2041, config)
        canada = new_state.jurisdiction_state['canada']
        # Still a LIRA, grown; no LIF yet.
        assert adult_lira_slot(canada, 0)['balance'] > 100000, "LIRA should still be growing"
        assert adult_lif_slot(canada, 0)['balance'] == 0, "no conversion before the backstop"

    def test_early_election_converts_before_age_71_quebec(self):
        """The fix: electing conversion at retirement (age 62, year 2041)
        converts the LIRA to a LIF in 2041, not 2050."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000, lira_birth_year=1979,
            lira_jurisdiction='quebec', lira_conversion_year=2041,
        )
        # One year before the election: still accumulating.
        _, state_2040 = _run_year(state, 2040, config)
        assert adult_lira_slot(state_2040.jurisdiction_state['canada'], 0)['balance'] > 0
        assert adult_lif_slot(state_2040.jurisdiction_state['canada'], 0)['balance'] == 0
        # The election year: LIRA -> LIF.
        result, new_state = _run_year(state_2040, 2041, config)
        canada = new_state.jurisdiction_state['canada']
        assert adult_lira_slot(canada, 0)['balance'] == 0, "LIRA must be depleted at conversion"
        assert adult_lif_slot(canada, 0)['balance'] > 0, "LIF must hold the converted balance"
        assert adult_lif_slot(canada, 0)['jurisdiction'] == 'quebec', "jurisdiction preserved"

    def test_lif_withdrawals_begin_after_early_conversion(self):
        """After an early conversion, the mandatory LIF minimum fires."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000, lira_birth_year=1979,
            lira_jurisdiction='quebec', lira_conversion_year=2041,
        )
        _, state_2041 = _run_year(state, 2041, config)  # convert
        # Year after conversion: LIF is in decumulation -> withdrawal > 0.
        result, _ = _run_year(state_2041, 2042, config)
        assert result.lif_withdrawal > 0, (
            f"LIF minimum must fire after early conversion, got "
            f"{result.lif_withdrawal}")

    def test_age_71_backstop_fires_with_no_election(self):
        """Regression: the mandatory backstop still converts at 71 when no
        early election is made (byte-identical to the pre-#708 path)."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000, lira_birth_year=1979,
            lira_jurisdiction='quebec', lira_conversion_year=0,
        )
        # The backstop year (1979 + 71 = 2050).
        result, new_state = _run_year(state, 2050, config)
        canada = new_state.jurisdiction_state['canada']
        assert adult_lira_slot(canada, 0)['balance'] == 0, "backstop must convert at 71"
        assert adult_lif_slot(canada, 0)['balance'] > 0

    def test_federal_early_election_raises_loudly_not_silently(self):
        """An early-conversion election for an unsourced jurisdiction
        (federal) must fail loudly during the run, not silently guess."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000, lira_birth_year=1979,
            lira_jurisdiction='federal', lira_conversion_year=2044,
        )
        with pytest.raises(ValueError, match="not sourced"):
            _run_year(state, 2026, config)  # raises on the first LIRA year

    def test_federal_no_election_runs_normally(self):
        """Federal with NO early election is fine -- the sourced age-71
        backstop applies; nothing is guessed, no raise."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000, lira_birth_year=1979,
            lira_jurisdiction='federal', lira_conversion_year=0,
        )
        result, new_state = _run_year(state, 2040, config)
        canada = new_state.jurisdiction_state['canada']
        assert adult_lira_slot(canada, 0)['balance'] > 0  # still accumulating, no raise


# ---------------------------------------------------------------------------
# 4. Adapter: lira.conversion_date -> conversion_year
# ---------------------------------------------------------------------------

class TestAdapterConversionDate:
    """input_contract.to_internal_config threads conversion_date through."""

    def test_conversion_date_becomes_conversion_year(self):
        doc = copy.deepcopy(_base_doc())
        lira = next(a for a in doc["accounts"] if a["kind"] == "lira")
        lira["lira"]["conversion_date"] = "2044-06-01"
        legacy = ic.to_internal_config(doc)
        assert legacy["lira"]["conversion_year"] == 2044

    def test_absent_conversion_date_yields_none(self):
        legacy = ic.to_internal_config(copy.deepcopy(_base_doc()))
        assert legacy["lira"]["conversion_year"] is None

    def test_two_lira_accounts_disagreeing_on_conversion_date_are_refused(self):
        doc = copy.deepcopy(_base_doc())
        first = next(a for a in doc["accounts"] if a["kind"] == "lira")
        first["lira"]["conversion_date"] = "2044-06-01"
        second = copy.deepcopy(first)
        second["id"] = "p1_lira_second"
        second["balance"]["amount"] = 2000
        second["lira"]["conversion_date"] = "2050-01-01"  # disagrees
        doc["accounts"].append(second)
        with pytest.raises(contract_errors.ContractAdaptationError, match="conversion date"):
            ic.to_internal_config(doc)

    def test_two_lira_accounts_agreeing_on_conversion_date_sum_balances(self):
        doc = copy.deepcopy(_base_doc())
        first = next(a for a in doc["accounts"] if a["kind"] == "lira")
        first["lira"]["conversion_date"] = "2044-06-01"
        second = copy.deepcopy(first)
        second["id"] = "p1_lira_second"
        second["balance"]["amount"] = 2000
        # same owner (same birth year), same jurisdiction, same conversion_date
        doc["accounts"].append(second)
        legacy = ic.to_internal_config(doc)
        assert legacy["lira"]["conversion_year"] == 2044
        assert legacy["lira"]["balance"] == first["balance"]["amount"] + 2000


# ---------------------------------------------------------------------------
# 5. Schema accepts the optional conversion_date leaf
# ---------------------------------------------------------------------------

class TestSchemaAcceptsConversionDate:
    def test_validates_with_conversion_date(self):
        doc = copy.deepcopy(_base_doc())
        lira = next(a for a in doc["accounts"] if a["kind"] == "lira")
        lira["lira"]["conversion_date"] = "2044-06-01"
        contract_schema.validate_contract(doc)  # must not raise

    def test_validates_without_conversion_date(self):
        contract_schema.validate_contract(copy.deepcopy(_base_doc()))  # optional -> valid

# ---------------------------------------------------------------------------
# 6. Off-table year falls back to the nearest prescribed rate (not a crash,
#    not a silent 0). A LIF opened for an early-retirement horizon is priced
#    in years beyond the published prescribed-rate table, so the nearest-year
#    fallback in the Quebec prescribed-rate lookup must be exercised.
# ---------------------------------------------------------------------------

class TestOffTablePrescribedRateFallback:
    def test_quebec_under_55_maximum_uses_nearest_year_when_year_off_table(self):
        # 2050 is beyond the prescribed-rate table (2024-2026); a Quebec FRV
        # holder under 55 must still get a finite maximum computed from the
        # NEAREST available prescribed rate, not a KeyError or a 0.
        far = lif_maximum_withdrawal(
            100_000.0, 50, 2050, jurisdiction="quebec")
        near = lif_maximum_withdrawal(
            100_000.0, 50, 2026, jurisdiction="quebec")
        assert far > 0, "off-table Quebec under-55 maximum collapsed to 0"
        # The nearest available year IS 2026, so the off-table result equals it.
        assert far == near, (
            f"off-table year should reuse the nearest prescribed rate "
            f"(2026): got {far} vs {near}")
