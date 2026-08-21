#!/usr/bin/env python3
"""Unit tests for output plugins (DP#8: compose through data)."""

import unittest
from output_plugins import (
    OutputFormat, TextReport, JsonReport, HtmlReport, MarkdownReport,
    create_report, write_report, write_year_by_year_csv,
)


def _fake_year_by_year(n=10):
    """Fabricated per-year series (DP#4/DP#15: round numbers, no personal data)."""
    series = []
    for y in range(1, n + 1):
        series.append({
            'year': y,
            'mortgage_payment': 12000,
            'mortgage_interest': 4000 - y * 100,
            'mortgage_principal': 8000 + y * 100,
            'mortgage_balance': 200000 - y * 8000,
            'heloc_balance': 50000,
            'sm_qc_deductible': 1000 + y,
            'rrsp_tax_savings': 5000,
            'readvance_tax_savings': 500,
            'primary_marginal': 0.50,
            'total_family_income': 300000,
            'annual_savings': 60000,
            'contributions': {'primary_rrsp': 10000, 'primary_tfsa': 7000},
            'total_assets': 400000 + y * 50000,
            'total_debt': 250000 - y * 8000,
        })
    return series


class TestOutputPlugins(unittest.TestCase):
    """Test that all three output plugins produce valid output."""

    def setUp(self):
        self.results = [
            {'label': 'No Refinance | Yes (Readvanceable) | No', 'cash_out': 0, 'resp_cash_out': 0,
             'readvanceable_mortgage': True, 'deduct_later': False, 'ltv': 0.13,
             'net_benefit': 1500000, 'future_value': 1600000, 'total_debt': 300000},
            {'label': 'Maximum Refinance (80%) | Yes (Readvanceable) | No', 'cash_out': 500000, 'resp_cash_out': 0,
             'readvanceable_mortgage': True, 'deduct_later': False, 'ltv': 0.80,
             'net_benefit': 1400000, 'future_value': 3000000, 'total_debt': 1600000},
        ]
        self.cfg = {
            'family': {'members': [
                {'role': 'primary', 'gross_income': 160000, 'rrsp_room_accumulated': 180000, 'tfsa_room_accumulated': 35000},
                {'role': 'spouse', 'gross_income': 70000, 'rrsp_room_accumulated': 110000, 'tfsa_room_accumulated': 35000},
            ]},
            'property': {'house_value': 800000, 'mortgage_balance': 100000, 'margin_available': 250000},
            'accounts': {'resp_current_balance': 130000},
            'assumptions': {'investment_return': 0.07, 'resp_eap_taxable_portion': 0.60, 'resp_eap_tax_rate': 0.15},
        }

    def test_text_report_contains_key_data(self):
        report = TextReport(self.results, self.cfg, title="Test Report")
        output = report.render()
        self.assertIn("Test Report", output)
        self.assertIn("No Refinance | Yes (Readvanceable) | No", output)
        # Issue #789: the "Best per category" label is DERIVED from the row's
        # own cash_out / readvanceable_mortgage / deduct_later (DP#9: one
        # source of truth), so it uses the derived space-joined form -- never
        # a pass-through of a stale row label that can diverge from the data.
        self.assertIn("Maximum Refinance (80%) Yes (Readvanceable) No", output)
        self.assertIn("Situation: Primary $160,000", output)

    def test_json_report_is_valid_json(self):
        import json
        report = JsonReport(self.results, self.cfg, title="Test Report")
        output = report.render()
        data = json.loads(output)
        self.assertEqual(data['title'], "Test Report")
        self.assertEqual(len(data['scenarios']), 2)
        self.assertIn('situation', data)
        self.assertEqual(data['situation']['primary_income'], 160000)

    def test_html_report_contains_tags(self):
        report = HtmlReport(self.results, self.cfg, title="Test Report")
        output = report.render()
        self.assertIn("<!DOCTYPE html>", output)
        self.assertIn("<title>Test Report</title>", output)
        self.assertIn("No Refinance | Yes (Readvanceable) | No", output)
        self.assertIn("Maximum Refinance (80%) | Yes (Readvanceable) | No", output)
        # Table structure
        self.assertIn("<table>", output)
        self.assertIn("Net Benefit", output)

    def test_factory_creates_correct_type(self):
        text = create_report(OutputFormat.TEXT, self.results, self.cfg)
        self.assertIsInstance(text, TextReport)
        json_r = create_report(OutputFormat.JSON, self.results, self.cfg)
        self.assertIsInstance(json_r, JsonReport)
        html = create_report(OutputFormat.HTML, self.results, self.cfg)
        self.assertIsInstance(html, HtmlReport)
        md = create_report(OutputFormat.MARKDOWN, self.results, self.cfg)
        self.assertIsInstance(md, MarkdownReport)

    def test_markdown_report_contains_key_headers(self):
        """Issue #814: the Markdown report renders without error and carries
        the key section headers and the derived category label (DP#9) as clean
        GitHub-flavored Markdown headings and tables."""
        output = MarkdownReport(self.results, self.cfg, title="Test Report").render()
        self.assertIn("# Test Report", output)
        self.assertIn("## Situation Summary", output)
        self.assertIn("## Model Fidelity", output)
        self.assertIn("## Best Per Category", output)
        self.assertIn("## Optimal Refinance Level", output)
        self.assertIn("## Top 2 Scenarios", output)
        # GFM table structure (header separator row) is present.
        self.assertIn("| --- |", output)
        # The scenario label reaches the table verbatim.
        self.assertIn("No Refinance | Yes (Readvanceable) | No", output)
        # The "Best per category" label is DERIVED (issue #789 / DP#9), so it
        # uses the space-joined form, not a pass-through of a stale row label.
        self.assertIn("Maximum Refinance (80%) Yes (Readvanceable) No", output)

    def test_markdown_renders_year_by_year_table(self):
        """The #1 scenario's per-year series renders as a Markdown table when
        present (issue #248) — thin reuse of the shared YEAR_GROUPS columns."""
        results, n = self._results_with_series()
        output = MarkdownReport(results, self.cfg, title="YBY").render()
        self.assertIn("## Year-by-Year Breakdown", output)
        self.assertIn("### Balances", output)

    def test_markdown_write_to_file(self):
        """write_report(fmt=MARKDOWN, ...) writes a Markdown file (issue #814)."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix='.md', delete=False) as f:
            path = f.name
        try:
            write_report(OutputFormat.MARKDOWN, self.results, self.cfg, path,
                         "Write Test")
            with open(path) as f:
                content = f.read()
            self.assertIn("# Write Test", content)
            self.assertIn("## Situation Summary", content)
        finally:
            os.unlink(path)

    def test_unknown_format_raises_value_error(self):
        """An unrecognised format is a loud ValueError, never a silent no-op
        or a favourable default (DP#32)."""
        with self.assertRaises(ValueError):
            create_report("no-such-format", self.results, self.cfg)

    def test_write_to_file(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix='.html', delete=False) as f:
            path = f.name
        try:
            write_report(OutputFormat.HTML, self.results, self.cfg, path, "Write Test")
            with open(path) as f:
                content = f.read()
            self.assertIn("Write Test", content)
            self.assertIn("<!DOCTYPE html>", content)
        finally:
            os.unlink(path)

    # ── Year-by-year breakdown (issue #248) ──────────────────────────────

    def _results_with_series(self, n=10):
        results = [dict(r) for r in self.results]
        # Attach the per-year series to the top-ranked (#1) scenario.
        results[0]['year_by_year'] = _fake_year_by_year(n)
        return results, n

    def test_json_includes_year_by_year_for_top_scenario(self):
        import json
        results, n = self._results_with_series()
        report = JsonReport(results, self.cfg, title="YBY")
        data = json.loads(report.render())
        # Top-level convenience array == projection years.
        self.assertEqual(len(data['year_by_year']), n)
        # The #1 scenario carries its own series too.
        top = sorted(data['scenarios'], key=lambda r: r['net_benefit'], reverse=True)[0]
        self.assertIn('year_by_year', top)
        self.assertEqual(len(top['year_by_year']), n)
        # All expected concern-columns are present.
        keys = top['year_by_year'][0].keys()
        for k in ['mortgage_payment', 'mortgage_interest', 'mortgage_principal',
                  'mortgage_balance', 'primary_marginal', 'rrsp_tax_savings',
                  'readvance_tax_savings', 'sm_qc_deductible', 'total_family_income',
                  'annual_savings', 'contributions', 'total_assets', 'total_debt']:
            self.assertIn(k, keys)

    def test_html_renders_year_by_year_table(self):
        results, n = self._results_with_series()
        output = HtmlReport(results, self.cfg, title="YBY").render()
        self.assertIn("Year-by-Year Breakdown", output)
        # One <tr> per year in the breakdown table (plus header rows elsewhere).
        self.assertGreaterEqual(output.count("<tr>"), n)

    def test_html_year_by_year_embeds_rich_data_and_tabs(self):
        """Issue #239 follow-up: HTML surfaces the full richness of year_by_year.

        The card embeds the per-year series for the top scenarios as JSON,
        exposes a scenario selector, and renders a tab per concern group so
        every YearResult field is reachable without external dependencies.
        """
        results, n = self._results_with_series()
        output = HtmlReport(results, self.cfg, title="YBY").render()
        # Embedded JSON payload for the JS layer.
        self.assertIn('id="yby-data"', output)
        # Scenario selector + at least the four concern tabs.
        self.assertIn('id="yby-scenario"', output)
        for group in ("Balances", "Contributions", "Taxes & SM", "Mortgage & Cash Flow"):
            self.assertIn(f'data-group="{group}"', output)
        # The embedded payload parses and carries the #1 scenario's series.
        import json as _json, re as _re
        m = _re.search(r'<script type="application/json" id="yby-data">(.*?)</script>',
                       output, _re.DOTALL)
        self.assertIsNotNone(m, "embedded yby-data script not found")
        payload = _json.loads(m.group(1))
        self.assertGreaterEqual(len(payload), 1)
        self.assertEqual(len(payload[0]['year_by_year']), n)
        # The server-side fallback (Balances) renders a Net Worth column.
        self.assertIn("Net Worth", output)

    def test_html_renders_smith_manoeuvre_section_server_side(self):
        """Issue #239: when the #1 scenario uses the Smith Manoeuvre, its SM
        values render server-side (no JS / no tab click) and are non-zero."""
        results, _ = self._results_with_series()
        output = HtmlReport(results, self.cfg, title="SM").render()
        self.assertIn('id="yby-sm-table"', output)
        # The fixture's #1 scenario has non-zero SM fields, so the rendered
        # section must show them (not blank/$0). SM Tax Savings = $500/yr.
        import re as _re
        m = _re.search(r'<table id="yby-sm-table">(.*?)</table>', output, _re.DOTALL)
        self.assertIsNotNone(m, "server-side SM table not found")
        sm_table = m.group(1)
        self.assertIn("SM Tax Savings", sm_table)
        self.assertIn("$500", sm_table)  # readvance_tax_savings, non-zero

    def test_html_omits_smith_manoeuvre_section_when_inactive(self):
        """The server-side SM section is absent when no SM field is non-zero,
        so non-readvanceable scenarios don't show an empty SM table."""
        results, _ = self._results_with_series()
        # Zero out every SM field in the #1 scenario's series.
        for yr in results[0]['year_by_year']:
            yr['readvance_tax_savings'] = 0
            yr['sm_qc_deductible'] = 0
        output = HtmlReport(results, self.cfg, title="No SM").render()
        self.assertNotIn('id="yby-sm-table"', output)

    def test_yby_card_explicit_empty_label_not_overridden_by_strategy(self):
        """DP#32 (#606): an explicit label='' is a value (the caller chose no
        label), not absence -- it must not silently fall through to
        'strategy'. Only a genuinely MISSING 'label' key falls back."""
        results, n = self._results_with_series()
        results[0]['label'] = ''
        results[0]['strategy'] = 'Should Not Appear'
        report = HtmlReport(results, self.cfg, title="YBY")
        scenarios = report._render_year_by_year_card(results)
        self.assertNotIn('Should Not Appear', scenarios)
        # Missing 'label' key still falls back to 'strategy'.
        del results[0]['label']
        scenarios_missing = report._render_year_by_year_card(results)
        self.assertIn('Should Not Appear', scenarios_missing)

    def test_html_year_by_year_omitted_gracefully(self):
        """No yby-data payload / no card crash when year_by_year is absent."""
        output = HtmlReport(self.results, self.cfg, title="No YBY").render()
        self.assertNotIn('id="yby-data"', output)

    def test_text_renders_year_by_year_table(self):
        results, n = self._results_with_series()
        output = TextReport(results, self.cfg, title="YBY").render()
        self.assertIn("Year-by-year breakdown", output)
        self.assertIn("Prim MTR", output)

    def test_csv_long_format_one_row_per_year(self):
        import tempfile, os, csv
        results, n = self._results_with_series()
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as f:
            path = f.name
        try:
            rows_written = write_year_by_year_csv(results, path)
            self.assertEqual(rows_written, n)
            with open(path, newline='') as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
            self.assertEqual(len(rows), n)
            # Scenario identifier columns present.
            self.assertIn('scenario_rank', rows[0])
            self.assertIn('scenario_label', rows[0])
            # YearResult fields present as columns.
            self.assertIn('mortgage_payment', rows[0])
            self.assertIn('primary_marginal', rows[0])
            # Years are sequential 1..n.
            self.assertEqual([int(r['year']) for r in rows], list(range(1, n + 1)))
        finally:
            os.unlink(path)

    def test_year_by_year_absent_is_graceful(self):
        """Reports must not crash when no year_by_year is attached."""
        for cls in (TextReport, JsonReport, HtmlReport, MarkdownReport):
            output = cls(self.results, self.cfg, title="No YBY").render()
            self.assertIsInstance(output, str)

    def test_empty_results(self):
        """Should not crash with empty results."""
        for fmt in [OutputFormat.TEXT, OutputFormat.JSON, OutputFormat.HTML,
                    OutputFormat.MARKDOWN]:
            report = create_report(fmt, [], {'family': {'members': []}, 'property': {}, 'accounts': {}, 'assumptions': {}})
            output = report.render()
            self.assertIsInstance(output, str)
            self.assertTrue(len(output) > 0)


class TestMarkdownReportSections(unittest.TestCase):
    """Issue #814: the conditional Markdown sections (equity grants, exhausted
    marker, model-fidelity findings, RESP EAP/Collapse, runway not-checked)
    each render as clean GFM. Fabricated round numbers / role-based names
    (DP#4/DP#15)."""

    def _cfg(self, **extra):
        cfg = {
            'family': {'members': [
                {'role': 'primary', 'gross_income': 160000,
                 'rrsp_room_accumulated': 180000, 'tfsa_room_accumulated': 35000},
                {'role': 'spouse', 'gross_income': 70000,
                 'rrsp_room_accumulated': 110000, 'tfsa_room_accumulated': 35000},
            ]},
            'property': {'house_value': 800000, 'mortgage_balance': 100000,
                         'margin_available': 250000},
            'accounts': {'resp_current_balance': 130000},
            'assumptions': {'investment_return': 0.07},
        }
        cfg.update(extra)
        return cfg

    def test_markdown_equity_grants_section(self):
        """A declared equity grant surfaces as a recorded-$0 section (#768)."""
        cfg = self._cfg(equity_grants=[
            {'id': 'grant_a', 'strike': 12.50,
             'vesting': {'fully_vested_date': '2030-01-01'}}])
        results = [{'label': 'base', 'net_benefit': 1000,
                    'cash_out': 0, 'resp_cash_out': 0}]
        output = MarkdownReport(results, cfg, title="Grants").render()
        self.assertIn("## Equity Grants", output)
        self.assertIn("grant_a", output)
        self.assertIn("valued $0", output)

    def test_markdown_exhausted_scenario_marked_inline(self):
        """Issue #707: a bankrupt scenario is marked inline so the bare net
        benefit cannot be read as an achievable retirement."""
        results = [{'label': 'bust', 'net_benefit': 500000,
                    'cash_out': 0, 'resp_cash_out': 0,
                    'drawdown_shortfall': {'exhausted': True,
                                           'first_shortfall_year': 2041}}]
        output = MarkdownReport(results, self._cfg()).render()
        self.assertIn("EXHAUSTED yr 2041", output)

    def test_markdown_fidelity_findings_become_nested_list(self):
        """Issue #685: a run-specific model-fidelity finding maps to a nested
        Markdown list item (the shared `      * ` spelling → `  - `), so the
        caveat's OWN figures reach the reader (DP#9)."""
        cfg = self._cfg()
        cfg['assumptions']['rate_path_conflicts'] = [
            {'liability_kind': 'mortgage', 'liability_id': 'm1',
             'declared_rate': 0.05, 'believed_rate': 0.06}]
        results = [{'label': 'base', 'net_benefit': 1000,
                    'cash_out': 0, 'resp_cash_out': 0}]
        output = MarkdownReport(results, cfg).render()
        # The approximation renders as a top-level list item and its finding as
        # a nested one carrying the run's own declared/believed rates.
        self.assertIn("\n- ", output)
        self.assertIn("\n  - ", output)
        self.assertIn("SIGNED rate 5.00%", output)

    def test_markdown_resp_eap_and_collapse_rows(self):
        """The RESP table renders EAP and Collapse rows (with their Δ-vs-keep
        verdicts) when those variants are present alongside a keep row."""
        results = [
            {'label': 'Keep', 'cash_out': 0, 'resp_cash_out': 0,
             'net_benefit': 1000000},
            {'label': 'RESP EAP', 'cash_out': 0, 'resp_cash_out': 50000,
             'net_benefit': 1010000},
            {'label': 'RESP Collapse', 'cash_out': 0, 'resp_cash_out': 50000,
             'net_benefit': 990000},
        ]
        output = MarkdownReport(results, self._cfg()).render()
        self.assertIn("## RESP Cash-Out Analysis", output)
        self.assertIn("RESP → EAP", output)
        self.assertIn("RESP ↘ Collapse", output)

    def test_markdown_runway_not_checked_notice(self):
        """Issue #758: when a scenario carries a 'runway' key that never
        engaged, the report prints a LOUD NOT-CHECKED notice — absence is
        visible, never mistaken for a finding of safety (DP#32)."""
        results = [{'label': 'base', 'net_benefit': 1000,
                    'cash_out': 0, 'resp_cash_out': 0, 'runway': None}]
        output = MarkdownReport(results, self._cfg()).render()
        self.assertIn("## Runway", output)
        self.assertIn("NOT CHECKED", output)

    def test_markdown_year_by_year_skips_sm_group_when_inactive(self):
        """The Taxes & SM per-year group is skipped when no SM field is
        non-zero, so a non-readvanceable run shows no empty SM table."""
        series = _fake_year_by_year(5)
        for yr in series:
            for k in ('sm_qc_deductible', 'readvance_tax_savings'):
                yr[k] = 0
        results = [{'label': 'no-sm', 'net_benefit': 1000,
                    'cash_out': 0, 'resp_cash_out': 0,
                    'year_by_year': series}]
        output = MarkdownReport(results, self._cfg()).render()
        self.assertIn("### Balances", output)
        self.assertNotIn("### Taxes & SM", output)


class TestRunwaySection(unittest.TestCase):
    """Issue #758: the reports must render the runway (months-to-insolvency)
    section when results carry an engaged runway verdict. Fabricated round
    numbers, role-based names (DP#4/DP#15). Uses the real
    ``RunwayResult.to_dict()`` so the rendered fields track the source of
    truth, not a hand-copied dict that could drift (DP#9)."""

    def _results(self):
        from runway import RunwayResult
        # A ruined scenario (headline is a labelled interpolation inside a
        # bracket) that also trips both report caveats, and a surviving
        # scenario (headline is the >=floor form).
        ruined = RunwayResult(
            engaged=True, runway_months=18.0, runway_months_bracket=(12.0, 24.0),
            stress_begins_months=6.0, interpolated=True,
            method='linear interpolation between year 1 and year 2',
            first_ruin_year=2, relies_on_credit_facility=True,
            drew_registered=True).to_dict()
        survives = RunwayResult(
            engaged=True, runway_months=None, survives_horizon_months=240.0,
            method='survives the simulated horizon').to_dict()
        return [
            {'label': 'Base income', 'strategy': 'Base',
             'income_scenario_id': 'base', 'income_scenario_label': 'Base income',
             'net_benefit': 1_000_000, 'total_debt': 200_000, 'runway': ruined},
            {'label': 'Optimistic income', 'strategy': 'Optimistic',
             'income_scenario_id': 'opt', 'income_scenario_label': 'Optimistic income',
             'net_benefit': 1_100_000, 'total_debt': 200_000, 'runway': survives},
        ]

    def _cfg(self):
        return {
            'family': {'members': [
                {'role': 'primary', 'gross_income': 120000,
                 'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 20000},
            ]},
            'property': {'house_value': 600000, 'mortgage_balance': 200000,
                         'margin_available': 50000},
            'accounts': {},
            'assumptions': {'investment_return': 0.06},
        }

    def test_text_report_renders_runway_figure(self):
        output = TextReport(self._results(), self._cfg(), title="Runway").render()
        self.assertIn("Runway", output)
        self.assertIn("months to insolvency", output)
        # The interpolated headline reaches the reader as months, labelled.
        self.assertIn("~18 mo", output)
        # The surviving scenario prints its floor, not "0 mo".
        self.assertIn("survives", output)

    def test_json_report_carries_runway_rows(self):
        import json
        payload = json.loads(JsonReport(self._results(), self._cfg()).render())
        self.assertIn('runway', payload)
        self.assertEqual(len(payload['runway']), 2)
        months = {row['runway']['runway_months'] for row in payload['runway']}
        self.assertIn(18.0, months)          # the ruined scenario's headline
        self.assertIn(None, months)          # the surviving scenario (not "0")

    def test_html_report_renders_runway_card(self):
        output = HtmlReport(self._results(), self._cfg()).render()
        self.assertIn("Runway", output)
        self.assertIn("months to insolvency", output)
        self.assertIn("~18 mo", output)

    def test_markdown_report_renders_runway_section(self):
        output = MarkdownReport(self._results(), self._cfg()).render()
        self.assertIn("## Runway", output)
        self.assertIn("months to insolvency".lower(),
                      output.lower())
        self.assertIn("~18 mo", output)

    def test_runway_rows_dedupe_by_income_scenario(self):
        """One runway row per DISTINCT income scenario: a second result
        carrying an already-seen income_scenario_id is dropped (issue #758,
        DP#17 -- the dedup branch of _runway_by_scenario). Two results share
        'base'; only the first survives."""
        from output_plugins import _runway_by_scenario
        results = self._results()
        # A second 'base'-id result: same scenario, a worse-ranked strategy.
        dup = dict(results[0])
        dup['label'] = 'Base income (alt strategy)'
        dup['net_benefit'] = 900_000
        rows = _runway_by_scenario(results + [dup])
        # Still two rows (base, opt) -- the duplicate 'base' was deduped, not
        # rendered a second time.
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted(r['label'] for r in rows),
            ['Base income', 'Optimistic income'])


class TestExplicitReportApi(unittest.TestCase):
    """Issue #722 (DP#9): the report API has an EXPLICIT interface -- no
    ``**kwargs`` catch-all. Every option a report supports is a named
    parameter; an unknown keyword raises ``TypeError`` loudly instead of
    being silently swallowed by ``kwargs.get``.
    """

    def setUp(self):
        self.results = [{'label': 'base', 'net_benefit': 1000,
                         'year_by_year': []}]
        self.cfg = {'family': {'members': [
            {'role': 'primary', 'gross_income': 100000}]},
            'property': {'house_value': 500000, 'mortgage_balance': 200000},
            'accounts': {}, 'assumptions': {}}

    def test_json_report_unknown_kwarg_raises_typeerror(self):
        """A typo'd keyword is loud, not silent (the catch-all is gone)."""
        with self.assertRaises(TypeError):
            JsonReport(self.results, self.cfg, title='T', typo=5)

    def test_create_report_unknown_kwarg_raises_typeerror(self):
        with self.assertRaises(TypeError):
            create_report(OutputFormat.JSON, self.results, self.cfg, typo=5)

    def test_write_report_unknown_kwarg_raises_typeerror(self):
        with self.assertRaises(TypeError):
            write_report(OutputFormat.JSON, self.results, self.cfg,
                         '/tmp/unused.json', typo=5)

    def test_indent_is_named_option_with_default(self):
        """``indent`` is the one option JSON output supports; default 2."""
        report = JsonReport(self.results, self.cfg, title='T')
        self.assertEqual(report.indent, 2)

    def test_indent_keyword_controls_json_pretty_width(self):
        """The known option still works: indent=4 produces 4-space JSON."""
        import json as _json
        out2 = JsonReport(self.results, self.cfg, title='T', indent=2).render()
        out4 = JsonReport(self.results, self.cfg, title='T', indent=4).render()
        self.assertNotEqual(out2, out4)
        # Both still valid JSON; indent=4 actually uses 4-space indents.
        self.assertIn('    "title"', out4)
        self.assertNotIn('    "title"', out2)
        _json.loads(out4)

    def test_create_report_forwards_indent_to_json_only(self):
        """indent reaches JsonReport; text/html accept (ignore) it."""
        json_r = create_report(OutputFormat.JSON, self.results, self.cfg,
                               indent=4)
        self.assertEqual(json_r.indent, 4)
        # text/html have no indent option but must not error on it.
        self.assertTrue(create_report(OutputFormat.TEXT, self.results,
                                      self.cfg, indent=4).render())
        self.assertTrue(create_report(OutputFormat.HTML, self.results,
                                      self.cfg, indent=4).render())

    def test_no_kwargs_catch_all_remains_in_report_api(self):
        """Invariant: the report construction entry points declare no
        ``**kwargs`` -- an explicit interface (DP#9). Guards against the
        catch-all being reintroduced."""
        import inspect
        for fn in (JsonReport.__init__, create_report, write_report):
            sig = inspect.signature(fn)
            self.assertNotIn(
                'kwargs', sig.parameters,
                f'{fn.__qualname__} reintroduced a **kwargs catch-all (#722)')


class TestMarkdownCliFlag(unittest.TestCase):
    """Issue #814: `optimize.py --md [PATH]` writes a Markdown report, mirroring
    the existing --html/--json/--txt flags. Drives the real CLI end-to-end over
    a two-generation subset of the SHIPPED example config (the sub-family Phase
    1's adapter can honestly map -- DP#15: fabricated shipped data, no personal
    figures), so the wiring is exercised, not just asserted about."""

    def test_optimize_md_flag_writes_markdown_file(self):
        import sys, json, tempfile, os
        import countries.canada  # noqa: F401 -- register the Canada providers
        import optimize
        from test_input_contract import _load_example, _two_generation_subset

        doc = _two_generation_subset(_load_example())
        tmpdir = tempfile.mkdtemp()
        input_path = os.path.join(tmpdir, "input.json")
        md_path = os.path.join(tmpdir, "report.md")
        with open(input_path, "w") as f:
            json.dump(doc, f)

        argv_bak = sys.argv
        try:
            sys.argv = ["optimize.py", "--input", input_path, "--md", md_path]
            optimize.main()  # must not raise
            self.assertTrue(os.path.exists(md_path),
                            "--md did not write the Markdown report")
            with open(md_path) as f:
                content = f.read()
            # Clean GFM: an H1 title, the situation heading, and a table.
            self.assertIn("# Strategy Optimizer Results", content)
            self.assertIn("## Situation Summary", content)
            self.assertIn("| --- |", content)
        finally:
            sys.argv = argv_bak
            for p in (input_path, md_path):
                if os.path.exists(p):
                    os.unlink(p)
            os.rmdir(tmpdir)


class TestPerMemberSavingsPlan(unittest.TestCase):
    """Epic #841 bite 5: the report surfaces a per-member savings plan.

    Bites 1 & 2 promoted every family member to a savings subject (a child's
    own income funds the child's own registered accounts, threaded separately
    from the household pot). This bite REPORTS that picture. DP#15: role-based
    names and fabricated round numbers only. DP#32: a member with no accounts
    (the golden household's RESP-only children) is a real empty -- reported as
    such, never invented into accounts, never a crash.
    """

    def _results(self):
        return [{'label': 'Scenario A', 'cash_out': 0,
                 'readvanceable_mortgage': False, 'deduct_later': False,
                 'ltv': 0.60, 'net_benefit': 500000,
                 'future_value': 1000000, 'total_debt': 300000}]

    def _cfg_with_child_saver(self):
        # Fabricated round numbers (DP#15): NOT the household's real figures.
        return {
            'family': {
                'members': [
                    {'role': 'primary', 'gross_income': 100000,
                     'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 20000,
                     'rrsp_balance': 15000},
                ],
                'children': [
                    # A declared child saver with FHSA room + a first-home goal.
                    {'role': 'child', 'name': 'child_a', 'gross_income': 12000,
                     'fhsa_room_accumulated': 20000, 'tfsa_room_accumulated': 8000,
                     'fhsa_balance': 3000},
                ],
            },
            'property': {'house_value': 600000, 'mortgage_balance': 300000,
                         'margin_available': 50000},
            'accounts': {},
            'savings': {'rate': 0.10},
            'assumptions': {'investment_return': 0.06},
        }

    def _cfg_resp_only_children(self):
        # The golden-shaped case: children exist but own NO registered accounts
        # and hold NO room (RESP beneficiaries only).
        return {
            'family': {
                'members': [
                    {'role': 'primary', 'gross_income': 100000,
                     'rrsp_room_accumulated': 40000},
                ],
                'children': [
                    {'role': 'child', 'name': 'child_a', 'gross_income': 0},
                    {'role': 'child', 'name': 'child_b', 'gross_income': 0},
                ],
            },
            'property': {'house_value': 600000, 'mortgage_balance': 300000,
                         'margin_available': 50000},
            'accounts': {'resp_current_balance': 50000},
            'savings': {'rate': 0.10},
            'assumptions': {'investment_return': 0.06},
        }

    def test_text_report_shows_child_saver_accounts_and_room(self):
        out = TextReport(self._results(), self._cfg_with_child_saver()).render()
        self.assertIn("Per-member savings plan", out)
        # The child is surfaced with its OWN accounts + room (fabricated round
        # numbers reach the reader).
        self.assertIn("child_a", out)
        self.assertIn("FHSA", out)
        self.assertIn("20,000", out)   # the child's FHSA room
        self.assertIn("8,000", out)    # the child's TFSA room
        # The child's per-year contribution: income $12,000 * savings rate 0.10.
        self.assertIn("$1,200/yr", out)
        # A child with FHSA room + a first-home goal gets the FHSA-first plan.
        self.assertIn("FHSA-first plan", out)

    def test_markdown_report_shows_child_saver_section(self):
        out = MarkdownReport(self._results(), self._cfg_with_child_saver()).render()
        self.assertIn("## Per-Member Savings Plan", out)
        self.assertIn("### child_a", out)
        self.assertIn("FHSA-first plan", out)
        # The adult is present too (a savings subject with its own accounts).
        self.assertIn("### Primary", out)

    def test_resp_only_children_render_without_inventing_accounts(self):
        # DP#32: a child that owns nothing is a real empty -- the section
        # renders, names the child, and says "no accounts" rather than
        # fabricating a TFSA/FHSA/RRSP row or crashing.
        cfg = self._cfg_resp_only_children()
        text_out = TextReport(self._results(), cfg).render()
        md_out = MarkdownReport(self._results(), cfg).render()
        for out in (text_out, md_out):
            self.assertIn("child_a", out)
            self.assertIn("child_b", out)
        self.assertIn("no registered accounts", text_out)
        self.assertIn("_No registered accounts modelled._", md_out)
        # Nothing invented: no FHSA-first plan and no FHSA/TFSA balance rows for
        # a child that owns neither.
        self.assertNotIn("FHSA-first plan", text_out)
        self.assertNotIn("FHSA-first plan", md_out)

    def test_no_family_declared_skips_the_section_gracefully(self):
        cfg = {'family': {'members': []}, 'property': {}, 'accounts': {},
               'assumptions': {}}
        text_out = TextReport(self._results(), cfg).render()
        md_out = MarkdownReport(self._results(), cfg).render()
        # No members and no children -> the section is simply omitted, no crash.
        self.assertNotIn("Per-member savings plan", text_out)
        self.assertNotIn("## Per-Member Savings Plan", md_out)

    def test_member_label_falls_back_to_role_then_generic(self):
        # DP#32/#4: the display label degrades gracefully. A child with no
        # declared name is shown by its ROLE (capitalized); a member whose role
        # is neither primary/spouse and who has no name at all falls back to the
        # generic 'Member' -- neither reaches for a real name (DP#15) nor crashes.
        cfg = {
            'family': {
                'members': [
                    # An unrecognized/empty role with no name -> 'Member'.
                    {'role': '', 'gross_income': 0},
                ],
                'children': [
                    # A child with no name -> shown by role -> 'Child'.
                    {'role': 'child', 'gross_income': 0},
                ],
            },
            'property': {'house_value': 500000, 'mortgage_balance': 200000,
                         'margin_available': 40000},
            'accounts': {},
            'savings': {'rate': 0.10},
            'assumptions': {'investment_return': 0.06},
        }
        text_out = TextReport(self._results(), cfg).render()
        md_out = MarkdownReport(self._results(), cfg).render()
        for out in (text_out, md_out):
            self.assertIn("Child", out)    # the nameless child, shown by role
            self.assertIn("Member", out)   # the roleless/nameless generic fallback


if __name__ == '__main__':
    unittest.main()
