#!/usr/bin/env python3
"""Tests for Issue #719 (DP#9): remove the print_decision_report() no-op wrapper.

Per DP#9 (no backward compatibility -- no shims, no deprecation cycles),
``simulate.print_decision_report()`` was a legacy wrapper whose entire body was
``pass``. It had no live callers anywhere in the tree; it survived only as an
import and an ``__all__`` entry in the package ``__init__``. A no-op kept "for
compatibility" is exactly the dead shim DP#9 forbids.

These tests lock the removal:
- ``simulate`` no longer defines the ``print_decision_report`` attribute.
- The package no longer re-exports it (neither importable nor in ``__all__``).
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from root_package_loader import load_root_package


class TestPrintDecisionReportRemoved(unittest.TestCase):
    """The legacy no-op wrapper must be gone (DP#9)."""

    def test_simulate_has_no_print_decision_report(self):
        import simulate

        self.assertFalse(
            hasattr(simulate, "print_decision_report"),
            "simulate.print_decision_report() is a DP#9 no-op wrapper and must "
            "be removed; callers use the current report API directly.",
        )

    def test_package_does_not_reexport_print_decision_report(self):
        pkg = load_root_package()
        self.assertNotIn(
            "print_decision_report",
            getattr(pkg, "__all__", []),
            "print_decision_report must not appear in the package __all__.",
        )
        self.assertFalse(
            hasattr(pkg, "print_decision_report"),
            "print_decision_report must not be re-exported by the package.",
        )


if __name__ == "__main__":
    unittest.main()
