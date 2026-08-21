#!/usr/bin/env python3
"""Issue #1002: the LIF maximum-withdrawal ceiling must cap the DISCRETIONARY draw.

Before #1002 the LIF statutory maximum-withdrawal ceiling (a per-jurisdiction
annual cap on TOTAL LIF withdrawals, PBSR s.20.1) was enforced on the FORCED
minimum path (``apply_lira_lif``: ``if lif_withdrawal > max_withdrawal:
lif_withdrawal = max_withdrawal``) but NOT on the DISCRETIONARY draw:
``plan_drawdown_net`` drew the ``lif`` token as a plain taxable source with no
``maximum_withdrawal`` cap. So a discretionary draw ordered against the LIF
could legally OVER-DRAW it -- the forced minimum took its statutory slice and
the discretionary draw on top could push the year's TOTAL LIF withdrawal past
the annual maximum.

The fix: ``apply_lira_lif`` stores the year's LIF statutory maximum
(``ws.lif_maximum_withdrawal``, computed on the same opening/converted fund the
forced minimum builds) and ``apply_retirement_drawdown`` passes
``max(0, lif_maximum_withdrawal - lif_withdrawal)`` -- the DISCRETIONARY room
left after the forced minimum -- as ``plan_drawdown_net``'s
``lif_max_withdrawal``. The ``lif`` token's gross draw is capped at that
ceiling; when it binds the residual shortfall falls through to the NEXT source
in ``drawdown_order`` (the waterfall continues -- it does not silently
under-deliver the net target).

These tests (DP#4/DP#15 -- fabricated round numbers, role-based names, no real
data) assert:

  1. Unit test (``plan_drawdown_net`` directly): a net target exceeding the LIF
     ceiling draws AT MOST the ceiling from the LIF, with the residual coming
     from the next source; money conserved (net delivered == net target when
     sources can cover it); the cap is a HARD ceiling, not a fallback (a 0.0
     ceiling caps the LIF draw at nothing and the whole need falls through).
  2. Integration test (full ``FamilySimulation``): a household whose drawdown
     order front-loads the LIF and whose net target exceeds the LIF maximum
     draws the LIF up to ``max - forced_minimum`` (so the forced minimum +
     discretionary draw equals the statutory maximum -- no over-draw), with the
     residual net need falling through to the next source (TFSA); the net
     target is still met (``net_delivered >= net_target``).

Run: .venv/bin/python -m pytest tests/test_issue_1002_lif_discretionary_ceiling.py -q
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings

import pytest

from countries.canada.retirement_transition import plan_drawdown_net
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from countries.canada.adapter import CanadaAdapter
from countries.canada.locked_in_account import LIF_CONVERSION_PROVIDER


# ────────────────────────────────────────────────────────────────────────────
# Unit tests: plan_drawdown_net caps the 'lif' token at lif_max_withdrawal
# ────────────────────────────────────────────────────────────────────────────

class TestPlanDrawdownNetLifCeiling:
    """``plan_drawdown_net``'s ``lif`` draw branch caps the discretionary LIF
    gross draw at ``lif_max_withdrawal`` and falls through to the next source."""

    def test_lif_draw_capped_at_ceiling_residual_falls_through(self):
        """Net target exceeds the LIF ceiling -> the LIF draw is capped at the
        ceiling (gross), and the residual net need is delivered by the NEXT
        source in drawdown_order (TFSA, tax-free). Money conserved: the after-
        tax proceeds meet the full net target."""
        # LIF balance ample ($500k), TFSA ample ($500k). Net need $50k.
        # LIF ceiling $20k gross -> the LIF delivers $20k gross, taxed at 40% =
        # $12k net; the residual $38k net comes from TFSA (tax-free, $1 = $1).
        canada = {'lif_balance': 500_000, 'tfsa_primary_balance': 500_000}
        plan = plan_drawdown_net(
            50_000, ['lif', 'tfsa'], canada, non_reg_balance=0, non_reg_acb=0,
            marginal_rate=0.40, lif_max_withdrawal=20_000)
        # The LIF gross draw is capped at the $20k ceiling (not the full $50k
        # net need grossed up, which pre-#1002 would have been ~$83.3k).
        lif_gross = -plan.balance_deltas.get('lif_balance', 0.0)
        assert lif_gross == pytest.approx(20_000, abs=0.01), (
            f"#1002: the discretionary LIF gross draw must be capped at the "
            f"lif_max_withdrawal ceiling (20_000), got {lif_gross!r}")
        # The residual net need fell through to TFSA: $50k net - $12k net from
        # the LIF (20k gross x (1 - 0.40)) = $38k from TFSA.
        tfsa_gross = -plan.balance_deltas.get('tfsa_primary_balance', 0.0)
        assert tfsa_gross == pytest.approx(38_000, abs=0.01), (
            f"#1002: the residual net need (38_000) must fall through to the "
            f"next source (TFSA), got TFSA draw {tfsa_gross!r}")
        # Money conserved: the after-tax proceeds meet the full net target.
        assert plan.net_delivered == pytest.approx(50_000, abs=0.01), (
            f"#1002: net delivered must meet the net target (50_000) with the "
            f"residual from the next source, got {plan.net_delivered!r}")

    def test_zero_ceiling_is_a_hard_zero_not_a_fallback(self):
        """DP#32: a 0.0 ceiling is a HARD zero (the LIF has no discretionary
        room this year -- the forced minimum already took the whole maximum),
        NOT a fallback that disables the cap. The LIF draw is capped at nothing
        and the ENTIRE net need falls through to the next source."""
        canada = {'lif_balance': 500_000, 'tfsa_primary_balance': 500_000}
        plan = plan_drawdown_net(
            50_000, ['lif', 'tfsa'], canada, non_reg_balance=0, non_reg_acb=0,
            marginal_rate=0.40, lif_max_withdrawal=0.0)
        # The LIF draw is capped at 0 (nothing drawn from the LIF).
        lif_gross = -plan.balance_deltas.get('lif_balance', 0.0)
        assert lif_gross == pytest.approx(0.0, abs=0.01), (
            f"#1002/DP#32: a 0.0 lif_max_withdrawal must cap the LIF draw at "
            f"nothing (the forced minimum took the whole maximum), got "
            f"{lif_gross!r} -- zero is a value, not a fallback")
        # The whole net need falls through to TFSA.
        tfsa_gross = -plan.balance_deltas.get('tfsa_primary_balance', 0.0)
        assert tfsa_gross == pytest.approx(50_000, abs=0.01), (
            f"#1002: with the LIF ceiling at 0 the entire net need must fall "
            f"through to the next source, got TFSA draw {tfsa_gross!r}")
        assert plan.net_delivered == pytest.approx(50_000, abs=0.01)

    def test_none_ceiling_disables_the_cap_preserving_pre_fix_path(self):
        """DP#13: ``lif_max_withdrawal=None`` (a direct caller not opting in)
        disables the cap entirely -- byte-identical to the pre-#1002 path. The
        LIF draws the whole net need (grossed up), no fall-through."""
        canada = {'lif_balance': 500_000, 'tfsa_primary_balance': 500_000}
        plan = plan_drawdown_net(
            50_000, ['lif', 'tfsa'], canada, non_reg_balance=0, non_reg_acb=0,
            marginal_rate=0.40, lif_max_withdrawal=None)
        # No cap -> the LIF funds the whole net need: $50k net / (1 - 0.40) gross.
        expected_lif_gross = 50_000 / 0.60
        lif_gross = -plan.balance_deltas.get('lif_balance', 0.0)
        assert lif_gross == pytest.approx(expected_lif_gross, abs=0.01), (
            f"#1002: lif_max_withdrawal=None must disable the cap (pre-#1002 "
            f"path) and let the LIF fund the whole need, got {lif_gross!r}")
        # TFSA untouched (no fall-through needed).
        tfsa_gross = -plan.balance_deltas.get('tfsa_primary_balance', 0.0)
        assert tfsa_gross == pytest.approx(0.0, abs=0.01)
        assert plan.net_delivered == pytest.approx(50_000, abs=0.01)

    def test_ceiling_does_not_bind_when_net_need_below_it(self):
        """When the net need is small enough that the LIF can cover it WITHOUT
        hitting the ceiling, the cap does not bind: the LIF draws only what is
        needed (grossed up), the next source is untouched."""
        # Net need $10k, ceiling $20k -> the LIF covers it (gross 10k/0.60).
        canada = {'lif_balance': 500_000, 'tfsa_primary_balance': 500_000}
        plan = plan_drawdown_net(
            10_000, ['lif', 'tfsa'], canada, non_reg_balance=0, non_reg_acb=0,
            marginal_rate=0.40, lif_max_withdrawal=20_000)
        expected_lif_gross = 10_000 / 0.60
        lif_gross = -plan.balance_deltas.get('lif_balance', 0.0)
        assert lif_gross == pytest.approx(expected_lif_gross, abs=0.01), (
            f"#1002: when the net need is below the ceiling the cap must not "
            f"bind (LIF draws only what's needed), got {lif_gross!r}")
        assert lif_gross < 20_000
        tfsa_gross = -plan.balance_deltas.get('tfsa_primary_balance', 0.0)
        assert tfsa_gross == pytest.approx(0.0, abs=0.01)
        assert plan.net_delivered == pytest.approx(10_000, abs=0.01)

    def test_ceiling_binds_and_no_next_source_surfaces_shortfall_honestly(self):
        """When the ceiling binds AND no later source can cover the residual,
        the draw does NOT silently under-deliver pretending it met the target:
        net_delivered < net_target, the shortfall surfaced honestly (DP#32)."""
        # LIF ceiling $20k gross, no TFSA / no other source to fall through to.
        canada = {'lif_balance': 500_000}
        plan = plan_drawdown_net(
            50_000, ['lif'], canada, non_reg_balance=0, non_reg_acb=0,
            marginal_rate=0.40, lif_max_withdrawal=20_000)
        lif_gross = -plan.balance_deltas.get('lif_balance', 0.0)
        assert lif_gross == pytest.approx(20_000, abs=0.01)
        # Only $12k net delivered (20k x 0.60) -- the $38k residual is a real
        # shortfall, NOT silently met.
        assert plan.net_delivered == pytest.approx(12_000, abs=0.01)
        assert plan.net_delivered < 50_000, (
            f"#1002/DP#32: when the ceiling binds and no source covers the "
            f"residual, the shortfall must surface honestly (net_delivered < "
            f"net_target), got net_delivered {plan.net_delivered!r}")


# ────────────────────────────────────────────────────────────────────────────
# Integration test: the full simulation enforces the ceiling end-to-end
# ────────────────────────────────────────────────────────────────────────────

def _lif_front_loaded_household():
    """A fabricated single retiree (DP#4/DP#15) whose LIRA converts to a LIF at
    the start of the projection (primary born 1950 -> age 76 in year 1, well
    past the age-71 conversion backstop and the age-65 retirement gate) and
    whose ``drawdown_order`` FRONT-LOADS the LIF (``['lif', 'tfsa']``).

    The LIF statutory maximum at age 76 (~8.38% of balance ~= $41.9k on a
    $500k converted balance) is far BELOW the net spending target (~$71k net),
    so the ceiling MUST bind: the discretionary LIF draw is capped and the
    residual net need falls through to TFSA. The forced LIF minimum (~5.82% =
    $29.1k) is taken first (apply_lira_lif), so the discretionary room is
    ``max - forced_min`` and the TOTAL LIF withdrawal (forced + discretionary)
    must equal the statutory maximum -- never over-draw."""
    return {
        'assumptions': {
            'projection_years': 2, 'investment_return': 0.0,
            'start_year': 2026, 'frozen_brackets': True,
            'salary_growth': 0.0, 'inflation': 0.0,
        },
        'property': {
            'house_value': 0, 'mortgage_balance': 0, 'margin_available': 0,
            'heloc_readvance': False,
        },
        'savings': {'rate': 0.0},
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1950, 'gross_income': 0,
                 'retirement_age': 65, 'rrsp_balance': 0, 'tfsa_balance': 200_000,
                 'cpp_monthly_estimated': 0, 'rrsp_room_accumulated': 0,
                 'tfsa_room_accumulated': 0},
            ],
            'children': [],
        },
        'lira': {'balance': 500_000, 'birth_year': 1950,
                 'jurisdiction': 'federal', 'reference_rate': 0.06},
        'retirement': {
            'spending_target': 80_000,
            'drawdown_order': ['lif', 'tfsa'],
        },
        'tax': {'province': 'ontario'},
    }


