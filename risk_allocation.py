#!/usr/bin/env python3
"""Issue #474: risk-aware asset-ALLOCATION recommendation.

#473 chose *which account holds which asset class* for tax efficiency (the
"where"). It never chose *how much* of each asset class to hold, nor named the
*kind* of product to buy. This module adds the risk side of a real household's
question -- "in what mix of stocks/bonds should I invest so I limit my risk
while keeping the upside?" -- as a recommendation layered on top of #473's
placement, not a replacement for it.

Two layers, exactly as the issue asks:

1. **A risk/horizon-driven target mix.** The recommended equity/fixed-income
   split responds to the household's DECLARED risk tolerance (``portfolio.
   risk_tolerance``) and to its horizon (a glide path: more fixed income as the
   primary earner nears retirement). The chosen mix is priced with Monte Carlo
   risk metrics (P10/P50/P90 terminal multiple and P(loss)) reusing the
   existing seeded ``StochasticReturn`` engine (DP#9 -- one MC model, not a
   second spelling) -- the same instrument #366/``--mc`` already uses.

2. **Product-category guidance.** Each bucket is mapped to a concrete product
   *category* (not a ticker -- advice-safe, jurisdiction-portable), anchored on
   the ``ETFType`` archetypes ``asset_location.py`` already classifies by tax
   treatment (DP#9): broad-market global equity index for the equity sleeve, an
   aggregate-bond ETF / GIC ladder for fixed income.

Absence is a strict no-op (DP#32): a household that declares no
``portfolio.risk_tolerance`` has not asked this question, so
:func:`recommend_allocation` returns ``None`` and nothing is surfaced. This
module never touches the simulation -- it only reads the config and reports --
so the golden trajectory cannot move by construction.

DP#33 -- a declaration is a LENS, not a BLINDFOLD: when the household has also
declared a current composition, its current equity/fixed-income split is carried
alongside the recommendation as ``declared_mix`` (an annotation the user can
compare against), it does not silently replace the search.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# The declared risk-tolerance categories and the base equity target each maps to
# (long-horizon, before the horizon glide is applied). These are DP#13 belief
# constants -- coarse, category-level anchors, the risk-side analogue of the
# fixed withholding-tax rates asset_location.py prices with. Ordered least- to
# most-aggressive so the mapping is self-documenting.
BASE_EQUITY_TARGET: Dict[str, float] = {
    "conservative": 0.40,
    "balanced": 0.60,
    "growth": 0.80,
    "aggressive": 1.00,
}

# Glide path: full base equity when retirement is >= GLIDE_START_YEARS away;
# approaching retirement, shave up to GLIDE_MAX_SHAVE off the equity weight
# (shift into fixed income to cut sequence-of-returns risk). A declared risk
# tolerance sets the *altitude*; the horizon sets how far the glide has
# descended.
GLIDE_START_YEARS = 20.0
GLIDE_MAX_SHAVE = 0.30

# Long-run return beliefs per sleeve (DP#21: pluggable INPUT, not hardcoded
# assumption). Equity carries the higher mean AND the higher volatility; fixed
# income is the ballast. The blended sigma is a weighted sum -- deliberately
# conservative (it assumes the sleeves co-move rather than diversify), so the
# reported downside is not understated.
#
# These four constants are the DEFAULT beliefs, preserved byte-for-byte from
# the pre-#993 literals so nothing moves. A household that disagrees with
# 6.8% equity can override WITHOUT editing source by declaring
# ``assumptions.return_beliefs`` (see ``_resolve_return_beliefs`` /
# ``recommend_allocation``), which threads the override into ``_mc_risk_metrics``.
# Absence of the config block falls back to these defaults -- an explicit
# ``return_beliefs`` dict is honoured as-is (no ``x or DEFAULT``), DP#32.
EQUITY_MEAN, EQUITY_SIGMA = 0.068, 0.16
FIXED_INCOME_MEAN, FIXED_INCOME_SIGMA = 0.030, 0.05

# The default sleeve-belief bundle, assembled from the four constants above so
# the override path and the default path read the SAME numbers (DP#9 -- one
# spelling of each belief). Keys match the ``assumptions.return_beliefs``
# config block a household declares to override (DP#21).
_DEFAULT_RETURN_BELIEFS = {
    "equity_mean": EQUITY_MEAN,
    "equity_sigma": EQUITY_SIGMA,
    "fixed_income_mean": FIXED_INCOME_MEAN,
    "fixed_income_sigma": FIXED_INCOME_SIGMA,
}


def _resolve_return_beliefs(
    cfg: Dict[str, Any],
    override: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """The sleeve return beliefs to price the recommended mix with (DP#21).

    Priority (highest wins), all DP#32-safe (no ``x or DEFAULT`` -- an explicit
    value of 0.0 for any belief is honoured, never coerced to the default):
      1. ``override`` -- an explicit beliefs dict handed to the caller (used to
         thread an override through ``recommend_allocation`` ->
         ``_mc_risk_metrics`` without re-reading the config).
      2. ``cfg["assumptions"]["return_beliefs"]`` -- the household's declared
         beliefs, the DP#21 pluggable-input path (a user who disagrees with 6.8%
         equity overrides here, without editing source).
      3. ``_DEFAULT_RETURN_BELIEFS`` -- the module constants, preserved
         byte-for-byte from the pre-#993 literals so behaviour is unchanged when
         nothing is declared.

    Returns a dict with keys ``equity_mean``, ``equity_sigma``,
    ``fixed_income_mean``, ``fixed_income_sigma``.
    """
    if override is not None:
        beliefs = override
    else:
        beliefs = cfg.get("assumptions", {}).get("return_beliefs")
    if beliefs is None:
        return dict(_DEFAULT_RETURN_BELIEFS)
    # Merge over the defaults so a partial override (e.g. only equity_mean) does
    # not silently drop the other three beliefs to 0 -- DP#32: a missing key in
    # an explicit override is absent input, not a coerced zero. A caller who
    # means a literal 0 for a belief sets it explicitly.
    resolved = dict(_DEFAULT_RETURN_BELIEFS)
    resolved.update(beliefs)
    return resolved

# Monte Carlo sampling for the recommended mix's risk metrics. Reproducible
# (fixed seed base, DP#23) so the reported P10/P50/P90 are deterministic.
MC_PATHS = 400
MC_SEED_BASE = 474


def _primary_member(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The primary earner, whose age/retirement drives the horizon glide."""
    for m in cfg.get("family", {}).get("members", []):
        if m.get("role") == "primary":
            return m
    return None


