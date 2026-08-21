#!/usr/bin/env python3
"""Tests for issue #88: is_quebec_resident stored as boolean in RESPChild —
should be computed from province per year (DP#28).

DP#28: Eligibility is a date-computed gate; programs enter and exit on a schedule.
DP#16: Modules auto-include when trigger data is present. Province='quebec'
automatically enables QESI.
"""

import pytest
from countries.canada.resp_rules import RESPChild, RESPCalculator


class TestRESPChildProvince:
    """Test that province field determines Quebec residency (DP#16, DP#28)."""

    def test_default_province_is_quebec(self):
        """Default province should be 'quebec' for backward compat."""
        child = RESPChild(name="test", birth_year=2010)
        assert child.province == 'quebec'

    def test_quebec_province_eligible_for_qesi(self):
        """Child in Quebec province should be QESI-eligible."""
        child = RESPChild(name="test", birth_year=2010, province='quebec')
        assert child.is_quebec_resident_in(2026) is True

    def test_ontario_province_not_eligible_for_qesi(self):
        """Child in Ontario province should not be QESI-eligible."""
        child = RESPChild(name="test", birth_year=2010, province='ontario')
        assert child.is_quebec_resident_in(2026) is False

    def test_bc_province_not_eligible_for_qesi(self):
        """Child in BC province should not be QESI-eligible."""
        child = RESPChild(name="test", birth_year=2010, province='bc')
        assert child.is_quebec_resident_in(2026) is False

    def test_qc_abbreviation_works(self):
        """QC abbreviation should be recognized as Quebec."""
        child = RESPChild(name="test", birth_year=2010, province='QC')
        assert child.is_quebec_resident_in(2026) is True

    def test_case_insensitive_province(self):
        """Province matching should be case-insensitive."""
        child = RESPChild(name="test", birth_year=2010, province='Quebec')
        assert child.is_quebec_resident_in(2026) is True

    def test_is_quebec_resident_false_sets_ontario(self):
        """RESPChild with province='ontario' should have is_quebec_resident_in return False."""
        child = RESPChild(name="test", birth_year=2012, province='ontario')
        assert child.is_quebec_resident_in(2026) is False

    def test_is_quebec_resident_true_from_province(self):
        """RESPChild with province='quebec' should have is_quebec_resident_in return True."""
        child = RESPChild(name="test", birth_year=2012, province='quebec')
        assert child.is_quebec_resident_in(2026) is True

    def test_province_overrides_is_quebec_resident(self):
        """Explicit province='ontario' should override is_quebec_resident default."""
        child = RESPChild(name="test", birth_year=2012, province='ontario')
        # is_quebec_resident defaults to True, but province='ontario' takes precedence
        assert child.is_quebec_resident_in(2026) is False

    def test_province_field_stored(self):
        """Province field should be stored on the RESPChild object."""
        child = RESPChild(name="test", birth_year=2010, province='ontario')
        assert child.province == 'ontario'


class TestQESIEligibilityFromProvince:
    """Test that QESI eligibility is computed from province (DP#16, DP#28)."""

    def setup_method(self):
        self.calc = RESPCalculator()

    def test_quebec_child_gets_qesi(self):
        """Quebec resident child should receive QESI."""
        child = RESPChild(name="child_a", birth_year=2017, province='quebec')
        result = self.calc.calculate_qesi(2500, child, 2026, 150000)
        assert result['total_qesi'] > 0
        assert result['eligible'] is True

    def test_ontario_child_no_qesi(self):
        """Ontario resident child should NOT receive QESI."""
        child = RESPChild(name="child_b", birth_year=2019, province='ontario')
        result = self.calc.calculate_qesi(2500, child, 2026, 150000)
        assert result['total_qesi'] == 0
        assert result['eligible'] is False
        assert 'Not a Quebec resident' in result.get('reason', '')

    def test_default_child_gets_qesi(self):
        """Default child (province='quebec') should receive QESI."""
        child = RESPChild(name="test", birth_year=2010)
        result = self.calc.calculate_qesi(2500, child, 2026, 150000)
        assert result['total_qesi'] > 0

    def test_backward_compat_no_qesi_when_flag_false(self):
        """is_quebec_resident=False with default province should not get QESI."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=False)
        result = self.calc.calculate_qesi(2500, child, 2026, 150000)
        assert result['total_qesi'] == 0
        assert result['eligible'] is False


class TestAdapterCreateRespChild:
    """Test that CanadaAdapter.create_resp_child uses province parameter."""

    def test_adapter_creates_with_province(self):
        """create_resp_child should accept province parameter."""
        from countries.canada.adapter import CanadaAdapter
        from simulation_config import SimulationConfig

        config = SimulationConfig()
        adapter = CanadaAdapter(config)
        child = adapter.create_resp_child(
            name="child_a", birth_year=2017, province='ontario'
        )
        assert child.province == 'ontario'
        assert child.is_quebec_resident_in(2026) is False

    def test_adapter_default_province_quebec(self):
        """Default province should be 'quebec'."""
        from countries.canada.adapter import CanadaAdapter
        from simulation_config import SimulationConfig

        config = SimulationConfig()
        adapter = CanadaAdapter(config)
        child = adapter.create_resp_child(name="Test", birth_year=2010)
        assert child.province == 'quebec'
        assert child.is_quebec_resident_in(2026) is True

