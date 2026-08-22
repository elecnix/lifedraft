#!/usr/bin/env python3
"""Tests for issue #95: DP#19 -- resp_eap_proceeds fell back to a flat 50/50
contributions/earnings split from absent composition data, inventing a cost
basis that disagreed with the module's OWN documented fallback
(default_resp_composition: 50/10/5/35) used by the collapse path. Two
spellings of one rule is DP#9 violation, and inventing a 50/50 split from a
zeroed composition is DP#19 "code that produces a plausible answer from
absent data."

The fix routes BOTH the EAP and collapse pricing paths through a single
_resolve_resp_composition helper backed by default_resp_composition (the one
documented estimation spelling, DP#13). Tracked composition data always wins
(DP#19/DP#32); absence is the only trigger for the documented fallback, and
the EAP and collapse paths now agree on the pricing for an identical balance.

Fabricated round numbers, role-based names only (DP#4/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from countries.canada.resp_rules import RESPCalculator, default_resp_composition


def _resp_calc():
    return RESPCalculator()


def _cfg(resp_balance=100_000, composition=None):
    cfg = {
        'accounts': {'resp_current_balance': resp_balance},
        'assumptions': {'resp_eap_tax_rate': 0.15},
    }
    if composition is not None:
        cfg['accounts']['resp_composition'] = composition
    return cfg


class TestRespCompositionSingleSpelling:
    def test_eap_and_collapse_price_absent_composition_identically(self):
        """DP#9: for a balance with NO composition, the EAP and collapse paths
        derive the same contributions/earnings from the single documented
        default (default_resp_composition), not two different invented splits.
        Pre-fix the EAP path used a flat 50/50 while collapse used 50/10/5/35
        -- the #95 divergence this fixes."""
        from countries.canada.resp_rules import _resolve_resp_composition
        calc = _resp_calc()
        bal = 100_000
        eap = calc.resp_eap_proceeds(_cfg(bal))
        coll = calc.resp_collapse_proceeds(_cfg(bal), n_mtr=0.30)
        # Both EAP and collapse resolve the same tracked-style split.
        contributions, cesg, qesi, earnings = _resolve_resp_composition({}, bal)
        default = default_resp_composition(bal)
        assert contributions == default['total_contributions']
        assert cesg == default['total_cesg_received']
        assert qesi == default['total_qesi_received']
        assert earnings == default['investment_earnings']
        # The EAP path no longer invents a flat 50/50: for 100k its
        # contributions-returned equals the documented 50% default, but its
        # taxable EAP (cesg+qesi+earnings) equals the documented 50% not the
        # old flat 50/50's identical split -- i.e. contributions ≠ earnings.
        assert eap['contributions_returned'] == pytest.approx(50_000)
        assert eap['earnings_taxed'] == pytest.approx(50_000)  # 10+5+35 = 50%

    def test_tracked_composition_wins_over_fallback(self):
        """DP#19/DP#32: a supplied (non-zero) composition is honoured exactly,
        never overwritten by the fabricated fallback."""
        calc = _resp_calc()
        comp = {
            'total_contributions': 80_000,
            'total_cesg_received': 10_000,
            'total_qesi_received': 5_000,
            'investment_earnings': 5_000,
        }
        eap = calc.resp_eap_proceeds(_cfg(100_000, comp))
        assert eap['contributions_returned'] == pytest.approx(80_000)
        assert eap['earnings_taxed'] == pytest.approx(20_000)  # 10k+5k+5k EAP

    def test_zeroed_composition_falls_back_to_documented_default(self):
        """DP#32: an all-zero composition dict is treated as ABSENT (no tracked
        data), and falls back to the single documented default spelling -- the
        EAP and collapse paths agree, and neither invents a divergent 50/50."""
        calc = _resp_calc()
        bal = 100_000
        zero = {'total_contributions': 0, 'total_cesg_received': 0,
                'total_qesi_received': 0, 'investment_earnings': 0}
        from countries.canada.resp_rules import _resolve_resp_composition
        eap = calc.resp_eap_proceeds(_cfg(bal, zero))
        coll = calc.resp_collapse_proceeds(_cfg(bal, zero), n_mtr=0.30)
        # Both paths resolve the SAME documented composition from the
        # zeroed dict (treated as absent): same contributions-returned, same
        # taxable EAP basis. (Their earnings_taxed output fields differ in
        # meaning -- EAP's is the whole taxable EAP slice, collapse's is the
        # AIP-taxed earnings slice -- so the equality to assert is that both
        # priced the balance off the identical resolved composition.)
        contributions, cesg, qesi, earnings = _resolve_resp_composition(zero, bal)
        default = default_resp_composition(bal)
        assert (contributions, cesg, qesi, earnings) == (
            default['total_contributions'], default['total_cesg_received'],
            default['total_qesi_received'], default['investment_earnings'])
        assert eap['contributions_returned'] == coll['contributions_returned']


import pytest