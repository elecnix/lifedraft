#!/usr/bin/env python3
"""Issue #473: wire asset-location placement into the optimize/simulate flow.

The optimizer already tells a household *which accounts* to fund and in what
order. It did not decide *which asset class belongs in which account* for tax
efficiency -- it took each account's composition verbatim and only scored the
consequence of that fixed placement. This module turns placement into an
OPTIMIZABLE dimension.

The falsifiable lever is #641's ``PortfolioConfig.registered_wht_drag()``: the
one tax that leaks from an otherwise tax-sheltered account is foreign
withholding tax, and its rate depends on the account KIND. US equity is
treaty-exempt in an RRSP (0%) but loses 15% unrecoverably in a TFSA. So the
account that shelters a foreign-equity sleeve changes the simulated after-tax
outcome -- the thing that made #473's earlier PRs unfalsifiable, now fixed.

What this module does (DP#33 -- a declaration is a LENS, not a BLINDFOLD):

* :func:`discover_placements` enumerates every distinct arrangement of the
  household's declared registered profiles across its registered accounts. The
  arrangement it actually declared is one row among them, marked ``declared``;
  the alternatives ANNOTATE the exploration, they do not replace the search.
* :func:`rank_placements` runs the FULL optimizer once per arrangement and reads
  the winner's objective, so the placement is ranked by the SAME simulation
  every other lever is scored on (DP#18: the choice reaches a key a rule reads --
  ``portfolio.accounts.{rrsp,tfsa}`` -> ``registered_wht_drag`` -> the fold).
* :func:`recommend_asset_location` reports the chosen placement, its after-tax
  benefit over the worst arrangement, and -- reusing ``asset_location.py``'s
  scoring (DP#9, one WHT model) -- the per-asset-class tax-drag comparison.

Absence is a strict no-op: a household that declares no foreign holding on any
registered account (the golden household -- composition only on ``non_reg``) has
nothing to place, so :func:`discover_placements` yields a single arrangement and
:func:`recommend_asset_location` returns ``None`` (DP#32).
"""
from __future__ import annotations

import itertools
from copy import deepcopy
from typing import Any, Dict, List, Optional

# The registered kinds whose foreign-withholding-tax drag differs by shelter --
# the only accounts asset location can move a tax term between (#641). non_reg is
# excluded: its WHT is recoverable via the foreign tax credit and its composition
# already reaches the engine through non_reg_after_tax_return (double-counting it
# here is the exact trap #641 warns against).
REGISTERED_KINDS = ("rrsp", "tfsa")


def _foreign_weight(acct_data: Dict[str, Any]) -> float:
    """The foreign-equity intensity of a portfolio account's declared holdings.

    Foreign income (US/international dividends) is the only income character
    whose tax treatment depends on the sheltering account kind, so it is the
    only signal that decides where a sleeve *wants* to live. Reads both the
    yield character (``foreign_income``) and the allocation buckets
    (``us_equity_pct``/``intl_equity_pct``) so a profile expressed either way is
    recognised.
    """
    y = acct_data.get("yield", {})
    comp = acct_data.get("composition", {})
    return (y.get("foreign_income", 0)
            + comp.get("us_equity_pct", 0)
            + comp.get("intl_equity_pct", 0))


