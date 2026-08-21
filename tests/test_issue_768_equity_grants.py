#!/usr/bin/env python3
"""Issue #768: record illiquid private-company equity grants, valued at $0 for
solvency/runway (DP#32 -- absence-of-record is not a labelled $0).

A private, unvested, or strike-undetermined option grant is NOT a liquid
asset: it cannot be sold, cannot pay a mortgage, and realises only on a
speculative, dated liquidity event outside the projection. An engine that
counted one toward runway would print exactly the confidently-wrong number
this repo exists to prevent. Pre-#768 the contract had no field for such a
grant -- it could not be miscounted, but the engine also could not state
"equity grant: present, valued $0, vests X, strike TBD." The absence of a
record is not the same as a labelled $0.

## What this tests (DP#4/DP#15: fabricated round numbers, role-based names)

1. The contract block is SCHEMA-VALID and LOADS (round-trips contract ->
   SimulationConfig -> contract).
2. DP#32 $0 guard: a contract WITH a declared grant has runway / solvency /
   liquid NW IDENTICAL to the same contract WITHOUT it -- the grant does not
   raise runway by a cent, by construction (no rule reads it).
3. The grant is SURFACED in output (TXT/JSON/HTML) as 'recorded, valued $0',
   not silently dropped.
4. strike=null surfaces as 'strike TBD' (never an invented value).
5. An owner that names no declared person is refused loudly (DP#32), not
   silently dropped.
"""

import json
import unittest
from datetime import date

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers
from runway import compute_runway
from simulation_config import SimulationConfig
from output_plugins import (
    JsonReport, TextReport, HtmlReport,
    _equity_grant_line, _equity_grants_summary,
)

# Sibling-import the #674 fixture (no `tests.` prefix per repo convention).
from test_issue_674_income_shocks import _job_loss_household, _run


def _grant(*, strike=None, owner='primary', gid='primary_options_2026'):
    """A fabricated private-company option grant (DP#15: round numbers)."""
    return {
        'id': gid, 'owner': owner, 'grantor': 'Acme Holdings',
        'grant_date': '2026-03-01',
        'vesting': {'cliff_date': '2027-03-01',
                    'fully_vested_date': '2030-03-01',
                    'schedule': '25% at 1-year cliff, then monthly over 36mo'},
        'strike': strike, 'liquidity': 'private',
        'shares': 10_000, 'fully_diluted_pct': 0.12,
        'notes': 'exercisable 7 years after vesting',
    }


class TestEquityGrantContractLoads(unittest.TestCase):
    """The block is schema-valid, loads, and round-trips (DP#24)."""

    def test_contract_with_grant_loads_and_round_trips(self):
        import input_contract as ic
        from test_input_contract import _load_example, _two_generation_subset
        doc = _two_generation_subset(_load_example())
        doc['equity_grants'] = [_grant(owner='p1')]
        legacy = ic.to_internal_config(doc)
        self.assertEqual(len(legacy['equity_grants']), 1)
        self.assertIsNone(legacy['equity_grants'][0]['strike'])  # null = TBD carried through
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(cfg.equity_grants[0]['owner'], 'p1')
        # Round-trip back out (DP#24): the block is re-emitted.
        out = cfg.to_dict()
        self.assertIn('equity_grants', out)
        self.assertIsNone(out['equity_grants'][0]['strike'])

    def test_contract_without_grant_round_trips_to_absent(self):
        import input_contract as ic
        from test_input_contract import _load_example, _two_generation_subset
        doc = _two_generation_subset(_load_example())
        # No equity_grants key -- the household declares none.
        legacy = ic.to_internal_config(doc)
        self.assertNotIn('equity_grants', legacy)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(cfg.equity_grants, [])
        self.assertNotIn('equity_grants', cfg.to_dict())  # absent, not fabricated

    def test_unknown_key_is_rejected(self):
        """additionalProperties:false -- a typo (equity_grantz) is a load error."""
        import jsonschema
        import input_contract as ic
        doc = json.loads(json.dumps(_load_example_for_schema()))
        doc['equity_grantz'] = []
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(doc, ic.compose_schema())

    def test_owner_that_names_no_person_is_refused(self):
        """DP#32: an owner typo is refused loudly, not a silently-dropped grant."""
        import input_contract as ic
        from test_input_contract import _load_example, _two_generation_subset
        doc = _two_generation_subset(_load_example())
        doc['equity_grants'] = [_grant(owner='nobody')]
        with self.assertRaises(ic.ContractAdaptationError) as cm:
            ic.to_internal_config(doc)
        msg = str(cm.exception).lower()
        self.assertIn('equity grant', msg)
        self.assertIn('nobody', msg)


def _load_example_for_schema():
    with open('schema/example.json') as f:
        return json.load(f)


