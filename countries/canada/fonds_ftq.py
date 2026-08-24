#!/usr/bin/env python3
"""Fonds de solidarité FTQ — product rules encoded in the module, not the
input JSON (issue #826, DP#7/#10/#12).

WHY THIS MODULE EXISTS
----------------------
Issue #823 added GENERIC per-account ``expected_return`` / ``locked_until``
primitives -- fine for an arbitrary illiquid holding. It then modeled Fonds
FTQ by making the household re-state ``locked_until:{age:65}`` and
``expected_return:0.073`` on every FTQ account. That is the design error
#826 corrects: FTQ's rules are well-known and IDENTICAL for every holder
(DP#7 model the mechanism/product, DP#10 one module per program, DP#12
well-known government-program rules belong in a data/module layer, not
per-user input). This module encodes them; the input contract only flags
the account with ``product='fonds_ftq'`` and gives the balance.

WHAT THE MODULE OWNS (the FTQ program's well-known rules)
---------------------------------------------------------
- **Illiquidity**: RRSP-held Fonds FTQ shares are locked until the holder
  reaches age 65 (or a qualifying event -- retirement, death, disability,
  emigration, HBP/LLP withdrawal). This module encodes the age-65 unlock
  as the ``locked_until`` product default.
- **Expected return**: FTQ's published 10-year compound annual return (CAR)
  is the product's ``expected_return`` default. It is a DERIVED, SOURCED
  figure (see ``_FTQ_10Y_CAR`` below), not a bare magic number -- a
  maintainer updates one constant here when FTQ publishes a new CAR, and
  every FTQ holder picks it up (DP#12: real, sourced, centrally-maintained
  data; DP#20: year-versioned in principle, though the 10-yr CAR is a
  rolling figure updated annually).
- **LSIF tax credit**: the 30% federal+Quebec credit on up to $5,000/yr is
  ALREADY modeled in ``countries/canada/lsif_credit.py`` (ITA s.127.4 /
  TA s.1029.8.5). This module does NOT duplicate it -- it re-exports the
  eligibility/credit primitives so a caller resolving an FTQ account can
  reach them through the program's own module (DP#10).

RESOLUTION (DP#13: a declared value wins over a fallback)
---------------------------------------------------------
``contract_accounts._map_account_overrides`` calls ``resolve_product()`` for
an account whose ``product`` is set. The product supplies
``expected_return`` / ``locked_until`` DEFAULTS into the SAME #823
downstream machinery (the growth blend + solvency illiquidity). An EXPLICIT
``account.expected_return`` / ``account.locked_until`` on the same account
OVERRIDES the product default (DP#13) -- so a household that disagrees with
the module's 7.3% can declare its own rate and win, but a household that
just flags ``product='fonds_ftq'`` gets the module's rules with no
restatement.

References
----------
- fondsftq.com financial results (10-year CAR, share price)
- protegez-vous.ca (FTQ share redemption / holding-period rules)
- ITA s.127.4 / TA s.1029.8.5 (LSIF credit -- in lsif_credit.py)
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

# ── Sourced, centrally-maintained FTQ return data (DP#12/#20) ───────────────
#
# FTQ's published 10-year compound annual return (CAR), the figure a
# long-horizon projection should grow an FTQ holding at. It is DERIVED from
# FTQ's audited financial results, not a bare assumption:
#
#   FY2026 share price $70.07, an 8.6% return for the fiscal year
#   (fondsftq.com 2025-2026 financial results). The 10-year CAR of 7.3%
#   is the compounded annual return FTQ reports over the trailing decade --
#   the stable, below-broad-market figure that reflects FTQ's mandate to
#   invest in private illiquid Quebec SMEs (more stable than listed equity,
#   but below the ~8-10% broad-market return the global return_model defaults
#   to). With the LSIF 30% credit on $5,000/yr, the EFFECTIVE 10-year return
#   is materially higher (~13.4%), but the credit is a CASH flow modeled in
#   lsif_credit.py, not a return-rate bump here -- the holding itself grows
#   at the pre-credit CAR.
#
# A maintainer updates this ONE constant when FTQ publishes a new CAR; every
# account flagged product='fonds_ftq' picks it up without touching the input
# JSON (DP#12). The recent-year figures above document the basis so the
# update is a cited revision, not an opaque edit.
_FTQ_10Y_CAR = 0.073  # 7.3% -- FTQ published 10-year CAR (fondsftq.com)

# The age at which RRSP-held FTQ shares become redeemable (the standard
# unlock; qualifying events -- death, disability, emigration, HBP/LLP -- are
# out of scope for a deterministic projection's runway/solvency question,
# which is "can this balance be liquidated in a stress year BEFORE 65").
_FTQ_UNLOCK_AGE = 65


@dataclass(frozen=True)
class ProductRules:
    """The rules a product module supplies for an account flagged with that
    product (issue #826). Both fields are OPTIONAL -- a product may supply
    one, both, or neither (e.g. a future product might supply only a return,
    with no illiquidity). ``None`` means "the product does not opine on this
    field; use the account's own value or the global default" -- distinct
    from a product that affirmatively sets a value.
    """
    expected_return: Optional[float] = None
    locked_until: Optional[Dict[str, Any]] = None


def ftq_product_rules() -> ProductRules:
    """The Fonds FTQ product rules (issue #826): a 7.3% 10-year-CAR expected
    return and locked-until-age-65 illiquidity.

    Returns a fresh ``ProductRules`` each call (the values are immutable
    scalars/dicts, but a fresh dataclass keeps the product resolution path
    free of module-level mutable state a caller could accidentally mutate).
    """
    return ProductRules(
        expected_return=_FTQ_10Y_CAR,
        locked_until={"age": _FTQ_UNLOCK_AGE},
    )


# The closed map of product-id -> resolver. DP#16: a product's rules
# auto-include when the account carries the product flag -- there is no
# second registration step. Adding a new product module means adding one
# entry here.
_PRODUCTS = {
    "fonds_ftq": ftq_product_rules,
}


def resolve_product(product: Optional[str]) -> Optional[ProductRules]:
    """Resolve a product flag to its module-supplied rules (issue #826).

    Returns ``None`` when ``product`` is None/empty (a generic account with
    no product-module rules -- today's behaviour). Raises ``ValueError`` for
    an unknown product id -- a product flag the module does not know is a
    typo / a not-yet-implemented product, and silently treating it as
    "generic" would hide the flag the household deliberately set (DP#32).
    """
    if not product:
        return None
    resolver = _PRODUCTS.get(product)
    if resolver is None:
        raise ValueError(
            f"Unknown account product {product!r} (issue #826). Known "
            f"products: {sorted(_PRODUCTS)}. The product flag must resolve "
            f"to a registered product module (countries/canada/); an "
            f"unknown product is a typo or a not-yet-implemented product, "
            f"not a generic account (DP#32)."
        )
    return resolver()


# Re-export the LSIF credit primitives so a caller resolving an FTQ account
# can reach the credit machinery through the program's own module (DP#10:
# one module per program; the credit lives in lsif_credit.py, this module
# points to it rather than duplicating it).
from countries.canada.lsif_credit import (  # noqa: E402  (re-export after defs)
    FEDERAL_LSIF_RATE,
    LSIF_PURCHASE_MAX,
    QUEBEC_LSIF_RATE,
    LSIFCreditResult,
    LSIFPurchase,
    compute_lsif_credit,
    federal_lsif_rate,
    lsif_from_config,
)

__all__ = [
    "ProductRules",
    "ftq_product_rules",
    "resolve_product",
    "FTQ_10Y_CAR",
    "FTQ_UNLOCK_AGE",
    # LSIF re-exports (the credit is owned by lsif_credit.py; re-exported
    # here so the FTQ program has one entry point).
    "LSIFPurchase",
    "LSIFCreditResult",
    "compute_lsif_credit",
    "lsif_from_config",
    "federal_lsif_rate",
    "QUEBEC_LSIF_RATE",
    "FEDERAL_LSIF_RATE",
    "LSIF_PURCHASE_MAX",
]


# Aliases for the sourced constants (documented above) -- exported so a test
# or report can cite the figure the module actually used, not a restated
# magic number.
FTQ_10Y_CAR = _FTQ_10Y_CAR
FTQ_UNLOCK_AGE = _FTQ_UNLOCK_AGE
