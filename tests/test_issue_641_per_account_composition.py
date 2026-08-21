#!/usr/bin/env python3
"""Issue #641: per-account portfolio composition must reach the engine for
EVERY account kind, not just ``non_reg``.

Before this fix only ``portfolio.accounts.non_reg``'s declared holdings
influenced a balance's growth (via ``non_reg_after_tax_return``). A registered
account (rrsp/tfsa) declared its composition into a dataclass field that no
growth rule read, so an asset-location recommendation ("hold the US equity in
the RRSP, not the TFSA") produced *the same simulated number either way* -- the
blocker this issue describes.

The one tax that genuinely leaks from an otherwise tax-sheltered account is
foreign withholding tax (WHT): unrecoverable in a TFSA, treaty-exempt for US
equity in an RRSP (``income_type.WHT_BY_ACCOUNT``). So a registered account's
in-account growth rate is ``gross - wht_drag(its holdings, its kind)`` -- the
canonical asset-location result the model previously could not express.

These tests assert:
- the per-account WHT drag is derived from each account's OWN holdings and kind
  (RRSP < TFSA for the same foreign holding -- the US-treaty advantage);
- the pure fold honours that drag (a registered balance grows below the flat
  gross rate when it holds foreign assets);
- declaring foreign holdings on registered accounts changes the simulated
  household OUTCOME (DP#18: it reaches a key a rule reads, not a dead leaf);
- absence is a strict no-op (the golden household declares no registered
  composition, so its terminal total_assets is byte-identical).
"""

import copy
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from countries.canada.portfolio import PortfolioConfig
from test_golden_trajectory_581 import golden_household_config, _run, GROSS_RETURN


def _foreign_reg(pct_foreign_yield: float = 0.02) -> dict:
    """A registered account holding foreign (US/intl) equity paying dividends.

    Foreign dividends attract withholding tax; the composition percentages are
    the declared allocation, the yield block is the income character the WHT
    physics reads.
    """
    return {
        'composition': {'us_equity_pct': 0.6, 'intl_equity_pct': 0.4},
        'yield': {'foreign_income': pct_foreign_yield},
    }


class TestPortfolioWhtDrag:
    """PortfolioConfig derives a per-registered-kind WHT drag from each
    account's OWN holdings and kind (not one global rate)."""

    def test_rrsp_drag_below_tfsa_for_same_foreign_holding(self):
        """The same foreign holding leaks LESS in an RRSP than a TFSA: US
        equity is treaty-exempt in an RRSP but fully withheld in a TFSA. This
        differential IS asset location -- the thing #641 unblocks."""
        pf = PortfolioConfig.from_dict({'accounts': {
            'rrsp': _foreign_reg(), 'tfsa': _foreign_reg(),
        }})
        drag = pf.registered_wht_drag()
        assert drag['rrsp'] > 0
        assert drag['tfsa'] > 0
        assert drag['rrsp'] < drag['tfsa']

    def test_no_foreign_holding_no_drag(self):
        """A registered account with only domestic/fixed-income holdings has no
        WHT leak -- it grows exactly at the flat gross rate (no-op)."""
        pf = PortfolioConfig.from_dict({'accounts': {
            'rrsp': {'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                     'yield': {'eligible_dividends': 0.015, 'interest': 0.01}},
        }})
        assert 'rrsp' not in pf.registered_wht_drag()

    def test_non_reg_not_in_registered_drag(self):
        """non_reg WHT is recoverable via the foreign tax credit and is already
        handled by the non_reg after-tax path -- it must not be double-counted
        in the registered drag."""
        pf = PortfolioConfig.from_dict({'accounts': {'non_reg': _foreign_reg()}})
        assert 'non_reg' not in pf.registered_wht_drag()


class TestPureFoldHonoursWhtDrag:
    """simulate_year_pure grows a registered pot below the flat gross rate when
    a per-pot WHT drag is supplied -- and byte-identically when it is not."""

    def _run_year(self, registered_wht_drag):
        from simulation_state import SimState, simulate_year_pure
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            investment_return=0.07,
            family_members=[
                {'role': 'primary', 'gross_income': 130000,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
                {'role': 'spouse', 'gross_income': 50000,
                 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 30000},
            ],
        )
        state = SimState.initial(config)
        state.jurisdiction_state['canada']['adult_rrsp']['primary']['own'] = 50000
        state.jurisdiction_state['canada']['adult_tfsa']['primary']['balance'] = 30000

        allocations = {'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
                       'primary_tfsa': 0, 'spouse_tfsa': 0, 'fhsa': 0,
                       'non_reg': 0, 'resp': 0,
                       '_primary_income': 130000, '_spouse_income': 50000,
                       '_annual_savings': 0}
        result, _ = simulate_year_pure(
            state=state, year=0, allocations=allocations, config=config,
            investment_return=0.07, registered_wht_drag=registered_wht_drag,
            use_readvanceable=False, mortgage_data={'end_balance': 0},
        )
        return result

    def test_wht_drag_reduces_registered_growth(self):
        flat = self._run_year(None)
        dragged = self._run_year({'rrsp': 0.003, 'tfsa': 0.003})

        # Flat: gross 7% (today's behaviour, absence is a no-op).
        assert flat.primary_rrsp == pytest.approx(50000 * 1.07, abs=1)
        assert flat.primary_tfsa == pytest.approx(30000 * 1.07, abs=1)
        # Dragged: 7% - 0.3% = 6.7% -- the balance reflects the holdings.
        assert dragged.primary_rrsp == pytest.approx(50000 * 1.067, abs=1)
        assert dragged.primary_tfsa == pytest.approx(30000 * 1.067, abs=1)
        assert dragged.primary_rrsp < flat.primary_rrsp
        assert dragged.primary_tfsa < flat.primary_tfsa


class TestBehaviouralOutcome:
    """DP#18: declaring foreign holdings on registered accounts must move the
    simulated household outcome, not merely populate a dead field."""

    def test_registered_foreign_holdings_lower_terminal_assets(self):
        base_cfg = golden_household_config()
        variant = copy.deepcopy(base_cfg)
        variant['portfolio']['accounts']['rrsp'] = _foreign_reg()
        variant['portfolio']['accounts']['tfsa'] = _foreign_reg()

        base_terminal = _run(base_cfg)[-1].total_assets
        variant_terminal = _run(variant)[-1].total_assets

        # WHT drag compounds over the horizon: the household that holds
        # unsheltered-from-WHT foreign equity in its registered accounts ends
        # strictly poorer. Composition reached the engine.
        assert variant_terminal < base_terminal


class TestAbsenceIsNoOp:
    """The golden household declares composition only on non_reg; wiring
    registered composition must not touch its numbers."""

    def test_golden_invariant_unchanged(self):
        assert _run(golden_household_config())[-1].total_assets == 9709753.139463063
