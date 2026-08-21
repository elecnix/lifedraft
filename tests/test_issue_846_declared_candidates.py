"""Issue #846 / #845: a declared candidate list must never SILENTLY collapse an
exploration, and the household's declared refinance options must actually reach
the optimizer.

All data is fabricated with round numbers — no personal data (DP#4, DP#15).

## The two defects these tests pin

**#846 — "computed, then discarded."** ``input_contract.py`` maps the
schema-required ``decisions.mortgage.refinance_options`` onto
``cfg['scenarios']['refinance']``; ``discover_anchors`` faithfully converts it
into ``anchors['refinance']``; and ``optimize.py`` then read only
``anchors['income']`` and substituted a hardcoded LTV ladder for the household's
declaration. Every existing guard stayed green because the leaf IS read and the
code IS executed — reachability and coverage measure reading and execution, not
whether the computed result is ever *used*. ``test_declared_cash_out_changes_the
_optimizers_refinance_analysis`` is the missing measurement: it mutates the leaf
and asserts optimize.py's own numbers move.

**#846 — silent narrowing.** ``discover_anchors`` resolves four dimensions with
``if declared: use it else: auto-discover``, so declaring candidates REPLACES the
auto ladder. Real and measurable: declaring the two refinance options in
``schema/example.json`` takes simulate.py's grid from 240 overlays to 48. DP#32
already forbids this for ``sensitivity.sweeps`` (#771).
"""

import pytest

import optimize
from scenario_discovery import discover_narrowings, format_narrowings


def _cfg():
    """A fabricated household with round numbers and NO declared scenarios."""
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
            'house_value': 800000,
            'mortgage_balance': 300000,
            'mortgage_rate': 0.05,
            'margin_available': 50000,
            'ltv_max': 0.80,
            'heloc_readvance': True,
            'refinance_amortization_years': 25,
        },
        'accounts': {'resp_current_balance': 0},
        'assumptions': {'investment_return': 0.07},
        'scenarios': {},
    }


# ── 1. discover_narrowings: the counted fact ────────────────────────────────

class TestDiscoverNarrowings:
    """The pure logic half: which declared lists narrowed their auto sweep."""

    def test_no_declaration_is_not_a_narrowing(self):
        """DP#32: absence is not a narrowing — auto-discovery ran in full."""
        assert discover_narrowings(_cfg()) == []

    def test_single_declared_refinance_option_is_flagged_as_a_single_point(self):
        cfg = _cfg()
        cfg['scenarios']['refinance'] = [
            {'id': 'no_cash_out', 'label': 'Draw from the line instead', 'cash_out': 0},
        ]
        [row] = discover_narrowings(cfg)
        assert row['dimension'] == 'refinance'
        assert row['declared_count'] == 1
        assert row['collapsed_to_single_point'] is True
        assert row['narrowed'] is True
        # The auto ladder it replaced is real and much larger — the notice must
        # be able to quote what was given up, not merely that something was.
        assert row['auto_count'] > 1

    def test_declared_fewer_than_auto_is_flagged_even_when_not_a_single_point(self):
        """2 candidates against a 10-rung ladder is still a 5x narrowing."""
        cfg = _cfg()
        cfg['scenarios']['refinance'] = [
            {'id': 'advance', 'label': 'Take a mortgage advance', 'cash_out': 100000},
            {'id': 'line', 'label': 'Draw from the line', 'cash_out': 0},
        ]
        [row] = discover_narrowings(cfg)
        assert row['collapsed_to_single_point'] is False
        assert row['fewer_than_auto'] is True
        assert row['narrowed'] is True

    def test_mortgage_strategy_resp_action_no_longer_narrow(self):
        """#890/DP#33: mortgage, strategy and resp_action now ANNOTATE their
        auto-discovered sweep in ``discover_anchors`` (a declared candidate is
        UNIONED over it, never replacing it), so they can no longer narrow and
        are absent from ``discover_narrowings``. Their union is pinned in
        ``tests/test_issue_890_other_dims_lens.py``. refinance is the sole
        surviving narrowable dimension (it still replaces in simulate.py's grid)."""
        cfg = _cfg()
        cfg['accounts']['resp_current_balance'] = 50000  # auto -> keep/eap/collapse
        cfg['property']['renewal_options'] = [
            {'name': 'Five-year fixed', 'rate': 0.045, 'type': 'fixed', 'term_years': 5},
        ]
        cfg['scenarios']['mortgage'] = [{'id': 'm', 'label': 'One mortgage', 'rate': 0.05}]
        cfg['scenarios']['strategy'] = [{'id': 's', 'label': 'One strategy', 'tfsa_pct': 1.0}]
        cfg['scenarios']['resp_action'] = [{'id': 'keep'}]
        dims = {r['dimension'] for r in discover_narrowings(cfg)}
        assert dims == set()  # none of the three appears; refinance undeclared here

    def test_unknown_dimension_is_refused_by_name(self):
        """DP#32: a non-narrowable dimension has nothing to compare against, so
        asking is a programming error — it must fail loudly rather than return a
        reassuring empty list."""
        import scenario_discovery as sd
        with pytest.raises(ValueError, match='income'):
            sd._auto_candidates(_cfg(), 'income')


