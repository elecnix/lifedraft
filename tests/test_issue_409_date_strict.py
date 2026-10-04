"""``$defs.date`` must reject anything the engine cannot compute against.

Issue #409. ``format`` is annotation-only in JSON Schema unless a format
checker is explicitly supplied, so the date type accepted ``"not-a-date"``,
``"2026-7-1"`` and ``"2026-02-30"`` alike. A date that no age- or
day-granularity rule can be evaluated against is precisely the kind of
input this repo treats as a loud failure rather than a default, and it was
being admitted at the one boundary meant to be strict.

The nine cases below are the issue's own table, measured on ``main`` at
``c949015`` where every one of them was ACCEPTED.
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contract_schema import validate_contract  # noqa: E402
import contract_schema  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO_ROOT, "schema", "example.json")


def _example() -> dict:
    with open(EXAMPLE, encoding="utf-8") as handle:
        return json.load(handle)


class DateIsStrictAtTheBoundary(unittest.TestCase):
    """A well-formed, real date is accepted."""

    def test_the_shipped_example_still_validates(self):
        """The fix must not reject a document the repo ships as valid."""
        validate_contract(_example())

    def test_a_real_iso_date_is_accepted(self):
        doc = _example()
        doc["as_of"] = "2026-06-30"
        validate_contract(doc)


class DateRejectsWhatNoRuleCanUse(unittest.TestCase):
    """Every case here was ACCEPTED on main. Each is now refused."""

    # (value, why it is unusable)
    UNUSABLE = [
        ("2026-7-1", "not zero-padded, so it sorts wrongly as a string"),
        ("2026-02-30", "February has no 30th"),
        ("2026-13-01", "month 13"),
        ("2026-00-10", "month 0"),
        ("2026-06-31", "June has no 31st"),
        ("not-a-date", "a bare word"),
        ("20260630", "no separators"),
        ("2026-06-30T00:00:00Z", "a datetime, which the field excludes"),
    ]

    def test_each_unusable_date_is_refused(self):
        for value, why in self.UNUSABLE:
            with self.subTest(as_of=value):
                doc = _example()
                doc["as_of"] = value
                with self.assertRaises(Exception, msg=f"{value} was accepted ({why})"):
                    validate_contract(doc)

    def test_every_calendar_impossible_date_in_2026_is_refused(self):
        """Sweep the whole year rather than trusting the sampled cases.

        ``pattern`` alone cannot express this — it accepts 2026-02-30 — so
        the sweep is the evidence that the FormatChecker is actually wired
        in and not merely documented.
        """
        import datetime

        accepted_impossible = []
        for month in range(1, 13):
            # Walk down from 31 to the last day that actually exists; the day
            # after it is always impossible, and always still a valid SHAPE
            # for the pattern (two digits, valid month), so only a real
            # calendar parse can reject it.
            day = 31
            while True:
                try:
                    datetime.date(2026, month, day)
                    break
                except ValueError:
                    day -= 1
            impossible = f"2026-{month:02d}-{day + 1:02d}"
            doc = _example()
            doc["as_of"] = impossible
            try:
                validate_contract(doc)
                accepted_impossible.append(impossible)
            except Exception:
                pass
        self.assertEqual(
            accepted_impossible, [],
            "a calendar-impossible date was accepted; these are the days "
            "after the last real day of each month, so only a calendar parse "
            "can reject them",
        )


class TheGuardWouldNoticeIfItCameBack(unittest.TestCase):
    """Detector, not just a fix."""

    def test_the_validator_is_built_with_a_format_checker(self):
        """If the FormatChecker is dropped, this goes red.

        A behavioural test alone proves the cases are refused *today*; it
        does not say which mechanism does it, so a later edit could satisfy
        the table by widening the pattern and silently lose calendar
        validation. This asserts the mechanism.
        """
        validator = contract_schema.get_validator()
        self.assertIsNotNone(
            getattr(validator, "format_checker", None),
            "the validator was built without a format_checker, so 'format' "
            "is annotation-only again (#409)",
        )

    def test_the_date_definition_carries_the_padding_pattern(self):
        schema = contract_schema.get_validator().schema
        date_def = schema["$defs"]["date"]
        self.assertIn("pattern", date_def)
        self.assertEqual(date_def["format"], "date")


if __name__ == "__main__":
    unittest.main()
