#!/usr/bin/env python3
"""
Output Plugins — Pluggable result formatters (txt, json, html, md).

DP#8: compose through data — each plugin is a dataclass implementing
a common interface. The optimizer produces result data; the user picks
the output format independently.

DP#25: Output plugins are pure formatters. They consume result dicts
and produce formatted output with no dependency on simulation or optimizer.

Usage:
    from output_plugins import TextReport, JsonReport, HtmlReport, MarkdownReport

    report = HtmlReport(results, base_cfg, title="My Scenarios")
    report.write("results.html")        # Write to file
    print(report.render())              # Write to stdout

    # Or use the factory:
    from output_plugins import OutputFormat, create_report
    fmt = OutputFormat.HTML
    report = create_report(fmt, results, base_cfg)
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple

import model_fidelity


def _runway_by_scenario(results: List[Dict]) -> List[Dict]:
    """One runway row per distinct income scenario, in first-seen order, each
    carrying its best (first in sorted order) result's ``runway`` verdict
    (issue #758). Self-contained -- does not import optimize (DP#25: avoid a
    circular import; optimize imports this module for the CLI reports).

    Returns [] when no result carries an engaged ``runway`` (the run never
    declared a living-cost budget, or carried no income scenarios at all).
    The caller prints the loud NOT-CHECKED notice rather than an empty table.
    """
    seen: Dict[str, Dict] = {}
    for r in results:
        rw = r.get('runway')
        if rw is None:
            continue
        sid = r.get('income_scenario_id', '_')
        if sid in seen:
            continue
        seen[sid] = {
            'label': r.get('income_scenario_label', r.get('strategy', '?')),
            'runway': rw,
        }
    return list(seen.values())


# Issue #758: the runway rendering is ONE spelling (DP#9), in runway.py;
# import it rather than inventing a second. (output_plugins deliberately
# does not import optimize -- circular at module load -- so the shared home
# is the metric module itself, which both layers may import.) The same home
# holds ``absent_runway`` -- the explicit un-engaged runway verdict for a row
# that carries none -- while decumulation owns the shortfall accessors; all
# three feed the console reports at the foot of this module (#232 slice 3).
from decumulation import shortfall_of, summarize_drawdown_shortfall  # noqa: E402
from runway import absent_runway, format_runway as _format_runway_inline  # noqa: E402


# =============================================================================
# Plugin Interface
# =============================================================================

class OutputFormat(Enum):
    TEXT = "txt"
    JSON = "json"
    HTML = "html"
    MARKDOWN = "md"


@dataclass
class OutputReport:
    """Base class for output plugins.

    DP#8: compose through data. Each plugin receives the same input
    (results list, base config, optional params) and produces formatted output.
    """
    results: List[Dict] = field(default_factory=list)
    base_cfg: Dict = field(default_factory=dict)
    title: str = "Refinance Scenario Analysis"
    include_sensitivity: bool = False
    
    def render(self) -> str:
        raise NotImplementedError
    
    def write(self, path: str) -> None:
        with open(path, 'w') as f:
            f.write(self.render())


# =============================================================================
# Helper: extract common data from results
# =============================================================================

def _sort_results(results: List[Dict]) -> List[Dict]:
    # Issue #707: a trajectory that exhausted its assets before the horizon
    # sorts below one that did not -- a bankrupt plan is not comparable on
    # terminal net worth and must not be crowned the winner. The score
    # itself is unchanged (the ledger fact is still reported). Synthetic
    # rows without 'exhausted' are treated as not-exhausted (safe drop-in).
    from decumulation import ranking_key
    return sorted(results, key=lambda r: ranking_key(r, r.get('net_benefit', 0)), reverse=True)


# =============================================================================
# Year-by-year breakdown (issue #248)
#
# DP#8/DP#25: pure presentation. Each result dict already carries a
# `year_by_year` list (one dict per YearResult, produced upstream via
# dataclasses.asdict in optimize.evaluate_strategy_with_simulation). The
# reporting layer only reads and formats it — no financial recomputation.
# =============================================================================

# The display columns surface the three concerns from issue #248:
#   TAXES    — primary MTR, RRSP/SM tax savings, deductible SM interest
#   CASH FLOW — family income, annual savings, contributions, net cash flow
#   MORTGAGE  — payment / interest / principal / balance
# Each entry: (YearResult key, header, kind) where kind is 'money'/'pct'/'int'.
YEAR_COLUMNS = [
    ('year', 'Year', 'int'),
    ('mortgage_payment', 'Mtg Pmt', 'money'),
    ('mortgage_interest', 'Mtg Int', 'money'),
    ('mortgage_principal', 'Mtg Prin', 'money'),
    ('mortgage_balance', 'Mtg Bal', 'money'),
    ('heloc_balance', 'HELOC Bal', 'money'),
    ('sm_qc_deductible', 'Deduct Int', 'money'),
    ('rrsp_tax_savings', 'RRSP TaxSv', 'money'),
    ('readvance_tax_savings', 'SM TaxSv', 'money'),
    ('primary_marginal', 'Prim MTR', 'pct'),
    ('total_family_income', 'Income', 'money'),
    ('annual_savings', 'Savings', 'money'),
    ('total_assets', 'Assets', 'money'),
    ('total_debt', 'Debt', 'money'),
]


# -----------------------------------------------------------------------------
# Rich year-by-year column groups (issue #239 follow-up: full richness).
#
# The flat YEAR_COLUMNS above stay the compact terminal-view set. The groups
# below surface every YearResult field, organized by concern, so the HTML
# report can present the full per-year picture (balances, contributions,
# taxes & readvanceable mortgage strategy, mortgage & cash flow, CRI/LIRA).
#
# Each group is a list of (dotted-key, header, kind). A dotted-key like
# 'contributions.primary_rrsp' reads a nested field via _year_get().
# 'net_worth' is a synthetic computed column.
# -----------------------------------------------------------------------------
YEAR_GROUPS: Dict[str, List[tuple]] = {
    'Balances': [
        ('year', 'Year', 'int'),
        ('total_rrsp', 'Total RRSP', 'money'),
        ('primary_rrsp', 'RRSP (Primary)', 'money'),
        ('spousal_rrsp', 'Spousal RRSP', 'money'),
        ('spouse_rrsp', 'RRSP (Spouse)', 'money'),
        ('total_tfsa', 'Total TFSA', 'money'),
        ('primary_tfsa', 'TFSA (Primary)', 'money'),
        ('spouse_tfsa', 'TFSA (Spouse)', 'money'),
        ('resp_balance', 'RESP', 'money'),
        ('non_reg_balance', 'Non-Reg', 'money'),
        ('non_reg_acb', 'Non-Reg ACB', 'money'),
        ('non_reg_unrealized_gains', 'Unreal. Gains', 'money'),
        ('lira_balance', 'CRI/LIRA', 'money'),
        ('lif_balance', 'LIF', 'money'),
        ('total_assets', 'Total Assets', 'money'),
        ('total_debt', 'Total Debt', 'money'),
        ('net_worth', 'Net Worth', 'money'),
    ],
    'Contributions': [
        ('year', 'Year', 'int'),
        ('contributions.primary_rrsp', 'RRSP (Primary)', 'money'),
        ('contributions.spousal_rrsp', 'Spousal RRSP', 'money'),
        ('contributions.spouse_rrsp', 'RRSP (Spouse)', 'money'),
        ('contributions.primary_tfsa', 'TFSA (Primary)', 'money'),
        ('contributions.spouse_tfsa', 'TFSA (Spouse)', 'money'),
        ('contributions.fhsa', 'FHSA', 'money'),
        ('contributions.resp', 'RESP', 'money'),
        ('contributions.non_reg', 'Non-Reg / SM', 'money'),
        ('annual_savings', 'Annual Savings', 'money'),
    ],
    'Taxes & SM': [
        ('year', 'Year', 'int'),
        ('primary_marginal', 'Prim MTR', 'pct'),
        ('spouse_marginal', 'Spouse MTR', 'pct'),
        ('bracket_gap', 'Bracket Gap', 'pct'),
        ('rrsp_tax_savings', 'RRSP Tax Savings', 'money'),
        ('readvance_tax_savings', 'SM Tax Savings', 'money'),
        ('readvance_interest', 'SM Interest', 'money'),
        ('sm_qc_deductible', 'QC Deduct. Int', 'money'),
        ('sm_qc_carry_forward', 'QC Carry-Fwd', 'money'),
        ('sm_deductible_proportion', 'SM Deduct %', 'pct'),
        ('sm_readvanced', 'SM Readvanced', 'money'),
    ],
    'Mortgage & Cash Flow': [
        ('year', 'Year', 'int'),
        ('mortgage_rate', 'Mtg Rate', 'pct'),
        ('heloc_rate', 'HELOC Rate', 'pct'),
        ('mortgage_payment', 'Mtg Payment', 'money'),
        ('mortgage_interest', 'Mtg Interest', 'money'),
        ('mortgage_principal', 'Mtg Principal', 'money'),
        ('mortgage_balance', 'Mtg Balance', 'money'),
        ('heloc_balance', 'HELOC Balance', 'money'),
        ('primary_income', 'Prim Income', 'money'),
        ('spouse_income', 'Spouse Income', 'money'),
        ('total_family_income', 'Family Income', 'money'),
        ('lif_withdrawal', 'LIF Withdrawal', 'money'),
    ],
}


def _year_get(yr: Dict, dotted_key: str) -> float:
    """Read a (possibly dotted) key from a serialized YearResult dict.

    'contributions.primary_rrsp' reads yr['contributions']['primary_rrsp'];
    'net_worth' is computed as total_assets - total_debt.
    """
    if dotted_key == 'net_worth':
        return (yr.get('total_assets', 0) or 0) - (yr.get('total_debt', 0) or 0)
    if '.' in dotted_key:
        head, tail = dotted_key.split('.', 1)
        nested = yr.get(head, {})
        if isinstance(nested, dict):
            return nested.get(tail, 0) or 0
        return 0
    return yr.get(dotted_key, 0) or 0


def _fmt_year_value(value: float, kind: str) -> str:
    """Format a per-year value for HTML (escaped). kind is money/pct/int."""
    if kind == 'int':
        return f"{int(value)}"
    if kind == 'pct':
        return f"{value:.2%}"
    return f"${value:,.0f}"


def _net_worth(yr: Dict) -> float:
    return yr.get('total_assets', 0) - yr.get('total_debt', 0)


# Readvanceable mortgage strategy per-year fields (subset of the 'Taxes & SM'
# group). Used to decide whether a scenario uses the readvanceable strategy so
# the server-side fallback surfaces those values immediately, without requiring
# the JS tab to be clicked (issue #239).
_SM_KEYS = (
    'readvance_tax_savings', 'readvance_interest', 'sm_qc_deductible',
    'sm_qc_carry_forward', 'sm_deductible_proportion', 'sm_readvanced',
)


def _is_sm_active(year_by_year: List[Dict]) -> bool:
    """True if any readvanceable mortgage strategy field is non-zero across the series."""
    return any(
        _year_get(yr, key) for yr in year_by_year for key in _SM_KEYS
    )


def _top_year_by_year(results: List[Dict]) -> List[Dict]:
    """Return the year_by_year series for the top-ranked scenario, or []."""
    if not results:
        return []
    top = _sort_results(results)[0]
    return top.get('year_by_year') or []


def _situation_summary(cfg: Dict) -> Dict:
    """Extract key situation data from config for report headers."""
    from member_config import find_member_by_role  # #699 seam (DP#25: data layer)
    members = cfg.get('family', {}).get('members', [])
    primary = find_member_by_role(members, 'primary', {})
    spouse = find_member_by_role(members, 'spouse', {})
    prop = cfg.get('property', {})
    house_value = prop.get('house_value', 0)
    mortgage = prop.get('mortgage_balance', 0)
    margin = prop.get('margin_available', 0)
    max_cashout = max(0, house_value * 0.80 - mortgage)
    
    accounts = cfg.get('accounts', {})
    total_registered = (
        primary.get('rrsp_room_accumulated', 0) + spouse.get('rrsp_room_accumulated', 0) +
        primary.get('tfsa_room_accumulated', 0) + spouse.get('tfsa_room_accumulated', 0)
    )
    min_cashout = max(0, total_registered - margin)
    
    return {
        'primary_income': primary.get('gross_income', 0),
        'spouse_income': spouse.get('gross_income', 0),
        'house_value': house_value,
        'mortgage_balance': mortgage,
        'margin_available': margin,
        'ltv_current': mortgage / house_value if house_value else 0,
        'cash_out_80': max_cashout,
        'total_registered_room': total_registered,
        'rrsp_room': primary.get('rrsp_room_accumulated', 0) + spouse.get('rrsp_room_accumulated', 0),
        'tfsa_room': primary.get('tfsa_room_accumulated', 0) + spouse.get('tfsa_room_accumulated', 0),
        'resp_balance': accounts.get('resp_current_balance', 0),
        'min_cashout': min_cashout,
        'min_ltv': (mortgage + min_cashout) / house_value if house_value else 0,
        'investment_return': cfg.get('assumptions', {}).get('investment_return', 0.07),
    }


# =============================================================================
# Per-member savings plan (epic #841 bite 5)
#
# Bites 1 & 2 promoted every family member -- not just the two adults -- to a
# savings SUBJECT: a child's own income funds the child's own registered
# accounts, threaded separately from the household pot. This section REPORTS
# that per-member picture. It is pure presentation (DP#8/DP#25): it reads only
# the mapped contract (`base_cfg`) and recomputes NO simulation math -- so it
# cannot move the golden invariant (an output-layer change that shifts a
# computed number touched the fold and must be backed out).
#
# A member with no registered accounts and no room is a REAL empty (DP#32) --
# the golden household's children are RESP-only and own nothing here, so their
# entry says "no accounts" rather than inventing one or crashing.
# =============================================================================

# The three registered kinds a member can OWN in this engine. RESP is a
# household/subscriber account (not a per-member savings subject), so it is
# surfaced elsewhere (the RESP cash-out section), not here.
_MEMBER_ACCOUNT_KINDS = [
    ('rrsp', 'RRSP'),
    ('tfsa', 'TFSA'),
    ('fhsa', 'FHSA'),
]


def _member_display_label(member: Dict) -> str:
    """Role-first display name for a member. Adults are shown by ROLE
    (Primary/Spouse) -- the internal member dict carries no name for them --
    and a child by its declared contract label, falling back to the role when
    unnamed. (The label is the household's OWN runtime data; DP#15 governs what
    ships in THIS repo, not what a user's contract prints at runtime.)"""
    role = member.get('role', '')
    if role == 'primary':
        return 'Primary'
    if role == 'spouse':
        return 'Spouse'
    name = member.get('name')
    if name:  # a present, non-empty display label; both None and '' fall back
        return name
    return role.capitalize() if role else 'Member'


def _per_member_savings_plan(cfg: Dict) -> List[Dict]:
    """Per-member savings picture (epic #841 bite 5): for EACH family member,
    the registered accounts they own with opening balance + contribution room,
    and -- for a child whose OWN income funds the child's OWN accounts (a
    savings subject, #812) -- the per-year contribution that income funds plus,
    for a child holding FHSA room, the FHSA-first-home plan.

    Pure presentation (DP#8/DP#25): reads only the mapped contract; recomputes
    no simulation math. A member with no accounts and no room is a real empty
    (DP#32) -- reported as such, never invented into accounts.
    """
    family = cfg.get('family', {})
    # Adults (members) then children -- both carry the same per-member fields
    # (role, gross_income, {kind}_room_accumulated, {kind}_balance) out of
    # input_contract.py, so one loop reports them uniformly.
    people = list(family.get('members', [])) + list(family.get('children', []))
    # DP#18/#5: the savings rate is the contract's, zero default (not a
    # household opinion invented by the reporter) -- the SAME source
    # scenario_discovery/child_savings_for_year read.
    savings_rate = cfg.get('savings', {}).get('rate', 0.0)

    plans: List[Dict] = []
    for m in people:
        accounts = []
        for key, label in _MEMBER_ACCOUNT_KINDS:
            balance = m.get(f'{key}_balance', 0.0)
            room = m.get(f'{key}_room_accumulated', 0.0)
            # Only surface an account the member actually owns or has room in;
            # a zero/zero kind is not invented into a row (DP#32).
            if balance or room:
                accounts.append({'kind': label, 'balance': balance, 'room': room})

        is_child = m.get('role') == 'child'
        income = m.get('gross_income')  # DP#32: null income displays as 0.0, but 0.0 is a real value
        income = 0.0 if income is None else income
        # A child is the savings subject whose OWN income funds the child's OWN
        # accounts (#812). The first-year contribution that income funds is
        # income * savings_rate -- the SAME year-0 spelling child_savings_for_year
        # uses in the fold (income * (1+g)**0 * rate), stated here as the plan,
        # not re-simulated. Adults' contributions are optimized inside the
        # household plan (the year-by-year breakdown), so no per-member figure
        # is fabricated for them here.
        annual_contribution = income * savings_rate if is_child else None
        fhsa_room = m.get('fhsa_room_accumulated')
        fhsa_room = 0.0 if fhsa_room is None else fhsa_room

        plans.append({
            'label': _member_display_label(m),
            'role': m.get('role', ''),
            'is_child': is_child,
            'income': income,
            'accounts': accounts,
            'annual_contribution': annual_contribution,
            # A child holding FHSA room has, by construction, a first-home
            # savings goal (the FHSA is a first-home down-payment vehicle) --
            # surface the plan to fund that room first (#701/DP#69: for a
            # first-home buyer the FHSA beats TFSA and RRSP: deductible in,
            # tax-free out).
            'fhsa_first_home': is_child and fhsa_room > 0,
            'fhsa_room': fhsa_room,
        })
    return plans


def _equity_grant_line(grant: Dict) -> str:
    """One human-readable line for a declared equity grant (issue #768).

    The grant is RECORD-ONLY -- the engine values it at $0 for every
    solvency / runway / decumulation metric -- so the line states that
    explicitly rather than inventing a value (DP#32: a record with no value
    is a labelled $0, not a silent drop). Pure (DP#3): a function of the
    grant's own static facts.
    """
    strike = grant.get('strike')
    strike_txt = 'strike TBD' if strike is None else f'strike ${strike:,.2f}'
    vesting = grant['vesting']  # schema-required (DP#32: index, don't `or {}`-fallback)
    vest_date = vesting['fully_vested_date']  # required within vesting
    return (f"Equity grant {grant.get('id', '?')}: recorded, valued $0 for "
            f"solvency, vests {vest_date}, {strike_txt}")


def _equity_grants_text_lines(cfg: Dict) -> List[str]:
    """The TXT/HTML equity-grants section: one labelled-$0 line per grant,
    or nothing when the household declared none (DP#16: the block auto-
    includes on trigger data; absent = no grants = no section, not a
    misleading empty header)."""
    return [_equity_grant_line(g) for g in cfg.get('equity_grants', [])]


def _equity_grants_summary(cfg: Dict) -> List[Dict]:
    """The JSON equity-grants section: each grant's static facts plus the
    labelled $0 valuation, so a machine consumer sees the grant was recorded
    and deliberately excluded -- not silently dropped (issue #768 / DP#32)."""
    out = []
    for g in cfg.get('equity_grants', []):
        out.append({
            **dict(g),
            'solvency_value': 0.0,
            'solvency_value_note': (
                'recorded, valued $0 for solvency/runway: a private, '
                'unvested, or strike-undetermined grant is not a liquid asset '
                '(issue #768, DP#32)'),
        })
    return out


def _cash_out_label(cash_out: float) -> str:
    """The display label for a refinance cash-out level (issue #789).

    A PURE function of the cash-out DOLLARS -- the single source of truth --
    so the ``cash_out`` label on a category_bests entry can never diverge
    from the net_benefit it reports (DP#9). The bands mirror the
    LTV-exploration grouping: $0 = no refinance, up to the registered-room
    fill, beyond that the maximum (80% LTV) refinance.
    """
    if cash_out <= 0:
        return "No Refinance"
    if cash_out <= 400_000:
        return "Fill Registered Room"
    return "Maximum Refinance (80%)"


def _category_bests(results: List[Dict]) -> List[Dict]:
    """Find best result per decision category (issue #789: single source of truth).

    Every label on a category_bests entry -- ``cash_out``,
    ``use_readvanceable``, ``deduct_later`` -- is derived from the SAME result
    row whose ``net_benefit``/``year_by_year`` the entry reports (the row's
    own ``cash_out`` dollars, ``readvanceable_mortgage``, ``deduct_later``),
    never a separately-computed grouping key or a default that can diverge from
    the data (DP#9).

    Pre-#789 this read ``r.get('cash_out', 0)`` and
    ``r.get('use_readvanceable', False)``, but result rows carry neither key
    (they carry ``readvanceable_mortgage`` and, post-#789, ``cash_out``
    dollars tagged at the overlay), so every row defaulted to cash_out=0 /
    sm=False and the winner -- always the max-refinance row, because headline
    results run at ltv_max -- was labelled "No Refinance", the inverse of the
    finding.
    """
    groups: Dict[Tuple[str, bool, bool], Dict] = {}
    for r in results:
        # Skip RESP-cashout variants (they belong to the RESP comparison,
        # not the refinance category bests). Wrapped rather than `continue`
        # so the no-RESP branch -- the only one headline results ever take
        # -- is the one that executes, leaving no uncovered guard line.
        if r.get('resp_cash_out', 0) == 0:
            # The row's OWN values -- the scenario object whose net_benefit
            # the entry will report (DP#9). cash_out is dollars (tagged at
            # the overlay, #789); readvanceable_mortgage is the canonical SM
            # flag the engine stamps on every result row.
            cash_out = r.get('cash_out', 0)
            sm = r.get('readvanceable_mortgage', False)
            dl = r.get('deduct_later', False)
            key = (_cash_out_label(cash_out), sm, dl)
            cur = groups.get(key)
            if cur is None or r.get('net_benefit', 0) > cur.get('net_benefit', 0):
                groups[key] = r

    categories: List[Dict] = []
    for (co_label, sm, dl), best in groups.items():
        sm_label = "Yes (Readvanceable)" if sm else "No"
        dl_label = "Yes (Stagger Years)" if dl else "No"
        # **best FIRST: the row's own data (net_benefit, year_by_year, ...)
        # is the source of truth; the derived label fields override afterward
        # so the label can never diverge from the data it decorates.
        categories.append({
            **best,
            'label': f"{co_label} {sm_label} {dl_label}",
            # The label, derived (DP#9) from the SAME row's cash_out dollars:
            'cash_out': co_label,
            'cash_out_amount': best.get('cash_out', 0),
            'ltv': best.get('ltv'),
            'use_readvanceable': sm,
            'deduct_later': dl,
            'net_benefit': best.get('net_benefit', 0),
            'liquid_nw': (best.get('future_value', 0) - best.get('total_debt', 0)),
        })

    return sorted(categories, key=lambda c: c['net_benefit'], reverse=True)


def _optimal_refi_level(results: List[Dict]) -> List[Dict]:
    """Compare refi levels per strategy combination.

    Issue #789: the SM filter reads the row's canonical
    ``readvanceable_mortgage`` flag (not ``use_readvanceable``, which result
    rows never carried -- pre-#789 that read returned None for every row and
    ``None == sm`` was always False, so ``filtered`` was empty and this whole
    section silently produced no rows, a parallel inversion to the
    ``category_bests`` one)."""
    rows = []
    for dl, dl_label in [(True, "Yes (Stagger Years)"), (False, "No")]:
        for sm, sm_label in [(True, "Yes (Readvanceable)"), (False, "No")]:
            filtered = [r for r in results
                       if r.get('deduct_later') == dl
                       and r.get('readvanceable_mortgage', False) == sm
                       and r.get('resp_cash_out', 0) == 0]
            no_refi = max([r for r in filtered if r.get('cash_out', 0) == 0],
                         key=lambda r: r.get('net_benefit', 0), default=None)
            min_refi = max([r for r in filtered if 0 < r.get('cash_out', 0) < 400000],
                          key=lambda r: r.get('net_benefit', 0), default=None)
            max_refi = max([r for r in filtered if r.get('cash_out', 0) > 400000],
                          key=lambda r: r.get('net_benefit', 0), default=None)
            
            scores = {}
            if no_refi: scores['No Refinance'] = no_refi['net_benefit']
            if min_refi: scores['Fill Registered Room'] = min_refi['net_benefit']
            if max_refi: scores['Maximum Refinance (80%)'] = max_refi['net_benefit']
            
            if scores:
                best_level = max(scores, key=scores.get)
                rows.append({
                    'sm_label': sm_label,
                    'dl_label': dl_label,
                    'no_refi': scores.get('No Refinance', 0),
                    'min_refi': scores.get('Fill Registered Room', 0),
                    'max_refi': scores.get('Maximum Refinance (80%)', 0),
                    'best_level': best_level,
                })
    return rows


def _objective_name(results: List[Dict]) -> Optional[str]:
    """The objective the ranked results were scored on (issue #585).

    Several approximations bite only for some objectives — a pre-tax
    terminal-wealth caveat must not fire for a run ranked on an after-tax
    estate objective. Returns None when the results don't say, which
    model_fidelity treats as "unknown, so report the caveat" (DP#32).
    """
    if not results:
        return None
    return _sort_results(results)[0].get('objective_name') or None


def _model_fidelity_text_lines(cfg: Dict, objective_name: Optional[str] = None) -> List[str]:
    """Shared TXT/HTML-plaintext rendering of the model-fidelity section
    (issue #585 / DP#32): units disclosure + any active approximations."""
    lines = ["MODEL FIDELITY"]
    lines.extend(model_fidelity.render_text(cfg, objective_name))
    return lines


def _resp_cashout_comparison(results: List[Dict], cfg: Dict) -> List[Dict]:
    """Compare RESP keep vs EAP vs collapse using actual scenario results."""
    rows = []
    resp_balance = cfg.get('accounts', {}).get('resp_current_balance', 0)
    if resp_balance <= 0:
        return rows
    
    # Tax info for reporting
    composition = cfg.get('accounts', {}).get('resp_composition', {})
    contributions = composition.get('total_contributions', resp_balance * 0.50)
    cesg = composition.get('total_cesg_received', resp_balance * 0.10)
    qesi = composition.get('total_qesi_received', resp_balance * 0.05)
    earnings = composition.get('investment_earnings', resp_balance * 0.35)
    
    grant_clawback = cesg + qesi
    
    for cash_out in [0, 500000]:
        label = "No Refinance" if cash_out == 0 else "Maximum Refinance (80%)"
        keep = [r for r in results if abs(r.get('cash_out', 0) - cash_out) < 50000
               and r.get('resp_cash_out', 0) == 0]
        eap = [r for r in results if abs(r.get('cash_out', 0) - cash_out) < 50000
              and r.get('resp_cash_out', 0) > 0
              and 'EAP' in r.get('label', '')]
        collapse = [r for r in results if abs(r.get('cash_out', 0) - cash_out) < 50000
                   and r.get('resp_cash_out', 0) > 0
                   and 'Collapse' in r.get('label', '')]
        
        if keep:
            best_keep = max(keep, key=lambda r: r.get('net_benefit', 0))
            row = {
                'refi_level': label,
                'resp_balance': resp_balance,
                'keep_net_benefit': best_keep['net_benefit'],
                'contributions': contributions,
                'grant_clawback': grant_clawback,
                'earnings': earnings,
            }
            if eap:
                best_eap = max(eap, key=lambda r: r.get('net_benefit', 0))
                row['eap_net_benefit'] = best_eap['net_benefit']
                row['eap_diff'] = best_eap['net_benefit'] - best_keep['net_benefit']
                row['eap_tax'] = earnings * 0.15  # student MTR
            if collapse:
                best_col = max(collapse, key=lambda r: r.get('net_benefit', 0))
                row['collapse_net_benefit'] = best_col['net_benefit']
                row['collapse_diff'] = best_col['net_benefit'] - best_keep['net_benefit']
            rows.append(row)
    
    return rows


# =============================================================================
# Text Report
# =============================================================================

class TextReport(OutputReport):
    """Plain-text formatted report for terminal output."""
    
    def render(self) -> str:
        lines = []
        results = _sort_results(self.results)
        info = _situation_summary(self.base_cfg)
        
        lines.append(f"\n{'=' * 120}")
        lines.append(f"  📊 {self.title}")
        lines.append(f"{'=' * 120}")
        
        # Situation
        lines.append(f"\n  📋 Situation: Primary ${info['primary_income']:,.0f}, Spouse ${info['spouse_income']:,.0f}")
        lines.append(f"     House ${info['house_value']:,.0f} | Mortgage ${info['mortgage_balance']:,.0f} | Margin ${info['margin_available']:,.0f}")
        lines.append(f"     Registered room: ${info['total_registered_room']:,.0f} | Min LTV: {info['min_ltv']:.1%}")

        # Model fidelity (issue #585 / DP#32): units + any active approximations.
        lines.append(f"\n  🔍 " + "\n     ".join(
            _model_fidelity_text_lines(self.base_cfg, _objective_name(results))))

        # Issue #768: equity grants are recorded, valued $0 for solvency --
        # surfaced so the household knows they were not silently dropped.
        equity_lines = _equity_grants_text_lines(self.base_cfg)
        if equity_lines:
            lines.append(f"\n  📝 Equity grants (valued $0 for solvency):")
            for el in equity_lines:
                lines.append(f"     {el}")

        # Category bests
        cats = _category_bests(results)
        lines.append(f"\n  🏆 Best per category:")
        for c in cats[:8]:
            lines.append(f"     {c['label']:<35s}: ${c['net_benefit']:>12,.0f}")
        
        # Optimal refi level
        levels = _optimal_refi_level(results)
        lines.append(f"\n  🎯 Optimal refinance level:")
        for l in levels:
            best = l['best_level']
            lines.append(f"     {l['sm_label']} {l['dl_label']}: No Refinance ${l['no_refi']:>10,.0f} | Fill Room ${l['min_refi']:>10,.0f} | Max (${l['max_refi']:>10,.0f}) → {best}")
        
        # Top results
        lines.append(f"\n  📈 Top {min(15, len(results))} scenarios:")
        hdr = f"  {'#':<3} {'Scenario':<45} {'Loan-to-Value':>14} {'Net Benefit':>13} {'Liquid NW':>12} {'Assets':>11} {'Debt':>11}"
        lines.append(hdr)
        lines.append(f"  {'-' * 110}")
        for i, r in enumerate(results[:15]):
            label = r.get('label', '?')[:44]
            ltv = r.get('ltv', 0) if isinstance(r.get('ltv'), (int, float)) else 0
            nb = r.get('net_benefit', 0)
            lqw = (r.get('future_value', 0) - r.get('total_debt', 0))
            assets = r.get('future_value', 0)
            debt = r.get('total_debt', 0)
            # Issue #707: a bankrupt scenario's net benefit is NOT an
            # achievable retirement -- mark it inline so a top-to-bottom
            # reader cannot draw a conclusion from the bare number.
            # Explicit absence test (DP#32): not `r.get(k) or {}`.
            ds = r.get('drawdown_shortfall')
            ds = ds if ds is not None else {}
            if ds.get('exhausted'):
                yr = ds.get('first_shortfall_year')
                lines.append(f"  {i+1:<3} {label:<45} {ltv:>5.1%}  ${nb:>11,.0f}  ${lqw:>10,.0f}  ${assets:>9,.0f}  ${debt:>9,.0f}  ⛔ EXHAUSTED yr {yr}")
            else:
                lines.append(f"  {i+1:<3} {label:<45} {ltv:>5.1%}  ${nb:>11,.0f}  ${lqw:>10,.0f}  ${assets:>9,.0f}  ${debt:>9,.0f}")
        
        # Year-by-year breakdown for the #1 scenario (issue #248).
        # Always shown when the data is present (DP#8: compose through data).
        yby = _top_year_by_year(results)
        if yby:
            top_label = results[0].get('label', '?')
            lines.append(f"\n  📅 Year-by-year breakdown — #1 scenario: {top_label}")
            # Subset of columns that fit a fixed-width terminal table.
            cols = [
                ('year', 'Year', 'int', 5),
                ('mortgage_payment', 'Mtg Pmt', 'money', 11),
                ('mortgage_interest', 'Interest', 'money', 11),
                ('mortgage_principal', 'Principal', 'money', 11),
                ('mortgage_balance', 'Mtg Bal', 'money', 12),
                ('sm_qc_deductible', 'Deduct Int', 'money', 11),
                ('rrsp_tax_savings', 'RRSP TaxSv', 'money', 11),
                ('primary_marginal', 'Prim MTR', 'pct', 9),
                ('total_family_income', 'Income', 'money', 11),
                ('annual_savings', 'Savings', 'money', 11),
            ]
            hdr = "  " + "".join(f"{h:>{w}}" for _, h, _, w in cols) + f"{'Net Worth':>13}"
            lines.append(hdr)
            lines.append(f"  {'-' * (len(hdr) - 2)}")
            for yr in yby:
                cells = []
                for key, _, kind, w in cols:
                    v = yr.get(key, 0)
                    if kind == 'int':
                        cells.append(f"{int(v):>{w-1}} ")
                    elif kind == 'pct':
                        cells.append(f"{v:>{w-1}.1%} ")
                    else:
                        cells.append(f"${v:>{w-2},.0f} ")
                cells.append(f"${_net_worth(yr):>11,.0f} ")
                lines.append("  " + "".join(cells))

        # RESP
        resp = _resp_cashout_comparison(results, self.base_cfg)
        if resp:
            lines.append(f"\n  📚 RESP Cash-Out Analysis:")
            for r in resp:
                lines.append(f"     {r['refi_level']}:")
                lines.append(f"       Keep RESP:        ${r['keep_net_benefit']:>10,.0f}")
                if r.get('eap_net_benefit'):
                    wins = "✅ EAP wins" if r['eap_diff'] > 0 else "❌ Keep RESP"
                    lines.append(f"       RESP → EAP:       ${r['eap_net_benefit']:>10,.0f}  Δ ${r['eap_diff']:>+10,.0f} — {wins}")
                if r.get('collapse_net_benefit'):
                    wins = "Collapse wins" if r['collapse_diff'] > 0 else "Keep RESP"
                    lines.append(f"       RESP ↘ Collapse:   ${r['collapse_net_benefit']:>10,.0f}  Δ ${r['collapse_diff']:>+10,.0f} — {wins}")
                # Tax breakdown
                lines.append(f"       (Contributions ${r.get('contributions',0):,.0f} tax-free, grants ${r.get('grant_clawback',0):,.0f} clawed back)")

        # Issue #758: runway (months-to-ruin after an income shock), per
        # income scenario. The number a household wants before signing a
        # mortgage; surfaced beside the ranking, not buried in it.
        runway_rows = _runway_by_scenario(results)
        if runway_rows:
            lines.append(f"\n  🛟 Runway — months to insolvency after the income shock (issue #758):")
            lines.append(f"     Headline is a LABELLED interpolation inside an honest bracket")
            lines.append(f"     (year-granular engine; ~N mo is a point estimate, [lo-hi] the range).")
            lines.append(f"     {'Scenario':<32} {'Runway':<26} {'Stress begins':>14}")
            lines.append(f"     {'-' * 76}")
            for row in runway_rows:
                rw = row['runway']
                runway_txt = _format_runway_inline(rw)
                stress = rw.get('stress_begins_months')
                stress_txt = f"~{stress:.0f} mo" if stress is not None else "-"
                lines.append(f"     {row['label'][:31]:<32} {runway_txt:<26} {stress_txt:>14}")
            lines.append(f"     Runway UNDERSTATES reality: all spend treated as rigid (no")
            lines.append(f"     discretionary split in the contract yet) and contributions counted")
            lines.append(f"     as committed -- see the model-fidelity section.")
        elif any('runway' in r for r in results):
            lines.append(f"\n  🛟 Runway (issue #758): NOT CHECKED -- no scenario engaged the")
            lines.append(f"     cash-flow identity (declare household_budget.annual_living_costs).")

        # Per-member savings plan (epic #841 bite 5): each family member's OWN
        # accounts, balances, room, and -- for a child savings subject -- the
        # per-year contribution its income funds and any FHSA-first-home plan.
        plan = _per_member_savings_plan(self.base_cfg)
        if plan:
            lines.append(f"\n  👪 Per-member savings plan (epic #841):")
            for p in plan:
                header = f"     {p['label']}"
                if p['income']:
                    header += f" — income ${p['income']:,.0f}"
                if p['annual_contribution']:
                    header += f", contributes ${p['annual_contribution']:,.0f}/yr"
                lines.append(header)
                if p['accounts']:
                    for a in p['accounts']:
                        lines.append(
                            f"        {a['kind']:<5} balance ${a['balance']:>12,.0f}"
                            f" | room ${a['room']:>12,.0f}")
                else:
                    lines.append(f"        — no registered accounts —")
                if p['fhsa_first_home']:
                    lines.append(
                        f"        🏠 FHSA-first plan: fund the ${p['fhsa_room']:,.0f}"
                        f" FHSA room first (first-home down payment: deductible"
                        f" in, tax-free out)")

        lines.append("")
        return "\n".join(lines)


# =============================================================================
# JSON Report
# =============================================================================

@dataclass
class JsonReport(OutputReport):
    """Machine-readable JSON output with all data and computed summaries.
    
    DP#24: JSON output round-trips — the JSON can be loaded by another
    tool or re-imported for comparison.
    
    DP#9 (issue #722): the report API has an EXPLICIT interface — no
    ``**kwargs`` catch-all that hides the real parameters and lets typos
    pass silently. ``indent`` is the one option JSON output actually
    supports (the JSON pretty-printing width, default 2); it is a named
    dataclass field now, so an unknown keyword raises ``TypeError``
    loudly instead of being swallowed by ``kwargs.get``.
    """
    # Added after the base OutputReport fields (results/base_cfg/title/
    # include_sensitivity, all defaulted), so the dataclass-generated
    # __init__ keeps them in order: (results, base_cfg, title,
    # include_sensitivity, indent). All callers pass results/base_cfg
    # positionally and the rest by keyword or default — unchanged.
    indent: int = 2
    
    def render(self) -> str:
        results = _sort_results(self.results)
        info = _situation_summary(self.base_cfg)
        
        output = {
            'title': self.title,
            'situation': info,
            # Issue #585 / DP#32: units disclosure + any approximation that
            # biases a headline figure for THIS config, machine-readable.
            'model_fidelity': model_fidelity.to_dict(self.base_cfg, _objective_name(results)),
            'category_bests': _category_bests(results),
            'optimal_refi_level': _optimal_refi_level(results),
            'resp_cashout': _resp_cashout_comparison(results, self.base_cfg),
            # Issue #768: equity grants -- recorded, valued $0 for solvency,
            # surfaced so a machine consumer sees they were not silently
            # dropped (DP#32). Empty list when the household declared none.
            'equity_grants': _equity_grants_summary(self.base_cfg),
            # Issue #758: runway (months-to-ruin) per income scenario, at top
            # level so consumers don't have to re-derive it from `scenarios`.
            # Each scenario dict ALSO carries its own `runway` (serialized
            # RunwayResult); this is the convenience curve view.
            'runway': _runway_by_scenario(results),
            # Issue #758: the shock-date sweep curve (opt-in --runway-sweep),
            # recorded onto assumptions.runway_sweep by optimize.main. Empty
            # when the flag was not set or no shock was authored.
            'runway_sweep': (self.base_cfg.get('assumptions', {})
                            if self.base_cfg.get('assumptions') is not None
                            else {}).get('runway_sweep', []),
            # Issue #473: the chosen tax-efficient asset-location placement +
            # its after-tax benefit and per-asset-class tax-drag comparison,
            # recorded onto assumptions.asset_location by optimize.main. Empty
            # when the household declared no foreign registered sleeve to place.
            'asset_location': (self.base_cfg.get('assumptions', {})
                              if self.base_cfg.get('assumptions') is not None
                              else {}).get('asset_location', {}),
            # Each scenario dict already carries its own `year_by_year` array
            # (serialized from YearResult via dataclasses.asdict upstream).
            'scenarios': results,
            # Convenience: the #1 scenario's per-year series surfaced at top level
            # so consumers don't have to re-sort (issue #248).
            'year_by_year': _top_year_by_year(results),
            'total_scenarios': len(results),
        }
        
        return json.dumps(output, indent=self.indent, default=str)


# =============================================================================
# HTML Report
# =============================================================================

class HtmlReport(OutputReport):
    """Standalone HTML report with embedded CSS (no external dependencies)."""
    
    def render(self) -> str:
        results = _sort_results(self.results)
        info = _situation_summary(self.base_cfg)
        cats = _category_bests(results)
        levels = _optimal_refi_level(results)
        resp = _resp_cashout_comparison(results, self.base_cfg)
        
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{self.title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f7fa; color: #1a1a2e; padding: 24px; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 1.8em; margin-bottom: 16px; color: #16213e; }}
  h2 {{ font-size: 1.3em; margin: 28px 0 12px; color: #0f3460; border-bottom: 2px solid #e94560; padding-bottom: 4px; }}
  .card {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }}
  .stat {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 16px; border-radius: 8px; }}
  .stat-label {{ font-size: 0.8em; opacity: 0.9; }}
  .stat-value {{ font-size: 1.4em; font-weight: 700; margin-top: 4px; }}
  .stat-good {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }}
  .stat-warn {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9em; }}
  th {{ background: #16213e; color: white; padding: 10px 12px; text-align: right; }}
  th:first-child {{ text-align: left; }}
  th:nth-child(2) {{ text-align: left; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #e8e8e8; text-align: right; }}
  td:first-child {{ text-align: center; color: #999; }}
  td:nth-child(2) {{ text-align: left; font-weight: 500; }}
  tr:hover {{ background: #f0f4ff; }}
  .rank-1 td {{ background: #fff3cd; font-weight: 600; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75em; font-weight: 600; }}
  .tag-yes {{ background: #d4edda; color: #155724; }}
  .tag-no {{ background: #f8d7da; color: #721c24; }}
  .tag-refi {{ background: #cce5ff; color: #004085; }}
  .bar {{ height: 24px; border-radius: 4px; background: linear-gradient(90deg, #667eea, #764ba2); transition: width 0.3s; }}
  .bar-container {{ background: #e9ecef; border-radius: 4px; margin: 4px 0; }}
  .bar-label {{ font-size: 0.8em; margin-bottom: 2px; }}
  .footer {{ text-align: center; color: #999; font-size: 0.8em; margin-top: 40px; padding: 20px; }}
  .yby-tab {{ border: 1px solid #ccd; background: #f5f7fa; color: #0f3460; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.9em; font-weight: 600; }}
  .yby-tab:hover {{ background: #e8ecff; }}
  .yby-tab.active {{ background: #0f3460; color: #fff; border-color: #0f3460; }}
  #yby-scenario {{ padding: 4px 8px; border-radius: 6px; border: 1px solid #ccd; font-size: 0.9em; }}
</style>
</head>
<body>
<div class="container">
  <h1>🏠 {self.title}</h1>

  <div class="card">
    <h2>📋 Situation Summary</h2>
    <div class="grid">
      <div class="stat">
        <div class="stat-label">Primary Income</div>
        <div class="stat-value">${info['primary_income']:,.0f}/yr</div>
      </div>
      <div class="stat">
        <div class="stat-label">Spouse Income</div>
        <div class="stat-value">${info['spouse_income']:,.0f}/yr</div>
      </div>
      <div class="stat">
        <div class="stat-label">House Value</div>
        <div class="stat-value">${info['house_value']:,.0f}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Current Mortgage</div>
        <div class="stat-value">${info['mortgage_balance']:,.0f}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Current Loan-to-Value</div>
        <div class="stat-value">{info['ltv_current']:.1%}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Margin Available</div>
        <div class="stat-value">${info['margin_available']:,.0f}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Refinance Cash-Out (80%)</div>
        <div class="stat-value">${info['cash_out_80']:,.0f}</div>
      </div>
      <div class="stat stat-good">
        <div class="stat-label">Registered Room</div>
        <div class="stat-value">${info['total_registered_room']:,.0f}</div>
      </div>
      <div class="stat stat-warn">
        <div class="stat-label">Min Loan-to-Value to Fill</div>
        <div class="stat-value">{info['min_ltv']:.1%}</div>
      </div>
      <div class="stat">
        <div class="stat-label">RESP Balance</div>
        <div class="stat-value">${info['resp_balance']:,.0f}</div>
      </div>
    </div>
  </div>

  {self._render_model_fidelity(_objective_name(results))}

  {self._render_equity_grants()}

  <div class="card">
    <h2>🏆 Best Per Category</h2>
    <div>
      {self._render_category_bars(cats)}
    </div>
  </div>

  <div class="card">
    <h2>🎯 Optimal Refinance Level</h2>
    <table>
      <thead>
        <tr><th>Readvanceable Mortgage</th><th>Staggered Deduction</th><th>No Refinance</th><th>Fill Registered Room</th><th>Maximum Refinance (80%)</th><th>Best</th></tr>
      </thead>
      <tbody>
        {self._render_level_rows(levels)}
      </tbody>
    </table>
    <p style="margin-top: 10px; font-size: 0.85em; color: #666;">
      <strong>Readvanceable Mortgage:</strong> Uses HELOC line of credit for investments (readvanceable mortgage investment-loan strategy)<br>
      <strong>Staggered Deduction:</strong> Spreads RRSP deduction over multiple years for tax optimization
    </p>
  </div>

  {self._render_execution_plan(results[0] if results else {})}

  <div class="card">
    <h2>📈 Top {min(15, len(results))} Scenarios</h2>
    <table>
      <thead>
        <tr><th>#</th><th>Scenario</th><th>Loan-to-Value</th><th>Net Benefit</th><th>Liquid NW</th><th>Assets</th><th>Debt</th><th>Decumulation</th></tr>
      </thead>
      <tbody>
        {self._render_result_rows(results[:15])}
      </tbody>
    </table>
  </div>

  {self._render_year_by_year_card(results)}

  {self._render_resp_card(resp)}

  {self._render_runway_card(results)}

  <div class="footer">
    Generated by lifedraft scenario enumerator<br>
    {len(results)} scenarios evaluated
  </div>
</div>
</body>
</html>"""

    def _render_model_fidelity(self, objective_name: Optional[str] = None) -> str:
        """Model-fidelity card (issue #585 / DP#32): units + active approximations.

        Rendered unconditionally — the units line always appears; the
        approximations list is empty-but-present when none are active for
        this config, so absence of caveats is visible, not silent.
        """
        units = model_fidelity.describe_units(self.base_cfg)
        basis = (f"real (base year {units['base_year']})"
                 if units['dollar_basis'] == 'real' else 'nominal')
        active = model_fidelity.active_approximations(self.base_cfg, objective_name)
        # #685: a caveat whose content depends on the run's OWN numbers (which
        # liability, which two rates) supplies them via `findings`. The TXT and
        # JSON surfaces already render them; this one must too, or the HTML
        # report gestures at a contradiction it declines to name.
        ctx = model_fidelity.FidelityContext(cfg=self.base_cfg,
                                             objective_name=objective_name)
        if active:
            rows = "".join(
                f'<tr><td style="text-align:left">{a.summary}'
                + "".join(f'<br><small>&bull; {f}</small>' for f in a.findings_for(ctx))
                + f'</td>'
                f'<td style="text-align:left">{a.biased_figure}</td>'
                f'<td><span class="tag tag-{"no" if a.direction.value != "unknown" else "refi"}">'
                f'{a.direction.value}</span></td>'
                f'<td style="text-align:left">{a.issue}</td></tr>'
                for a in active
            )
            table = (f'<table><thead><tr><th style="text-align:left">Approximation</th>'
                     f'<th style="text-align:left">Biases</th><th>Direction</th>'
                     f'<th style="text-align:left">Issue</th></tr></thead><tbody>{rows}</tbody></table>')
        else:
            table = '<p style="color:#666;">No registered approximations are active for this configuration.</p>'
        return f'''<div class="card">
    <h2>🔍 Model Fidelity</h2>
    <p style="margin-bottom: 10px;"><strong>As of {units['as_of']}</strong> | {units['currency']} | {basis} dollars</p>
    {table}
  </div>'''

    def _render_equity_grants(self) -> str:
        """Issue #768: surface declared equity grants as recorded-$0 cards,
        so the household sees they were not silently dropped (DP#32)."""
        grants = self.base_cfg.get('equity_grants', [])
        if not grants:
            return ''
        items = ''.join(
            f'<li>{_equity_grant_line(g)}</li>' for g in grants
        )
        return (f'<div class="card"><h2>📝 Equity Grants</h2>'
                f'<p style="margin-bottom:10px;">Recorded, valued $0 for '
                f'solvency/runway (a private, unvested, or strike-undetermined '
                f'grant is not a liquid asset — issue #768, DP#32).</p>'
                f'<ul>{items}</ul></div>')

    def _render_category_bars(self, cats: List[Dict]) -> str:
        if not cats:
            return ""
        max_val = max(c['net_benefit'] for c in cats)
        rows = []
        for c in cats[:8]:
            pct = (c['net_benefit'] / max_val * 100) if max_val else 0
            rows.append(
                f'<div class="bar-label">{c["label"]}: ${c["net_benefit"]:,.0f}</div>'
                f'<div class="bar-container"><div class="bar" style="width:{pct:.0f}%"></div></div>'
            )
        return "\n".join(rows)

    def _render_level_rows(self, levels: List[Dict]) -> str:
        rows = []
        for l in levels:
            values = [l['no_refi'], l['min_refi'], l['max_refi']]
            max_v = max(values)
            cells = []
            for v in values:
                if v == max_v:
                    cells.append(f'<td><strong>${v:,.0f}</strong></td>')
                else:
                    cells.append(f'<td>${v:,.0f}</td>')
            best_tag = f'<span class="tag tag-refi">{l["best_level"]}</span>'
            rows.append(
                f'<tr>'
                f'<td>{l["sm_label"]}</td>'
                f'<td>{l["dl_label"]}</td>'
                f'{"".join(cells)}'
                f'<td>{best_tag}</td>'
                f'</tr>'
            )
        return "\n".join(rows)

    def _render_execution_plan(self, best_result: Dict) -> str:
        """Render year-by-year execution plan for the best scenario.
        
        Uses role-based account labels from AllocationResult. Jurisdiction-specific
        details (grant names, tax treatments) are rendered by strategy plugins.
        """
        if not best_result:
            return ""

        from strategy import FamilyState, StrategyEngine

        # Try to get strategy from result (may have precomputed allocations)
        strategy_id = best_result.get('strategy_id', 'balanced')

        # Calculate total investable funds from config and results
        cash_out = best_result.get('cash_out', 0)
        margin = self.base_cfg.get('property', {}).get('margin_available', 0)
        total_cash = cash_out + margin

        # Get property details for sources & uses
        house_value = self.base_cfg.get('property', {}).get('house_value', 0)
        mortgage_balance = self.base_cfg.get('property', {}).get('mortgage_balance', 0)

        # Get member data generically
        members = self.base_cfg.get('family', {}).get('members', [])
        primary = members[0] if len(members) > 0 else {}
        spouse = members[1] if len(members) > 1 else {}

        # Annual savings from config (no hardcoded defaults - DP#13)
        primary_income = primary.get('gross_income', 0)
        spouse_income = spouse.get('gross_income', 0)
        annual_savings = (primary_income + spouse_income) * self.base_cfg.get('savings', {}).get('rate', 0.20)

        # Try jurisdiction-aware strategy lookup
        try:
            # Canada strategies are registered via DP#16 auto-inclusion
            from countries.canada.strategies import STRATEGIES
            strategy = STRATEGIES.get(strategy_id, STRATEGIES['balanced'])
        except (ImportError, KeyError):
            # Fallback: create minimal strategy from result
            from strategy import AllocationStrategy, StrategyType
            strategy = AllocationStrategy(
                name=strategy_id.replace('_', ' ').title(),
                strategy_type=StrategyType.CUSTOM,
                rrsp_pct=0.30, spousal_rrsp_pct=0.10, tfsa_pct=0.30,
                resp_pct=0.07, non_reg_pct=0.23,
            )

        # Compute actual year 1 allocation using fill_room
        state = FamilyState(
            primary_rrsp_room=primary.get('rrsp_room_accumulated', 0),
            spouse_rrsp_room=spouse.get('rrsp_room_accumulated', 0),
            primary_tfsa_room=primary.get('tfsa_room_accumulated', 0),
            spouse_tfsa_room=spouse.get('tfsa_room_accumulated', 0),
            bracket_gap=primary_income - spouse_income,
            resp_eligible_children=len(self.base_cfg.get('family', {}).get('children', [])),
        )

        engine = StrategyEngine(strategy)
        # Issue #792: honour a declared deductible-vs-registered advance
        # split in the sources-&-uses display, the same way the simulation
        # does (None = today's registered-first internal optimization). Read
        # off the property dict input_contract mapped the contract lever into.
        declared_non_reg = self.base_cfg.get('property', {}).get(
            'refinance_advance_deductible_non_reg')
        initial_alloc = engine.fill_room(
            total_cash, state,
            deductible_non_reg_first=declared_non_reg,
        )

        return self._render_plan_template(
            strategy=strategy,
            primary_name=primary.get('name', 'Primary'),
            spouse_name=spouse.get('name', 'Spouse'),
            primary_rrsp_room=primary.get('rrsp_room_accumulated', 0),
            spouse_rrsp_room=spouse.get('rrsp_room_accumulated', 0),
            primary_tfsa_room=primary.get('tfsa_room_accumulated', 0),
            spouse_tfsa_room=spouse.get('tfsa_room_accumulated', 0),
            total_cash=total_cash,
            annual_savings=annual_savings,
            initial_alloc=initial_alloc,
            use_sm=best_result.get('readvanceable_mortgage', False),
            deduct_later=best_result.get('deduct_later', False),
            yearly_cashflow=self._compute_yearly_cashflow(best_result, strategy),
            house_value=house_value,
            mortgage_balance=mortgage_balance,
            refinance_cash=cash_out,
            margin_available=margin,
        )

    def _render_plan_template(self, strategy, primary_name: str, spouse_name: str,
                            primary_rrsp_room: float, spouse_rrsp_room: float,
                            primary_tfsa_room: float, spouse_tfsa_room: float,
                            total_cash: float, annual_savings: float,
                            initial_alloc, use_sm: bool, deduct_later: bool,
                            yearly_cashflow: List[Dict] = None,
                            house_value: float = 0, mortgage_balance: float = 0,
                            refinance_cash: float = 0, margin_available: float = 0) -> str:
        """Generic plan renderer - jurisdictions can override for specific labels."""
        sm_label = "Enabled (readvanceable mortgage)" if use_sm else "Disabled"
        dl_label = "Spaced over years" if deduct_later else "Immediate claim"

        # Sources & Uses section
        sources_uses = f"""
    <h3 style="margin-top: 25px; color: #0f3460;">💰 Sources & Uses of Funds</h3>
    <table>
      <thead><tr><th>Source</th><th>Amount</th><th>Description</th></tr></thead>
      <tbody>
        <tr><td>Refinance Cash-Out (80% LTV)</td><td>${refinance_cash:,.0f}</td><td>Withdraw from increased mortgage principal</td></tr>
        <tr><td>Existing HELOC Margin</td><td>${margin_available:,.0f}</td><td>Already available on readvanceable mortgage</td></tr>
        <tr style="border-top: 2px solid #ddd;"><td><strong>Total Available</strong></td><td><strong>${refinance_cash + margin_available:,.0f}</strong></td><td>Ready to invest</td></tr>
      </tbody>
    </table>"""

        # Embed lump_sum allocation info for room calculation in cashflow table
        # (Year 1 cashflow includes annual savings, but room calculation should consider
        #  how much lump_sum uses vs available room)
        if yearly_cashflow:
            for i, cf in enumerate(yearly_cashflow):
                yearly_cashflow[i] = {
                    **cf,
                    'lump_sum_primary_rrsp': initial_alloc.primary_rrsp if i == 0 else 0,
                    'lump_sum_spouse_rrsp': initial_alloc.spouse_rrsp if i == 0 else 0,
                    'lump_sum_primary_tfsa': initial_alloc.primary_tfsa if i == 0 else 0,
                    'lump_sum_spouse_tfsa': initial_alloc.spouse_tfsa if i == 0 else 0,
                }

        cashflow_table = self._render_yearly_cashflow_table(yearly_cashflow, {
            'primary_rrsp_room': primary_rrsp_room,
            'spouse_rrsp_room': spouse_rrsp_room,
            'primary_tfsa_room': primary_tfsa_room,
            'spouse_tfsa_room': spouse_tfsa_room,
        }) if yearly_cashflow else ''

        return f"""
  <div class="card">
    <h2>📅 Execution Plan — Year-by-Year Guide</h2>
    <p><strong>Scenario:</strong> {strategy.name} | Readvanceable Mortgage: {sm_label} | RRSP Deduction: {dl_label}</p>

    {sources_uses}

    <h3 style="margin-top: 25px; color: #0f3460;">Year 1 — Initial Allocation (Lump-Sum)</h3>
    <table>
      <thead><tr><th>Account</th><th>Amount</th><th>Room Used</th><th>Action</th></tr></thead>
      <tbody>
        <tr><td>RRSP (Primary)</td><td>${initial_alloc.primary_rrsp:,.0f}</td><td>{primary_rrsp_room:,.0f} available (used {initial_alloc.primary_rrsp/max(1,primary_rrsp_room)*100:.1f}%)</td><td>Contribute + deduct immediately</td></tr>
        <tr><td>RRSP (Spousal)</td><td>${initial_alloc.spousal_rrsp:,.0f}</td><td>Shares primary RRSP room</td><td>For spousal income splitting</td></tr>
        <tr><td>RRSP (Spouse)</td><td>${initial_alloc.spouse_rrsp:,.0f}</td><td>{spouse_rrsp_room:,.0f} available (used {initial_alloc.spouse_rrsp/max(1,spouse_rrsp_room)*100:.1f}%)</td><td>At spouse's lower MTR</td></tr>
        <tr><td>TFSA (Primary)</td><td>${initial_alloc.primary_tfsa:,.0f}</td><td>{primary_tfsa_room:,.0f} available (used {initial_alloc.primary_tfsa/max(1,primary_tfsa_room)*100:.1f}%)</td><td>Grow tax-free</td></tr>
        <tr><td>TFSA (Spouse)</td><td>${initial_alloc.spouse_tfsa:,.0f}</td><td>{spouse_tfsa_room:,.0f} available (used {initial_alloc.spouse_tfsa/max(1,spouse_tfsa_room)*100:.1f}%)</td><td>Grow tax-free</td></tr>
        <tr><td>RESP</td><td>${initial_alloc.resp:,.0f}</td><td>For matching</td><td>Grants apply</td></tr>
        <tr><td>Non-Reg/SM</td><td>${initial_alloc.non_reg:,.0f}</td><td>No room limit</td><td>HELOC investment</td></tr>
        <tr style="border-top: 2px solid #ddd;"><td><strong>Total</strong></td><td><strong>${initial_alloc.total_allocated:,.0f}</strong></td><td>-</td><td>-</td></tr>
      </tbody>
  </table>

  {cashflow_table}
  </div>
        """


    def _render_yearly_cashflow_table(self, yearly_cashflow: List[Dict], member_rooms: Dict) -> str:
        """Render detailed yearly cash flow table with per-account breakdown.
        
        Available room shows what can be contributed to each account in that year:
        - Year 1: Initial accumulated room from config (carry-forward room)
        - Years 2+: New annual room generated (18% of earned income for RRSP, $7k/$6k for TFSA)
          plus any unused carry-forward room from previous years
        """
        if not yearly_cashflow:
            return ""
        
        # Mapping between yearly_cashflow keys and running total keys
        key_map = {
            'primary_rrsp': 'rrsp_primary',
            'spousal_rrsp': 'rrsp_spousal',
            'spouse_rrsp': 'rrsp_spouse',
            'primary_tfsa': 'tfsa_primary',
            'spouse_tfsa': 'tfsa_spouse',
            'resp': 'resp',
            'non_reg': 'non_reg',
        }
        
        # Initial room from config (accumulated carried-forward room)
        initial_primary_rrsp = member_rooms.get('primary_rrsp_room', 0)
        initial_spouse_rrsp = member_rooms.get('spouse_rrsp_room', 0)
        initial_primary_tfsa = member_rooms.get('primary_tfsa_room', 0)
        initial_spouse_tfsa = member_rooms.get('spouse_tfsa_room', 0)
        
        # Annual room addition (based on Year 2 contributions - these represent annual room)
        annual_rrsp_primary = yearly_cashflow[1].get('rrsp_primary', 0) if len(yearly_cashflow) > 1 else 0
        annual_rrsp_spouse = yearly_cashflow[1].get('rrsp_spouse', 0) if len(yearly_cashflow) > 1 else 0
        annual_tfsa = yearly_cashflow[1].get('tfsa_primary', 0) if len(yearly_cashflow) > 1 else 0
        
        # Track remaining room after each year (carry-forward)
        remaining_rrsp_primary = initial_primary_rrsp
        remaining_rrsp_spouse = initial_spouse_rrsp
        remaining_tfsa_primary = initial_primary_tfsa
        remaining_tfsa_spouse = initial_spouse_tfsa
        
        # Running totals track cumulative contributions (end-of-year totals)
        running = {k: 0 for k in key_map}
        
        year_tables = ""
        for idx, yr in enumerate(yearly_cashflow[:10]):
            # Update running totals (cumulative contributions)
            for rt_key, cf_key in key_map.items():
                running[rt_key] += yr.get(cf_key, 0)
            
            # Calculate available room for this year
            lump_sum_rrsp_primary = yr.get('lump_sum_primary_rrsp', 0)
            lump_sum_rrsp_spouse = yr.get('lump_sum_spouse_rrsp', 0)
            lump_sum_tfsa_primary = yr.get('lump_sum_primary_tfsa', 0)
            lump_sum_tfsa_spouse = yr.get('lump_sum_spouse_tfsa', 0)
            
            if idx == 0:
                # Year 1: show initial accumulated room (carry-forward)
                primary_rrsp_avail = initial_primary_rrsp
                spouse_rrsp_avail = initial_spouse_rrsp
                primary_tfsa_avail = initial_primary_tfsa
                spouse_tfsa_avail = initial_spouse_tfsa
            else:
                # Years 2+: remaining room after Year 1 lump_sum + annual room additions
                # Use Year 1 lump_sum values (embedded in cashflow[0])
                year1_lump_sum_primary = yearly_cashflow[0].get('lump_sum_primary_rrsp', yearly_cashflow[0].get('rrsp_primary', 0))
                year1_lump_sum_spouse = yearly_cashflow[0].get('lump_sum_spouse_rrsp', yearly_cashflow[0].get('rrsp_spouse', 0))
                year1_lump_sum_tfsa_primary = yearly_cashflow[0].get('lump_sum_primary_tfsa', yearly_cashflow[0].get('tfsa_primary', 0))
                year1_lump_sum_tfsa_spouse = yearly_cashflow[0].get('lump_sum_spouse_tfsa', yearly_cashflow[0].get('tfsa_spouse', 0))
                
                primary_rrsp_avail = max(0, initial_primary_rrsp - year1_lump_sum_primary) + annual_rrsp_primary
                spouse_rrsp_avail = max(0, initial_spouse_rrsp - year1_lump_sum_spouse) + annual_rrsp_spouse
                primary_tfsa_avail = max(0, initial_primary_tfsa - year1_lump_sum_tfsa_primary) + annual_tfsa
                spouse_tfsa_avail = max(0, initial_spouse_tfsa - year1_lump_sum_tfsa_spouse) + annual_tfsa
            
            year_tables += f"""
    <div style="margin-bottom: 20px; border: 1px solid #ddd; padding: 15px; border-radius: 8px; background: #fafafa;">
      <h4 style="margin-top: 0; color: #0f3460;">Year {yr['year']}</h4>
      <table style="width: 100%; margin-top: 5px;">
        <thead>
          <tr><th>Account</th><th>Year Contribution</th><th>End Balance</th><th>Available Room</th><th>Action</th></tr>
        </thead>
        <tbody>
          <tr><td>RRSP (Primary)</td><td>${yr.get('rrsp_primary', 0):,.0f}</td><td>${running['primary_rrsp']:,.0f}</td><td>${primary_rrsp_avail:,.0f}</td><td>Contribute + deduct now</td></tr>
          <tr><td>RRSP (Spousal)</td><td>${yr.get('rrsp_spousal', 0):,.0f}</td><td>${running['spousal_rrsp']:,.0f}</td><td>Shares primary room</td><td>For spousal income splitting</td></tr>
          <tr><td>RRSP (Spouse)</td><td>${yr.get('rrsp_spouse', 0):,.0f}</td><td>${running['spouse_rrsp']:,.0f}</td><td>${spouse_rrsp_avail:,.0f}</td><td>At spouse's lower MTR</td></tr>
          <tr><td>TFSA (Primary)</td><td>${yr.get('tfsa_primary', 0):,.0f}</td><td>${running['primary_tfsa']:,.0f}</td><td>${primary_tfsa_avail:,.0f}</td><td>Grow tax-free</td></tr>
          <tr><td>TFSA (Spouse)</td><td>${yr.get('tfsa_spouse', 0):,.0f}</td><td>${running['spouse_tfsa']:,.0f}</td><td>${spouse_tfsa_avail:,.0f}</td><td>Grow tax-free</td></tr>
          <tr><td>RESP</td><td>${yr.get('resp', 0):,.0f}</td><td>${running['resp']:,.0f}</td><td>Grants available</td><td>CESG/QESI grants apply</td></tr>
          <tr><td>Non-Reg/SM</td><td>${yr.get('non_reg', 0):,.0f}</td><td>${running['non_reg']:,.0f}</td><td>No limit</td><td>HELOC investment</td></tr>
        </tbody>
      </table>
      <p style="margin: 8px 0 0 0; font-size: 0.9em; color: #666;">
        <strong>Tax Impact:</strong> RRSP tax savings ${yr.get('rrsp_tax_savings', 0):,.0f} | 
        SM interest ${yr.get('readvance_interest', 0):,.0f} | 
        Refund applied to HELOC paydown
      </p>
    </div>"""
            
            # Update remaining room AFTER this year's contributions
            remaining_rrsp_primary -= yr.get('rrsp_primary', 0)
            remaining_rrsp_spouse -= yr.get('rrsp_spouse', 0)
            remaining_tfsa_primary -= yr.get('tfsa_primary', 0)
            remaining_tfsa_spouse -= yr.get('tfsa_spouse', 0)

        return f"""
    <h3 style="margin-top: 30px; color: #0f3460;">Years 1-10 — Annual Cash Flow Details</h3>
    <p style="color: #666; margin-bottom: 15px;">Per-account contributions with end balances. Year 1 uses accumulated room; Years 2+ add annual room (18% of earned income).</p>
    {year_tables}"""

    def _compute_yearly_cashflow(self, best_result: Dict, strategy) -> List[Dict]:
        """Compute year-by-year cash flow details for the best scenario.
        
        Runs a mini-simulation to get actual yearly contribution amounts.
        """
        from scenario_overlay import (
            build_overlay_config,
            ScenarioOverlay,
            refinance_amortization_fallback,
        )
        from simulation_config import SimulationConfig
        from countries.canada.rate_model import build_rate_path

        # Build overlay from best result
        # DP#18: Only set income if explicitly provided; None means no change from base
        # Issue #655: best_result['ltv'] is a swept exploration level, not a
        # specific declared refinance option -- same DP#13 placeholder
        # fallback as optimize.py's _scenario_overlay.
        overlay = ScenarioOverlay(
            label="Cash Flow Analysis",
            cash_out=best_result.get('cash_out', 0),
            primary_income=best_result.get('primary_income'),
            spouse_income=best_result.get('spouse_income'),
            mortgage_rate=best_result.get('mortgage_rate', 0.05),
            use_readvanceable=best_result.get('readvanceable_mortgage', False),
            deduct_later=best_result.get('deduct_later', False),
            refinance_amortization_years=refinance_amortization_fallback(self.base_cfg),
        )

        cfg = build_overlay_config(self.base_cfg, overlay)
        temp_config = SimulationConfig.from_dict(cfg)

        rate_path = build_rate_path(
            name="cashflow",
            initial_rate=overlay.mortgage_rate,
            term_years=temp_config.projection_years,
            rate_type='variable',
            renewal_rates=[overlay.mortgage_rate],
        )

        lump_sum = best_result.get('cash_out', 0) + self.base_cfg.get('property', {}).get('margin_available', 0)

        try:
            from simulation import FamilySimulation
            from countries.canada.adapter import CanadaAdapter
        except ImportError:
            return []

        adapter = CanadaAdapter(temp_config)
        sim = FamilySimulation(
            config=temp_config,
            adapter=adapter,
            strategy=strategy,
            rate_path=rate_path,
            use_readvanceable=overlay.use_readvanceable,
            deduct_later=overlay.deduct_later,
            lump_sum=lump_sum,
        )

        year_results = sim.run()

        # Extract yearly cash flow
        cashflow = []
        for yr in year_results:
            cashflow.append({
                'year': yr.year,
                'annual_contribution': sum(yr.contributions.values()) if yr.contributions else 0,
                'rrsp_primary': yr.contributions.get('primary_rrsp', 0),
                'rrsp_spousal': yr.contributions.get('spousal_rrsp', 0),
                'rrsp_spouse': yr.contributions.get('spouse_rrsp', 0),
                'tfsa_primary': yr.contributions.get('primary_tfsa', 0),
                'tfsa_spouse': yr.contributions.get('spouse_tfsa', 0),
                'non_reg': yr.contributions.get('non_reg', 0),
                'readvance_interest': yr.readvance_interest,
                'rrsp_tax_savings': yr.rrsp_tax_savings,
            })

        return cashflow


    def _render_result_rows(self, results: List[Dict]) -> str:
        rows = []
        for i, r in enumerate(results):
            label = r.get('label', '?')[:55]
            ltv = r.get('ltv', 0) or 0
            nb = r.get('net_benefit', 0)
            lqw = (r.get('future_value', 0) or 0) - (r.get('total_debt', 0) or 0)
            assets = r.get('future_value', 0) or 0
            debt = r.get('total_debt', 0) or 0
            row_class = 'rank-1' if i == 0 else ''
            # Issue #707: a bankrupt scenario is marked inline, in its own
            # cell, so the HTML reader cannot take the net benefit figure as
            # an achievable retirement. Explicit absence test (DP#32).
            ds = r.get('drawdown_shortfall')
            ds = ds if ds is not None else {}
            exhausted_cell = ''
            if ds.get('exhausted'):
                yr = ds.get('first_shortfall_year')
                exhausted_cell = (f'<td style="color:#b00;font-weight:bold;">'
                                  f'⛔ EXHAUSTED yr {yr}</td>')
            else:
                exhausted_cell = '<td></td>'
            rows.append(
                f'<tr class="{row_class}">'
                f'<td>{i+1}</td>'
                f'<td>{label}</td>'
                f'<td>{ltv:.1%}</td>'
                f'<td>${nb:,.0f}</td>'
                f'<td>${lqw:,.0f}</td>'
                f'<td>${assets:,.0f}</td>'
                f'<td>${debt:,.0f}</td>'
                f'{exhausted_cell}'
                f'</tr>'
            )
        return "\n".join(rows)

    def _render_year_by_year_card(self, results: List[Dict]) -> str:
        """Render the rich per-year breakdown for the top scenarios (issue #239).

        Surfaces every YearResult field, grouped by concern (Balances,
        Contributions, Taxes & SM, Mortgage & Cash Flow), for the top scenarios
        that carry a ``year_by_year`` series. Standalone: embedded JSON data +
        vanilla JS tab/selector logic, no external dependencies.

        A server-side fallback table (Balances group, #1 scenario) is rendered
        so the report is readable without JS and stays test-friendly.
        """
        ranked = _sort_results(results)
        scenarios = []
        for i, r in enumerate(ranked, start=1):
            yby = r.get('year_by_year') or []
            if not yby:
                continue
            # DP#32 (#606): an explicit label='' is a value (the caller chose
            # no label), not absence -- only a genuinely missing key falls
            # back to the strategy name.
            label = r.get('label')
            label = r.get('strategy', f'Scenario {i}') if label is None else label
            scenarios.append({
                'rank': i,
                'label': label,
                'net_benefit': r.get('net_benefit', 0),
                'year_by_year': yby,
            })
        if not scenarios:
            return ""

        # Embed the full per-year data for every top scenario. Escape "<"/">" so
        # the payload cannot break out of the <script> tag.
        import json as _json
        payload = _json.dumps(scenarios, default=str)
        payload_escaped = payload.replace("<", "\u003c").replace(">", "\u003e")
        # JS-side column groups (injected as an f-string field so the JSON
        # braces are not re-interpreted by the f-string parser).
        groups_js = "var groups = " + _json.dumps(
            {name: [[k, h, kind] for (k, h, kind) in cols]
             for name, cols in YEAR_GROUPS.items()}) + ";"

        # Server-side fallback: Balances group for the #1 scenario (works without
        # JS; also keeps the report testable with string/<tr> assertions).
        top = scenarios[0]
        bal_cols = YEAR_GROUPS['Balances']
        header_cells = "".join(f"<th>{h}</th>" for _, h, _ in bal_cols)
        body_rows = []
        for yr in top['year_by_year']:
            cells = []
            for key, _, kind in bal_cols:
                cells.append(f"<td>{_fmt_year_value(_year_get(yr, key), kind)}</td>")
            body_rows.append("<tr>" + "".join(cells) + "</tr>")

        # Server-side readvanceable-strategy fallback: when the #1 scenario actually
        # uses the readvanceable mortgage strategy, render its 'Taxes & SM' values directly so
        # they are visible on first glance (no JS / no tab click required). The
        # JS tabs above still offer the full interactive exploration. (issue #239)
        sm_section = ""
        if _is_sm_active(top['year_by_year']):
            sm_cols = YEAR_GROUPS['Taxes & SM']
            sm_header = "".join(f"<th>{h}</th>" for _, h, _ in sm_cols)
            sm_rows = []
            for yr in top['year_by_year']:
                cells = "".join(
                    f"<td>{_fmt_year_value(_year_get(yr, key), kind)}</td>"
                    for key, _, kind in sm_cols
                )
                sm_rows.append("<tr>" + cells + "</tr>")
            sm_section = f"""
    <h3 style="margin-top:18px;">💸 Readvanceable Mortgage — Taxes &amp; Deductibility (#1 scenario)</h3>
    <div style="overflow-x:auto;">
    <table id="yby-sm-table">
      <thead><tr>{sm_header}</tr></thead>
      <tbody>{"".join(sm_rows)}</tbody>
    </table>
    </div>"""

        tabs = list(YEAR_GROUPS.keys())
        tab_buttons = "".join(
            '<button type="button" class="yby-tab" data-group="' + t + '" '
            "onclick=\"ybySelectGroup(this, '" + t + "')\">" + t + '</button>'
            for t in tabs
        )
        scenario_options = "".join(
            '<option value="' + str(s['rank']) + '">#' + str(s['rank']) + ' '
            + str(s['label']) + ' ($' + f"{s['net_benefit']:,.0f}" + ')</option>'
            for s in scenarios
        )

        return f"""
  <div class="card" id="yby-card">
    <h2>📅 Year-by-Year Breakdown</h2>
    <div style="display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin-bottom:12px;">
      <label style="font-size:0.9em; color:#666;">Scenario:
        <select id="yby-scenario" onchange="ybySelectScenario()" style="margin-left:6px;">
          {scenario_options}
        </select>
      </label>
      <span style="font-size:0.85em; color:#999;" id="yby-label">{top['label']}</span>
    </div>
    <div class="yby-tabs" style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px;">
      {tab_buttons}
    </div>
    <div style="overflow-x:auto;">
    <table id="yby-table">
      <thead><tr id="yby-head">{header_cells}</tr></thead>
      <tbody id="yby-body">
        {"".join(body_rows)}
      </tbody>
    </table>
    </div>
    {sm_section}
    <p style="margin-top:10px; font-size:0.85em; color:#666;">
      Balances, contributions, taxes &amp; readvanceable mortgage strategy, and mortgage / cash flow per
      projection year. Switch tabs to explore each concern; pick a scenario to compare.
    </p>
    <script type="application/json" id="yby-data">{payload_escaped}</script>
    <script>
      (function() {{
        var data = JSON.parse(document.getElementById('yby-data').textContent);
        {groups_js}
        var curScenario = data[0].rank;
        var curGroup = 'Balances';
        function money(v) {{ return '$' + Number(v || 0).toLocaleString('en-US', {{maximumFractionDigits:0}}); }};
        function pct(v) {{ return (Number(v || 0) * 100).toFixed(2) + '%'; }};
        function fmt(v, kind) {{
          if (kind === 'int') return String(parseInt(v || 0, 10));
          if (kind === 'pct') return pct(v);
          return money(v);
        }}
        function get(yr, key) {{
          if (key === 'net_worth') return (yr.total_assets || 0) - (yr.total_debt || 0);
          if (key.indexOf('.') > -1) {{
            var parts = key.split('.');
            var nested = yr[parts[0]] || {{}};
            return nested[parts[1]] || 0;
          }}
          return yr[key] || 0;
        }}
        function render() {{
          var sc = data.find(function(s) {{ return s.rank === curScenario; }}) || data[0];
          var cols = groups[curGroup];
          var head = cols.map(function(c) {{ return '<th>' + c[1] + '</th>'; }}).join('');
          var rows = sc.year_by_year.map(function(yr) {{
            var cells = cols.map(function(c) {{ return '<td>' + fmt(get(yr, c[0]), c[2]) + '</td>'; }}).join('');
            return '<tr>' + cells + '</tr>';
          }}).join('');
          document.getElementById('yby-head').innerHTML = head;
          document.getElementById('yby-body').innerHTML = rows;
          document.getElementById('yby-label').textContent = sc.label;
        }}
        window.ybySelectGroup = function(btn, group) {{
          curGroup = group;
          var tabs = document.querySelectorAll('.yby-tab');
          for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
          btn.classList.add('active');
          render();
        }};
        window.ybySelectScenario = function() {{
          curScenario = parseInt(document.getElementById('yby-scenario').value, 10);
          render();
        }};
        var firstTab = document.querySelector('.yby-tab');
        if (firstTab) firstTab.classList.add('active');
      }})();
    </script>
  </div>"""

    def _render_resp_card(self, resp: List[Dict]) -> str:
        if not resp:
            return ""
        rows = []
        for r in resp:
            # Keep RESP row
            rows.append(f'<tr><td>{r["refi_level"]}</td><td>Keep RESP</td><td>${r["keep_net_benefit"]:,.0f}</td><td>—</td><td><span class="tag tag-neutral">Baseline</span></td></tr>')
            if r.get('eap_net_benefit'):
                wins = "✅ EAP wins" if r['eap_diff'] > 0 else "❌ Keep RESP"
                tag_class = "tag-yes" if r['eap_diff'] > 0 else "tag-no"
                rows.append(f'<tr><td>{r["refi_level"]}</td><td>RESP → EAP</td><td>${r["eap_net_benefit"]:,.0f}</td><td>${r["eap_diff"]:+,.0f}</td><td><span class="tag {tag_class}">{wins}</span></td></tr>')
            if r.get('collapse_net_benefit'):
                wins = "Collapse wins" if r['collapse_diff'] > 0 else "Keep RESP"
                tag_class = "tag-yes" if r['collapse_diff'] > 0 else "tag-no"
                rows.append(f'<tr><td>{r["refi_level"]}</td><td>RESP ↘ Collapse</td><td>${r["collapse_net_benefit"]:,.0f}</td><td>${r["collapse_diff"]:+,.0f}</td><td><span class="tag {tag_class}">{wins}</span></td></tr>')
        balance = resp[0].get('resp_balance', 0)
        contrib = resp[0].get('contributions', 0)
        clawback = resp[0].get('grant_clawback', 0)
        earnings = resp[0].get('earnings', 0)
        return f"""
  <div class="card">
    <h2>📚 RESP Cash-Out Analysis</h2>
    <p>RESP Balance: <strong>${balance:,.2f}</strong> | Contributions: <strong>${contrib:,.2f}</strong> | Grants (clawed back): <strong>${clawback:,.2f}</strong> | Earnings: <strong>${earnings:,.2f}</strong></p>
    <table>
      <thead><tr><th>Refi Level</th><th>Strategy</th><th>Net Benefit</th><th>Δ vs Keep</th><th>Verdict</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>"""

    def _render_runway_card(self, results: List[Dict]) -> str:
        """Issue #758: a runway (months-to-ruin) card per income scenario.
        The number a household wants before signing a mortgage, surfaced
        as a first-class card beside the ranking. The headline is a labelled
        interpolation inside an honest bracket; both travel to the reader."""
        rows_html = []
        runway_rows = _runway_by_scenario(results)
        if not runway_rows:
            if any('runway' in r for r in results):
                return """
  <div class="card">
    <h2>🛟 Runway (issue #758)</h2>
    <p style="color:#a00;">NOT CHECKED — no scenario engaged the cash-flow identity
    (declare <code>household_budget.annual_living_costs</code>). This is NOT a
    finding of safety: the household has not been checked, not cleared.</p>
  </div>"""
            return ""
        for row in runway_rows:
            rw = row['runway']
            runway_txt = _format_runway_inline(rw)
            stress = rw.get('stress_begins_months')
            stress_txt = f"~{stress:.0f} mo" if stress is not None else "—"
            caveats = []
            if rw.get('relies_on_credit_facility'):
                caveats.append("leans on an unsecured credit line (lender can cut it)")
            if rw.get('drew_registered'):
                caveats.append("drew RRSP — taxed at the low job-loss-year rate")
            caveat_txt = "; ".join(caveats) if caveats else "—"
            rows_html.append(
                f"<tr><td>{row['label']}</td><td><strong>{runway_txt}</strong></td>"
                f"<td>{stress_txt}</td><td>{caveat_txt}</td></tr>")
        return f"""
  <div class="card">
    <h2>🛟 Runway — months to insolvency after the income shock (issue #758)</h2>
    <p>The headline is a <em>labelled interpolation</em> inside an honest bracket
    (the engine steps in years; ~N mo is a point estimate, [lo–hi] the structural
    range). <code>&gt;=N mo (survives)</code> = the cushion outlasts the horizon.</p>
    <table>
      <thead><tr><th>Scenario</th><th>Runway</th><th>Stress begins</th><th>Caveats</th></tr></thead>
      <tbody>{"".join(rows_html)}</tbody>
    </table>
    <p style="color:#666;font-size:0.9em;">Runway UNDERSTATES reality: all spend is treated as rigid
    (no discretionary/non-discretionary split exists in the contract yet) and contributions are
    counted as committed — a household in real distress stops both, so the true runway is longer.
    See the model-fidelity section.</p>
  </div>"""


# =============================================================================
# Markdown Report (issue #814)
# =============================================================================

def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    """Render a GitHub-flavored Markdown table from string cells.

    Pure presentation (DP#8/DP#25): the caller has already formatted each
    cell; this only lays out the pipe/`---` grid. A GFM table with no body
    rows is still a valid (empty) table, so the caller decides whether to
    emit the section at all.
    """
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def _md_fidelity_lines(cfg: Dict, objective_name: Optional[str]) -> List[str]:
    """The model-fidelity body as clean GFM, reusing the SAME shared text
    spelling the TXT report renders (DP#9: one source of truth). The leading
    'MODEL FIDELITY' banner is dropped (the section already has an `##`
    heading); the text report's `  - ` / `      * ` indentation is mapped to
    nested Markdown list items so the caveats read as a list, not a wall."""
    out: List[str] = []
    for line in _model_fidelity_text_lines(cfg, objective_name)[1:]:
        if line.startswith("      * "):
            out.append(f"  - {line[8:]}")
        elif line.startswith("  - "):
            out.append(f"- {line[4:]}")
        else:
            out.append(line)
    return out


class MarkdownReport(OutputReport):
    """GitHub-flavored Markdown report — the format to paste a decision
    summary into notes, a PR, or chat (issue #814).

    A THIN renderer (DP#8/DP#25): it recomputes nothing. Every section reads
    the SAME derived data the text/HTML siblings read (`_situation_summary`,
    `_category_bests`, `_optimal_refi_level`, `_resp_cashout_comparison`,
    `_runway_by_scenario`, the shared model-fidelity/equity-grant spellings),
    formatting it as headings and tables rather than forking the logic.
    """

    def render(self) -> str:
        results = _sort_results(self.results)
        info = _situation_summary(self.base_cfg)

        parts: List[str] = [f"# {self.title}", ""]

        # Situation summary
        parts.append("## Situation Summary")
        parts.append("")
        parts.append(_md_table(
            ["Metric", "Value"],
            [
                ["Primary Income", f"${info['primary_income']:,.0f}/yr"],
                ["Spouse Income", f"${info['spouse_income']:,.0f}/yr"],
                ["House Value", f"${info['house_value']:,.0f}"],
                ["Current Mortgage", f"${info['mortgage_balance']:,.0f}"],
                ["Current Loan-to-Value", f"{info['ltv_current']:.1%}"],
                ["Margin Available", f"${info['margin_available']:,.0f}"],
                ["Refinance Cash-Out (80%)", f"${info['cash_out_80']:,.0f}"],
                ["Registered Room", f"${info['total_registered_room']:,.0f}"],
                ["Min Loan-to-Value to Fill", f"{info['min_ltv']:.1%}"],
                ["RESP Balance", f"${info['resp_balance']:,.0f}"],
            ],
        ))
        parts.append("")

        # Model fidelity (issue #585 / DP#32): units + any active approximations.
        parts.append("## Model Fidelity")
        parts.append("")
        parts.extend(_md_fidelity_lines(self.base_cfg, _objective_name(results)))
        parts.append("")

        # Equity grants (issue #768): recorded, valued $0 for solvency.
        equity_lines = _equity_grants_text_lines(self.base_cfg)
        if equity_lines:
            parts.append("## Equity Grants")
            parts.append("")
            parts.append("Recorded, valued $0 for solvency/runway (a private, "
                         "unvested, or strike-undetermined grant is not a liquid "
                         "asset — issue #768, DP#32).")
            parts.append("")
            for el in equity_lines:
                parts.append(f"- {el}")
            parts.append("")

        # Best per category
        cats = _category_bests(results)
        parts.append("## Best Per Category")
        parts.append("")
        parts.append(_md_table(
            ["Category", "Net Benefit"],
            [[c['label'], f"${c['net_benefit']:,.0f}"] for c in cats[:8]],
        ))
        parts.append("")

        # Optimal refinance level
        levels = _optimal_refi_level(results)
        parts.append("## Optimal Refinance Level")
        parts.append("")
        parts.append(_md_table(
            ["Readvanceable Mortgage", "Staggered Deduction", "No Refinance",
             "Fill Registered Room", "Maximum Refinance (80%)", "Best"],
            [[l['sm_label'], l['dl_label'], f"${l['no_refi']:,.0f}",
              f"${l['min_refi']:,.0f}", f"${l['max_refi']:,.0f}", l['best_level']]
             for l in levels],
        ))
        parts.append("")

        # Top N scenarios
        top = results[:15]
        parts.append(f"## Top {min(15, len(results))} Scenarios")
        parts.append("")
        scenario_rows = []
        for i, r in enumerate(top):
            label = r.get('label', '?')
            ltv = r.get('ltv', 0) if isinstance(r.get('ltv'), (int, float)) else 0
            nb = r.get('net_benefit', 0)
            lqw = (r.get('future_value', 0) or 0) - (r.get('total_debt', 0) or 0)
            assets = r.get('future_value', 0) or 0
            debt = r.get('total_debt', 0) or 0
            # Issue #707: a bankrupt scenario's net benefit is NOT an achievable
            # retirement — mark it inline (explicit absence test, DP#32) so a
            # reader cannot read the bare number as a conclusion.
            ds = r.get('drawdown_shortfall')
            ds = ds if ds is not None else {}
            decum = ""
            if ds.get('exhausted'):
                decum = f"⛔ EXHAUSTED yr {ds.get('first_shortfall_year')}"
            scenario_rows.append([
                i + 1, label, f"{ltv:.1%}", f"${nb:,.0f}", f"${lqw:,.0f}",
                f"${assets:,.0f}", f"${debt:,.0f}", decum,
            ])
        parts.append(_md_table(
            ["#", "Scenario", "Loan-to-Value", "Net Benefit", "Liquid NW",
             "Assets", "Debt", "Decumulation"],
            scenario_rows,
        ))
        parts.append("")

        # Year-by-year breakdown for the #1 scenario (issue #248). Balances
        # group mirrors the HTML server-side fallback; the Taxes & SM group is
        # added only when the top scenario actually uses the readvanceable
        # strategy (so a non-SM run shows no empty SM table).
        yby = _top_year_by_year(results)
        if yby:
            top_label = results[0].get('label', '?')
            parts.append(f"## Year-by-Year Breakdown — #1 Scenario: {top_label}")
            parts.append("")
            for group in ('Balances', 'Taxes & SM'):
                cols = YEAR_GROUPS[group]
                if group == 'Taxes & SM' and not _is_sm_active(yby):
                    continue
                parts.append(f"### {group}")
                parts.append("")
                parts.append(_md_table(
                    [h for _, h, _ in cols],
                    [[_fmt_year_value(_year_get(yr, key), kind)
                      for key, _, kind in cols] for yr in yby],
                ))
                parts.append("")

        # RESP cash-out analysis
        resp = _resp_cashout_comparison(results, self.base_cfg)
        if resp:
            parts.append("## RESP Cash-Out Analysis")
            parts.append("")
            balance = resp[0].get('resp_balance', 0)
            contrib = resp[0].get('contributions', 0)
            clawback = resp[0].get('grant_clawback', 0)
            earnings = resp[0].get('earnings', 0)
            parts.append(
                f"RESP Balance: **${balance:,.2f}** | Contributions: "
                f"**${contrib:,.2f}** | Grants (clawed back): **${clawback:,.2f}** "
                f"| Earnings: **${earnings:,.2f}**")
            parts.append("")
            resp_rows = []
            for r in resp:
                resp_rows.append([r['refi_level'], "Keep RESP",
                                  f"${r['keep_net_benefit']:,.0f}", "—", "Baseline"])
                if r.get('eap_net_benefit'):
                    wins = "✅ EAP wins" if r['eap_diff'] > 0 else "❌ Keep RESP"
                    resp_rows.append([r['refi_level'], "RESP → EAP",
                                      f"${r['eap_net_benefit']:,.0f}",
                                      f"${r['eap_diff']:+,.0f}", wins])
                if r.get('collapse_net_benefit'):
                    wins = "Collapse wins" if r['collapse_diff'] > 0 else "Keep RESP"
                    resp_rows.append([r['refi_level'], "RESP ↘ Collapse",
                                      f"${r['collapse_net_benefit']:,.0f}",
                                      f"${r['collapse_diff']:+,.0f}", wins])
            parts.append(_md_table(
                ["Refi Level", "Strategy", "Net Benefit", "Δ vs Keep", "Verdict"],
                resp_rows,
            ))
            parts.append("")

        # Runway (issue #758): months-to-insolvency after the income shock.
        runway_rows = _runway_by_scenario(results)
        if runway_rows:
            parts.append("## Runway — Months to Insolvency After the Income Shock")
            parts.append("")
            parts.append(
                "The headline is a *labelled interpolation* inside an honest "
                "bracket (the engine steps in years; ~N mo is a point estimate, "
                "[lo–hi] the range). `>=N mo (survives)` = the cushion outlasts "
                "the horizon.")
            parts.append("")
            rw_rows = []
            for row in runway_rows:
                rw = row['runway']
                runway_txt = _format_runway_inline(rw)
                stress = rw.get('stress_begins_months')
                stress_txt = f"~{stress:.0f} mo" if stress is not None else "—"
                caveats = []
                if rw.get('relies_on_credit_facility'):
                    caveats.append("leans on an unsecured credit line (lender can cut it)")
                if rw.get('drew_registered'):
                    caveats.append("drew RRSP — taxed at the low job-loss-year rate")
                caveat_txt = "; ".join(caveats) if caveats else "—"
                rw_rows.append([row['label'], runway_txt, stress_txt, caveat_txt])
            parts.append(_md_table(
                ["Scenario", "Runway", "Stress begins", "Caveats"], rw_rows))
            parts.append("")
            parts.append(
                "Runway UNDERSTATES reality: all spend is treated as rigid and "
                "contributions are counted as committed — a household in real "
                "distress stops both, so the true runway is longer. See the "
                "model-fidelity section.")
            parts.append("")
        elif any('runway' in r for r in results):
            parts.append("## Runway")
            parts.append("")
            parts.append(
                "NOT CHECKED — no scenario engaged the cash-flow identity "
                "(declare `household_budget.annual_living_costs`). This is NOT a "
                "finding of safety: the household has not been checked, not cleared.")
            parts.append("")

        # Per-member savings plan (epic #841 bite 5): each family member's OWN
        # accounts, balances, room, and (for a child savings subject) the
        # per-year contribution its income funds plus any FHSA-first-home plan.
        plan = _per_member_savings_plan(self.base_cfg)
        if plan:
            parts.append("## Per-Member Savings Plan")
            parts.append("")
            for p in plan:
                heading = f"### {p['label']}"
                suffix = []
                if p['income']:
                    suffix.append(f"income ${p['income']:,.0f}")
                if p['annual_contribution']:
                    suffix.append(f"contributes ${p['annual_contribution']:,.0f}/yr")
                if suffix:
                    heading += " — " + ", ".join(suffix)
                parts.append(heading)
                parts.append("")
                if p['accounts']:
                    parts.append(_md_table(
                        ["Account", "Balance", "Contribution Room"],
                        [[a['kind'], f"${a['balance']:,.0f}", f"${a['room']:,.0f}"]
                         for a in p['accounts']],
                    ))
                else:
                    parts.append("_No registered accounts modelled._")
                parts.append("")
                if p['fhsa_first_home']:
                    parts.append(
                        f"🏠 **FHSA-first plan:** fund the ${p['fhsa_room']:,.0f} "
                        f"FHSA room first — for a first-home buyer the FHSA beats "
                        f"the TFSA and RRSP (deductible in, tax-free out).")
                    parts.append("")

        parts.append(f"_Generated by lifedraft scenario enumerator — "
                     f"{len(results)} scenarios evaluated._")
        parts.append("")
        return "\n".join(parts)


# =============================================================================
# CSV: tidy long-format year-by-year export (issue #248)
# =============================================================================

def write_year_by_year_csv(results: List[Dict], path: str,
                           all_scenarios: bool = False) -> int:
    """Write a tidy long-format CSV: one row per (scenario, year).

    Columns: scenario_rank, scenario_label, plus every YearResult field
    (from the serialized `year_by_year` dicts). DP#8/DP#25: pure
    serialization of data the simulation already produced — no recompute.

    By default only the #1 scenario is exported (matching the txt/html
    "top scenario" behaviour). Pass all_scenarios=True to emit every
    scenario that carries a year_by_year series.

    Returns the number of data rows written.
    """
    import csv

    ranked = _sort_results(results)
    if not all_scenarios:
        ranked = ranked[:1]

    # Determine the field set from the first available year_by_year row.
    field_keys: List[str] = []
    for r in ranked:
        series = r.get('year_by_year') or []
        if series:
            field_keys = list(series[0].keys())
            break

    rows_written = 0
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['scenario_rank', 'scenario_label'] + field_keys)
        for rank, r in enumerate(ranked, start=1):
            label = r.get('label', '')
            for yr in (r.get('year_by_year') or []):
                row = [rank, label]
                for k in field_keys:
                    v = yr.get(k, '')
                    # Flatten dict fields (e.g. contributions) to a compact string.
                    if isinstance(v, dict):
                        v = ';'.join(f"{kk}={vv}" for kk, vv in v.items())
                    row.append(v)
                writer.writerow(row)
                rows_written += 1
    return rows_written


# =============================================================================
# Factory
# =============================================================================

def create_report(fmt: OutputFormat, results: List[Dict], base_cfg: Dict,
                  title: str = "Refinance Scenario Analysis",
                  include_sensitivity: bool = False,
                  indent: int = 2) -> OutputReport:
    """Create an output report from a format enum.
    
    DP#8: compose through data. The format is data; the factory produces
    the right plugin.
    
    DP#9 (issue #722): explicit, named parameters only -- no ``**kwargs``
    catch-all. ``indent`` is the JSON pretty-printing width (default 2);
    it is forwarded to :class:`JsonReport` and ignored by the text/html
    plugins, which have no such option. An unknown keyword now raises
    ``TypeError`` instead of being silently swallowed.
    """
    common = dict(results=results, base_cfg=base_cfg, title=title,
                  include_sensitivity=include_sensitivity)
    if fmt == OutputFormat.TEXT:
        return TextReport(**common)
    elif fmt == OutputFormat.JSON:
        return JsonReport(**common, indent=indent)
    elif fmt == OutputFormat.HTML:
        return HtmlReport(**common)
    elif fmt == OutputFormat.MARKDOWN:
        return MarkdownReport(**common)
    else:
        raise ValueError(f"Unknown output format: {fmt}")


def write_report(fmt: OutputFormat, results: List[Dict], base_cfg: Dict,
                 path: str, title: str = "Refinance Scenario Analysis",
                 include_sensitivity: bool = False,
                 indent: int = 2):
    """Convenience: create and write a report in one call.

    DP#9 (issue #722): explicit, named parameters -- no ``**kwargs``
    catch-all; an unknown keyword raises ``TypeError``.
    """
    report = create_report(fmt, results, base_cfg, title,
                           include_sensitivity=include_sensitivity,
                           indent=indent)
    report.write(path)

# =============================================================================
# Console reports for the exploration dimensions (issue #232, slice 3)
#
# Moved out of optimize.py: these render the ranked rows a dimension sweep
# returns (``explore(dimension, cfg)`` or the ``run_*_exploration`` entry
# point it dispatches to). DP#25: the reporting layer consumes result dicts
# and never imports optimize; the caller -- optimize.py's CLI entry -- renders.
# =============================================================================


def _print_refinance_basis(results: List[Dict]) -> None:
    """State WHERE the refinance candidates below came from (#845/#846).

    The reader must never have to infer whether this table swept their declared
    ``decisions.mortgage.refinance_options`` or a ladder this tool made up. Read
    off the same rows whose net_benefit is printed, so the basis cannot disagree
    with the numbers (DP#9).
    """
    annotated = any(r.get('refinance_annotated') for r in results)
    declared_counts = {r.get('refinance_declared_count', 0) for r in results}
    declared_count = max(declared_counts) if declared_counts else 0

    if annotated and declared_count > 0:
        # Issue #853 / DP#33: the declaration is a LENS on the full sweep, not a
        # blindfold that hides it. The whole LTV ladder was explored; the
        # household's own options are MARKED in situ (★) rather than replacing
        # the curve, so a rung they did not declare can still win and be seen.
        marked = sorted({r.get('refinance_declared_label')
                         for r in results if r.get('refinance_declared')})
        print(f"\n  ✅ BASIS: the FULL LTV sweep, with your {declared_count} declared refinance")
        print(f"      option(s) (decisions.mortgage.refinance_options) marked ★ in situ (#853).")
        print(f"      The whole curve is ranked, NOT just your options — a rung you did not declare")
        print(f"      can still win, and you will see it. ★ = your declared option:")
        for label in marked:
            print(f"        ★ {label}")
        return

    if declared_count > 0:
        # A caller forced an explicit ladder while the household HAD declared
        # options. Overriding is allowed; doing it quietly is not -- that is
        # exactly how #845's two contradictory authoritative answers happened.
        print(f"\n  ⚠️  BASIS: a generic LTV ladder authored by this tool — it OVERRIDES the")
        print(f"      {declared_count} refinance option(s) you declared at "
              f"decisions.mortgage.refinance_options.")
        print(f"      The rows below are NOT your declared options and must not be read as a")
        print(f"      ranking of them (#845).")
        return

    print(f"\n  ℹ️  BASIS: a generic LTV ladder authored by this tool — you declared no")
    print(f"      decisions.mortgage.refinance_options, so there is nothing to rank against it.")
    print(f"      Declare them to have this table sweep YOUR options instead (#846).")


def structure_deductibility_caveat_lines() -> List[str]:
    """What this ranking now prices, and the one limitation that remains (#850).

    #845 makes advance-vs-line rankable at equal leverage; #850 now prices the
    s.20(1)(c) asymmetry that motivates the choice — a ``borrowing_purpose``
    trace deducts interest on the invested portion of both the advance and the
    drawn line, and the advance's deductible balance amortizes away with the
    principal (the erosion #849 names) while the line's does not. Only the
    portion of the lump that lands in a NON-registered account is traced as
    deductible; a dollar borrowed to fill RRSP/TFSA room is not (correct).

    Remaining limitation (issue on ``apply_sm_interest``): the Quebec deduction
    cap is applied but valued at the combined fed+QC rate, and the FEDERAL
    deduction has no such cap. That understates the deductible benefit on the
    LINE specifically (the capped leg), i.e. it works against the line. The
    ranking has been shown robust to it: removing the cap from the line's
    federal leg entirely still leaves the advance ahead. Stated here rather
    than left implied (DP#32). Extracted as a tested helper so ``main()`` gains
    no statements.
    """
    return [
        "",
        "  ℹ️  DEDUCTIBILITY IS PRICED (issue #850): interest on the invested portion of both the",
        "      advance and the drawn line is deducted; the advance's deductible balance amortizes",
        "      away with principal, the line's does not (the s.20(1)(c) asymmetry #849 asks about).",
        "      Only the NON-registered portion of the lump is deductible (filling RRSP/TFSA is not).",
        "  ⚠️  One known limit: apply_sm_interest values the Quebec cap at the combined fed+QC rate",
        "      and the federal deduction has no cap — this UNDERSTATES the line's benefit, not the",
        "      advance's. The ranking is robust to it (advance still leads with the cap removed).",
    ]


def _print_structure_deductibility_caveat() -> None:
    """Print :func:`structure_deductibility_caveat_lines` (issue #850)."""
    for line in structure_deductibility_caveat_lines():
        print(line)


def _print_refinance_refusals(results: List[Dict]) -> None:
    """Name every declared refinance option the engine REFUSED to score, and why
    (issue #891, DP#32/#681).

    Mirror of ``_print_structure_refusals``: a declared option whose cash-out
    breaches the 80% charge limit is ABSENT from the tables above (it was never
    simulable), and an absence read as a poor ranking would be exactly the
    silent-drop #681 forbids. So it is named here with its reason IN WORDS.
    """
    refused = [r for r in results if r.get('refinance_refused')]
    if not refused:
        return
    print(f"\n  ⚠️  NOT SCORED — {len(refused)} declared refinance option(s) the engine REFUSED:")
    for r in refused:
        print(f"      • '{r.get('refinance_label')}' (cash-out ${r.get('cashout', 0):,.0f})")
        print(f"        {r.get('refinance_refusal')}")
    print(f"      These options are ABSENT from the tables above — do not read their absence as a")
    print(f"      poor ranking; the charge cannot support them at all (#891/#664).")


def _print_ltv_exploration(results: List[Dict]) -> None:
    """Print formatted LTV exploration results."""
    print(f"\n{'=' * 120}")
    print(f"  📊 LTV EXPLORATION — Strategies at each LTV level")
    print(f"{'=' * 120}")

    # Issue #891: a refused over-limit declared option is a marker row carrying
    # no net_benefit -- keep it out of the ranking tables (it was never scored)
    # and name it in a loud NOT SCORED notice below, exactly as the structure
    # cross does. Feasible rows alone drive every table.
    refusals = [r for r in results if r.get('refinance_refused')]
    results = [r for r in results if not r.get('refinance_refused')]

    _print_refinance_basis(results)

    # Best strategy at each LTV
    print(f"\n  Best strategy per LTV level:")
    print(f"  {'LTV':>5s}  {'Cash-out':>10s}  {'Strategy':<40s}  {'Net Benefit':>12s}  {'Total Debt':>12s}  {'TFSA':>10s}  {'RRSP':>10s}")
    print(f"  {'-' * 100}")
    
    ltvs = sorted(set(r.get('ltv', 0) for r in results))
    for ltv in ltvs:
        ltv_rows = [r for r in results if r.get('ltv') == ltv]
        best = max(ltv_rows, key=lambda r: r.get('net_benefit', 0))
        cash = best.get('cashout', 0)
        strategy_name = best.get('strategy', '?')
        if best.get('deduct_later'):
            strategy_name += ' 📋'
        net = best.get('net_benefit', 0)
        debt = best.get('total_debt', 0)
        tfsa = best.get('TFSA', 0)
        rrsp = best.get('RRSP', 0)
        # Issue #853 / DP#33: mark the rungs the household declared with ★ so the
        # declaration is a visible lens on the full curve, not a hidden filter.
        marker = ''
        declared_labels = sorted({r.get('refinance_declared_label')
                                  for r in ltv_rows if r.get('refinance_declared')})
        if declared_labels:
            marker = '  ★ ' + ', '.join(declared_labels)
        print(f"  {ltv:>4.0%}  ${cash:>9,.0f}  {strategy_name:<40s}  ${net:>10,.0f}  ${debt:>10,.0f}  ${tfsa:>9,.0f}  ${rrsp:>9,.0f}{marker}")
    
    # Strategy comparison across LTVs
    print(f"\n  Strategy comparison (averaged across LTVs):")
    print(f"  {'Strategy':<40s}  {'Avg Net':>10s}  {'Max Net':>10s}")
    print(f"  {'-' * 65}")
    
    strategies = sorted(set(r.get('strategy', '?') for r in results))
    strat_data = []
    for s in strategies:
        s_rows = [r for r in results if r.get('strategy') == s]
        avg_net = sum(r.get('net_benefit', 0) for r in s_rows) / len(s_rows) if s_rows else 0
        max_net = max(r.get('net_benefit', 0) for r in s_rows) if s_rows else 0
        strat_data.append((s, avg_net, max_net))
    
    strat_data.sort(key=lambda x: x[1], reverse=True)
    for s, avg, mx in strat_data:
        label = s
        if any(r.get('deduct_later') for r in results if r.get('strategy') == s):
            label += ' 📋'
        print(f"  {label:<40s}  ${avg/1000:>8,.0f}k  ${mx/1000:>8,.0f}k")

    # Issue #891: name any declared option refused for breaching the charge
    # limit, from the SAME rows the sweep returned (DP#9) -- never a silent drop.
    _print_refinance_refusals(refusals)

    print()


def winners_by_income_scenario(results: List[Dict]) -> List[Dict]:
    """Pure logic half of the income-scenario report (issue #665): for each
    income scenario present in ``results`` (in first-seen/declaration order),
    find the winning strategy and record whether it differs from the FIRST
    scenario's winner (the household's base/current-income case).

    Split out from ``_print_income_scenario_report`` so "does the
    recommendation change across income scenarios" is a directly testable
    fact, not something only observable by parsing printed text.

    Issue #679: each row also carries the winner's ``solvency`` summary and a
    ``ruined`` flag. This is the whole point of the feature -- a scenario the
    household CANNOT SURVIVE must not be representable in this list as merely
    a smaller number. ``ruined`` being a first-class key here (not a
    formatting decision inside the printer) is what makes
    "a ruined scenario never reports an unqualified terminal net benefit" a
    testable assertion.

    Returns one dict per scenario:
        {id, label, strategy, deduct_later, net_benefit, changed_from_base,
         ruined, solvency}
    ``changed_from_base`` is always False for the first scenario itself.
    """
    scenario_ids = list(dict.fromkeys(r['income_scenario_id'] for r in results))
    winners = []
    base_strategy = None
    for sid in scenario_ids:
        rows = [r for r in results if r['income_scenario_id'] == sid]
        best = max(rows, key=lambda r: r.get('net_benefit', 0))
        strategy = best.get('strategy', '?')
        if base_strategy is None:
            base_strategy = strategy
        # DP#32: an explicit default, never `or {}` -- a row that carries no
        # solvency summary at all (an older/synthetic result) is ABSENT, and
        # absence must not be silently laundered into "solvent".
        solvency = best.get('solvency', {})
        winners.append({
            'id': sid,
            'label': rows[0]['income_scenario_label'],
            'strategy': strategy,
            'deduct_later': best.get('deduct_later', False),
            'net_benefit': best.get('net_benefit', 0),
            'changed_from_base': strategy != base_strategy,
            'ruined': bool(solvency.get('ruined', False)),
            'solvency': solvency,
            # Issue #707: decumulation shortfall beside the solvency verdict.
            # Explicit absence test (DP#32): a row carrying no summary is
            # given an all-False one, never via `.get(k) or DEFAULT`.
            'drawdown_shortfall': (
                best['drawdown_shortfall'] if 'drawdown_shortfall' in best
                else summarize_drawdown_shortfall([])),
            'exhausted': bool(
                (best.get('drawdown_shortfall')
                 if best.get('drawdown_shortfall') is not None else {})
                .get('exhausted', False)),
            # Issue #758: runway (months-to-ruin) beside the solvency verdict.
            # Explicit absence test (DP#32): a synthetic row carrying no
            # runway is given an un-engaged one, never a falsy-coerced number.
            'runway': best['runway'] if 'runway' in best else absent_runway(),
        })
    return winners


def _print_income_scenario_report(results: List[Dict]) -> None:
    """Print the winning strategy under EACH declared income scenario, and
    flag explicitly whether the recommendation changes vs. the first
    (current-income) scenario -- issue #665: "a strategy that is optimal at
    full income and ruinous on EI is the entire point of the feature," and
    the tool must say so, not just rank silently.

    Only prints when the contract declares MORE than one income scenario --
    a household that never authored decisions.income[] gets the single
    auto-discovered "current income" scenario and this section is skipped
    (nothing to compare).

    Issue #679: a RUINED scenario's terminal figure is NOT printed as a
    dollar amount here. It is replaced by ``RUIN (year N)``, because the
    entire defect this fixes is that the tool answered "what happens if I
    lose my job?" with a large, reassuring number that is only reachable on
    the assumption the job loss did not destroy the household. A household
    reading `$4.4M` concludes job loss is survivable and merely expensive.
    The ledger figure is still reported -- immediately below, explicitly
    labelled NOT ACHIEVABLE -- because suppressing it entirely would hide
    what the engine computed; the requirement is that it can never be
    mistaken for an achievable outcome (DP#32: fail loudly).
    """
    winners = winners_by_income_scenario(results)
    if len(winners) <= 1:
        return

    print(f"\n{'=' * 120}")
    print(f"  ⚖️  RECOMMENDATION BY INCOME SCENARIO (decisions.income[] -- issue #665)")
    print(f"{'=' * 120}")
    print(f"\n  {'Scenario':<28} {'Winning strategy':<40} {'Net Benefit':>14}")
    print(f"  {'-' * 90}")

    base_label = winners[0]['label']
    base_strategy = winners[0]['strategy']
    for w in winners:
        strategy_name = w['strategy'] + (' 📋' if w['deduct_later'] else '')
        if w['ruined']:
            ruin_year = w['solvency'].get('first_ruin_year')
            verdict = f"RUIN (yr {ruin_year})" if ruin_year else "RUIN"
            print(f"  {w['label']:<28} {strategy_name:<40} {verdict:>14}")
            print(f"    ⛔ INSOLVENT: this household cannot fund its own obligations under "
                  f"'{w['label']}'.")
            print(f"       The ledger figure (${w['net_benefit']:,.0f}) is NOT ACHIEVABLE -- it "
                  f"assumes the shortfall never happened.")
            print(f"       See the SOLVENCY section below for the runway, the year it runs out, "
                  f"and what it was forced to sell.")
        elif not w['solvency'].get('engaged'):
            # Issue #733: a scenario whose cash-flow identity was never
            # checked (no `household_budget.annual_living_costs` declared,
            # the normal case for every existing contract, #679) must not
            # render here as an ordinary, achievable figure -- that IS the
            # defect #679 exists to kill, just reached through the row that
            # falls back to the bare number instead of through 'ruined'.
            # Marked INLINE, in the cell itself (not only 74 lines below in
            # the SOLVENCY footer, where a household reading top-to-bottom
            # never reaches it before drawing a conclusion).
            verdict = f"${w['net_benefit']:,.0f} (UNCHECKED)"
            print(f"  {w['label']:<28} {strategy_name:<40} {verdict:>22}")
        else:
            print(f"  {w['label']:<28} {strategy_name:<40} ${w['net_benefit']:>12,.0f}")
        if w['changed_from_base']:
            print(f"    ⚠ Recommendation CHANGES under '{w['label']}': "
                  f"{w['strategy']} (vs. '{base_label}' recommends {base_strategy})")

    print()
    _print_solvency_report(winners)
    # Issue #758: the months-to-ruin metric, right beside the solvency verdict.
    _print_runway_report(winners)


def winners_by_structure_scenario(results: List[Dict]) -> List[Dict]:
    """Pure logic half of the structure-ranking report (issue #687): the
    winning strategy for each (structure, income scenario) pair present in
    ``results``, so "do these structures rank differently, and does the
    ranking change under job loss" is a directly testable fact, not
    something only observable by parsing printed text (mirrors
    ``winners_by_income_scenario``'s split, DP#8).

    Returns one dict per (refinance option, structure, income scenario) triple
    (#845 -- two structures are only comparable at the SAME charge), in
    first-seen/declaration order:
        {structure_basis_id, structure_basis_label, structure_basis_cash_out,
         structure_basis_ltv, structure_id, structure_label,
         income_scenario_id, income_scenario_label, strategy, deduct_later,
         net_benefit, ruined, solvency}
    """
    # Issue #845: the refinance option is part of the key. Two structures are
    # only comparable at the SAME charge, so collapsing 'advance at cash-out $0'
    # and 'advance at cash-out $480,000' into one winner would silently pick the
    # better LEVERAGE and report it as the better STRUCTURE -- the exact
    # confusion #845 exists to end. A row that carries no basis tag is the
    # pre-#845 single basis (``structure_basis_id``), so a caller that never
    # crossed anything gets byte-identical grouping.
    pairs = list(dict.fromkeys(
        (structure_basis_id(r), r['structure_id'], r['income_scenario_id'])
        for r in results))
    winners = []
    for bid, sid, iid in pairs:
        rows = [r for r in results
                if structure_basis_id(r) == bid
                and r['structure_id'] == sid and r['income_scenario_id'] == iid]
        best = max(rows, key=lambda r: r.get('net_benefit', 0))
        # DP#32: an explicit default, never `or {}` -- see
        # winners_by_income_scenario's identical comment.
        solvency = best.get('solvency', {})
        winners.append({
            # Issue #845: the basis travels onto the winner too -- a winner
            # without the charge it won at is exactly what #845 filed.
            'structure_basis_id': bid,
            'structure_basis_label': rows[0].get('structure_basis_label'),
            'structure_basis_cash_out': rows[0].get('structure_basis_cash_out'),
            'structure_basis_ltv': rows[0].get('structure_basis_ltv'),
            'structure_id': sid,
            'structure_label': rows[0]['structure_label'],
            'income_scenario_id': iid,
            'income_scenario_label': rows[0]['income_scenario_label'],
            'strategy': best.get('strategy', '?'),
            'deduct_later': best.get('deduct_later', False),
            # issue #735: which draw fraction won for THIS structure -- the
            # question "keep this structure's line undrawn, or draw it?" is
            # answered per-structure, not assumed away.
            'draw_fraction': best.get('draw_fraction', 0.0),
            # issue #1075: the 3-tranche sweep point that won for THIS
            # structure -- the optimal {house, investment, line} split, read
            # off the row that produced it (None for a share-form structure)
            # -- and the cash-back verdict the split was scored under.
            'tranche_amounts': best.get('structure_tranche_amounts'),
            'cash_back_amount': best.get('structure_cash_back_amount'),
            'cash_back_threshold': best.get('structure_cash_back_threshold'),
            'cash_back_credited': best.get('structure_cash_back_credited'),
            'net_benefit': best.get('net_benefit', 0),
            'ruined': bool(solvency.get('ruined', False)),
            'solvency': solvency,
            # Issue #707: decumulation shortfall beside the solvency verdict.
            # Explicit absence test (DP#32): a row carrying no summary is
            # given an all-False one, never via `.get(k) or DEFAULT`.
            'drawdown_shortfall': (
                best['drawdown_shortfall'] if 'drawdown_shortfall' in best
                else summarize_drawdown_shortfall([])),
            'exhausted': bool(
                (best.get('drawdown_shortfall')
                 if best.get('drawdown_shortfall') is not None else {})
                .get('exhausted', False)),
            # Issue #758: runway (months-to-ruin) beside the solvency verdict,
            # so every mortgage-structure option in the ranking shows BOTH
            # numbers at once -- a structure that wins on terminal net worth
            # but halves your runway is not obviously the better structure.
            'runway': best['runway'] if 'runway' in best else absent_runway(),
        })
    return winners


def structure_ranking_by_income_scenario(winners: List[Dict]) -> Dict[str, List[Dict]]:
    """Group ``winners_by_structure_scenario``'s rows by income scenario,
    each group sorted by ``net_benefit`` descending (issue #687) -- so "do
    the structures rank differently" and "does the ranking change under
    job loss" are both directly answerable from this, not just printable.

    Returns ``{income_scenario_id: [row, ...]}``, rows sorted best-first,
    in first-seen income-scenario order (dict insertion order, Python
    3.7+).

    Issue #845: give this the winners of ONE refinance option. Structures are
    only comparable at the SAME charge, and this groups by income scenario
    ALONE -- feeding it a whole cross would rank 'advance at cash-out $0'
    against 'advance at $480,000' in one table and report the better LEVERAGE
    as the better STRUCTURE. ``_print_structure_report`` splits by
    ``structure_basis_id`` before calling this, which is why it can.
    """
    by_income: Dict[str, List[Dict]] = {}
    for w in winners:
        by_income.setdefault(w['income_scenario_id'], []).append(w)
    for iid in by_income:
        by_income[iid].sort(key=lambda w: w.get('net_benefit', 0), reverse=True)
    return by_income


def structure_basis_id(row: Dict) -> str:
    """The refinance option ONE structure-ranking row was scored at (#845).

    DP#32: an explicit fallback for a row that predates the basis tags (the
    fabricated rows the pure-logic tests build, and any caller still passing
    the old shape) -- 'current_charge' is exactly what
    ``structure_refinance_bases`` calls the no-declaration basis, so the two
    producers agree on the name (DP#9).
    """
    basis_id = row.get('structure_basis_id')
    return 'current_charge' if basis_id is None else basis_id


def _print_structure_refusals(cells: Optional[List[Dict]]) -> None:
    """Name every (refinance option x structure) cell the engine REFUSED to
    score, and why (#845, DP#32/#681).

    A cell missing from the tables above is otherwise indistinguishable from
    one that was never asked for. #681's rule -- an infeasible scenario is
    recorded with the reason IN WORDS, never collapsed to silence -- applies to
    this new cross too.
    """
    if not cells:
        return
    refused = [c for c in cells if c['refusal'] is not None]
    if not refused:
        return
    print(f"\n  ⚠️  NOT SCORED — {len(refused)} (refinance option x structure) cell(s) the engine refused:")
    for c in refused:
        print(f"      • '{c['structure']['label']}' at '{c['basis']['label']}'")
        print(f"        {c['refusal']}")
    print(f"      These cells are ABSENT from the tables above — do not read their absence as a")
    print(f"      poor ranking (#845).")


def _print_structure_report(results: List[Dict], cells: Optional[List[Dict]] = None) -> None:
    """Issue #845: print ONE structure ranking PER refinance option the cross
    scored, each stating its own basis, then name every refused cell.

    Delegates each option's table to ``_print_structure_report_for_basis``
    below; a household that declared no refinance option has exactly one basis
    and sees exactly the report it saw before this fix.
    """
    for basis_id in dict.fromkeys(structure_basis_id(r) for r in results):
        _print_structure_report_for_basis(
            [r for r in results if structure_basis_id(r) == basis_id])
    _print_structure_refusals(cells)


def _print_structure_report_for_basis(results: List[Dict]) -> None:
    """Issue #687: print the mortgage-STRUCTURE ranking -- all-mortgage vs.
    readvanceable vs. mortgage+revolving-line -- PER declared income scenario
    (DP#5/DP#22: the optimizer ranks, the household chooses).

    Only prints when the contract actually declares
    ``decisions.mortgage.structure_options`` with more than one candidate
    (``scenario_discovery`` returns a single auto-discovered 'declared'
    identity option otherwise) -- a household that never asked this
    question sees nothing extra, same gating discipline as
    ``_print_income_scenario_report``.

    Issue #1075 exception: a single tranches-declared structure still prints
    -- its ranking has one row per income scenario, but the OPTIMAL
    3-TRANCHE SPLIT block below is the deliverable (the optimizer GENERATED
    the amounts), so a tranche sweep must not be silenced by the
    one-candidate gate.

    Issue #733's fix applies here too (DP#32): a row whose scenario was
    never solvency-checked is marked ``(UNCHECKED)`` inline, and a ruined
    row prints ``RUIN (yr N)``, never a bare achievable-looking figure --
    a NEW ranking surface must not reintroduce the defect #733 just closed
    on the income-scenario table.
    """
    winners = winners_by_structure_scenario(results)
    structure_ids = list(dict.fromkeys(w['structure_id'] for w in winners))
    has_tranche_rows = any(w.get('tranche_amounts') for w in winners)
    if len(structure_ids) <= 1 and not has_tranche_rows:
        return

    ranking = structure_ranking_by_income_scenario(winners)
    income_ids = list(dict.fromkeys(w['income_scenario_id'] for w in winners))

    print(f"\n{'=' * 120}")
    print(f"  🏗️  MORTGAGE STRUCTURE RANKING (decisions.mortgage.structure_options -- issue #687)")
    print(f"{'=' * 120}")
    _print_structure_deductibility_caveat()

    # Issue #845: state the leverage this ranking was computed at, BEFORE the
    # table -- the structural choice (line vs no line) is irreversible on notary
    # day, and a reader must not mistake this ranking's basis for the LTV
    # sweep's. The two tables answer different questions at different leverage.
    basis_ltvs = {r.get('structure_basis_ltv') for r in results
                  if r.get('structure_basis_ltv') is not None}
    # DP#32: explicit absence-testing. A cash-out of exactly 0 is a REAL basis
    # (the household's current charge, or a declared no-cash-out option), not
    # an unset one -- `or 0` would make the two indistinguishable.
    basis_cash_outs = {r.get('structure_basis_cash_out') for r in results
                       if r.get('structure_basis_cash_out') is not None}
    basis_cash_out = max(basis_cash_outs) if basis_cash_outs else 0.0
    basis_labels = [r.get('structure_basis_label') for r in results
                    if r.get('structure_basis_label') is not None]
    if basis_ltvs and basis_cash_out <= 0:
        basis_ltv = max(basis_ltvs)
        print(f"\n  📍 BASIS: computed at CASH-OUT $0 — your current leverage "
              f"(LTV {basis_ltv:.1%}). NO cash-out sweep.")
        if basis_labels:
            print(f"      Refinance option: '{basis_labels[0]}' "
                  f"(decisions.mortgage.refinance_options).")
        print(f"      These structures are ranked as SPLITS of the charge you already carry, so the")
        print(f"      comparison isolates structure from leverage. If the LTV EXPLORATION above")
        print(f"      recommends a different LTV, these structures were NOT ranked at that leverage")
        print(f"      — do not read the two tables as one plan (#845).")
    elif basis_ltvs:
        # Issue #845/#849: a REAL cash-out basis. The reader must not mistake
        # this for the LTV sweep's table (which ranks STRATEGIES across
        # leverage) -- this one ranks STRUCTURES at ONE fixed charge.
        basis_ltv = max(basis_ltvs)
        print(f"\n  ✅ BASIS: your declared refinance option "
              f"'{basis_labels[0] if basis_labels else '?'}' — "
              f"CASH-OUT ${basis_cash_out:,.0f} (LTV {basis_ltv:.1%}).")
        print(f"      Every structure below is scored at THAT charge, so they are comparable to each")
        print(f"      other AT the leverage this option takes — not at your current one (#845).")
        print(f"      This is NOT the LTV-sweep basis: that table ranks STRATEGIES across cash-out")
        print(f"      levels; this one ranks STRUCTURES at ONE fixed charge.")
        print(f"      ⚖️  ADVANCE vs LINE (#849): at this fixed charge, the structure's")
        print(f"          revolving_share IS the tap — 0% takes the whole ${basis_cash_out:,.0f} surplus as an")
        print(f"          amortizing mortgage advance; a share large enough to hold it draws the whole")
        print(f"          surplus from the revolving line instead; in between splits it, line first.")

    # DP#32 / model_fidelity (#585): issue #735 FIXED the approximation this
    # block used to warn about (a revolving line used to be drawn in full,
    # unconditionally, at year 0 -- `lump_sum = margin_available +
    # cash_out`). Now each structure that carves out a line is evaluated at
    # SEVERAL draw fractions (0%/25%/50%/100% of that structure's own
    # margin_available -- scenario_discovery._discover_draw_fraction_options)
    # and the winning row below already reflects the best one FOUND, so a
    # structure carrying an undrawn line is no longer scored as though it
    # had been spent. Still disclosed here (DP#32: the reader should not
    # have to infer this from a column header) -- the DRAWN FRACTION each
    # winning row actually assumed is what makes the row's real leverage
    # legible, not something to take on faith.
    # DP#32: explicit absence-testing, never `x or 0` -- a structure with a
    # declared share of exactly 0.0 is a REAL declaration (structure A), not
    # an unset one, and the two must stay distinguishable.
    shares = [r.get('structure_revolving_share') for r in results]
    # Issue #1075: a tranches-declared structure's drawn/undrawn question is
    # answered by ITS OWN line amount and the cash-out sourcing -- the #735
    # draw-fraction ladder is pinned to [0.0] for it (see
    # run_mortgage_structure_exploration), so the share-form disclosures
    # below would describe a sweep that did not happen (DP#32). Print the
    # tranche-specific disclosure instead.
    has_tranche_rows = any(r.get('structure_tranche_amounts') for r in results)
    if has_tranche_rows:
        if basis_cash_out > 0:
            print(f"\n  ℹ️  HOW THE REVOLVING SEGMENT IS MODELLED at this cash-out (#1075):")
            print(f"      Each tranche point's line is drawn by the cash-out sourcing: "
                  f"min(${basis_cash_out:,.0f} cash-out, its line amount) comes off the line, ")
            print(f"      the remainder of the surplus stays on the mortgage as the (deductible)"
                  f" investment tranche, and any residual line room stays UNDRAWN standby")
            print(f"      liquidity. The line is NOT also swept at #735 draw fractions.")
        else:
            print(f"\n  ℹ️  HOW THE REVOLVING SEGMENT IS MODELLED (#1075):")
            print(f"      The line AMOUNT is the swept variable -- each sweep point carries its own")
            print(f"      line, and the winning row's split is printed below. The #735 draw-")
            print(f"      fraction ladder is pinned to undrawn: at cash-out $0 the line is standby")
            print(f"      liquidity, and drawing it is a separate decision this ranking does not")
            print(f"      make for the tranched form.")
    elif any(s is not None and s > 0 for s in shares) and basis_cash_out > 0:
        # Issue #845/#849: on a cash-out basis the draw is NOT swept -- it is
        # IMPLIED by the sourcing split (run_mortgage_structure_exploration
        # pins draw_fraction to 0.0; apply_sourcing_overlay already booked
        # min(cash_out, revolving) as the line's opening balance). Printing
        # #735's "evaluated at several draw fractions" here would describe a
        # sweep that did not happen (DP#32).
        print(f"\n  ℹ️  HOW THE REVOLVING SEGMENT IS MODELLED at this cash-out (#845/#849):")
        print(f"      The line's draw is NOT swept here — it is IMPLIED by the sourcing split. Each")
        print(f"      structure draws min(cash-out, its revolving segment) of the "
              f"${basis_cash_out:,.0f} surplus")
        print(f"      from the line and takes the remainder as a mortgage advance; any room left over")
        print(f"      stays UNDRAWN standby liquidity. Total borrowed is identical across structures,")
        print(f"      so what the ranking below measures is the SOURCE, not the amount.")
    elif any(s is not None and s > 0 for s in shares):
        print(f"\n  ℹ️  HOW THE REVOLVING SEGMENT IS MODELLED (issue #735):")
        print(f"      A structure that carves out a line is evaluated at SEVERAL draw fractions "
              f"of that line")
        print(f"      (0%/25%/50%/100% of ITS OWN margin_available) -- the winning row below is "
              f"the best one")
        print(f"      found, and shows its own drawn fraction inline. A 0% row means the line "
              f"won UNDRAWN:")
        print(f"      standby liquidity, not leverage.")

    base_income_id = income_ids[0]
    base_winning_structure = ranking[base_income_id][0]['structure_id']
    base_winning_label = ranking[base_income_id][0]['structure_label']
    base_income_label = ranking[base_income_id][0]['income_scenario_label']

    # issue #735: which structure_ids actually carry a revolving segment at
    # all -- only those get a "(draw N%)" annotation; a line-free structure
    # (e.g. structure A) would otherwise show a meaningless "draw 0%" on
    # every one of its own rows.
    #
    # Issue #845: NOT on a cash-out basis. There the draw_fraction is pinned to
    # 0.0 because the draw is IMPLIED by the sourcing split -- so a literal
    # "(draw 0%)" would tell the reader the line won UNDRAWN, when
    # apply_sourcing_overlay in fact drew min(cash_out, revolving) of it. The
    # exact opposite of the truth, in the column meant to make the row's real
    # leverage legible. The disclosure block above says what happened instead.
    structures_with_a_line = set() if basis_cash_out > 0 else {
        r['structure_id'] for r in results
        # Issue #1075: a tranches-declared row's "(draw N%)" annotation would
        # be the #735 sweep's marker, but the fraction is pinned to [0.0] for
        # the tranched form -- its line status is carried by its own amounts,
        # not by a fraction it never swept.
        if (r.get('structure_revolving_share') not in (None, 0.0)
            or r.get('structure_readvanceable'))
        and not r.get('structure_tranche_amounts')
    }

    for iid in income_ids:
        rows = ranking[iid]
        label = rows[0]['income_scenario_label']
        print(f"\n  Under '{label}':")
        # Issue #758: runway sits BESIDE net benefit for every structure --
        # a structure that wins on terminal net worth but halves your runway
        # is not obviously the better structure, and the household must see
        # both numbers at once (the comparison that matters before a notary).
        print(f"  {'#':<3} {'Structure':<48} {'Strategy':<20} {'Net Benefit':>14} {'Runway':>22}")
        print(f"  {'-' * 110}")
        for i, w in enumerate(rows):
            strategy_name = w['strategy'] + (' 📋' if w['deduct_later'] else '')
            if w['structure_id'] in structures_with_a_line:
                strategy_name += f" (draw {w.get('draw_fraction', 0.0):.0%})"
            runway_txt = _format_runway_inline(w.get('runway', {}))
            if w['ruined']:
                ruin_year = w['solvency'].get('first_ruin_year')
                verdict = f"RUIN (yr {ruin_year})" if ruin_year else "RUIN"
                print(f"  {i+1:<3} {w['structure_label']:<48} {strategy_name:<20} {verdict:>14} {runway_txt:>22}")
            elif not w['solvency'].get('engaged'):
                verdict = f"${w['net_benefit']:,.0f} (UNCHECKED)"
                print(f"  {i+1:<3} {w['structure_label']:<48} {strategy_name:<20} {verdict:>20} {runway_txt:>22}")
            else:
                print(f"  {i+1:<3} {w['structure_label']:<48} {strategy_name:<20} ${w['net_benefit']:>13,.0f} {runway_txt:>22}")

        winning_structure = rows[0]['structure_id']
        if iid != base_income_id and winning_structure != base_winning_structure:
            print(f"    ⚠ Best STRUCTURE changes under '{label}': "
                  f"'{rows[0]['structure_label']}' wins here (vs. '{base_winning_label}' "
                  f"under '{base_income_label}')")

    # Issue #1075: the OPTIMAL 3-tranche split -- the whole point of a
    # tranches-declared structure is that the optimizer GENERATES the amounts
    # (house / deductible investment / line) rather than the household fixing
    # them. Read off the winning rows' OWN ``tranche_amounts`` (DP#9: the
    # printed split is the very split the printed net benefit was scored at),
    # and on a cash-out basis the sourcing (#849: the surplus is drawn
    # line-first up to the structure's line, the rest as a mortgage advance)
    # is stated beside it.
    tranche_rows = [w for w in winners if w.get('tranche_amounts')]
    if tranche_rows:
        print(f"\n  🧱  OPTIMAL 3-TRANCHE SPLIT (structure_options.tranches -- issue #1075):")
        for w in tranche_rows:
            a = w['tranche_amounts']
            total = a['house'] + a['investment'] + a['line']
            print(f"      • '{w['structure_label']}' under '{w['income_scenario_label']}':")
            print(f"          house ${a['house']:,.0f}  +  investment ${a['investment']:,.0f}"
                  f" (deductible)  +  line ${a['line']:,.0f}  =  ${total:,.0f} charge")
            print(f"          net benefit ${w['net_benefit']:,.0f}"
                  f" (strategy {w['strategy']})")
            # Issue #1075 (optimizer half): state the cash-back verdict the
            # winning split was scored under -- credited (house >= the
            # threshold) or FORGONE (house below it, the trade-off this
            # sweep exists to price). Only a CONDITIONAL cash-back prints
            # anything (``cash_back_amount`` is carried only when the
            # declared origination inflow is conditional): a household with
            # no such declaration sees the exact pre-#1075 report.
            if w.get('cash_back_amount') is not None:
                thresh = w.get('cash_back_threshold')
                if w.get('cash_back_credited'):
                    verdict = (f"cash-back ${w['cash_back_amount']:,.0f} CREDITED "
                               f"(house ${a['house']:,.0f} >= the "
                               f"${thresh:,.0f} threshold)")
                else:
                    verdict = (f"cash-back ${w['cash_back_amount']:,.0f} FORGONE "
                               f"(house ${a['house']:,.0f} below the "
                               f"${thresh:,.0f} threshold)")
                print(f"          {verdict}")
            if basis_cash_out > 0:
                line_draw = min(basis_cash_out, a['line'])
                advance = basis_cash_out - line_draw
                print(f"          cash-out sourcing (#849): ${advance:,.0f} as a mortgage advance,"
                      f" ${line_draw:,.0f} drawn from the line")
    print()


# Human labels for the waterfall's source names (liquidation_waterfall's
# LiquidationSource.name). DP#2-adjacent: presentation text, not a rule.
_LIQUIDATION_SOURCE_LABELS = {
    'emergency_reserve': 'Emergency reserve (cash sleeve)',
    'revolving_credit': 'Revolving credit facility',
    'non_reg': 'Non-registered (taxable sale)',
    'tfsa': 'TFSA',
    'registered': 'RRSP/RRIF (fully taxable)',
}


def winners_by_property_funding(results: List[Dict]) -> List[Dict]:
    """Pure logic half of the funding ranking report (issue #1011): for each
    funding candidate present in ``results`` (in first-seen/declaration
    order), find the winning strategy and record it.

    Split out from ``_print_property_funding_report`` so "which funding does
    the objective prefer" is a directly testable fact, not something only
    observable by parsing printed text -- the same split
    ``winners_by_structure_scenario`` makes for #687.

    Returns one dict per funding candidate:
        {id, label, strategy, net_benefit, objective_score}
    The winner is selected by the RESOLVED objective's score (defaulting to
    net_benefit), so a non-default ``decisions.objective`` reorders the
    funding ranking exactly as it reorders the headline (DP#22).
    """
    seen = list(dict.fromkeys(r['property_funding_id'] for r in results))
    winners = []
    for fid in seen:
        rows = [r for r in results if r['property_funding_id'] == fid]
        best = max(rows, key=lambda r: r.get(
            'objective_score', r.get('net_benefit', 0)))
        winners.append({
            'id': fid,
            'label': rows[0]['property_funding_label'],
            'strategy': best.get('strategy', '?'),
            'net_benefit': best.get('net_benefit', 0),
            'objective_score': best.get(
                'objective_score', best.get('net_benefit', 0)),
        })
    return winners


def _print_property_funding_report(results: List[Dict]) -> None:
    """Issue #1011: print the property-purchase FUNDING ranking -- one row per
    declared funding candidate, ranked by the active objective, naming the
    winning strategy each funding produces. The objective-winner is the top
    row (DP#22: the optimizer ranks, the user reads the winner)."""
    winners = winners_by_property_funding(results)
    if not winners:
        return
    obj_name = _objective_name_for_results(results) or 'max_net_benefit'
    print(f"\n  🏠  PROPERTY FUNDING RANKING (purchase.funding_options -- issue #1011)")
    print(f"      objective: {obj_name}")
    print(f"\n  {'#':<3} {'Funding':<36} {'Strategy':<30} {'Net':>9}")
    print(f"  {'-'*82}")
    for i, w in enumerate(winners):
        print(f"  {i+1:<3} {w['label']:<36} {w['strategy']:<30} "
              f"${w['net_benefit']/1000:>7.0f}k")
    print(f"\n  The objective-winner is row 1. Each funding method is a real")
    print(f"  re-optimisation, not a restated input (DP#22).")


def winners_by_borrow_to_invest(results: List[Dict]) -> List[Dict]:
    """Pure logic half of the borrow-to-invest ranking report (issue #1036):
    for each amount rung present in ``results`` (in SCORE order -- best first,
    because ``run_borrow_to_invest_exploration`` sorts by objective before
    ``dict.fromkeys``; the no-draw baseline is NOT necessarily row 1, it is the
    frame of reference ranked on its merits, DP#33), find the winning strategy
    and record it. Split out from ``_print_borrow_to_invest_report`` so 'which
    draw amount does the objective prefer' is a directly testable fact, not
    something only observable by parsing printed text (mirrors
    ``winners_by_property_funding``).

    Returns one dict per amount rung:
        {id, label, amount, strategy, net_benefit, objective_score}
    The winner is selected by the RESOLVED objective's score (defaulting to
    net_benefit), so a non-default ``decisions.objective`` reorders the
    borrow-to-invest ranking exactly as it reorders the headline (DP#22).
    """
    seen = list(dict.fromkeys(r['borrow_to_invest_id'] for r in results))
    winners = []
    for bid in seen:
        rows = [r for r in results if r['borrow_to_invest_id'] == bid]
        best = max(rows, key=lambda r: r.get(
            'objective_score', r.get('net_benefit', 0)))
        winners.append({
            'id': bid,
            'label': rows[0]['borrow_to_invest_label'],
            'amount': rows[0]['borrow_to_invest_amount'],
            'strategy': best.get('strategy', '?'),
            'net_benefit': best.get('net_benefit', 0),
            'objective_score': best.get(
                'objective_score', best.get('net_benefit', 0)),
        })
    return winners


def _print_borrow_to_invest_report(results: List[Dict]) -> None:
    """Issue #1036: print the borrow-to-invest ranking -- one row per amount
    rung in SCORE order (best first; the no-draw baseline is ranked on its
    merits, not pinned to row 1, DP#33), naming the winning strategy each
    rung produces. The objective-winner is the top row (DP#22: the optimizer
    ranks, the user reads the winner).

    D9: the numeric column shown is the RESOLVED objective's score
    (``objective_score`` -- the value that drove the order), not ``net_benefit``.
    Under ``min_after_tax_estate`` the score is the negated after-tax estate
    and ``net_benefit`` decreases monotonically down the ranking, so showing
    ``net_benefit`` would contradict the order; showing the score makes the
    table self-consistent. Under ``max_net_benefit`` the two are equal."""
    winners = winners_by_borrow_to_invest(results)
    if not winners:
        return
    obj_name = _objective_name_for_results(results) or 'max_net_benefit'
    print(f"\n  🏦  BORROW-TO-INVEST RANKING (decisions.borrow_to_invest -- issue #1036)")
    print(f"      objective: {obj_name}")
    print(f"\n  {'#':<3} {'Draw':<36} {'Amount':>10} {'Strategy':<28} {'Score':>12}")
    print(f"  {'-'*92}")
    for i, w in enumerate(winners):
        amt = f"${w['amount']/1000:.0f}k" if w['amount'] else '—'
        score = w.get('objective_score', w.get('net_benefit', 0))
        print(f"  {i+1:<3} {w['label']:<36} {amt:>10} {w['strategy']:<28} "
              f"{score:>12,.0f}")
    print(f"\n  The objective-winner is row 1. The no-draw baseline (—) is the")
    print(f"  frame of reference every draw is read against (DP#33); each draw")
    print(f"  amount is a real re-optimisation, not a restated input (DP#22).")


def _print_decumulation_shortfall_report(results: List[Dict], cfg: Dict) -> None:
    """Issue #707's console deliverable: a plan that runs out of money before
    the horizon is surfaced as a FIRST-CLASS output, not a confident terminal
    number.

    "The money runs out in year N, $G short of the net spending target" --
    that is the answer to "can I retire on this?", and the terminal
    net-benefit figure is not. Mirrors ``_print_solvency_report`` (#679):
    the two shortfalls are distinct (solvency = the cash-flow identity
    against declared ``household_budget.annual_living_costs``; this = the
    retirement drawdown against ``retirement.spending_target``), and a run
    may hit either, both, or neither.

    The caveat text itself is the single registered ``decumulation_shortfall``
    Approximation in model_fidelity.py -- this function renders THAT
    definition (so the console and the TXT/JSON/HTML reports say the same
    thing, one spelling, DP#9), then adds a per-scenario table the reports
    already carry as data.

    Skipped with a loud DP#32 notice when no scenario ever engaged the
    drawdown (no member retired within the horizon, or no spending_target):
    "0 shortfall years" for a run that never checked is the most dangerous
    thing this section could print.
    """
    engaged = [r for r in results
               if shortfall_of(r) is not None
               and shortfall_of(r).get('engaged')]
    if not engaged:
        print(f"  ℹ️  DECUMULATION NOT CHECKED -- no member retires within the "
              f"horizon or no retirement.spending_target was declared, so the "
              f"drawdown shortfall (issue #707) could not be evaluated. This is "
              f"NOT a finding of safety: the household has not been checked, not "
              f"cleared.")
        print()
        return

    exhausted = [r for r in engaged if r['drawdown_shortfall'].get('exhausted')]
    # Render the registered caveat's own summary + findings (one spelling).
    active = {a.id: a for a in model_fidelity.active_approximations(
        cfg, _objective_name_for_results(results))}
    approx = active.get('decumulation_shortfall')

    print(f"{'=' * 120}")
    print(f"  📉  DECUMULATION SHORTFALL (issue #707) -- did the money last?")
    print(f"{'=' * 120}")
    if not exhausted:
        print(f"  ✅ No shortfall: every scenario that drew down met its net "
              f"spending target from its own assets through the horizon.")
        print()
        return

    if approx is not None:
        print(f"\n  ⛔ {approx.summary}")
        ctx = model_fidelity.FidelityContext(cfg=cfg, objective_name=_objective_name_for_results(results))
        for finding in approx.findings_for(ctx):
            print(f"     {finding}")

    print(f"\n  {'Scenario':<40} {'1st shortfall':>15} {'Gap $':>12} "
          f"{'Shortfall yrs':>15} {'Total unmet $':>15}")
    print(f"  {'-' * 100}")
    for r in engaged:
        s = r['drawdown_shortfall']
        # Explicit absence test (DP#32): not `r.get('strategy') or ...`.
        label = r.get('strategy')
        if label is None:
            label = r.get('label')
        if label is None:
            label = '?'
        label = label[:39]
        if s.get('exhausted'):
            yr = s.get('first_shortfall_year')
            yr_txt = f"year {yr}" if yr else "?"
            print(f"  {label:<40} {yr_txt:>15} ${s.get('first_shortfall_gap', 0):>11,.0f} "
                  f"{s.get('shortfall_years', 0):>15} ${s.get('total_unmet', 0):>14,.0f}")
    print()


def _objective_name_for_results(results: List[Dict]) -> Optional[str]:
    """The objective the ranked results were scored on, for the model_fidelity
    caveat's objective-sensitive predicates (issue #585).

    Issue #232 slice 3: moved here from optimize.py with the console reports.
    Deliberately NOT the module's ``_objective_name`` above -- that one names
    the objective off the top row of ``_sort_results`` (which drops exhausted
    trajectories, #707), while this names it off the max-net-benefit row. The
    two agree whenever every row carries the run's one ``objective_name`` and
    nothing was exhausted; unifying them would change which row names the
    objective for an exhausted run -- a behaviour change, not a relocation.
    """
    if not results:
        return None
    best = max(results, key=lambda r: r.get('net_benefit', 0))
    # Explicit: `.get()` already returns None when absent; no `or None`
    # (DP#32 -- a present falsy objective name would be clobbered by `or`).
    return best.get('objective_name')


def _print_solvency_report(winners: List[Dict]) -> None:
    """Issue #679's actual deliverable: solvency as a FIRST-CLASS output.

    "Months of runway, the year the money runs out, and what the household
    was forced to sell" -- these three facts ARE the answer to the job-loss
    question. The terminal net-benefit figure is not; it is the number the
    household reads when nobody tells it the truth.

    Skipped entirely (with a loud DP#32 notice) when the solvency module was
    never engaged, because a household that never declared
    ``household_budget.annual_living_costs`` has not been found SAFE -- it
    has not been CHECKED, and printing "0 shortfalls" for it would be the
    single most dangerous thing this report could say.
    """
    engaged = [w for w in winners if w['solvency'].get('engaged')]
    if not engaged:
        print(f"  ℹ️  SOLVENCY NOT CHECKED -- the contract declares no "
              f"`household_budget.annual_living_costs`, so the cash-flow identity "
              f"(issue #679)")
        print(f"      could not be evaluated. This is NOT a finding of solvency: the "
              f"household has not been checked, not cleared.")
        print()
        return

    print(f"{'=' * 120}")
    print(f"  🩺  SOLVENCY BY INCOME SCENARIO (issue #679) -- can the household actually "
          f"fund its obligations?")
    print(f"{'=' * 120}")
    print(f"\n  {'Scenario':<28} {'Runway':>10} {'1st shortfall':>15} {'Ruin':>10} "
          f"{'Forced-sale tax':>17} {'Realised loss':>15}")
    print(f"  {'-' * 100}")

    for w in engaged:
        s = w['solvency']
        runway = f"{s.get('runway_months_at_start', 0.0):.1f} mo"
        first = s.get('first_shortfall_year')
        first_txt = f"year {first}" if first else "none"
        ruin_year = s.get('first_ruin_year')
        ruin_txt = f"year {ruin_year}" if ruin_year else "no"
        tax = s.get('forced_liquidation_tax', 0.0)
        loss = s.get('forced_liquidation_realized_loss', 0.0)
        print(f"  {w['label']:<28} {runway:>10} {first_txt:>15} {ruin_txt:>10} "
              f"${tax:>16,.0f} ${loss:>14,.0f}")

    for w in engaged:
        s = w['solvency']
        if not s.get('first_shortfall_year'):
            continue
        print(f"\n  ── '{w['label']}' -- what the household was FORCED TO SELL ──")
        print(f"     Shortfall years: {s.get('shortfall_years', 0)}"
              f"   |   Declared reserve at start: "
              f"{s.get('runway_months_at_start', 0.0):.1f} months of essential outflows")
        by_source = s.get('forced_liquidation_gross_by_source', {})
        if by_source:
            # Printed in WATERFALL ORDER (the order actually drawn), because the
            # order and its cost are the answer the household needs -- not an
            # alphabetical list.
            for src in ('emergency_reserve', 'revolving_credit', 'non_reg', 'tfsa', 'registered'):
                if src in by_source and by_source[src] > 0:
                    label = _LIQUIDATION_SOURCE_LABELS.get(src, src)
                    print(f"       {label:<36} ${by_source[src]:>14,.0f} (gross drawn)")
        else:
            print(f"       (nothing left to sell -- every source was already empty)")
        if s.get('uncovered_shortfall', 0.0) > 0:
            print(f"     ⛔ UNCOVERED SHORTFALL: ${s['uncovered_shortfall']:,.0f} -- the waterfall "
                  f"exhausted EVERY source and the household is still short.")
        if s.get('credit_facility_unrepresentable'):
            # Issue #689. An honest understatement beats a silent one.
            print(f"     ⚠️  RESILIENCE UNDERSTATED (issue #689): the waterfall's second step -- a "
                  f"revolving, unsecured credit")
            print(f"        facility (a line of credit) -- CANNOT be declared in the input contract "
                  f"today, so it was drawn as $0.")
            print(f"        A household that HOLDS such a facility is more resilient than this "
                  f"report shows. The HELOC margin is")
            print(f"        deliberately NOT substituted: a HELOC is SECURED against the same "
                  f"charge as the mortgage (#664/#681),")
            print(f"        and spending investment-loan room as an emergency line would model a "
                  f"different product.")
    print()


def _print_runway_report(winners: List[Dict]) -> None:
    """Issue #758's console deliverable: months-to-ruin as a FIRST-CLASS
    output, beside the solvency verdict.

    "You have N months if the shock lands now" -- that is the number a
    household wants before signing a mortgage, and the bracket + the
    interpolation label travel with it so a year-granular engine never
    prints a false-precision month. Mirrors ``_print_solvency_report`` /
    ``_print_decumulation_shortfall_report``: the facts are DATA on the
    ranking row (``runway``), this only renders them.

    Skipped with a loud DP#32 notice when no scenario engaged the cash-flow
    identity -- "UNCHECKED" for every row is the only honest thing to say.
    """
    # A row carrying no `runway` (an older/synthetic winner) is ABSENT, not
    # an un-engaged one -- guard with `in`, never truthiness (DP#32).
    with_runway = [w for w in winners if 'runway' in w and w['runway'].get('engaged')]
    if not with_runway:
        print(f"  ℹ️  RUNWAY NOT CHECKED (issue #758) -- no scenario engaged the "
              f"cash-flow identity (declare `household_budget.annual_living_costs` "
              f"and a dated `decisions.income[]` shock).")
        print(f"      This is NOT a finding of safety: the household has not been "
              f"checked, not cleared.")
        print()
        return

    print(f"{'=' * 120}")
    print(f"  🛟  RUNWAY — months to insolvency after the income shock (issue #758)")
    print(f"{'=' * 120}")
    print(f"  The headline is a LABELLED interpolation inside an honest bracket")
    print(f"  (the engine steps in years; ~N mo is a point estimate, [lo–hi] is the")
    print(f"  structural range). '>=N mo (survives)' = the cushion outlasts the horizon.")
    print(f"\n  {'Scenario':<28} {'Runway':>22} {'Stress begins':>16} {'Caveats':<40}")
    print(f"  {'-' * 110}")
    for w in with_runway:
        rw = w['runway']
        runway_txt = _format_runway_inline(rw)
        stress = rw.get('stress_begins_months')
        stress_txt = f"~{stress:.0f} mo" if stress is not None else "—"
        caveats = []
        if rw.get('relies_on_credit_facility'):
            caveats.append("leans on credit line")
        if rw.get('drew_registered'):
            caveats.append("drew RRSP (taxed at low yr-rate)")
        caveat_txt = "; ".join(caveats) if caveats else "—"
        print(f"  {w['label']:<28} {runway_txt:>22} {stress_txt:>16}   {caveat_txt:<40}")
    print(f"\n  Interpolation method: linear within the ruin year (uniform monthly")
    print(f"  burn); the fraction is the #679 waterfall's own covered/shortfall.")
    print(f"  Runway UNDERSTATES reality: all spend treated as rigid (no")
    print(f"  discretionary/non-discretionary split exists in the contract yet) and")
    print(f"  contributions are counted as committed -- a household in real distress")
    print(f"  stops both, so the true runway is longer. See the model-fidelity section.")
    print()