# ── 2. format_narrowings: the loud statement ────────────────────────────────

class TestFormatNarrowings:

    def test_fires_and_names_the_dimension_counts_and_contract_path(self):
        cfg = _cfg()
        cfg['scenarios']['refinance'] = [
            {'id': 'no_cash_out', 'label': 'Draw from the line instead', 'cash_out': 0},
        ]
        text = "\n".join(format_narrowings(discover_narrowings(cfg)))
        assert 'refinance' in text
        # names WHAT the household declared, and WHERE they declared it
        assert 'decisions.mortgage.refinance_options' in text
        assert 'SINGLE POINT' in text
        # never presents the collapse as an exploration
        assert 'declared 1 candidate(s)' in text

    def test_does_not_fire_when_nothing_was_narrowed(self):
        assert format_narrowings(discover_narrowings(_cfg())) == []


# ── 3. #846's core: the declared leaf must REACH the optimizer ──────────────

class TestDeclaredRefinanceReachesTheOptimizer:
    """The measurement the reachability suite structurally cannot make: the leaf
    reaches the internal config AND is read (both already true before #846) —
    the question is whether optimize.py's own OUTPUT depends on it."""

    def _declared(self, cash_out):
        cfg = _cfg()
        cfg['scenarios']['refinance'] = [
            {'id': 'line_draw', 'label': 'Take the surplus from the line', 'cash_out': 0},
            {'id': 'advance', 'label': 'Take the surplus as a mortgage advance',
             'cash_out': cash_out},
        ]
        return cfg

    def test_declared_cash_out_changes_the_optimizers_refinance_analysis(self):
        """#846's regression pin, updated for #853/DP#33. The declaration is no
        longer inert — but it now ANNOTATES the full ladder rather than replacing
        it, so a different declared cash-out lands at a different in-situ rung.
        $200k → 62.5% (inserted), $300k → 75% (inserted); each appears in its own
        run and not the other's, so the declaration still moves the analysis."""
        a = optimize.run_ltv_exploration(self._declared(200000))
        b = optimize.run_ltv_exploration(self._declared(300000))
        assert 200000 in {r['cashout'] for r in a}
        assert 200000 not in {r['cashout'] for r in b}
        assert 300000 in {r['cashout'] for r in b}
        assert 300000 not in {r['cashout'] for r in a}

    def test_declared_options_are_ranked_within_the_full_sweep(self):
        """#846's opening ask ('compare advance VS line') — plus #853: both are
        MARKED within the full ladder, not ranked in isolation, so a rung the
        household did not declare can still win and be seen."""
        results = optimize.run_ltv_exploration(self._declared(100000))
        declared_ids = {r['refinance_declared_id']
                        for r in results if r.get('refinance_declared')}
        assert {'line_draw', 'advance'} <= declared_ids
        # the full ladder survives alongside the two declared options (#853)
        ltvs = {round(r['ltv'], 4) for r in results}
        assert set(optimize.DEFAULT_LTV_LADDER) <= ltvs

    def test_undeclared_household_keeps_the_generic_ladder(self):
        """DP#13: the ladder is a fallback for absent input. A household that
        declared nothing must see exactly the sweep it saw before #846, with no
        declared rungs marked."""
        results = optimize.run_ltv_exploration(_cfg())
        assert {r['refinance_source'] for r in results} == {'ladder'}
        assert not any(r.get('refinance_declared') for r in results)
        assert sorted({r['ltv'] for r in results}) == optimize.DEFAULT_LTV_LADDER

    def test_declared_no_cash_out_option_reports_the_real_current_ltv(self):
        """#845/#846: a household that declines to refinance sits at its CURRENT
        LTV, not 0%. This is #846's own 'take it from the line' option, and #846
        puts it in optimize.py's LTV table — where a 0% label would misstate the
        very basis #845 exists to have stated. 300000/800000 = 37.5%."""
        results = optimize.run_ltv_exploration(self._declared(100000))
        line_rows = [r for r in results if r.get('refinance_declared_id') == 'line_draw']
        assert line_rows
        assert all(r['cashout'] == 0 for r in line_rows)
        assert all(r['ltv'] == pytest.approx(0.375) for r in line_rows)

    def test_explicit_ltv_steps_still_win_over_a_declaration(self):
        """A caller that passed an explicit ladder has already decided what to
        sweep; the override is allowed (it is _print_refinance_basis's job to
        say so out loud) — the union does not apply."""
        results = optimize.run_ltv_exploration(self._declared(100000),
                                               ltv_steps=[0.0, 0.5])
        assert {r['refinance_source'] for r in results} == {'ladder'}
        assert not any(r.get('refinance_declared') for r in results)
        assert sorted({r['ltv'] for r in results}) == [0.0, 0.5]


