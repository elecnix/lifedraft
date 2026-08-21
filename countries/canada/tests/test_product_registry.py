#!/usr/bin/env python3
"""Tests for the product/holding registry (issue #547).

Covers:
- Product: tax characteristics (asset class, yield by income type, MER,
  foreign content/withholding exposure, turnover/realization rate).
- ProductRegistry: load from data (DP#2), look up by name, built-in synthetic catalog.
- Deriving an account's composition + yield from holdings-by-product.
- Bucket-only inputs continue to work unchanged (regression).
- Product-CATEGORY recommendation per bucket (advice-safe, no tickers).
"""

import pytest

from countries.canada.portfolio import PortfolioConfig
from countries.canada.product_registry import (
    Product,
    ProductCategory,
    ProductRegistry,
    builtin_registry,
    recommend_category_for_bucket,
)

# =============================================================================
# Product
# =============================================================================

class TestProduct:
    def test_yield_breakdown_total(self):
        p = Product(
            name="Synthetic Global Equity Index",
            category=ProductCategory.GLOBAL_EQUITY_INDEX,
            cdn_equity_pct=0.3,
            us_equity_pct=0.5,
            intl_equity_pct=0.2,
            eligible_dividends=0.01,
            foreign_income=0.012,
            capital_gains=0.005,
            mer=0.002,
            foreign_content=0.7,
            turnover=0.05,
        )
        assert abs(p.composition.total_pct - 1.0) < 1e-9
        assert abs(p.yield_breakdown.total_yield - 0.027) < 1e-9

    def test_realization_split_uses_turnover(self):
        """Turnover governs how much capital-gain return is realized vs deferred."""
        p = Product(
            name="Synthetic Low-Turnover Equity",
            category=ProductCategory.GLOBAL_EQUITY_INDEX,
            cdn_equity_pct=1.0,
            capital_gains=0.04,
            turnover=0.1,
        )
        assert abs(p.realized_capital_gains - 0.004) < 1e-9
        assert abs(p.deferred_capital_gains - 0.036) < 1e-9


# =============================================================================
# ProductRegistry
# =============================================================================

class TestProductRegistry:
    def test_builtin_registry_uses_synthetic_names(self):
        """DP#15: no real tickers — built-in catalog uses synthetic names."""
        reg = builtin_registry()
        assert len(reg) > 0
        banned = {"vti", "xic", "vab", "zag", "xei", "vdy", "xaw", "vee", "xuu", "xre"}
        for name in reg.names():
            assert name.lower() not in banned

    def test_lookup_by_name(self):
        reg = builtin_registry()
        name = reg.names()[0]
        assert reg.get(name).name == name

    def test_unknown_product_raises(self):
        reg = builtin_registry()
        with pytest.raises(KeyError):
            reg.get("No Such Product")

    def test_from_dict_overlays_builtin(self):
        """User-supplied products (DP#2) extend/override the built-in catalog."""
        reg = ProductRegistry.from_dict({
            "Synthetic Custom Bond Fund": {
                "category": "aggregate_bond",
                "fixed_income_pct": 1.0,
                "interest": 0.035,
                "mer": 0.0009,
            }
        })
        p = reg.get("Synthetic Custom Bond Fund")
        assert p.category is ProductCategory.AGGREGATE_BOND
        assert abs(p.yield_breakdown.interest - 0.035) < 1e-9
        assert abs(p.composition.fixed_income_pct - 1.0) < 1e-9


# =============================================================================
# Deriving account composition + yield from holdings-by-product
# =============================================================================

