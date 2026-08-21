#!/usr/bin/env python3
"""Issue #891: a DECLARED over-limit refinance option must refuse-and-skip, not
crash the whole optimizer run.

``run_ltv_exploration`` sweeps the household's declared
``decisions.mortgage.refinance_options`` (#846) unioned over the LTV ladder
(#853). One declared option whose ``cash_out`` pushes secured debt past the 80%
charge limit used to raise ``ChargeLimitExceededError`` UNCAUGHT out of
``run_optimization`` -> ``build_overlay_config`` -> ``apply_overlay``, aborting
the ENTIRE run -- even though every other candidate (the feasible ladder rungs
and any within-limit declared option) is perfectly simulable.

The structure cross (``_compose_structure_cell`` / ``_print_structure_refusals``)
already handles this exact infeasibility gracefully: it records a refusal cell
with the reason IN WORDS and reports it, absent from the scored tables (DP#32/
#681). This is the same treatment for the LTV EXPLORATION table: an over-limit
declared option is REFUSED-and-SKIPPED (recorded, reason in words, absent from
the feasible scored rows), not fatal.

All data fabricated with round numbers -- no personal data (DP#4/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import optimize


def _cfg(refinance_options):
    """A fabricated readvanceable household. house_value 800k => 80% charge
    limit 640k; current mortgage 300k. A declared cash_out of 500k books an
    800k mortgage (100% LTV) -- refused; a cash_out of 100k books 400k (50%) --
    feasible."""
    return {
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': 150000,
                 'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 40000,
                 'fhsa_first_time_buyer_since': None, 'fhsa_room_accumulated': 0},
                {'role': 'spouse', 'gross_income': 70000,
                 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 40000,
                 'fhsa_first_time_buyer_since': None, 'fhsa_room_accumulated': 0},
            ],
            'children': [],
        },
        'property': {
            'house_value': 800000, 'mortgage_balance': 300000,
            'mortgage_rate': 0.05, 'margin_available': 50000, 'ltv_max': 0.80,
            'heloc_readvance': True, 'heloc_rate': 0.06,
            'refinance_amortization_years': 25,
        },
        'accounts': {'resp_current_balance': 0},
        'assumptions': {'investment_return': 0.07},
        'scenarios': {'refinance': refinance_options},
    }


class TestOverLimitRefinanceRefuseAndSkip:
    def test_over_limit_declared_option_does_not_crash_the_run(self):
        """The reproduction from #891: a declared option whose cash-out breaches
        the charge limit no longer aborts the whole sweep -- it completes."""
        cfg = _cfg([
            {'id': 'ok', 'label': 'Modest cash-out', 'cash_out': 100000},
            {'id': 'over', 'label': 'Over-limit advance', 'cash_out': 500000},
        ])
        # Must NOT raise ChargeLimitExceededError (or anything else).
        results = optimize.run_ltv_exploration(cfg)
        assert results, "the sweep produced no rows at all"

    def test_feasible_candidates_are_still_scored(self):
        """Refusing the over-limit option does not disturb the feasible ones:
        the ladder rungs and the within-limit declared option are scored."""
        cfg = _cfg([
            {'id': 'ok', 'label': 'Modest cash-out', 'cash_out': 100000},
            {'id': 'over', 'label': 'Over-limit advance', 'cash_out': 500000},
        ])
        results = optimize.run_ltv_exploration(cfg)
        scored = [r for r in results if not r.get('refinance_refused')]
        assert scored, "no feasible rows scored"
        # every scored row carries a real net_benefit number
        assert all('net_benefit' in r for r in scored)
        # the within-limit declared option (50% LTV rung) is marked ★ in situ
        assert any(r.get('refinance_declared_id') == 'ok' for r in scored)

    def test_over_limit_option_is_recorded_as_refused_not_scored(self):
        """DP#32/#681: the refused option is present with its reason IN WORDS
        and ABSENT from the feasible scored rows -- never silently dropped, and
        never ranked as a merely-poor option."""
        cfg = _cfg([
            {'id': 'ok', 'label': 'Modest cash-out', 'cash_out': 100000},
            {'id': 'over', 'label': 'Over-limit advance', 'cash_out': 500000},
        ])
        results = optimize.run_ltv_exploration(cfg)
        refused = [r for r in results if r.get('refinance_refused')]
        assert len(refused) == 1
        row = refused[0]
        assert row['refinance_id'] == 'over'
        assert 'ChargeLimitExceededError' in row['refinance_refusal']
        # a refused candidate carries no scored net_benefit -- it is not a
        # simulated data point.
        assert 'net_benefit' not in row
        # and it is absent from the feasible set (#891's "absent from feasible
        # results").
        scored = [r for r in results if not r.get('refinance_refused')]
        assert all(r.get('refinance_id') != 'over' for r in scored)

    def test_no_over_limit_declaration_produces_no_refusal_rows(self):
        """Absence-is-a-no-op (DP#13): a household whose declared options are all
        within limit gets exactly the pre-#891 result -- zero refused rows."""
        cfg = _cfg([
            {'id': 'ok', 'label': 'Modest cash-out', 'cash_out': 100000},
        ])
        results = optimize.run_ltv_exploration(cfg)
        assert results
        assert not any(r.get('refinance_refused') for r in results)


class TestOverLimitRefinanceReported:
    def test_print_ltv_exploration_names_the_refused_option(self, capsys):
        """The refusal is surfaced LOUDLY (like the structure cross's NOT SCORED
        notice), not swallowed -- the reader must not read its absence from the
        tables as a poor ranking."""
        cfg = _cfg([
            {'id': 'ok', 'label': 'Modest cash-out', 'cash_out': 100000},
            {'id': 'over', 'label': 'Over-limit advance', 'cash_out': 500000},
        ])
        results = optimize.run_ltv_exploration(cfg)
        optimize._print_ltv_exploration(results)
        out = capsys.readouterr().out
        assert 'Over-limit advance' in out
        assert 'REFUSED' in out or 'NOT SCORED' in out
