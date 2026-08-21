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
# is the metric module itself, which both layers may import.)
from runway import format_runway as _format_runway_inline  # noqa: E402


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
        from simulation_config import (
            SimulationConfig, build_overlay_config, ScenarioOverlay,
            refinance_amortization_fallback,
        )
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