class TestEquityGrantIsZeroForSolvencyAndRunway(unittest.TestCase):
    """The DP#32 guard: a declared grant must NOT raise runway/solvency/liquid
    NW by a cent. The grant is record-only -- no rule reads it, so the $0
    contribution is by construction, and this test pins that invariant."""

    def _both(self, ei_to=None):
        cfg_no = _job_loss_household(ei_to=ei_to)
        cfg_yes = _job_loss_household(ei_to=ei_to)
        cfg_yes.equity_grants = [_grant()]
        return _run(cfg_no), _run(cfg_yes)

    def test_ruin_and_shortfall_identical_with_and_without_grant(self):
        r_no, r_yes = self._both(ei_to=None)  # permanent EI shock -> engages solvency
        self.assertEqual([r.ruined for r in r_no], [r.ruined for r in r_yes])
        self.assertEqual([r.solvency_shortfall for r in r_no],
                         [r.solvency_shortfall for r in r_yes])

    def test_total_assets_and_liquid_nw_identical_with_and_without_grant(self):
        r_no, r_yes = self._both(ei_to=None)
        self.assertEqual([r.total_assets for r in r_no], [r.total_assets for r in r_yes])
        liquid_no = [r.total_assets - r.total_debt for r in r_no]
        liquid_yes = [r.total_assets - r.total_debt for r in r_yes]
        self.assertEqual(liquid_no, liquid_yes,
                         "an illiquid equity grant must not add a cent to liquid NW")

    def test_runway_identical_with_and_without_grant(self):
        r_no, r_yes = self._both(ei_to=None)
        rw_no = compute_runway(r_no, shock_date=date(2026, 1, 1), start_year=2026)
        rw_yes = compute_runway(r_yes, shock_date=date(2026, 1, 1), start_year=2026)
        self.assertEqual(rw_no.engaged, rw_yes.engaged)
        self.assertEqual(rw_no.runway_months, rw_yes.runway_months,
                         "a declared grant must not raise runway by a cent (#768/#758)")
        self.assertEqual(rw_no.runway_months_bracket, rw_yes.runway_months_bracket)

    def test_grant_with_a_strike_is_still_zero(self):
        """Even a grant WITH a declared strike is $0 for solvency (no liquid
        market, no dated liquidity event) -- a strike does not make it liquid."""
        cfg_no = _job_loss_household(ei_to=None)
        cfg_yes = _job_loss_household(ei_to=None)
        cfg_yes.equity_grants = [_grant(strike=2.50)]
        r_no, r_yes = _run(cfg_no), _run(cfg_yes)
        self.assertEqual([r.total_assets for r in r_no], [r.total_assets for r in r_yes])
        self.assertEqual([r.solvency_shortfall for r in r_no],
                         [r.solvency_shortfall for r in r_yes])


class TestEquityGrantIsSurfacedInOutput(unittest.TestCase):
    """The grant is surfaced as 'recorded, valued $0' -- not silently dropped."""

    def setUp(self):
        self.grant = _grant()
        self.cfg = {'equity_grants': [self.grant],
                    'family': {'members': [{'role': 'primary', 'gross_income': 1}]},
                    'property': {}, 'accounts': {}}

    def test_line_says_recorded_zero_with_strike_tbd(self):
        line = _equity_grant_line(self.grant)
        self.assertIn('Equity grant primary_options_2026', line)
        self.assertIn('valued $0 for solvency', line)
        self.assertIn('vests 2030-03-01', line)
        self.assertIn('strike TBD', line)

    def test_line_with_a_strike_shows_the_strike_not_tbd(self):
        g = _grant(strike=2.50)
        line = _equity_grant_line(g)
        self.assertIn('strike $2.50', line)
        self.assertNotIn('strike TBD', line)

    def test_text_report_surfaces_the_grant(self):
        out = TextReport([], self.cfg, title='T').render()
        self.assertIn('Equity grant primary_options_2026', out)
        self.assertIn('valued $0 for solvency', out)
        # A household with no grants gets no section (not a misleading empty header).
        no_grants = {'family': {'members': [{'role': 'primary', 'gross_income': 1}]},
                     'property': {}, 'accounts': {}}
        self.assertNotIn('Equity grants', TextReport([], no_grants, title='T').render())

    def test_json_report_carries_grants_with_zero_valuation(self):
        d = json.loads(JsonReport([], self.cfg, title='T').render())
        self.assertEqual(len(d['equity_grants']), 1)
        self.assertEqual(d['equity_grants'][0]['solvency_value'], 0.0)
        self.assertIsNone(d['equity_grants'][0]['strike'])
        # Empty list when the household declared none.
        no_grants = {'family': {'members': [{'role': 'primary', 'gross_income': 1}]},
                     'property': {}, 'accounts': {}}
        self.assertEqual(json.loads(JsonReport([], no_grants, title='T').render())['equity_grants'], [])

    def test_html_report_surfaces_the_grant(self):
        out = HtmlReport([], self.cfg, title='T').render()
        self.assertIn('Equity Grants', out)
        self.assertIn('primary_options_2026', out)
        self.assertIn('valued $0 for solvency', out)
        # A household with no grants gets no equity section.
        no_grants = {'family': {'members': [{'role': 'primary', 'gross_income': 1}]},
                     'property': {}, 'accounts': {}}
        self.assertNotIn('Equity Grants', HtmlReport([], no_grants, title='T').render())


if __name__ == '__main__':
    unittest.main()