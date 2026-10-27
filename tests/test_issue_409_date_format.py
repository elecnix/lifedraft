"""Issue #409: ``$defs.date`` must assert a real date, not merely a string.

The date type carried ``{"type": "string", "format": "date"}``. There was no
``pattern``, and JSON Schema treats ``format`` as annotation-only unless a
format checker is supplied, so the field accepted *any* string -- including
``"not-a-date"``, ``"20260630"``, a zero-month, and impossible days such as
2026-02-30.

A missing input must fail loudly (DP#32). A date no rule can compute against is
exactly such an input, and admitting it at the one boundary meant to be strict is
what this closes.

Two halves are required and neither is sufficient alone:

- ``pattern`` rejects the malformed SHAPE: ``2026-7-1`` (unpadded),
  ``not-a-date``, ``20260630``, and a datetime string.
- ``FormatChecker`` rejects the well-formed but IMPOSSIBLE: ``2026-02-30``,
  ``2026-13-01``, ``2026-06-31``. ``pattern`` cannot express calendar validity;
  only a real date parse can.

These drive the real ``input_contract.validate_contract`` entry point rather than
constructing a validator in the test, so a regression in schema composition or
in the validator wiring is caught rather than assumed.
"""

import copy
import json
import pathlib

import pytest

import contract_schema
import contract_errors
import input_contract

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "schema" / "example.json"

# Values that must be refused, with why each one is unacceptable.
INVALID = [
    ("2026-7-1", "unpadded month/day is not RFC 3339 full-date"),
    ("2026-02-30", "February 30 does not exist"),
    ("2026-13-01", "there is no month 13"),
    ("2026-00-10", "there is no month 0"),
    ("2026-06-31", "June has 30 days"),
    ("not-a-date", "not a date at all"),
    ("20260630", "missing separators"),
    ("2026-06-30T00:00:00Z", "a datetime, which the field's description excludes"),
    ("", "empty string"),
]


def _example_document():
    with EXAMPLE.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def valid_document():
    """The shipped example, which must keep validating -- a fix that refuses
    everything is not a fix, and this pins the valid case in the same breath."""
    doc = _example_document()
    assert input_contract.validate_contract(copy.deepcopy(doc)) is None
    return doc


class TestDateTypeAssertsAShape:
    def test_valid_example_still_validates(self, valid_document):
        assert valid_document["as_of"] == "2026-07-12"

    @pytest.mark.parametrize("value,why", INVALID)
    def test_bad_as_of_is_refused_with_an_accurate_reason(self, valid_document, value, why):
        doc = copy.deepcopy(valid_document)
        doc["as_of"] = value
        with pytest.raises(contract_errors.ContractValidationError) as excinfo:
            input_contract.validate_contract(doc)
        # The message must name the offending field, so a user knows which input
        # to fix -- a bare "invalid document" is not a loud enough failure.
        assert "as_of" in str(excinfo.value)


class TestDateTypeAssertsCalendarValidity:
    """The half ``pattern`` cannot do.

    ``^\\d{4}-\\d{2}-\\d{2}$`` accepts 2026-02-30 happily: the shape is right
    and the date is not. These are the cases that prove a format checker is
    actually wired up rather than merely declared.
    """

    @pytest.mark.parametrize(
        "value", ["2026-02-30", "2026-13-01", "2026-06-31", "2025-02-29", "2100-02-29"]
    )
    def test_impossible_calendar_date_is_refused(self, valid_document, value):
        doc = copy.deepcopy(valid_document)
        doc["as_of"] = value
        with pytest.raises(contract_errors.ContractValidationError):
            input_contract.validate_contract(doc)

    def test_leap_day_in_a_leap_year_is_accepted(self, valid_document):
        """The converse, so a fix cannot simply reject anything past February."""
        doc = copy.deepcopy(valid_document)
        doc["as_of"] = "2024-02-29"
        assert input_contract.validate_contract(doc) is None


class TestTheDateTypeItselfIsWired:
    """Assert the two properties directly against the composed schema.

    If these hold but the document tests above did not, the problem is in schema
    composition; if the document tests fail while these pass, it is in the
    validator wiring. Naming which half is broken is the point.
    """

    def test_date_def_has_a_pattern(self):
        schema = contract_schema.compose_schema()
        date_def = schema["$defs"]["date"]
        assert "pattern" in date_def, "$defs.date still accepts any string's shape"

    def test_validator_is_built_with_a_format_checker(self):
        """``format`` is annotation-only without one, so ``2026-02-30`` passes."""
        validator = contract_schema.get_validator()
        assert validator.format_checker is not None, (
            "no FormatChecker: 'format: date' is being ignored entirely"
        )