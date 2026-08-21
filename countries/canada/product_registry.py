#!/usr/bin/env python3
"""Product / holding registry (issue #547).

Maps a named investment product to its tax characteristics: asset-class
composition, yield breakdown by income type, MER, foreign content /
withholding exposure, and a turnover/realization rate (how much of the
capital-gain return is realized each year vs deferred).

This is the data-model foundation that lets an account list holdings by
product (the engine then DERIVES that account's composition + yield) instead
of hand-entering abstract buckets. Abstract bucket inputs keep working
unchanged (see countries/canada/portfolio.py).

DP#2/#12: product data is configuration, not logic — a user-supplied
``products`` section overlays the built-in synthetic catalog.
DP#7/#15: products are modelled by category, never by brand or real ticker.

Reachability: this is a reporting-side catalogue (DP#7), unreachable from
the simulation fold BY DESIGN — it derives account composition for
reporting, not the year loop. Expected in the #710 reach guard's allowlist,
not dead code (#746).
The built-in catalog uses synthetic names; recommendations name a product
CATEGORY (advice-safe, jurisdiction-portable), not an individual security.

References:
    countries/canada/portfolio.py — CompositionBreakdown, YieldBreakdown
    DESIGN_PRINCIPLES.md DP#2, DP#7, DP#15, DP#27, DP#30
"""

from dataclasses import dataclass, field
from enum import Enum

from countries.canada.portfolio import CompositionBreakdown, YieldBreakdown


class ProductCategory(Enum):
    """Advice-safe, jurisdiction-portable product categories (DP#7).

    A category describes a KIND of product ("aggregate-bond ETF / GIC ladder")
    rather than a specific security. The optimizer may name a category for a
    recommended bucket; it never names a ticker.
    """
    GLOBAL_EQUITY_INDEX = "global_equity_index"
    CANADIAN_DIVIDEND = "canadian_dividend"
    AGGREGATE_BOND = "aggregate_bond"
    HIGH_INTEREST_SAVINGS = "high_interest_savings"

    @property
    def label(self) -> str:
        return {
            ProductCategory.GLOBAL_EQUITY_INDEX: "broad-market global equity index ETF",
            ProductCategory.CANADIAN_DIVIDEND: "Canadian eligible-dividend ETF",
            ProductCategory.AGGREGATE_BOND: "aggregate-bond ETF / GIC ladder",
            ProductCategory.HIGH_INTEREST_SAVINGS: "high-interest savings ETF / GIC",
        }[self]


@dataclass
class Product:
    """A named investment product and its tax characteristics.

    Composition percentages and per-income-type yields are decimals (e.g.
    ``us_equity_pct=0.6``, ``interest=0.04``). ``turnover`` is the fraction of
    the capital-gain return realized each year (the rest is deferred, DP#27).
    ``foreign_content`` and ``withholding_exposure`` describe foreign-withholding
    exposure for asset-location reasoning.
    """
    name: str
    category: ProductCategory
    cdn_equity_pct: float = 0.0
    us_equity_pct: float = 0.0
    intl_equity_pct: float = 0.0
    fixed_income_pct: float = 0.0
    eligible_dividends: float = 0.0
    non_eligible_dividends: float = 0.0
    interest: float = 0.0
    capital_gains: float = 0.0
    return_of_capital: float = 0.0
    foreign_income: float = 0.0
    mer: float = 0.0
    foreign_content: float = 0.0
    withholding_exposure: float = 0.0
    turnover: float = 1.0

    @property
    def composition(self) -> CompositionBreakdown:
        return CompositionBreakdown(
            cdn_equity_pct=self.cdn_equity_pct,
            us_equity_pct=self.us_equity_pct,
            intl_equity_pct=self.intl_equity_pct,
            fixed_income_pct=self.fixed_income_pct,
        )

    @property
    def yield_breakdown(self) -> YieldBreakdown:
        return YieldBreakdown(
            eligible_dividends=self.eligible_dividends,
            non_eligible_dividends=self.non_eligible_dividends,
            interest=self.interest,
            capital_gains=self.capital_gains,
            return_of_capital=self.return_of_capital,
            foreign_income=self.foreign_income,
        )

    @property
    def realized_capital_gains(self) -> float:
        return self.capital_gains * self.turnover

    @property
    def deferred_capital_gains(self) -> float:
        return self.capital_gains * (1 - self.turnover)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Product":
        category = data["category"]
        if not isinstance(category, ProductCategory):
            category = ProductCategory(category)
        return cls(
            name=name,
            category=category,
            cdn_equity_pct=data.get("cdn_equity_pct", 0.0),
            us_equity_pct=data.get("us_equity_pct", 0.0),
            intl_equity_pct=data.get("intl_equity_pct", 0.0),
            fixed_income_pct=data.get("fixed_income_pct", 0.0),
            eligible_dividends=data.get("eligible_dividends", 0.0),
            non_eligible_dividends=data.get("non_eligible_dividends", 0.0),
            interest=data.get("interest", 0.0),
            capital_gains=data.get("capital_gains", 0.0),
            return_of_capital=data.get("return_of_capital", 0.0),
            foreign_income=data.get("foreign_income", 0.0),
            mer=data.get("mer", 0.0),
            foreign_content=data.get("foreign_content", 0.0),
            withholding_exposure=data.get("withholding_exposure", 0.0),
            turnover=data.get("turnover", 1.0),
        )

    def to_dict(self) -> dict:
        """Re-emit the dict shape ``from_dict`` consumes (DP#24, issue #731).

        ``name`` is deliberately NOT included -- it is the registry key
        under which this product dict lives (see ``ProductRegistry.to_dict``),
        not a field ``Product.from_dict`` reads off ``data``. ``category`` is
        emitted as its enum value (the string form the contract wire format
        and ``from_dict`` both accept), so ``from_dict(name, to_dict())``
        reconstructs the same ``ProductCategory``. Every other field is
        re-emitted verbatim -- this is the exact inverse of ``from_dict``,
        so a config carrying product data round-trips instead of silently
        dropping its products on a load->modify->save cycle.
        """
        return {
            "category": self.category.value,
            "cdn_equity_pct": self.cdn_equity_pct,
            "us_equity_pct": self.us_equity_pct,
            "intl_equity_pct": self.intl_equity_pct,
            "fixed_income_pct": self.fixed_income_pct,
            "eligible_dividends": self.eligible_dividends,
            "non_eligible_dividends": self.non_eligible_dividends,
            "interest": self.interest,
            "capital_gains": self.capital_gains,
            "return_of_capital": self.return_of_capital,
            "foreign_income": self.foreign_income,
            "mer": self.mer,
            "foreign_content": self.foreign_content,
            "withholding_exposure": self.withholding_exposure,
            "turnover": self.turnover,
        }