class TestHoldingsByProduct:
    def _registry_dict(self):
        return {
            "Synthetic Global Equity Index": {
                "category": "global_equity_index",
                "us_equity_pct": 0.6,
                "intl_equity_pct": 0.4,
                "foreign_income": 0.02,
                "capital_gains": 0.01,
                "mer": 0.002,
            },
            "Synthetic Aggregate Bond": {
                "category": "aggregate_bond",
                "fixed_income_pct": 1.0,
                "interest": 0.04,
                "mer": 0.001,
            },
        }

    def test_composition_and_yield_derived(self):
        cfg = {
            "products": self._registry_dict(),
            "accounts": {
                "non_reg": {
                    "balance": 100000,
                    "holdings": [
                        {"product": "Synthetic Global Equity Index", "weight": 0.6},
                        {"product": "Synthetic Aggregate Bond", "weight": 0.4},
                    ],
                }
            },
        }
        portfolio = PortfolioConfig.from_dict(cfg)
        acct = portfolio.accounts["non_reg"]

        # Composition: 60% equity product (60% US / 40% intl) + 40% bond
        assert abs(acct.composition.us_equity_pct - 0.36) < 1e-9
        assert abs(acct.composition.intl_equity_pct - 0.24) < 1e-9
        assert abs(acct.composition.fixed_income_pct - 0.40) < 1e-9
        assert abs(acct.composition.total_pct - 1.0) < 1e-9

        # Yield: foreign 0.6*0.02, cap gains 0.6*0.01, interest 0.4*0.04
        assert abs(acct.yield_breakdown.foreign_income - 0.012) < 1e-9
        assert abs(acct.yield_breakdown.capital_gains - 0.006) < 1e-9
        assert abs(acct.yield_breakdown.interest - 0.016) < 1e-9

    def test_weights_default_to_equal(self):
        cfg = {
            "products": self._registry_dict(),
            "accounts": {
                "tfsa": {
                    "balance": 50000,
                    "holdings": [
                        {"product": "Synthetic Global Equity Index"},
                        {"product": "Synthetic Aggregate Bond"},
                    ],
                }
            },
        }
        acct = PortfolioConfig.from_dict(cfg).accounts["tfsa"]
        assert abs(acct.composition.fixed_income_pct - 0.5) < 1e-9
        assert abs(acct.composition.us_equity_pct - 0.3) < 1e-9

    def test_unknown_product_in_holding_raises(self):
        cfg = {
            "accounts": {
                "rrsp": {
                    "balance": 10000,
                    "holdings": [{"product": "Nonexistent Fund", "weight": 1.0}],
                }
            }
        }
        with pytest.raises(KeyError):
            PortfolioConfig.from_dict(cfg)


# =============================================================================
# Regression: bucket-only inputs continue to work unchanged
# =============================================================================

class TestBucketOnlyBackwardCompat:
    def test_bucket_only_unchanged(self):
        cfg = {
            "accounts": {
                "non_reg": {
                    "balance": 200000,
                    "cost_basis": 150000,
                    "composition": {
                        "cdn_equity_pct": 0.5,
                        "fixed_income_pct": 0.5,
                    },
                    "yield": {
                        "eligible_dividends": 0.02,
                        "interest": 0.03,
                    },
                }
            }
        }
        acct = PortfolioConfig.from_dict(cfg).accounts["non_reg"]
        assert acct.balance == 200000
        assert acct.cost_basis == 150000
        assert abs(acct.composition.cdn_equity_pct - 0.5) < 1e-9
        assert abs(acct.composition.fixed_income_pct - 0.5) < 1e-9
        assert abs(acct.yield_breakdown.eligible_dividends - 0.02) < 1e-9
        assert abs(acct.yield_breakdown.interest - 0.03) < 1e-9

    def test_empty_config_still_works(self):
        portfolio = PortfolioConfig.from_dict({})
        assert portfolio.total_balance == 0
        assert portfolio.has_data is False


# =============================================================================
# Output: product-category recommendation per bucket
# =============================================================================

class TestCategoryRecommendation:
    def test_each_bucket_maps_to_category(self):
        assert recommend_category_for_bucket("fixed_income_pct") is ProductCategory.AGGREGATE_BOND
        assert recommend_category_for_bucket("cdn_equity_pct") is ProductCategory.CANADIAN_DIVIDEND
        assert recommend_category_for_bucket("us_equity_pct") is ProductCategory.GLOBAL_EQUITY_INDEX
        assert recommend_category_for_bucket("intl_equity_pct") is ProductCategory.GLOBAL_EQUITY_INDEX

    def test_category_labels_are_advice_safe(self):
        """Categories name a kind of product, not a ticker (DP#7, DP#15)."""
        for cat in ProductCategory:
            label = cat.label
            assert label
            assert " " in label  # descriptive phrase, not a ticker symbol

    def test_unknown_bucket_raises(self):
        with pytest.raises(KeyError):
            recommend_category_for_bucket("crypto_pct")


# =============================================================================
# Round-trip: to_dict / from_dict (DP#24, issue #731)
# =============================================================================

