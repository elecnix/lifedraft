"""Issue #1031 — ``compute_after_tax_estate`` prices the Smith-Manoeuvre
investment sleeve at death (the deemed disposition it was missing).

Pre-#1031 the estate calc subtracted the SM HELOC debt but IGNORED the
``sm_investment_balance`` asset it financed, and never priced that asset's
deemed-disposition capital-gains tax — so a leveraged household's estate was
understated by the whole SM portfolio net of its cap-gains tax, and
``min_after_tax_estate`` mis-ranked "leave a $5M SM portfolio untaxed" as a
near-zero estate (the die-with-zero "winner"). The fix: ``compute_estate`` now
prices the SM sleeve as a second capital-property pot mirroring the non-reg
pot (same ownership split + rollover, ITA s.70(5)), and ``_estate_call_args``
passes the terminal ``sm_investment_balance``/``sm_investment_cost_basis``.

Fabricated round numbers, role-based names (DP#4/DP#15). No personal data.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from objective import (
    MAX_AFTER_TAX_ESTATE, MIN_AFTER_TAX_ESTATE, compute_after_tax_estate,
)
from year_result import YearResult


# ── Helpers ─────────────────────────────────────────────────────────────────

def _yr(**kwargs) -> YearResult:
    """A single terminal YearResult with round, fabricated numbers (DP#4/#15)."""
    defaults = dict(
        primary_rrsp=0.0, spouse_rrsp=0.0, spousal_rrsp=0.0,
        total_tfsa=0.0, non_reg_balance=0.0, non_reg_acb=0.0,
        lif_balance=0.0, lira_balance=0.0,
        mortgage_balance=0.0, heloc_balance=0.0, total_debt=0.0,
        total_assets=0.0,
        sm_investment_balance=0.0, sm_investment_cost_basis=0.0,
        sm_heloc_balance=0.0,
    )
    defaults.update(kwargs)
    return YearResult(**defaults)


def _cfg(**kwargs):
    """A minimal estate cfg (the elections default via
    ``_UNDECLARED_ESTATE_DEFAULTS`` — spousal_rollover etc.). Province quebec,
    a $800k designated principal residence, no appreciation."""
    cfg = {
        'tax': {'province': 'quebec', 'start_year': 2026},
        'property': {'house_value': 800_000},
        'assumptions': {'start_year': 2026},
        'estate': {},
        'family': {'members': []},
    }
    cfg.update(kwargs)
    return cfg


# ============================================================================
# FIX 1 — the SM sleeve is priced in the estate.
# ============================================================================

def test_sm_sleeve_is_included_in_after_tax_estate_less_cap_gains_tax():
    """A household with an SM sleeve at death has its ``after_tax_estate``
    include the SM portfolio less its deemed-disposition capital-gains tax
    (and less the HELOC that financed it, which is already in ``debts``)."""
    # A terminal balance sheet mirroring the issue's repro shape: a $5.0M SM
    # portfolio (cost basis $500k -> $4.5M accrued gain), a $520k SM HELOC, a
    # paid-off $800k principal residence, no other financial accounts.
    sm_fmv, sm_acb, sm_heloc = 5_000_000.0, 500_000.0, 520_000.0
    final = _yr(
        sm_investment_balance=sm_fmv,
        sm_investment_cost_basis=sm_acb,
        sm_heloc_balance=sm_heloc,
        heloc_balance=sm_heloc,         # the SM HELOC is the household's debt
        total_debt=sm_heloc,
        total_assets=sm_fmv + 800_000,  # SM + house
    )
    estate = compute_after_tax_estate([final], _cfg())
    # The estate's taxable-investment component now carries the SM gross.
    assert estate.sm_investment_gross == sm_fmv
    # The cap-gains tax on the $4.5M accrued gain (50% inclusion) is priced --
    # strictly positive, not the $0 the pre-fix path booked (it ignored the
    # sleeve entirely).
    assert estate.sm_investment_tax > 0.0
    # The HELOC is subtracted as a debt (it always was); the SM asset is now
    # ADDED. Net effect: the estate is on the order of (house + SM - HELOC -
    # cap-gains tax), i.e. several million dollars -- NOT the ~$280k the
    # pre-fix path reported (house - HELOC, SM ignored).
    assert estate.net_estate > 3_000_000, (
        f"after_tax_estate {estate.net_estate:.0f} -- the SM portfolio is not "
        f"reaching the estate (pre-#1031 the $5M sleeve was ignored and the "
        f"estate was understated by the whole portfolio net of its tax)")


def test_sm_sleeve_cap_gains_tax_is_the_deemed_disposition_amount():
    """The SM tax is the capital-gains tax on (FMV - ACB) at the inclusion
    rate, stacked on the terminal return — mirroring the non-reg pot. With no
    other income, a $4.5M gain at 50% inclusion ($2.25M taxable) priced
    through Quebec's progressive brackets is a substantial, positive figure
    (and strictly more than the tax on a tiny gain)."""
    big_gain = compute_after_tax_estate(
        [_yr(sm_investment_balance=5_000_000, sm_investment_cost_basis=500_000,
             sm_heloc_balance=520_000, heloc_balance=520_000,
             total_debt=520_000, total_assets=5_800_000)],
        _cfg())
    small_gain = compute_after_tax_estate(
        [_yr(sm_investment_balance=5_000_000, sm_investment_cost_basis=4_900_000,
             sm_heloc_balance=520_000, heloc_balance=520_000,
             total_debt=520_000, total_assets=5_800_000)],
        _cfg())
    # Bigger accrued gain -> strictly more cap-gains tax.
    assert big_gain.sm_investment_tax > small_gain.sm_investment_tax
    # And a sleeve at cost (no gain) bears zero SM tax.
    no_gain = compute_after_tax_estate(
        [_yr(sm_investment_balance=5_000_000, sm_investment_cost_basis=5_000_000,
             sm_heloc_balance=520_000, heloc_balance=520_000,
             total_debt=520_000, total_assets=5_800_000)],
        _cfg())
    assert no_gain.sm_investment_tax == 0.0


def test_absent_sm_sleeve_is_byte_identical_to_pre_fix():
    """DP#32: a household with no SM sleeve (``sm_investment_balance`` == 0)
    has an estate byte-identical to the pre-#1031 path — the new SM pot is
    inert ($0 FMV -> $0 gross, $0 tax)."""
    no_sm = compute_after_tax_estate(
        [_yr(total_tfsa=100_000, non_reg_balance=200_000, non_reg_acb=150_000,
             total_assets=1_100_000, heloc_balance=30_000, total_debt=30_000)],
        _cfg())
    assert no_sm.sm_investment_gross == 0.0
    assert no_sm.sm_investment_tax == 0.0
    # And the non-reg pot is still priced (the SM pot did not displace it).
    assert no_sm.non_reg_gross == 200_000
    assert no_sm.non_reg_tax > 0.0


# ============================================================================
# min_after_tax_estate no longer mis-ranks an SM portfolio as a ~$0 estate.
# ============================================================================

def test_min_after_tax_estate_no_longer_ranks_sm_portfolio_as_near_zero():
    """Pre-#1031 ``min_after_tax_estate`` (the die-with-zero objective) picked
    the SM scenario as the "winner" precisely because it was BLIND to the
    largest asset — a $5M SM portfolio with a $520k HELOC scored ~$0 estate
    (HELOC subtracted, SM ignored). Post-fix the SM scenario's estate is in
    the millions, so ``min_after_tax_estate`` (which ranks the SMALLEST
    estate) no longer ranks it near zero — the spurious die-with-zero win is
    gone.

    ``MIN_AFTER_TAX_ESTATE.evaluate`` returns ``-net_estate`` (higher is
    better -> smaller estate), so the SM scenario now scores a large NEGATIVE
    (a big estate), not ~0.
    """
    sm_scenario = [_yr(
        sm_investment_balance=5_000_000, sm_investment_cost_basis=500_000,
        sm_heloc_balance=520_000, heloc_balance=520_000, total_debt=520_000,
        total_assets=5_800_000)]
    # A genuinely small-estate scenario (no SM, modest assets, same HELOC-free
    # baseline) — the kind of trajectory min_after_tax_estate SHOULD rank as
    # the die-with-zero winner.
    small_scenario = [_yr(
        total_tfsa=50_000, total_assets=850_000, total_debt=0.0)]

    sm_score = MIN_AFTER_TAX_ESTATE.evaluate(sm_scenario, _cfg())
    small_score = MIN_AFTER_TAX_ESTATE.evaluate(small_scenario, _cfg())
    sm_estate = -sm_score   # undo the negation (min objective: higher = smaller)

    # The SM scenario's estate is in the millions, not near zero (the bug).
    assert sm_estate > 3_000_000, (
        f"SM scenario estate {sm_estate:.0f} -- min_after_tax_estate still "
        f"ranks a $5M SM portfolio as a near-zero estate (the #1031 bug)")
    # And the genuinely small scenario ranks as the smaller estate (the
    # die-with-zero winner is now the truly small one, not the blinded SM one).
    assert small_score > sm_score, (
        "min_after_tax_estate should rank the small-estate scenario ABOVE the "
        "SM scenario (smaller estate = higher score); the SM portfolio no "
        "longer scores as ~$0")