def _run(cfg_dict):
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()


def _lif_max_for_year(opening_lif_balance, birth_year, calendar_year,
                      jurisdiction='federal', reference_rate=0.06):
    """The statutory LIF maximum-withdrawal for the year on the opening balance
    -- the same primitive the forced-minimum path uses (DP#10:
    locked_in_account.py owns LIF rules)."""
    fund = LIF_CONVERSION_PROVIDER.make_lif_fund(
        balance=opening_lif_balance, owner_birth_year=birth_year,
        reference_rate=reference_rate, jurisdiction=jurisdiction)
    return fund.maximum_withdrawal(calendar_year)


def test_discretionary_lif_draw_capped_total_lif_never_exceeds_statutory_max():
    """In every year the LIF is drawn discretionarily, the TOTAL LIF withdrawal
    (forced minimum + discretionary draw) must not exceed the statutory
    maximum-withdrawal for that year -- the bug let the discretionary draw
    over-draw the LIF on top of the forced minimum."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        results = _run(_lif_front_loaded_household())

    bailed = False
    for r in results:
        # The YearResult does not expose the forced LIF minimum directly as a
        # standalone field; reconstruct the opening LIF balance from the prior
        # year's closing balance (investment_return=0 -> no growth, so the
        # opening equals the prior closing; year 1 opens at the converted
        # $500k since the LIRA->LIF conversion happens in year 1).
        if r.year == 1:
            opening_lif = 500_000.0
            # Conversion year: the forced-minimum block is gated on
            # opening_lif_balance > 0, which is 0 in the conversion year, so
            # NO forced minimum is taken (lif_withdrawal == 0) and the whole
            # statutory maximum is discretionary room.
            forced_min = 0.0
        else:
            # opening_lif == prior year's closing lif_balance (0% growth).
            opening_lif = results[r.year - 2].lif_balance
            forced_min = _forced_lif_minimum(opening_lif, 1950, 2025 + r.year)
        cal_year = 2025 + r.year
        max_total = _lif_max_for_year(opening_lif, 1950, cal_year)
        # The discretionary LIF draw is the LIF share of drawdown_taxable (no
        # RRIF in this household, so drawdown_taxable is entirely the LIF draw).
        discretionary_lif = r.drawdown_taxable
        total_lif = forced_min + discretionary_lif
        # The TOTAL LIF withdrawal must not exceed the statutory maximum.
        assert total_lif <= max_total + 1.0, (
            f"#1002: in year {r.year} (cal {cal_year}) the total LIF "
            f"withdrawal (forced min {forced_min:.2f} + discretionary "
            f"{discretionary_lif:.2f} = {total_lif:.2f}) must not exceed the "
            f"statutory maximum ({max_total:.2f}) -- the discretionary draw "
            f"over-drew the LIF")
        # And the discretionary draw alone is at most (max - forced_min).
        assert discretionary_lif <= (max_total - forced_min) + 1.0
        bailed = True
    assert bailed, "#1002: the test household never drew from the LIF discretionarily"


def _forced_lif_minimum(opening_lif_balance, birth_year, calendar_year,
                        jurisdiction='federal', reference_rate=0.06):
    """The forced LIF minimum for the year (the slice apply_lira_lif takes
    before the discretionary draw), capped at the maximum as the forced path
    does."""
    fund = LIF_CONVERSION_PROVIDER.make_lif_fund(
        balance=opening_lif_balance, owner_birth_year=birth_year,
        reference_rate=reference_rate, jurisdiction=jurisdiction)
    mn = fund.minimum_withdrawal(calendar_year)
    mx = fund.maximum_withdrawal(calendar_year)
    if mn > mx and mx > 0:
        mn = mx
    return mn


def test_residual_net_need_falls_through_to_next_source():
    """When the LIF ceiling binds, the residual net need must fall through to
    the NEXT source in drawdown_order (TFSA) -- the waterfall continues and
    does not silently under-deliver the net target. TFSA must be drawn in every
    year the LIF ceiling binds."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        results = _run(_lif_front_loaded_household())

    prev_tfsa = None
    fell_through_at_least_once = False
    for r in results:
        if prev_tfsa is not None:
            tfsa_delta = r.total_tfsa - prev_tfsa
            # TFSA is tax-free and earns 0% here (investment_return=0); any
            # material DROP is a draw (the residual falling through).
            if tfsa_delta < -1.0:
                fell_through_at_least_once = True
        prev_tfsa = r.total_tfsa
    assert fell_through_at_least_once, (
        "#1002: when the LIF ceiling binds the residual net need must fall "
        "through to the next source (TFSA drawn), but TFSA was never drawn")


def test_net_target_still_met_after_fall_through():
    """The fall-through must still MEET the net target (or surface the
    shortfall honestly): with TFSA ample ($200k) the residual is always
    coverable, so net_delivered >= net_target in every drawdown year."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        results = _run(_lif_front_loaded_household())
    for r in results:
        if r.drawdown_net_target > 0:
            assert r.drawdown_net_delivered >= r.drawdown_net_target - 1.0, (
                f"#1002: the residual fall-through must still meet the net "
                f"target in year {r.year}: delivered {r.drawdown_net_delivered:.2f} "
                f"< target {r.drawdown_net_target:.2f}")