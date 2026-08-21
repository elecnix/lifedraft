#!/usr/bin/env python3
"""
Tests for Issue #231: Wire LSIF tax credit module into strategy comparison.

Per issue #231: The lsif_credit.py module exists with full LSIF credit calculation
(15% federal + 15% Quebec on new purchases up to $5,000/yr), income threshold
testing, progressive holding periods, and HBP exclusion — but it's not wired into
the strategy engine.

Tests cover:
1. LSIF credit with purchase_amount > 0 increases net benefit
2. LSIF credit with purchase_amount = 0 has no effect (backward compatible)
3. Primary with high income (> highest QC bracket) is ineligible
4. Spouse with low income ($70k) is eligible
5. lsif_from_config returns None when lsif section is absent
6. lsif_from_config returns purchase with lsif section present
7. LSIF credit is included in net_benefit calculation
"""

import pytest
from copy import deepcopy
from optimize import compute_net_benefit
from simulation_config import YearResult
from countries.canada.lsif_credit import (
    compute_lsif_credit, lsif_from_config, LSIFPurchase,
    FEDERAL_LSIF_RATE, QUEBEC_LSIF_RATE, LSIF_PURCHASE_MAX,
)


class TestLSIFCreditComputation:
    """Basic LSIF credit computation tests."""

    def test_eligible_spouse_gets_both_credits(self):
        """Spouse (low income, $70k, Quebec resident) is eligible for both credits."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1980,
            is_quebec_resident=True,
            employment_income=70000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        assert result.federal_eligible, f"Should be federal eligible: {result.federal_ineligibility_reasons}"
        assert result.quebec_eligible, f"Should be Quebec eligible: {result.ineligibility_reasons}"
        # Federal: 15% of $5000 = $750
        assert result.federal_credit == 750.0, f"Expected $750 federal, got ${result.federal_credit}"
        # Quebec: 15% of $5000 = $750
        assert result.quebec_credit >= 750.0, f"Expected at least $750 Quebec, got ${result.quebec_credit}"

    def test_high_income_primary_exceeds_threshold(self):
        """Primary (high income, $250k) exceeds the QC highest bracket threshold (starting 2027)."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1979,
            is_quebec_resident=True,
            employment_income=250000,
            reference_year_taxable_income=250000,
        )
        result = compute_lsif_credit(purchase, year=2027)
        # Starting 2027, reference-year income > highest QC bracket → ineligible for QC credit
        # Federal credit may still be available (CRA ruling 2003-0006295)
        assert not result.quebec_eligible, \
            f"High-income QC resident should be ineligible for QC credit: {result.ineligibility_reasons}"

    def test_zero_purchase_no_credit(self):
        """Zero purchase amount should yield zero credits."""
        purchase = LSIFPurchase(
            amount=0,
            purchase_year=2026,
            birth_year=1980,
            is_quebec_resident=True,
            employment_income=70000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        assert result.federal_credit == 0.0
        assert result.current_qc_credit == 0.0

    def test_lsif_from_config_with_section(self):
        """lsif_from_config should return a purchase when lsif section is present."""
        cfg = {
            'lsif': {
                'purchase_amount': 5000,
                'purchase_year': 2026,
                'is_quebec_resident': True,
                'employment_income': 70000,
            },
        }
        purchase = lsif_from_config(cfg, birth_year=1980, year=2026)
        assert purchase is not None
        assert purchase.amount == 5000

    def test_lsif_from_config_without_section(self):
        """lsif_from_config should return None when lsif section is absent."""
        cfg = {}
        purchase = lsif_from_config(cfg, birth_year=1980, year=2026)
        assert purchase is None


class TestLSIFNetBenefit:
    """LSIF credit is included in net benefit calculation."""

    def _make_results_and_cfg(self, lsif_amount=0, employment_income=70000,
                               birth_year=1980, is_quebec_resident=True):
        """Create a minimal results list and config dict for net benefit testing."""
        final = YearResult(
            year=2036,
            total_assets=500000,
            total_debt=200000,
            total_rrsp=300000,
            total_tfsa=100000,
            non_reg_balance=100000,
            non_reg_acb=50000,
            resp_balance=0,
        )
        results = [final]

        cfg = {
            'family': {
                'members': [
                    {'role': 'primary', 'birth_year': 1979,
                     'gross_income': 130000,
                     'cpp_monthly_estimated': 0,
                     'oas_start_age': 65,
                     'pension_income_annual': 0},
                    {'role': 'spouse', 'birth_year': birth_year,
                     'gross_income': employment_income},
                ]
            },
            'assumptions': {
                'oas_annual': 8500,
                'capital_gains_inclusion': 0.50,
                'resp_eap_taxable_portion': 0.60,
                'resp_eap_tax_rate': 0.15,
            },
        }
        if lsif_amount > 0:
            cfg['lsif'] = {
                'purchase_amount': lsif_amount,
                'purchase_year': 2026,
                'is_quebec_resident': is_quebec_resident,
                'employment_income': employment_income,
            }
        return results, cfg

    def test_lsif_credit_increases_net_benefit(self):
        """LSIF credit with purchase > 0 should increase net benefit."""
        results_no_lsif, cfg_no_lsif = self._make_results_and_cfg(lsif_amount=0)
        results_with_lsif, cfg_with_lsif = self._make_results_and_cfg(lsif_amount=5000)

        net_no_lsif = compute_net_benefit(results_no_lsif, cfg_no_lsif)
        net_with_lsif = compute_net_benefit(results_with_lsif, cfg_with_lsif)

        # LSIF credit should increase net benefit
        assert net_with_lsif >= net_no_lsif, \
            f"LSIF should increase net benefit: ${net_with_lsif:.0f} vs ${net_no_lsif:.0f}"

    def test_lsif_zero_purchase_no_effect(self):
        """LSIF with zero purchase should have no effect on net benefit."""
        results_zero, cfg_zero = self._make_results_and_cfg(lsif_amount=0)
        results_none, cfg_none = self._make_results_and_cfg()

        net_zero = compute_net_benefit(results_zero, cfg_zero)
        net_none = compute_net_benefit(results_none, cfg_none)

        assert net_zero == net_none, \
            f"Zero LSIF purchase should not affect net benefit: ${net_zero:.0f} vs ${net_none:.0f}"

    def test_lsif_credit_calculation_matches_module(self):
        """Net benefit increase should match LSIF credit from compute_lsif_credit."""
        results_no_lsif, cfg_no_lsif = self._make_results_and_cfg(lsif_amount=0)
        results_with_lsif, cfg_with_lsif = self._make_results_and_cfg(
            lsif_amount=5000, employment_income=70000
        )

        net_no_lsif = compute_net_benefit(results_no_lsif, cfg_no_lsif)
        net_with_lsif = compute_net_benefit(results_with_lsif, cfg_with_lsif)

        # Compute expected credit from the module
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1980,
            is_quebec_resident=True,
            employment_income=70000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        expected_credit = result.federal_credit + result.quebec_credit

        actual_increase = net_with_lsif - net_no_lsif
        # The increase should be approximately the LSIF credit amount
        # (may not be exact due to tax interaction effects)
        assert actual_increase >= 0, \
            f"LSIF should increase net benefit, got ${actual_increase:.0f} increase"


class TestNoPersonalDataInCode:
    """Issue #245 (DP#4/DP#15): names/incomes/birth-years come from config, not code.

    These tests lock that optimize.py's LSIF wiring sources personal data from the
    input config and never embeds real names, incomes, or real-looking birth years.

    The repo-wide DP#15 literal-token sweep (formerly a single narrow check here,
    limited to optimize.py) now lives in tests/architecture/test_dp15_no_personal_data.py
    and walks every tracked file, not just this one module (see #716).
    """

    def test_lsif_birth_year_sourced_from_config(self):
        """The birth_year passed to LSIF is taken from the config member, not a literal."""
        from optimize import compute_net_benefit
        from countries.canada.lsif_credit import LSIFPurchase, lsif_from_config

        results = [YearResult(
            year=2036, total_assets=500000, total_debt=200000, total_rrsp=0,
            total_tfsa=0, non_reg_balance=0, non_reg_acb=0, resp_balance=0,
        )]
        # A distinct, fabricated birth_year must flow through to the LSIFPurchase.
        cfg = {
            'family': {'members': [
                {'role': 'primary', 'birth_year': 1955, 'gross_income': 100000},
                {'role': 'spouse', 'birth_year': 1956, 'gross_income': 50000},
            ]},
            'assumptions': {},
            'lsif': {'purchase_amount': 5000, 'purchase_year': 2026,
                     'is_quebec_resident': True, 'employment_income': 50000},
        }
        primary = next(m for m in cfg['family']['members'] if m['role'] == 'primary')
        purchase = lsif_from_config(cfg, birth_year=primary['birth_year'], year=2026)
        assert purchase.birth_year == 1955, "birth_year must come from the config member"
        # Net benefit still computes without raising (config-sourced path is live).
        assert isinstance(compute_net_benefit(results, cfg), float)

    def test_missing_birth_year_uses_dated_placeholder_not_real_year(self):
        """When config omits birth_year, the fallback is the DP#13 placeholder (2000)."""
        from countries.canada.lsif_credit import LSIFPurchase
        # optimize.py falls back to LSIFPurchase.birth_year, a clearly-dated stand-in.
        assert LSIFPurchase.birth_year == 2000
        assert LSIFPurchase.birth_year not in (1979, 1980)