# ── 4. The basis is stated, never inferred (#845) ───────────────────────────

class TestRefinanceBasisIsStated:

    def _rows(self, source, declared_count, annotated=False, declared_label='My option'):
        return [{'refinance_source': source,
                 'refinance_declared_count': declared_count,
                 'refinance_annotated': annotated,
                 'refinance_declared': annotated,
                 'refinance_declared_label': declared_label if annotated else None,
                 'ltv': 0.5, 'cashout': 0, 'net_benefit': 1.0, 'strategy': 's'}]

    def test_declared_union_is_named_as_a_lens_on_the_full_sweep(self, capsys):
        """#853/DP#33: a declaration now ANNOTATES the full ladder, so the basis
        names the whole sweep and marks the declared options ★ in situ rather
        than presenting the declaration as a replacement."""
        optimize._print_refinance_basis(self._rows('ladder', 2, annotated=True))
        out = capsys.readouterr().out
        assert 'decisions.mortgage.refinance_options' in out
        assert 'FULL LTV sweep' in out
        assert '★' in out

    def test_ladder_overriding_a_declaration_says_so_loudly(self, capsys):
        """#845: never two contradictory authoritative answers from one run. An
        explicit caller ladder (not annotated) that overrides a declaration."""
        optimize._print_refinance_basis(self._rows('ladder', 1, annotated=False))
        out = capsys.readouterr().out
        assert 'OVERRIDES' in out
        assert 'NOT your declared options' in out

    def test_ladder_with_no_declaration_states_it_is_generic(self, capsys):
        optimize._print_refinance_basis(self._rows('ladder', 0))
        out = capsys.readouterr().out
        assert 'generic LTV ladder' in out
        assert 'OVERRIDES' not in out


class TestRefinanceLtvDerivation:
    """The declared-option ltv derivation _print_refinance_basis's LTV column
    depends on (#846's correction to _convert_refinance_scenarios)."""

    def test_declared_ltv_is_honoured_when_stated(self):
        """DP#32: a scenario that STATES its ltv keeps it — derivation is for
        absence only."""
        import scenario_discovery as sd
        cfg = _cfg()
        [row] = sd._convert_refinance_scenarios(
            [{'id': 'x', 'label': 'X', 'cash_out': 100000, 'ltv': 0.625}], cfg)
        assert row['ltv'] == 0.625

    def test_no_house_value_yields_no_invented_ltv(self):
        """An LTV is not computable without a house value, and inventing one is
        exactly the confident-wrong-number this repo exists to prevent."""
        import scenario_discovery as sd
        cfg = _cfg()
        cfg['property']['house_value'] = 0
        [row] = sd._convert_refinance_scenarios(
            [{'id': 'x', 'label': 'X', 'cash_out': 100000}], cfg)
        assert row['ltv'] == 0


