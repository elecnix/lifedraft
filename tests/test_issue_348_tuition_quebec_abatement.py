"""#348: a Quebec resident's FEDERAL tuition credit is abated.

The Quebec abatement is computed by the CRA on line 42900 -- basic federal
tax, which is *after* the s.118-to-s.118.9 non-refundable credits have been
subtracted (ITA s.120(4), "tax otherwise payable under this Part"). A federal
credit therefore lowers basic federal tax, which shrinks the 16.5% abatement
by 16.5% of the credit. The engine abated the TAX but credited the full 15%,
so a Quebec resident's tuition credit was overstated by exactly that much.

Measured on `main` at c949015, where the Quebec federal portion equalled
Ontario's exactly -- no abatement applied at all:

| tuition | main (QC) | correct | main (ON) |
|---------|-----------|---------|-----------|
| 400     | 92.00     | 82.10   | 60.00     |
| 2 500   | 575.00    | 513.13  | 375.00    |
| 4 000   | 920.00    | 821.00  | 600.00    |
| 9 650   | 2 219.50  | 1 980.66| 1 447.50  |

Cross-checked against the CFFP (Université de Sherbrooke) 2024 worked
examples, which publish $400 -> $50 federal ("400 $ x 12.525 %"),
$4 000 -> $501, and $9 650 -> $1 208.
"""
import pytest

from countries.canada.tax_calc import tuition_tax_credit

# (tuition, expected combined Quebec credit) — from the issue's acceptance test
# and the CFFP tables.
QUEBEC_CASES = [
    (400, 82.10),
    (2500, 513.13),
    (4000, 821.00),
    (9650, 1980.66),
]


@pytest.mark.parametrize("tuition,expected", QUEBEC_CASES)
def test_a_quebec_residents_credit_is_abated(tuition, expected):
    assert tuition_tax_credit(tuition, 2024, province="quebec") == pytest.approx(
        expected, abs=0.02
    )


@pytest.mark.parametrize("tuition", [t for t, _ in QUEBEC_CASES])
def test_a_non_quebec_resident_is_unchanged(tuition):
    """Every other province has an abatement of 0, so nothing may move."""
    assert tuition_tax_credit(tuition, 2024, province="ontario") == pytest.approx(
        tuition * 0.15
    )


def test_the_quebec_federal_portion_alone_matches_the_published_figure():
    """The CFFP publishes the FEDERAL component on its own: $4,000 -> $501."""
    assert tuition_tax_credit(
        4000, 2024, province="quebec", federal_only=True
    ) == pytest.approx(501.00, abs=0.01)


def test_federal_only_omits_the_provincial_credit_but_keeps_the_abatement():
    """`federal_only` is the transferable shape: abated federal, no TP-1 8%.

    A transfer is federal by statute, so the provincial credit never travels --
    but the supporter's province still sets the rate that federal portion is
    worth, which is the whole point of #348.
    """
    full = tuition_tax_credit(4000, 2024, province="quebec")
    federal = tuition_tax_credit(4000, 2024, province="quebec", federal_only=True)
    # 4000 x 0.08 = 320 is the provincial credit that must NOT be included.
    assert full - federal == pytest.approx(320.00, abs=0.01)
    # ...and the federal part is the abated rate, not the face rate.
    assert federal < 4000 * 0.15


def test_the_transfer_cap_is_abated_for_a_quebec_supporter():
    """$5,000 of tuition transfers $626.25 of federal credit, not $750."""
    from rules_tuition_credit import _FEDERAL_TUITION_TRANSFER_LIMIT

    cap_quebec = tuition_tax_credit(
        _FEDERAL_TUITION_TRANSFER_LIMIT, 2024,
        province="quebec", federal_only=True,
    )
    cap_elsewhere = tuition_tax_credit(
        _FEDERAL_TUITION_TRANSFER_LIMIT, 2024,
        province="ontario", federal_only=True,
    )
    assert cap_quebec == pytest.approx(626.25, abs=0.01)
    assert cap_elsewhere == pytest.approx(750.00, abs=0.01)


def test_the_abatement_is_read_from_data_not_hardcoded():
    """The rate must come from the Quebec record, so it tracks the year.

    Hardcoding 0.165 would pass every case above and then be wrong the day
    the province changes its rate, with nothing to notice.
    """
    from tax_data import TaxDataProvider

    provider = TaxDataProvider()
    record = provider._load_year(2024, "canada", "quebec")
    assert record.provincial_abatement == pytest.approx(0.165, abs=0.0001)

    expected = 4000 * 0.15 * (1 - record.provincial_abatement) + 4000 * 0.08
    assert tuition_tax_credit(
        4000, 2024, provider=provider, province="quebec"
    ) == pytest.approx(expected, abs=0.01)


def test_zero_tuition_is_still_zero():
    assert tuition_tax_credit(0, 2024, province="quebec") == 0.0
    assert tuition_tax_credit(None, 2024, province="quebec") == 0.0