@dataclass
class ProductRegistry:
    """Name -> Product lookup. Built-in synthetic catalog + user overlay (DP#2)."""
    products: dict[str, Product] = field(default_factory=dict)

    def get(self, name: str) -> Product:
        return self.products[name]

    def names(self) -> list[str]:
        return list(self.products)

    def __len__(self) -> int:
        return len(self.products)

    @classmethod
    def from_dict(cls, data: dict | None, *, include_builtin: bool = True) -> "ProductRegistry":
        products = dict(_BUILTIN_PRODUCTS) if include_builtin else {}
        for name, pdata in (data or {}).items():
            products[name] = Product.from_dict(name, pdata)
        return cls(products=products)

    def to_dict(self) -> dict:
        """Re-emit the dict shape ``from_dict`` consumes (DP#24, issue #731).

        Returns ``{name: product.to_dict()}`` for every product in this
        registry -- the exact shape ``from_dict(data)`` reads. A registry
        built WITH the built-in catalog re-emits the built-ins too, so
        ``from_dict(reg.to_dict(), include_builtin=True)`` reconstructs the
        same set (built-ins are overwritten with identical values, i.e.
        idempotent); a user-only registry (``include_builtin=False``)
        round-trips through ``from_dict(reg.to_dict(), include_builtin=False)``
        exactly. Before this, neither ``Product`` nor ``ProductRegistry`` had
        a ``to_dict``, so a config carrying product data silently dropped
        its products on any load->modify->save cycle.
        """
        return {name: product.to_dict() for name, product in self.products.items()}


# Synthetic catalog (DP#15: no real tickers). Round, illustrative figures.
_BUILTIN_PRODUCTS: dict[str, Product] = {
    p.name: p
    for p in [
        Product(
            name="Synthetic Global Equity Index",
            category=ProductCategory.GLOBAL_EQUITY_INDEX,
            cdn_equity_pct=0.25,
            us_equity_pct=0.45,
            intl_equity_pct=0.30,
            eligible_dividends=0.004,
            foreign_income=0.014,
            capital_gains=0.012,
            mer=0.002,
            foreign_content=0.75,
            withholding_exposure=0.011,
            turnover=0.05,
        ),
        Product(
            name="Synthetic Canadian Dividend",
            category=ProductCategory.CANADIAN_DIVIDEND,
            cdn_equity_pct=1.0,
            eligible_dividends=0.035,
            capital_gains=0.01,
            mer=0.0022,
            turnover=0.1,
        ),
        Product(
            name="Synthetic Aggregate Bond",
            category=ProductCategory.AGGREGATE_BOND,
            fixed_income_pct=1.0,
            interest=0.038,
            mer=0.001,
            turnover=0.2,
        ),
        Product(
            name="Synthetic High-Interest Savings",
            category=ProductCategory.HIGH_INTEREST_SAVINGS,
            fixed_income_pct=1.0,
            interest=0.045,
            mer=0.0015,
        ),
    ]
}


def builtin_registry() -> ProductRegistry:
    return ProductRegistry(products=dict(_BUILTIN_PRODUCTS))


# Each abstract bucket maps to the advice-safe product category that best
# fills it (DP#7). Used to name a category for a recommended bucket.
_BUCKET_CATEGORY = {
    "cdn_equity_pct": ProductCategory.CANADIAN_DIVIDEND,
    "us_equity_pct": ProductCategory.GLOBAL_EQUITY_INDEX,
    "intl_equity_pct": ProductCategory.GLOBAL_EQUITY_INDEX,
    "fixed_income_pct": ProductCategory.AGGREGATE_BOND,
}


def recommend_category_for_bucket(bucket: str) -> ProductCategory:
    """Name an advice-safe product category for a composition bucket."""
    return _BUCKET_CATEGORY[bucket]
