#!/usr/bin/env python3
"""Tests for issue #656: an unrecognised debt purpose silently defaulted to
DEDUCTIBLE.

``CanadaAdapter.create_debt_purpose()`` mapped an unknown purpose string to
``DebtPurpose.INVESTMENT`` -- the one value whose interest is tax-deductible
under ITA s.20(1)(c). ``DebtPurpose`` exists to separate:

- ``INVESTMENT`` -> deductible (s.20(1)(c))
- ``RRSP_CONTRIBUTION`` / ``TFSA_CONTRIBUTION`` -> expressly PROHIBITED by
  ITA s.18(11)
- ``PERSONAL`` -> not deductible

A typo, a renamed key, or a new purpose added upstream and never registered
in ``_PURPOSE_MAP`` used to silently convert non-deductible borrowing into
deductible borrowing -- manufacturing a tax deduction that does not exist,
the exact error a tracing module is built to prevent. There is no
defensible default (the "safe" direction, PERSONAL, would just as silently
destroy a legitimate deduction) -- absence must fail loudly instead (DP#32).

DP#15: all data below is fabricated, round, role-based (DP#4) -- not that
any of it is personal data to begin with; this module has no financial
figures.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.adapter import CanadaAdapter, _PURPOSE_MAP
from countries.canada.debt import DebtPurpose


class UnknownPurposeRaisesTest(unittest.TestCase):
    """The core #656 fix: an unrecognised purpose fails loudly, it does not
    silently become the one deductible value."""

    def test_unknown_purpose_raises(self):
        with self.assertRaises(KeyError):
            CanadaAdapter.create_debt_purpose(None, "not-a-real-purpose")

    def test_unknown_purpose_error_names_the_bad_value(self):
        try:
            CanadaAdapter.create_debt_purpose(None, "not-a-real-purpose")
            self.fail("expected KeyError")
        except KeyError as e:
            msg = str(e)
            self.assertIn("not-a-real-purpose", msg)

    def test_unknown_purpose_error_lists_valid_purposes(self):
        try:
            CanadaAdapter.create_debt_purpose(None, "not-a-real-purpose")
            self.fail("expected KeyError")
        except KeyError as e:
            msg = str(e)
            for valid in _PURPOSE_MAP:
                self.assertIn(valid, msg)

    def test_a_typo_of_a_valid_purpose_still_raises(self):
        """A near-miss (e.g. a trailing space, a case change) must not
        coincidentally resolve to anything -- it is not the string CRA
        tracing recognises."""
        with self.assertRaises(KeyError):
            CanadaAdapter.create_debt_purpose(None, "Investment")  # wrong case

    def test_empty_string_raises(self):
        with self.assertRaises(KeyError):
            CanadaAdapter.create_debt_purpose(None, "")

    def test_none_raises(self):
        with self.assertRaises(KeyError):
            CanadaAdapter.create_debt_purpose(None, None)


class KnownPurposesStillResolveTest(unittest.TestCase):
    """Every legitimate purpose string still maps to its own DebtPurpose --
    the loud-failure fix must not collaterally break real input."""

    def test_investment_purpose_is_deductible(self):
        self.assertEqual(
            CanadaAdapter.create_debt_purpose(None, "investment"),
            DebtPurpose.INVESTMENT,
        )

    def test_every_debt_purpose_value_round_trips(self):
        """Every DebtPurpose member's OWN .value string maps back to that
        same member -- the map is generated from the enum, so this cannot
        drift (see PurposeMapCannotDriftTest for the structural guarantee)."""
        for purpose in DebtPurpose:
            self.assertEqual(
                CanadaAdapter.create_debt_purpose(None, purpose.value),
                purpose,
            )


class PurposeMapCannotDriftTest(unittest.TestCase):
    """#656's second enforcement ask: the map cannot silently fall out of
    sync with DebtPurpose -- e.g. a new member added to the enum (a new
    CRA-recognised borrowing purpose) without ever being registered here,
    which would make that purpose string unrecognised and (pre-#656) would
    have silently defaulted it to deductible."""

    def test_purpose_map_keys_equal_debt_purpose_values(self):
        self.assertEqual(
            set(_PURPOSE_MAP),
            {p.value for p in DebtPurpose},
        )

    def test_purpose_map_values_equal_debt_purpose_members(self):
        self.assertEqual(
            set(_PURPOSE_MAP.values()),
            set(DebtPurpose),
        )

    def test_purpose_map_has_no_extra_or_missing_entries(self):
        """Belt-and-suspenders on the two assertions above: same
        cardinality too, so a hypothetical (key collision -> value lost)
        bug in a hand-edited map would also be caught."""
        self.assertEqual(len(_PURPOSE_MAP), len(list(DebtPurpose)))


if __name__ == "__main__":
    unittest.main()