def _horizon(cfg: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """Years-to-retirement and years-to-horizon for the primary earner.

    Returns ``None`` -- not a coerced default -- when the config lacks the fields
    that date the glide (start year, birth year, retirement age). DP#32: absence
    of the horizon is a real "cannot answer", not a silent zero.
    """
    assumptions = cfg.get("assumptions", {})
    primary = _primary_member(cfg)
    if primary is None:
        return None
    start_year = assumptions.get("start_year")
    birth_year = primary.get("birth_year")
    retirement_age = primary.get("retirement_age")
    if start_year is None or birth_year is None or retirement_age is None:
        return None
    current_age = start_year - birth_year
    horizon_age = assumptions.get("horizon_age")
    years_to_retirement = max(0, retirement_age - current_age)
    years_to_horizon = (max(1, horizon_age - current_age)
                        if horizon_age is not None
                        else max(1, years_to_retirement))
    return {
        "current_age": current_age,
        "years_to_retirement": years_to_retirement,
        "years_to_horizon": years_to_horizon,
    }


def recommended_mix(risk_tolerance: str, years_to_retirement: int) -> Dict[str, float]:
    """The recommended equity/fixed-income split for a declared risk tolerance,
    glided by the years remaining to retirement.

    Raises ``ValueError`` (loudly, naming the valid categories -- DP#32) for an
    unrecognised risk tolerance rather than coercing it to a default mix.
    """
    if risk_tolerance not in BASE_EQUITY_TARGET:
        raise ValueError(
            f"unknown risk_tolerance {risk_tolerance!r}; "
            f"expected one of {sorted(BASE_EQUITY_TARGET)}")
    base_equity = BASE_EQUITY_TARGET[risk_tolerance]
    glide = min(1.0, max(0.0, years_to_retirement / GLIDE_START_YEARS))
    equity = base_equity - GLIDE_MAX_SHAVE * (1.0 - glide)
    equity = min(1.0, max(0.0, equity))
    return {"equity_pct": equity, "fixed_income_pct": 1.0 - equity}


def _product_categories(mix: Dict[str, float]) -> List[Dict[str, Any]]:
    """Map each bucket of the recommended mix to a concrete product *category*
    (issue #474 layer 2), anchored on ``asset_location.ETFType`` (DP#9). Only
    buckets with a positive weight are named."""
    from countries.canada.asset_location import ETFType

    buckets = [
        ("equity", mix["equity_pct"], ETFType.INTERNATIONAL_EQUITY,
         "broad-market global equity index ETF"),
        ("fixed_income", mix["fixed_income_pct"], ETFType.BONDS,
         "aggregate-bond ETF or GIC ladder"),
    ]
    return [
        {"bucket": name, "pct": pct, "etf_type": etf.value, "category": category}
        for name, pct, etf, category in buckets
        if pct > 0
    ]


def _mc_risk_metrics(mix: Dict[str, float], years_to_horizon: int,
                      seed: int = MC_SEED_BASE,
                      return_beliefs: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Monte Carlo P10/P50/P90 terminal-wealth multiple and P(loss) for the
    recommended mix, over the household's investment horizon.

    Reuses the existing ``StochasticReturn`` engine (DP#9): each path draws a
    reproducible sequence of blended annual returns and compounds them to a
    terminal multiple of the starting balance. The percentile indices match the
    Monte Carlo optimizer's convention (``monte_carlo_optimizer._compute_risk_measures``).

    DP#23: ``seed`` threads the reproducible RNG base to the caller. The default
    (``MC_SEED_BASE = 474``) preserves the historical behaviour byte-for-byte;
    ``seed=0`` is a valid, falsy seed and is honoured as-is (no ``seed or ...``).

    DP#21: ``return_beliefs`` threads the sleeve return beliefs (equity/fixed-
    income mean & sigma) to the caller. ``None`` (the default) uses the module
    constants ``_DEFAULT_RETURN_BELIEFS``, preserving the pre-#993 behaviour
    byte-for-byte. An explicit dict overrides -- a user who disagrees with 6.8%
    equity can pass their own beliefs without editing source. An explicit 0.0
    for any belief is honoured (no ``x or DEFAULT``), DP#32.
    """
    from return_model import StochasticReturn

    beliefs = _resolve_return_beliefs({}, override=return_beliefs)
    equity, fixed_income = mix["equity_pct"], mix["fixed_income_pct"]
    blended_mean = (equity * beliefs["equity_mean"]
                    + fixed_income * beliefs["fixed_income_mean"])
    blended_sigma = (equity * beliefs["equity_sigma"]
                     + fixed_income * beliefs["fixed_income_sigma"])

    multiples: List[float] = []
    for i in range(MC_PATHS):
        model = StochasticReturn(mean=blended_mean, sigma=blended_sigma,
                                 seed=seed + i, n_years=years_to_horizon)
        multiple = 1.0
        for year in range(years_to_horizon):
            multiple *= 1.0 + model.return_for_year(year)
        multiples.append(multiple)
    multiples.sort()

    n = len(multiples)
    p10 = multiples[max(0, int(0.1 * n) - 1)]
    p50 = multiples[max(0, int(0.5 * n) - 1)]
    p90 = multiples[max(0, int(0.9 * n) - 1)]
    p_loss = sum(1 for m in multiples if m < 1.0) / n
    return {
        "blended_mean": blended_mean,
        "blended_sigma": blended_sigma,
        "horizon_years": years_to_horizon,
        "p10": p10,
        "p50": p50,
        "p90": p90,
        "probability_of_loss": p_loss,
    }


def _declared_mix(cfg: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """The household's CURRENTLY-declared equity/fixed-income split, aggregated
    across every portfolio account's composition, or ``None`` when no
    composition is declared. Carried alongside the recommendation as an
    annotation (DP#33), never used to replace the search."""
    accounts = cfg.get("portfolio", {}).get("accounts", {})
    equity = 0.0
    fixed_income = 0.0
    for acct in accounts.values():
        comp = acct.get("composition", {})
        equity += (comp.get("cdn_equity_pct", 0)
                   + comp.get("us_equity_pct", 0)
                   + comp.get("intl_equity_pct", 0))
        fixed_income += comp.get("fixed_income_pct", 0)
    total = equity + fixed_income
    if total <= 0:
        return None
    return {"equity_pct": equity / total, "fixed_income_pct": fixed_income / total}


def recommend_allocation(cfg: Dict[str, Any],
                         seed: int = MC_SEED_BASE,
                         return_beliefs: Optional[Dict[str, float]] = None) -> Optional[Dict[str, Any]]:
    """The risk-aware allocation recommendation for a household, or ``None`` when
    the household declares no ``portfolio.risk_tolerance`` (a strict no-op --
    the golden household, which declares none, gets nothing).

    Returns the recommended equity/fixed-income mix, its Monte Carlo risk
    metrics, the per-bucket product-category guidance, the mix applied to each
    portfolio account, and -- when the household declared a current composition
    -- that declared mix as an annotation (DP#33).

    DP#23: ``seed`` threads the reproducible RNG base to ``_mc_risk_metrics``.
    The default (``MC_SEED_BASE = 474``) preserves the historical behaviour
    byte-for-byte; ``seed=0`` is a valid, falsy seed and is honoured as-is.

    DP#21: ``return_beliefs`` threads the sleeve return beliefs (equity/fixed-
    income mean & sigma) into ``_mc_risk_metrics``. ``None`` (the default) reads
    ``cfg["assumptions"]["return_beliefs"]`` when the household declares one,
    else falls back to the module constants -- preserving the pre-#993 behaviour
    byte-for-byte. An explicit dict overrides the config; an explicit 0.0 for
    any belief is honoured (no ``x or DEFAULT``), DP#32. A user who disagrees
    with 6.8% equity can override via ``assumptions.return_beliefs`` (or the
    parameter) without editing source.
    """
    portfolio = cfg.get("portfolio", {})
    risk_tolerance = portfolio.get("risk_tolerance")
    # Explicit None-check (DP#32): absence is a no-op, not a coerced default.
    if risk_tolerance is None:
        return None
    horizon = _horizon(cfg)
    if horizon is None:
        return None

    beliefs = _resolve_return_beliefs(cfg, override=return_beliefs)
    mix = recommended_mix(risk_tolerance, horizon["years_to_retirement"])
    accounts = portfolio.get("accounts", {})
    return {
        "risk_tolerance": risk_tolerance,
        "years_to_retirement": horizon["years_to_retirement"],
        "recommended_mix": mix,
        "declared_mix": _declared_mix(cfg),
        "product_categories": _product_categories(mix),
        "risk_metrics": _mc_risk_metrics(mix, horizon["years_to_horizon"],
                                     seed=seed, return_beliefs=beliefs),
        "per_account": {kind: dict(mix) for kind in accounts},
    }


def format_allocation(rec: Dict[str, Any]) -> str:
    """A readable console block for the recommended mix, its risk metrics, and
    the per-bucket product-category guidance (acceptance: the recommendation is
    surfaced in output)."""
    mix = rec["recommended_mix"]
    metrics = rec["risk_metrics"]
    lines = ["  📈 RISK-AWARE ALLOCATION (equity/fixed-income mix)"]
    lines.append(f"     Risk tolerance: {rec['risk_tolerance']} "
                 f"({rec['years_to_retirement']} yrs to retirement)")
    lines.append(f"     Recommended mix: {mix['equity_pct']*100:.0f}% equity / "
                 f"{mix['fixed_income_pct']*100:.0f}% fixed income")
    declared = rec.get("declared_mix")
    if declared is not None:
        lines.append(f"     (currently declared: "
                     f"{declared['equity_pct']*100:.0f}% equity / "
                     f"{declared['fixed_income_pct']*100:.0f}% fixed income)")
    lines.append(f"     {metrics['horizon_years']}-yr terminal multiple  "
                 f"P10 {metrics['p10']:.2f}x  P50 {metrics['p50']:.2f}x  "
                 f"P90 {metrics['p90']:.2f}x  P(loss) "
                 f"{metrics['probability_of_loss']*100:.1f}%")
    for b in rec["product_categories"]:
        lines.append(f"     {b['pct']*100:5.0f}% {b['bucket']:<13} -> {b['category']}")
    return "\n".join(lines)