def _profile_of(acct_data: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """The 'what it holds' profile of a portfolio account -- its composition and
    yield character, deliberately WITHOUT the account-level balance/cost_basis.

    Placement moves the *investments* between accounts; the money already in each
    account (its balance, seeded from the member's rrsp/tfsa balance) stays in
    the slot it belongs to. Copying only composition+yield keeps that invariant.
    """
    return {
        "composition": dict(acct_data.get("composition", {})),
        "yield": dict(acct_data.get("yield", {})),
    }


def _arrangement_key(arrangement: Dict[str, Dict]) -> tuple:
    """A hashable identity for an arrangement, used to drop duplicate
    permutations (two arrangements that place identical profiles in the same
    slots are the same decision -- e.g. when the profiles do not differ)."""
    return tuple(
        (kind,
         tuple(sorted(arrangement[kind]["composition"].items())),
         tuple(sorted(arrangement[kind]["yield"].items())))
        for kind in sorted(arrangement)
    )


def _foreign_landing(arrangement: Dict[str, Dict]) -> Optional[str]:
    """The registered kind that ends up holding the most foreign exposure under
    this arrangement, or ``None`` if none of the assigned profiles is foreign."""
    best_kind: Optional[str] = None
    best_weight = 0.0
    for kind, profile in arrangement.items():
        weight = _foreign_weight(profile)
        if weight > best_weight:
            best_weight = weight
            best_kind = kind
    return best_kind


def discover_placements(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Enumerate the distinct asset-location arrangements available to a
    household (DP#33: the declared arrangement is one annotated row among all).

    A placement candidate is an assignment of the household's declared
    registered profiles (composition + yield) to its registered account slots.
    A real choice exists only when the household declares BOTH registered
    accounts in ``portfolio.accounts`` AND at least one carries a foreign sleeve
    (foreign WHT is the only tax term placement can move, #641). Otherwise there
    is nothing to place, and exactly ONE arrangement -- the declared one -- is
    returned, so the caller is a strict no-op (DP#32).

    Each returned candidate carries: ``id``, ``label``, ``arrangement``
    (``{kind: profile}``), ``foreign_kind`` (where the foreign sleeve lands), and
    ``declared`` (whether this is the household's own declared arrangement).
    """
    accounts = cfg.get("portfolio", {}).get("accounts", {})
    slots = [k for k in REGISTERED_KINDS if k in accounts]
    declared_arrangement = {k: _profile_of(accounts[k]) for k in slots}
    declared = {
        "id": "as_declared",
        "label": "As declared",
        "arrangement": declared_arrangement,
        "foreign_kind": _foreign_landing(declared_arrangement),
        "declared": True,
    }

    # Fewer than two slots, or no foreign sleeve anywhere: no placement decision
    # to make. Explicit checks, not truthiness -- an absent portfolio is a
    # legitimate "nothing declared", not a value to coerce.
    if len(slots) < 2:
        return [declared]
    if not any(_foreign_weight(accounts[k]) > 0 for k in slots):
        return [declared]

    profiles = [_profile_of(accounts[k]) for k in slots]
    candidates: List[Dict[str, Any]] = []
    seen: set = set()
    declared_key = _arrangement_key(declared_arrangement)
    for perm in itertools.permutations(range(len(slots))):
        arrangement = {slots[i]: profiles[perm[i]] for i in range(len(slots))}
        key = _arrangement_key(arrangement)
        if key in seen:
            continue
        seen.add(key)
        is_declared = key == declared_key
        foreign_kind = _foreign_landing(arrangement)
        if is_declared:
            candidates.append(declared)
        else:
            landing = foreign_kind if foreign_kind is not None else "none"
            candidates.append({
                "id": f"foreign_in_{landing}",
                "label": f"Foreign equity in {landing.upper()}"
                         if foreign_kind is not None else "Alternative placement",
                "arrangement": arrangement,
                "foreign_kind": foreign_kind,
                "declared": False,
            })

    # All permutations collapsed to the declared arrangement (identical profiles):
    # no genuine alternative, so it is still a no-op.
    if len(candidates) < 2:
        return [declared]
    return candidates


def apply_placement(cfg: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of ``cfg`` with each registered account holding the
    profile the candidate assigns to it (DP#18: the overlay lands on
    ``portfolio.accounts.{kind}`` -- exactly the key ``registered_wht_drag``
    reads). Each account keeps its own balance/cost_basis; only what it holds
    (composition + yield) changes."""
    variant = deepcopy(cfg)
    accounts = variant["portfolio"]["accounts"]
    for kind, profile in candidate["arrangement"].items():
        accounts[kind]["composition"] = dict(profile["composition"])
        accounts[kind]["yield"] = dict(profile["yield"])
    return variant


def rank_placements(cfg: Dict[str, Any], input_path: str = "input.json",
                    objective=None) -> List[Dict[str, Any]]:
    """Run the full optimizer once per placement arrangement and rank them by
    the winning strategy's objective (DP#22: the optimizer ranks; the household
    chooses). Returns one row per arrangement, best-first.

    The objective is read from the SAME optimization every other lever is scored
    on, so the placement's after-tax consequence is the real simulated outcome
    (via #641's ``registered_wht_drag``), not a separate closed-form estimate.
    """
    # Local import breaks the optimize <-> asset_location_optimize cycle: optimize
    # imports this module at load time; this function needs run_optimization only
    # when actually invoked.
    from optimize import run_optimization

    rows: List[Dict[str, Any]] = []
    for placement in discover_placements(cfg):
        variant = apply_placement(cfg, placement)
        results = run_optimization(variant, input_path, objective=objective,
                                     include_year_by_year=False)  # score-only caller — skip year_by_year serialization, #1058
        if not results:
            continue
        best = results[0]
        rows.append({
            "placement_id": placement["id"],
            "placement_label": placement["label"],
            "foreign_kind": placement["foreign_kind"],
            "declared": placement["declared"],
            "strategy": best.get("strategy"),
            "objective_score": best.get("objective_score", best.get("net_benefit", 0)),
        })
    rows.sort(key=lambda r: r["objective_score"], reverse=True)
    return rows


def _drag_comparison(cfg: Dict[str, Any], objective=None) -> Dict[str, Any]:
    """The per-asset-class tax-drag comparison for the chosen placement, reusing
    ``asset_location.light_vs_ludicrous`` (DP#9: one WHT model, not a second
    spelling). Builds the household's combined registered sleeve as
    ``PortfolioHolding``s and prices the light (same everywhere) vs ludicrous
    (per-account) approaches at the primary earner's marginal rate."""
    from countries.canada.asset_location import (
        PortfolioHolding, ETFType, light_vs_ludicrous,
    )
    from tax_calculator import marginal_rate
    from tax_data import default_tax_provider

    province = cfg.get("tax", {}).get("province", "quebec")
    members = cfg.get("family", {}).get("members", [])
    primary_income = 0
    for m in members:
        if m.get("role") == "primary":
            primary_income = m.get("gross_income", 0)
    brackets = default_tax_provider().get_combined_brackets(province=province)
    mtr = marginal_rate(primary_income, brackets)

    # Combine the registered accounts' declared composition into one sleeve; the
    # buckets map onto the ETF tax archetypes asset_location.py scores.
    accounts = cfg.get("portfolio", {}).get("accounts", {})
    buckets = {
        ETFType.CANADIAN_EQUITY: "cdn_equity_pct",
        ETFType.US_LISTED_EQUITY: "us_equity_pct",
        ETFType.INTERNATIONAL_EQUITY: "intl_equity_pct",
        ETFType.BONDS: "fixed_income_pct",
    }
    totals: Dict[ETFType, float] = {etf: 0.0 for etf in buckets}
    for kind in REGISTERED_KINDS:
        comp = accounts.get(kind, {}).get("composition", {})
        for etf, field in buckets.items():
            totals[etf] += comp.get(field, 0)
    grand = sum(totals.values())
    holdings = []
    if grand > 0:
        for etf, weight in totals.items():
            if weight > 0:
                holdings.append(PortfolioHolding(
                    name=etf.value, etf_type=etf, allocation_pct=weight / grand))

    return light_vs_ludicrous(holdings, marginal_rate=mtr, province=province)


def recommend_asset_location(cfg: Dict[str, Any], input_path: str = "input.json",
                             objective=None) -> Optional[Dict[str, Any]]:
    """The asset-location recommendation for a household, or ``None`` when there
    is no placement decision to make (a strict no-op for the golden household).

    Runs :func:`rank_placements` and reports the chosen (top-ranked) arrangement,
    its after-tax benefit over the worst arrangement (lifetime dollars, from the
    simulation), and the reused ``asset_location.py`` per-asset-class drag
    comparison.
    """
    placements = discover_placements(cfg)
    if len(placements) < 2:
        return None
    rows = rank_placements(cfg, input_path, objective=objective)
    if len(rows) < 2:
        return None
    best = rows[0]
    worst = rows[-1]
    return {
        "chosen": best,
        "foreign_kind": best["foreign_kind"],
        "ranking": rows,
        "after_tax_benefit": best["objective_score"] - worst["objective_score"],
        "drag_comparison": _drag_comparison(cfg, objective=objective),
    }


# ── Issue #859 Part B: asset location ACROSS members ──────────────────────────
#
# #473 (above) places an asset class among ONE member's accounts. Part B extends
# the decision ACROSS family members: put a foreign-equity sleeve in the MEMBER
# (and their registered account) where it is most tax-efficient for the family as
# a whole -- maximizing the FAMILY after-tax objective (#861,
# ``max_family_after_tax_networth``). Two levers, both reused (DP#9):
#
#   * #641's ``PortfolioConfig.registered_wht_drag()`` -- the foreign sleeve leaks
#     withholding tax that differs by account KIND (treaty-exempt in an RRSP,
#     unrecoverable in a TFSA); a domestic sleeve leaks nothing. The composition
#     reaches the score, so the placement is falsifiable (the #473/#641 unblock).
#   * #861's per-member deemed-disposition seam
#     (``after_tax_networth_of_own_accounts``, resolved through the estate
#     provider exactly as ``objective.py`` does -- DP#25, no ``countries`` import)
#     -- each member's registered pot is taxed as ordinary income at their OWN
#     terminal bracket, so a growth sleeve sheltered in a LOWER-bracket member's
#     RRSP is taxed less at the horizon. That is the cross-member decision.
#
# SCOPE (#917): like #473 this operates at the internal-config / optimize-flow
# level. The contract adapter threads only ``non_reg`` composition today, so this
# is reachable + tested from an internally-constructed multi-member config but is
# a strict no-op from a ``--input`` contract until #917 wires per-member
# composition. Absence is a no-op everywhere: no declared ``cross_member_sleeve``
# (the golden household) -> a single arrangement and no recommendation (DP#32).


def _sleeve_drag(sleeve: Dict[str, Any]) -> Dict[str, float]:
    """The per-registered-kind foreign-withholding-tax drag of a sleeve's
    declared holdings, reusing #641's ``PortfolioConfig.registered_wht_drag()``
    (DP#9 -- one WHT model). Empty for a domestic/fixed-income-only sleeve (no
    foreign income to withhold), so such a sleeve grows at the flat gross rate.

    Prices the sleeve through ``AccountPortfolio.wht_drag_bps`` -- the SAME WHT
    physics ``PortfolioConfig.registered_wht_drag`` sums (DP#9) -- built from the
    sleeve's declared yield buckets. It deliberately does NOT go through
    ``PortfolioConfig.from_dict``: the sleeve declares no product ``holdings``, so
    the registry-derivation branch that path carries is never exercised at
    runtime and must not be pulled into the reachable graph either.
    """
    from countries.canada.portfolio import AccountPortfolio, YieldBreakdown
    yld = sleeve.get("yield", {})
    account = AccountPortfolio(
        yield_breakdown=YieldBreakdown(
            eligible_dividends=yld.get("eligible_dividends", 0),
            non_eligible_dividends=yld.get("non_eligible_dividends", 0),
            interest=yld.get("interest", 0),
            capital_gains=yld.get("capital_gains", 0),
            return_of_capital=yld.get("return_of_capital", 0),
            foreign_income=yld.get("foreign_income", 0)))
    drag: Dict[str, float] = {}
    for kind in REGISTERED_KINDS:
        rate = account.wht_drag_bps(kind) / 10000.0
        if rate > 0:
            drag[kind] = rate
    return drag


def _cross_member_sleeve(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The declared cross-member sleeve to place, or ``None`` when none is
    declared (a strict no-op -- the golden household, DP#32). Explicit key
    lookups, never truthiness on a possibly-absent block."""
    al = cfg.get("asset_location")
    if al is None:
        return None
    return al.get("cross_member_sleeve")


def _member_slots(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every registered (member, kind) slot the family declares -- the hosts a
    sleeve could live in. A member declares its available registered kinds
    explicitly via ``member['registered'] = {kind: base_balance}`` (DP#32: only
    listed kinds are slots; a base of 0 is a declared empty pot, not absence)."""
    slots: List[Dict[str, Any]] = []
    for member in cfg.get("family", {}).get("members", []):
        registered = member.get("registered")
        if registered is None:
            continue
        for kind in REGISTERED_KINDS:
            if kind in registered:
                slots.append({"member": member["id"], "kind": kind,
                              "base": registered[kind]})
    return slots


def _horizon(cfg: Dict[str, Any]) -> Optional[tuple]:
    """The (gross return, years) needed to project a sleeve to the horizon, or
    ``None`` when either is undeclared -- nothing to project, a no-op (DP#32)."""
    gross = cfg.get("investment_return")
    years = cfg.get("projection_years")
    if gross is None or years is None:
        return None
    return gross, years


def discover_member_placements(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Enumerate the cross-member placements of the family's foreign sleeve
    (DP#33: the declared placement is one annotated row among all).

    A genuine choice exists only when a ``cross_member_sleeve`` is declared, the
    horizon is projectable, AND the family declares at least two registered
    slots spanning DISTINCT members (a within-member choice is #473's job, not
    Part B's). Otherwise exactly one arrangement -- the declared one -- is
    returned and the caller is a strict no-op.

    Each candidate carries ``id``, ``label``, ``member``, ``kind`` (the host),
    and ``declared`` (whether it is the family's own declared placement).
    """
    sleeve = _cross_member_sleeve(cfg)
    slots = _member_slots(cfg)
    home = sleeve.get("home") if sleeve is not None else None

    def _candidate(slot: Dict[str, Any]) -> Dict[str, Any]:
        declared = (home is not None
                    and slot["member"] == home.get("member")
                    and slot["kind"] == home.get("kind"))
        return {
            "id": f"sleeve_in_{slot['member']}_{slot['kind']}",
            "label": f"Foreign sleeve in {slot['member']}'s {slot['kind'].upper()}",
            "member": slot["member"],
            "kind": slot["kind"],
            "declared": declared,
        }

    _NO_CHOICE = {"id": "as_declared", "label": "As declared", "member": None,
                  "kind": None, "declared": True}

    # No sleeve / no horizon: nothing to place. Fall back to the declared slot if
    # one is even identifiable, else a single-arrangement no-op.
    if sleeve is None or _horizon(cfg) is None:
        declared_slot = next(
            (s for s in slots if home is not None
             and s["member"] == home.get("member") and s["kind"] == home.get("kind")),
            None)
        return [_candidate(declared_slot)] if declared_slot is not None else [_NO_CHOICE]

    distinct_members = {s["member"] for s in slots}
    if len(slots) < 2 or len(distinct_members) < 2:
        # Only a within-member (or no) alternative: not a cross-member decision.
        declared = [_candidate(s) for s in slots if _candidate(s)["declared"]]
        return declared or [_NO_CHOICE]

    return [_candidate(s) for s in slots]


def _score_family_placement(cfg: Dict[str, Any], sleeve: Dict[str, Any],
                            host_member: str, host_kind: str) -> float:
    """The FAMILY after-tax net worth when the sleeve lives in
    ``(host_member, host_kind)`` -- the ``max_family_after_tax_networth``
    objective (#861) evaluated over the projected per-member pots.

    Each member's base registered pots grow at the flat gross rate; the host's
    chosen pot additionally carries the sleeve, which grows at ``gross -
    wht_drag(kind)`` (#641 -- the foreign leak, zero for a domestic sleeve). Each
    member's terminal pots are then valued on the SAME per-member deemed-
    disposition seam the family objective sums (DP#9/DP#25): RRSP taxed as
    ordinary income at the member's own bracket, TFSA tax-free. Summing that seam
    across members IS the family after-tax net worth for a family of independent
    members (the shape ``_children_/_extra_adults_after_tax_networth`` compute).
    """
    from jurisdiction_providers import get_provider
    from tax_data import default_tax_provider

    seam = get_provider("estate")["after_tax_networth_of_own_accounts"]
    tax_cfg = cfg.get("tax", {})
    brackets = default_tax_provider().get_combined_brackets(
        tax_cfg.get("year", 2026), tax_cfg.get("province", "quebec"))
    gross, years = _horizon(cfg)
    drag = _sleeve_drag(sleeve)
    sleeve_balance = sleeve.get("balance", 0.0)

    total = 0.0
    for member in cfg["family"]["members"]:
        registered = member.get("registered")
        if registered is None:
            continue
        pots = {kind: registered.get(kind, 0.0) * (1 + gross) ** years
                for kind in REGISTERED_KINDS}
        if member["id"] == host_member:
            net_return = gross - drag.get(host_kind, 0.0)
            pots[host_kind] += sleeve_balance * (1 + net_return) ** years
        total += seam(rrsp=pots["rrsp"], tfsa=pots["tfsa"], fhsa=0.0,
                      non_reg_fmv=0.0, non_reg_acb=0.0, brackets=brackets)
    return total


def rank_member_placements(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Score every cross-member placement on the FAMILY after-tax objective and
    rank best-first (DP#22: the optimizer ranks; the family chooses). One row
    per candidate; ``[]`` when there is no genuine cross-member choice."""
    sleeve = _cross_member_sleeve(cfg)
    candidates = discover_member_placements(cfg)
    if sleeve is None or _horizon(cfg) is None or len(candidates) < 2:
        return []
    rows: List[Dict[str, Any]] = []
    for cand in candidates:
        score = _score_family_placement(cfg, sleeve, cand["member"], cand["kind"])
        rows.append({**cand, "objective_score": score})
    rows.sort(key=lambda r: r["objective_score"], reverse=True)
    return rows


def recommend_cross_member_location(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The cross-member asset-location recommendation, or ``None`` when there is
    no cross-member placement decision to make (a strict no-op for the golden
    household). Reports the chosen host member+account, its after-tax benefit
    over the worst placement (family dollars), and the full ranking."""
    from objective import MAX_FAMILY_AFTER_TAX_NETWORTH

    rows = rank_member_placements(cfg)
    if len(rows) < 2:
        return None
    best, worst = rows[0], rows[-1]
    return {
        "chosen": best,
        "objective": MAX_FAMILY_AFTER_TAX_NETWORTH.name,
        "ranking": rows,
        "after_tax_benefit": best["objective_score"] - worst["objective_score"],
    }


def format_cross_member_location(rec: Dict[str, Any]) -> str:
    """A readable console block for the chosen cross-member placement + its
    ranked alternatives (mirrors #473's ``format_asset_location``)."""
    chosen = rec["chosen"]
    lines = ["  📍 ASSET LOCATION (across family members)"]
    lines.append(f"     Hold the foreign-equity sleeve in {chosen['member']}'s "
                 f"{chosen['kind'].upper()} (lowest family after-tax tax).")
    lines.append(f"     After-tax benefit over the worst placement: "
                 f"${rec['after_tax_benefit']:,.0f}")
    lines.append(f"     {'placement':<34} {'family objective':>18}")
    lines.append(f"     {'-' * 34} {'-' * 18}")
    for r in rec["ranking"]:
        mark = " ★" if r["declared"] else "  "
        lines.append(f"     {r['label']:<32}{mark} ${r['objective_score']:>16,.0f}")
    return "\n".join(lines)


def format_asset_location(rec: Dict[str, Any]) -> str:
    """A readable console block for the chosen placement + its ranked
    alternatives (acceptance criterion: optimize/simulate print the
    recommendation for the chosen scenario)."""
    lines = ["  📍 ASSET LOCATION (tax-efficient placement)"]
    foreign_kind = rec.get("foreign_kind")
    if foreign_kind is not None:
        lines.append(f"     Hold the foreign-equity sleeve in the "
                     f"{foreign_kind.upper()} (lowest withholding-tax drag).")
    lines.append(f"     After-tax benefit over the worst placement: "
                 f"${rec['after_tax_benefit']:,.0f}")
    drag = rec.get("drag_comparison", {})
    savings_bps = drag.get("savings_bps")
    if savings_bps is not None:
        lines.append(f"     Tax-drag saved (ludicrous vs light): "
                     f"{savings_bps:.1f} bps/yr")
    lines.append(f"     {'placement':<28} {'objective':>16}")
    lines.append(f"     {'-' * 28} {'-' * 16}")
    for r in rec["ranking"]:
        mark = " ★" if r["declared"] else "  "
        lines.append(f"     {r['placement_label']:<26}{mark} "
                     f"${r['objective_score']:>14,.0f}")
    return "\n".join(lines)
