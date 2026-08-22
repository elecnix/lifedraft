#!/usr/bin/env python3
"""Tests for issue #86: DP#10 -- one module per government program.

cpp_basic_exemption_pensionable computes CPP/QPP pensionable earnings after
the basic exemption (CPP Act s.20 -- the Year's Basic Exemption). It is CPP
program logic and previously lived in tax_calc.py (the tax-calculation
convenience module), one more fragment of CPP logic sprawled across
retirement.py / cpp_estimator.py / cpp_sharing.py / tax_calc.py.

Issue #86 relocates it to the consolidated CPP/QPP module (cpp_sharing).
tax_calc.py may CALL it but must not OWN it; this test pins that:
  1. the function's defining module is cpp_sharing (its true home);
  2. it is re-exported from countries.canada (the package facade) and from
     tax_calc (an import-compatibility re-export, DP#9: one spelling, not a
     second definition);
  3. the moved function computes identically (same result whether reached
     through cpp_sharing or the tax_calc re-export -- DP#9).
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCppBasicExemptionLivesInCppModule:
    def test_function_module_is_cpp_sharing(self):
        """DP#10: the CPP Act s.20 basic-exemption logic lives in the
        consolidated CPP/QPP module, its defining home."""
        from countries.canada.cpp_sharing import cpp_basic_exemption_pensionable
        mod = inspect.getmodule(cpp_basic_exemption_pensionable).__name__
        assert mod == 'countries.canada.cpp_sharing'

    def test_tax_calc_reexports_not_defines(self):
        """tax_calc may re-export it for import compatibility, but its
        defining module is cpp_sharing (DP#9: one spelling). The re-export
        must be the SAME function object, not a duplicate implementation."""
        from countries.canada.tax_calc import cpp_basic_exemption_pensionable as tc_fn
        from countries.canada.cpp_sharing import cpp_basic_exemption_pensionable as cs_fn
        assert tc_fn is cs_fn

    def test_package_facade_exposes_it(self):
        from countries.canada import cpp_basic_exemption_pensionable
        from countries.canada.cpp_sharing import cpp_basic_exemption_pensionable as cs_fn
        assert cpp_basic_exemption_pensionable is cs_fn

    def test_moved_function_computes_identically(self):
        """Behaviour is unchanged by the move (DP#9): the same inputs give the
        same result through the cpp_sharing home and the tax_calc re-export."""
        from countries.canada.cpp_sharing import cpp_basic_exemption_pensionable as cs_fn
        from countries.canada.tax_calc import cpp_basic_exemption_pensionable as tc_fn
        for income in (0, 30_000, 50_000, 120_000):
            for province in ('quebec', 'ontario'):
                a = cs_fn(income, year=2026, province=province)
                b = tc_fn(income, year=2026, province=province)
                assert a == b
                assert a['pensionable_earnings'] <= income
                # The basic exemption still applies: pensionable never exceeds
                # income and is floored at 0.
                assert a['pensionable_earnings'] >= 0