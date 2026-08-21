#!/usr/bin/env python3
"""Issue #912 (follow-on to #641): per-account portfolio composition must reach
the growth path for FHSA / LIRA / LIF, not only rrsp/tfsa.

#641 wired the foreign-withholding-tax (WHT) drag of a registered pot's declared
holdings into rrsp/tfsa growth (``PortfolioConfig.registered_wht_drag`` ->
``RuleContext`` -> ``_blended_pot_rate``). FHSA, LIRA and LIF still grew at the
flat gross rate and ignored their declared composition -- separate flat-rate
paths ``_blended_pot_rate`` did not cover. This issue extends the SAME machinery
(no new return math) to those three pots so a declared foreign-equity mix on
them drags their return too.

The WHT regime reuses the existing rrsp/tfsa physics (DP#9 -- no new
``WHT_BY_ACCOUNT`` rows):
- an FHSA is a tax-free account with NO US-treaty exemption, so its foreign
  holdings leak exactly like a TFSA's;
- a LIRA/LIF is a locked-in retirement account that DOES carry the RRSP
  US-treaty exemption, so its foreign holdings leak exactly like an RRSP's.

These tests assert:
- ``registered_wht_drag`` now derives a per-kind drag for fhsa/lira/lif with the
  correct regime (fhsa == tfsa; lira == lif == rrsp; the rrsp-like < tfsa-like
  differential is the US-treaty advantage);
- the pure fold honours the drag on each of the three growth paths (a balance
  grows below the flat gross rate when it holds foreign assets -- DP#18: the
  composition reaches a key a rule reads);
- declaring foreign holdings on an FHSA moves the simulated household outcome;
- absence is a strict no-op (the golden household declares no fhsa/lira/lif
  composition, so its terminal total_assets is byte-identical).
"""

import copy
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from countries.canada.portfolio import PortfolioConfig
from test_golden_trajectory_581 import golden_household_config, _run


def _foreign_reg() -> dict:
    """A registered account holding foreign (US/intl) equity paying dividends.

    Foreign dividends attract withholding tax; the composition percentages are
    the declared allocation, the yield block is the income character the WHT
    physics reads.
    """
    return {
        'composition': {'us_equity_pct': 0.6, 'intl_equity_pct': 0.4},
        'yield': {'foreign_income': 0.02},
    }


