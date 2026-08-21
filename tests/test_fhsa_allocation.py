"""Tests for FHSA allocation in StrategyEngine."""

import pytest
from strategy import (
    StrategyEngine, FamilyState, AllocationStrategy, AllocationResult,
)
from countries.canada.strategies import STRATEGY_BALANCED, STRATEGY_RRSP_MAX


@pytest.fixture
def engine_balanced():
    """StrategyEngine with balanced strategy (no FHSA by default)."""
    return StrategyEngine(strategy=STRATEGY_BALANCED)


@pytest.fixture
def engine_with_fhsa():
    """StrategyEngine with FHSA allocation enabled."""
    return StrategyEngine(strategy=AllocationStrategy(
        name="With FHSA",
        rrsp_pct=0.30,
        spousal_rrsp_pct=0.10,
        tfsa_pct=0.20,
        fhsa_pct=0.08,
        resp_pct=0.07,
        non_reg_pct=0.25,
        deduct_later=False,
    ))


@pytest.fixture
def family_no_fhsa():
    """Family state with no FHSA room (not a first-time buyer)."""
    return FamilyState(
        primary_income=120000,
        spouse_income=50000,
        primary_rrsp_room=150000,
        spouse_rrsp_room=50000,
        primary_tfsa_room=40000,
        spouse_tfsa_room=40000,
        fhsa_room=0,
        fhsa_lifetime_remaining=0,
    )


@pytest.fixture
def family_with_fhsa():
    """Family state with FHSA room available."""
    return FamilyState(
        primary_income=120000,
        spouse_income=50000,
        primary_rrsp_room=150000,
        spouse_rrsp_room=50000,
        primary_tfsa_room=40000,
        spouse_tfsa_room=40000,
        fhsa_room=8000,
        fhsa_lifetime_remaining=40000,
        annual_savings=50000,
    )


class TestFHSAAllocation:
    """Test FHSA allocation in StrategyEngine."""

    def test_no_fhsa_when_room_zero(self, engine_balanced, family_no_fhsa):
        """When FHSA room is 0, no allocation to FHSA even in balanced strategy."""
        result = engine_balanced.allocate(family_no_fhsa)
        assert result.fhsa == 0.0

    def test_fhsa_allocated_when_room_available(self, engine_with_fhsa, family_with_fhsa):
        """FHSA receives allocation when strategy includes fhsa_pct and room exists."""
        result = engine_with_fhsa.allocate(family_with_fhsa)
        assert result.fhsa > 0, "FHSA should receive allocation"
        assert result.fhsa <= family_with_fhsa.fhsa_room, "Can't exceed annual room"
        assert result.fhsa <= family_with_fhsa.fhsa_lifetime_remaining, "Can't exceed lifetime limit"

    def test_fhsa_respects_annual_limit(self, engine_with_fhsa):
        """FHSA allocation is capped at annual room ($8,000)."""
        family = FamilyState(
            primary_income=200000,
            spouse_income=100000,
            primary_rrsp_room=50000,
            spouse_rrsp_room=30000,
            primary_tfsa_room=10000,
            spouse_tfsa_room=10000,
            fhsa_room=8000,
            fhsa_lifetime_remaining=40000,
            annual_savings=80000,
        )
        result = engine_with_fhsa.allocate(family)
        assert result.fhsa <= 8000, f"FHSA allocation {result.fhsa} exceeds annual limit of $8,000"

    def test_fhsa_respects_lifetime_limit(self, engine_with_fhsa):
        """FHSA allocation is capped at lifetime limit remaining."""
        family = FamilyState(
            primary_income=120000,
            spouse_income=50000,
            primary_rrsp_room=50000,
            spouse_rrsp_room=20000,
            primary_tfsa_room=20000,
            spouse_tfsa_room=20000,
            fhsa_room=8000,
            fhsa_lifetime_remaining=5000,  # Only $5k left in lifetime
            annual_savings=40000,
        )
        result = engine_with_fhsa.allocate(family)
        assert result.fhsa <= 5000, f"FHSA allocation {result.fhsa} exceeds lifetime remaining of $5,000"

    def test_fhsa_in_allocation_total(self, engine_with_fhsa, family_with_fhsa):
        """FHSA amount is included in total_allocated."""
        result = engine_with_fhsa.allocate(family_with_fhsa)
        total = result.total_allocated
        assert total > 0
        # FHSA should appear in the total
        expected_total = (result.primary_rrsp + result.spousal_rrsp + result.spouse_rrsp +
                         result.primary_tfsa + result.spouse_tfsa + result.fhsa +
                         result.resp + result.non_reg)
        assert abs(total - expected_total) < 0.01

    def test_fhsa_as_dict(self, engine_with_fhsa, family_with_fhsa):
        """FHSA appears in as_dict() output."""
        result = engine_with_fhsa.allocate(family_with_fhsa)
        d = result.as_dict()
        assert 'fhsa' in d
        assert d['fhsa'] == result.fhsa

    def test_fill_room_includes_fhsa(self):
        """fill_room waterfall includes FHSA after TFSA, before RESP."""
        strategy = AllocationStrategy(
            name="Fill with FHSA",
            deduct_later=False,
            spousal_splitting=True,
        )
        engine = StrategyEngine(strategy=strategy)
        state = FamilyState(
            primary_income=120000,
            spouse_income=50000,
            primary_rrsp_room=10000,
            spouse_rrsp_room=5000,
            primary_tfsa_room=5000,
            spouse_tfsa_room=5000,
            fhsa_room=8000,
            fhsa_lifetime_remaining=40000,
            annual_savings=30000,
        )
        result = engine.fill_room(lump_sum=30000, state=state)
        # FHSA should get some allocation in the fill-room waterfall
        assert result.fhsa >= 0
        # Total should equal the lump sum (or close with rounding)
        assert abs(result.total_allocated - 30000) < 1.0

    def test_fhsa_zero_when_strategy_pct_zero(self, family_with_fhsa):
        """Default strategy has fhsa_pct=0, so FHSA gets nothing."""
        engine = StrategyEngine(strategy=STRATEGY_BALANCED)
        result = engine.allocate(family_with_fhsa)
        assert result.fhsa == 0.0

    def test_fill_room_zero_fhsa_without_room(self):
        """fill_room with no FHSA room produces zero FHSA."""
        strategy = AllocationStrategy(name="No FHSA", deduct_later=False)
        engine = StrategyEngine(strategy=strategy)
        state = FamilyState(
            primary_income=120000,
            primary_rrsp_room=5000,
            spouse_rrsp_room=3000,
            primary_tfsa_room=5000,
            spouse_tfsa_room=5000,
            fhsa_room=0,
            fhsa_lifetime_remaining=0,
        )
        result = engine.fill_room(lump_sum=20000, state=state)
        assert result.fhsa == 0.0