class TestProductRoundTrip:
    """DP#24: Product.to_dict() is the inverse of Product.from_dict().

    A config carrying product data must round-trip -- before #731 neither
    Product nor ProductRegistry had a to_dict(), so products were silently
    dropped on any load->modify->save cycle (the same class of silent drop
    as #729's lira / #730's cash_out).
    """

    def _product_data(self):
        # Fabricated round numbers (DP#15), exercising several field kinds:
        # a composition pct, a yield-by-income-type figure, MER, foreign
        # exposure, and turnover (whose default is 1.0 -- re-emit must carry
        # a non-default through, not silently reset it).
        return {
            "category": "global_equity_index",
            "cdn_equity_pct": 0.3,
            "us_equity_pct": 0.5,
            "intl_equity_pct": 0.2,
            "eligible_dividends": 0.01,
            "foreign_income": 0.012,
            "capital_gains": 0.005,
            "mer": 0.002,
            "foreign_content": 0.7,
            "withholding_exposure": 0.011,
            "turnover": 0.05,
        }

    def test_product_to_dict_then_from_dict_preserves_every_field(self):
        p = Product.from_dict("Synthetic Round-Trip Equity", self._product_data())
        # General DP#24 assertion: the dict the household declared is what
        # comes back, key for key -- not a hardcoded single-field pin.
        assert Product.from_dict(p.name, p.to_dict()).to_dict() == p.to_dict()

    def test_category_round_trips_through_its_string_value(self):
        """to_dict emits the enum .value (the wire string); from_dict rebuilds
        the enum. The round-tripped Product has the same ProductCategory."""
        p = Product.from_dict("Synthetic Round-Trip Equity", self._product_data())
        rt = Product.from_dict(p.name, p.to_dict())
        assert rt.category is ProductCategory.GLOBAL_EQUITY_INDEX
        # And the emitted category is the string, not the enum repr.
        assert p.to_dict()["category"] == "global_equity_index"

    def test_turnover_default_is_not_silently_reset(self):
        """A non-default turnover (0.05) survives the round trip -- the
        re-emitter must carry it, not drop it back to from_dict's 1.0 default."""
        p = Product.from_dict("Synthetic Round-Trip Equity", self._product_data())
        rt = Product.from_dict(p.name, p.to_dict())
        assert abs(rt.turnover - 0.05) < 1e-12

    def test_to_dict_omits_name(self):
        """name is the registry KEY, not a field from_dict reads off data --
        so to_dict must not include it (it lives on ProductRegistry.to_dict)."""
        p = Product.from_dict("Synthetic Round-Trip Equity", self._product_data())
        assert "name" not in p.to_dict()


class TestProductRegistryRoundTrip:
    """DP#24: ProductRegistry.to_dict() is the inverse of from_dict()."""

    def _user_products(self):
        return {
            "Synthetic Custom Bond Fund": {
                "category": "aggregate_bond",
                "fixed_income_pct": 1.0,
                "interest": 0.035,
                "mer": 0.0009,
                "turnover": 0.25,
            },
            "Synthetic Custom Equity Fund": {
                "category": "canadian_dividend",
                "cdn_equity_pct": 1.0,
                "eligible_dividends": 0.03,
                "capital_gains": 0.01,
                "mer": 0.0022,
                "turnover": 0.1,
            },
        }

    def test_user_registry_round_trips_equal(self):
        """from_dict(to_dict(reg)) preserves every declared product (general
        dict-equality, not a hardcoded-value pin)."""
        reg = ProductRegistry.from_dict(self._user_products(), include_builtin=False)
        rt = ProductRegistry.from_dict(reg.to_dict(), include_builtin=False)
        assert rt.to_dict() == reg.to_dict()
        assert sorted(rt.names()) == sorted(self._user_products())

    def test_empty_registry_round_trips_to_empty(self):
        """Absence round-trips to absence -- no fabricated product (DP#32)."""
        empty = ProductRegistry.from_dict(None, include_builtin=False)
        assert empty.to_dict() == {}
        rt = ProductRegistry.from_dict(empty.to_dict(), include_builtin=False)
        assert rt.to_dict() == {}
        assert len(rt) == 0

    def test_registry_including_builtins_round_trips(self):
        """A registry built WITH the built-in catalog round-trips through
        from_dict(..., include_builtin=True) -- the built-ins are re-merged
        idempotently (overwritten with identical values)."""
        reg = ProductRegistry.from_dict(self._user_products(), include_builtin=True)
        rt = ProductRegistry.from_dict(reg.to_dict(), include_builtin=True)
        assert rt.to_dict() == reg.to_dict()
        # The user-declared products survive alongside the built-ins.
        assert "Synthetic Custom Bond Fund" in rt.names()