class TestPortfolioWhtDrag:
    """PortfolioConfig now derives a per-kind WHT drag for fhsa/lira/lif too,
    each in the regime its holdings actually face (reusing the rrsp/tfsa
    physics)."""

    def test_all_three_pots_get_a_drag(self):
        pf = PortfolioConfig.from_dict({'accounts': {
            'fhsa': _foreign_reg(), 'lira': _foreign_reg(), 'lif': _foreign_reg(),
        }})
        drag = pf.registered_wht_drag()
        assert drag['fhsa'] > 0
        assert drag['lira'] > 0
        assert drag['lif'] > 0

    def test_fhsa_leaks_like_a_tfsa(self):
        """An FHSA has no US-treaty exemption -- its foreign holding leaks the
        same as a TFSA's."""
        pf = PortfolioConfig.from_dict({'accounts': {
            'fhsa': _foreign_reg(), 'tfsa': _foreign_reg(),
        }})
        drag = pf.registered_wht_drag()
        assert drag['fhsa'] == pytest.approx(drag['tfsa'])

    def test_lira_and_lif_leak_like_an_rrsp(self):
        """LIRA/LIF are locked-in retirement accounts that carry the RRSP
        US-treaty exemption -- their foreign holdings leak the same as an
        RRSP's, strictly less than a TFSA's."""
        pf = PortfolioConfig.from_dict({'accounts': {
            'rrsp': _foreign_reg(), 'lira': _foreign_reg(),
            'lif': _foreign_reg(), 'tfsa': _foreign_reg(),
        }})
        drag = pf.registered_wht_drag()
        assert drag['lira'] == pytest.approx(drag['rrsp'])
        assert drag['lif'] == pytest.approx(drag['rrsp'])
        assert drag['lira'] < drag['tfsa']

    def test_no_foreign_holding_no_drag(self):
        """A domestic/fixed-income-only pot has no WHT leak -- no entry, so it
        keeps the flat gross rate (no-op)."""
        pf = PortfolioConfig.from_dict({'accounts': {
            'fhsa': {'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                     'yield': {'eligible_dividends': 0.015, 'interest': 0.01}},
        }})
        assert 'fhsa' not in pf.registered_wht_drag()


class TestPureFoldHonoursWhtDrag:
    """simulate_year_pure grows the fhsa/lira/lif pots below the flat gross rate
    when a per-pot WHT drag is supplied -- and byte-identically when it is
    not."""

    def _run_year(self, registered_wht_drag, *, seed):
        from simulation_state import SimState, simulate_year_pure
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            investment_return=0.07,
            family_members=[
                {'role': 'primary', 'gross_income': 130000},
                {'role': 'spouse', 'gross_income': 50000},
            ],
        )
        state = SimState.initial(config)
        seed(state.jurisdiction_state['canada'])

        allocations = {'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
                       'primary_tfsa': 0, 'spouse_tfsa': 0, 'fhsa': 0,
                       'non_reg': 0, 'resp': 0,
                       '_primary_income': 130000, '_spouse_income': 50000,
                       '_annual_savings': 0}
        result, _ = simulate_year_pure(
            state=state, year=0, allocations=allocations, config=config,
            investment_return=0.07, registered_wht_drag=registered_wht_drag,
            use_readvanceable=False, mortgage_data={'end_balance': 0},
            # LIRA->LIF conversion and LIF factor lookups are date-computed from
            # birth_year, so a real calendar year is needed (an owner born 1980
            # is 46 -- pre-conversion; one born 1950 is 76 -- in decumulation).
            calendar_year=2026,
        )
        return result

    def test_fhsa_drag_reduces_growth(self):
        def seed(canada):
            canada['adult_fhsa']['primary'] = {
                'balance': 20000, 'room': 0, 'lifetime_used': 0.0,
                'lifetime_limit': 40000}

        flat = self._run_year(None, seed=seed)
        dragged = self._run_year({'fhsa': 0.003}, seed=seed)
        # Only the FHSA is funded, so the whole total_assets delta is its growth.
        assert flat.total_assets == pytest.approx(20000 * 1.07, abs=1)
        assert dragged.total_assets == pytest.approx(20000 * 1.067, abs=1)
        assert dragged.total_assets < flat.total_assets

    def test_lira_drag_reduces_growth(self):
        def seed(canada):
            # Owner age 46 in 2026 -> no LIF conversion this year; LIRA just grows.
            canada['adult_lira']['primary'].update(
                {'balance': 100000, 'birth_year': 1980,
                 'jurisdiction': 'federal', 'reference_rate': 0.06,
                 'conversion_year': 0})

        flat = self._run_year(None, seed=seed)
        dragged = self._run_year({'lira': 0.003}, seed=seed)
        assert flat.lira_balance == pytest.approx(100000 * 1.07, abs=1)
        assert dragged.lira_balance == pytest.approx(100000 * 1.067, abs=1)
        assert dragged.lira_balance < flat.lira_balance

    def test_lif_drag_reduces_growth(self):
        def seed(canada):
            # A converted LIF held by a 76-year-old: mandatory withdrawal then
            # growth. The withdrawal is identical either way; only the growth
            # rate differs, so the dragged closing balance is strictly lower.
            canada['adult_lif']['primary'].update(
                {'balance': 100000, 'birth_year': 1950,
                 'jurisdiction': 'federal', 'reference_rate': 0.06})

        flat = self._run_year(None, seed=seed)
        dragged = self._run_year({'lif': 0.003}, seed=seed)
        assert flat.lif_balance > 0
        assert dragged.lif_balance < flat.lif_balance

    def test_lif_defensive_branch_honours_drag(self):
        """The defensive path -- a LIF balance still coexisting with an
        unconverted LIRA (young owner) -- grows the LIF at the WHT-dragged rate
        too, not the flat one."""
        def seed(canada):
            # Owner age 46 -> LIRA does not convert, so new_lira_balance stays
            # > 0; the LIF's own withdrawal branch is skipped and the defensive
            # elif (opening_lif_balance > 0 and opening_lira_balance > 0) fires.
            canada['adult_lira']['primary'].update(
                {'balance': 80000, 'birth_year': 1980,
                 'jurisdiction': 'federal', 'reference_rate': 0.06,
                 'conversion_year': 0})
            canada['adult_lif']['primary'].update(
                {'balance': 50000, 'birth_year': 0,
                 'jurisdiction': 'federal', 'reference_rate': 0.06})

        flat = self._run_year(None, seed=seed)
        dragged = self._run_year({'lif': 0.003}, seed=seed)
        assert flat.lif_balance == pytest.approx(50000 * 1.07, abs=1)
        assert dragged.lif_balance == pytest.approx(50000 * 1.067, abs=1)
        assert dragged.lif_balance < flat.lif_balance


class TestBehaviouralOutcome:
    """DP#18: declaring foreign holdings on an FHSA must move the simulated
    household outcome, not merely populate a dead field."""

    def test_fhsa_foreign_holdings_lower_terminal_assets(self):
        # Give the household a funded FHSA; the two runs differ ONLY by whether
        # that FHSA declares a foreign-equity composition, so any terminal gap
        # is attributable solely to the WHT drag reaching the FHSA growth path.
        base_cfg = golden_household_config()
        base_cfg['family']['members'][0]['fhsa_balance'] = 50_000
        variant = copy.deepcopy(base_cfg)
        variant['portfolio']['accounts']['fhsa'] = _foreign_reg()

        base_terminal = _run(base_cfg)[-1].total_assets
        variant_terminal = _run(variant)[-1].total_assets

        assert variant_terminal < base_terminal


class TestAbsenceIsNoOp:
    """The golden household declares no fhsa/lira/lif composition; wiring their
    composition must not touch its numbers."""

    def test_golden_invariant_unchanged(self):
        assert _run(golden_household_config())[-1].total_assets == 9709753.139463063