class TestSimulateCliNamesTheNarrowing:
    """#846's narrowing is REAL in simulate.py: declaring the two refinance
    options in schema/example.json takes its grid from 240 overlays to 48. The
    'Total combinations' line prints the already-narrowed count, so the notice
    must sit right beside it."""

    def test_discovery_report_names_the_narrowing_when_given_the_cfg(self, capsys):
        import simulate
        from scenario_discovery import discover_anchors
        cfg = _cfg()
        cfg['scenarios']['refinance'] = [
            {'id': 'only', 'label': 'The only option', 'cash_out': 0},
        ]
        simulate.print_discovery(discover_anchors(cfg), cfg)
        out = capsys.readouterr().out
        assert 'Total combinations' in out
        assert 'REPLACED THE AUTO-DISCOVERED EXPLORATION' in out
        assert 'decisions.mortgage.refinance_options' in out

    def test_discovery_report_says_nothing_without_a_cfg(self, capsys):
        """DP#13: absence means 'this caller cannot answer the question', not
        'nothing was narrowed' — so assert nothing rather than print a
        reassuring silence."""
        import simulate
        from scenario_discovery import discover_anchors
        simulate.print_discovery(discover_anchors(_cfg()))
        assert 'REPLACED THE AUTO-DISCOVERED' not in capsys.readouterr().out


class TestOptimizeCliHasNoNarrowingNotice:
    """#890/DP#33: optimize.py's narrowing notice was REMOVED. #846 gave it a
    loud "declared candidates REPLACED the exploration" notice; #853 excluded
    refinance (optimize's LTV table unions it); #890 made the other three
    dimensions annotate too — so NO declared dimension narrows in optimize.py and
    the notice had nothing left to say. The one surface where a declaration still
    narrows (refinance in simulate.py's grid) keeps the notice there
    (TestSimulateCliNamesTheNarrowing)."""

    def test_optimize_no_longer_exposes_a_narrowing_notice(self):
        assert not hasattr(optimize, '_print_narrowing_notice')


class TestHeadlineRankingStatesItsBasis:
    """#845: the headline table refinances every strategy to ltv_max. It used to
    call that 'current LTV', which is close to the opposite of what it is."""

    def test_header_names_max_ltv_and_the_cash_out_dollars(self, capsys):
        optimize._print_headline_basis(_cfg(), 0.80)
        out = capsys.readouterr().out
        assert 'MAX LTV 80%' in out
        # 800000 * 0.80 - 300000 = 340000
        assert '$340,000' in out
        assert 'current LTV' not in out

    def test_declared_options_are_named_as_NOT_the_basis(self, capsys):
        """#846: the headline does not yet honour the declaration — say so
        rather than let a no-cash-out household read a max-cash-out ranking as
        its own plan."""
        cfg = _cfg()
        cfg['scenarios']['refinance'] = [
            {'id': 'line', 'label': 'From the line', 'cash_out': 0},
        ]
        optimize._print_headline_basis(cfg, 0.80)
        out = capsys.readouterr().out
        assert 'NOT the 1 option(s) you declared' in out
        assert 'DIFFERENT plans' in out

    def test_no_declaration_gets_no_override_warning(self, capsys):
        optimize._print_headline_basis(_cfg(), 0.80)
        assert 'you declared' not in capsys.readouterr().out


class TestStructureRankingStatesItsLeverage:
    """#845: the structure ranking is computed at cash-out $0 while the LTV
    sweep recommends a different leverage. The structural choice is irreversible
    on notary day, so the reader must be told which plan it was scored on."""

    def _structure_rows(self):
        base = {
            'structure_basis_ltv': 0.375, 'structure_basis_cash_out': 0.0,
            'income_scenario_id': 'current', 'income_scenario_label': 'Current income',
            'strategy': 'balanced', 'deduct_later': False, 'net_benefit': 100000.0,
            'solvency': {'engaged': True, 'ruined': False}, 'draw_fraction': 0.0,
        }
        rows = []
        for sid, label, share in (('all_mortgage', 'All mortgage', 0.0),
                                  ('split_line', 'Mortgage + line', 0.3)):
            r = dict(base)
            r.update({'structure_id': sid, 'structure_label': label,
                      'structure_revolving_share': share,
                      'structure_readvanceable': None})
            rows.append(r)
        return rows

    def test_report_states_the_cash_out_and_ltv_it_was_computed_at(self, capsys):
        optimize._print_structure_report(self._structure_rows())
        out = capsys.readouterr().out
        assert 'CASH-OUT $0' in out
        assert '37.5%' in out            # the basis LTV, named
        assert 'NO cash-out sweep' in out

    def test_report_warns_against_reading_it_with_the_ltv_sweep(self, capsys):
        optimize._print_structure_report(self._structure_rows())
        out = capsys.readouterr().out
        assert 'do not read the two tables as one plan' in out.lower()
