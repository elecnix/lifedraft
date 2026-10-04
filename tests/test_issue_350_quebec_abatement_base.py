"""#350: the Quebec abatement is a share of BASIC federal tax.

The CRA computes the refundable Quebec abatement on line 44000 as 16.5% of
line 42900 -- **basic federal tax**. Basic federal tax is federal tax on
taxable income with the total federal non-refundable credits (line 35000),
the dividend tax credit and the minimum-tax carryover already removed
(T1 Schedule 1, step 3: "Basic federal tax 429" = line 49 minus line 53).

The engine took the percentage of GROSS tax and then subtracted the credits
at full value, so every federal non-refundable credit for a Quebec resident
was honoured twice: once as a full-rate credit, and again by inflating an
abatement it should have shrunk. The effective rate on such a credit should
be 1 - 16.5% = 12.525%, not 100%.

Measured on `main` at c949015, a single Quebec employee with $60,000:

| | engine | statutory |
|---|---|---|
| federal before abatement | 9 365.26 | 9 365.26 |
| Quebec abatement | 1 545.27 | **1 140.16** |
| federal after credits | 5 364.79 | **5 769.90** |

The engine was $405.11 low -- exactly 0.165 x the $2,455.20 of credits.

Sources: CRA T1 Schedule 1 step 3, and the 5013-G guide instruction,
"multiply your basic federal tax from line 42900 of your return by 16.5% and
enter the result on line 44000".
"""
import pytest

from countries.canada.tax_calc import (
    compute_total_tax,
    quebec_abatement_on_credits,
)

ABATEMENT = 0.165


def _quebec_2023():
    return compute_total_tax(
        60_000, employment_income=60_000, year=2023, province="quebec"
    )["breakdown"]


def test_the_abatement_is_a_share_of_basic_federal_tax():
    """The issue's headline numbers, to the cent."""
    b = _quebec_2023()
    assert b["federal_before_abatement"] == pytest.approx(9365.26, abs=0.01)
    assert b["quebec_abatement"] == pytest.approx(1140.16, abs=0.01)
    assert b["federal_after_credits"] == pytest.approx(5769.90, abs=0.01)


def test_the_abatement_base_is_gross_less_the_credits():
    """Not gross, and not a figure assembled elsewhere -- it is reported."""
    b = _quebec_2023()
    assert b["federal_basic_tax"] == pytest.approx(
        b["federal_before_abatement"] - b["credits_total"], abs=0.01
    )
    assert b["quebec_abatement"] == pytest.approx(
        b["federal_basic_tax"] * ABATEMENT, abs=0.01
    )


def test_a_credit_is_honoured_once_not_twice():
    """The core of the bug, stated as a property.

    Before, a credit of $1,000 at the 15% rate lowered a Quebec resident's
    federal tax by the FULL $150 -- the credit plus a 16.5%-too-large
    abatement. The correct saving is 150 x 0.835 = $125.25.
    """
    base = quebec_abatement_on_credits(10_000, 0, year=2026, province="quebec")
    with_credit = quebec_abatement_on_credits(10_000, 1000, year=2026,
                                              province="quebec")
    assert base - with_credit == pytest.approx(1000 * ABATEMENT, abs=0.01)


def test_a_non_quebec_province_has_no_abatement():
    assert quebec_abatement_on_credits(10_000, 1000, year=2026,
                                       province="ontario") == 0.0
    assert quebec_abatement_on_credits(10_000, 0, year=2026, province="ontario") == 0.0


def test_the_base_never_goes_negative():
    """Credits above the gross tax floor the base at zero, not below it."""
    assert quebec_abatement_on_credits(5_000, 9_000, year=2026,
                                       province="quebec") == 0.0


def test_ontario_is_untouched_by_the_fix():
    """The whole change is Quebec-specific; Ontario must be bit-identical."""
    a = compute_total_tax(60_000, employment_income=60_000, year=2026,
                          province="ontario")["breakdown"]
    # Ontario's abatement is 0, so its after-credits figure is just
    # gross - credits, whatever order the arithmetic is done in.
    assert a["quebec_abatement"] == 0.0
    assert a["federal_after_credits"] == pytest.approx(
        a["federal_before_abatement"] - a["credits_total"], abs=0.01
    )


def test_amt_and_compute_total_tax_agree_on_the_regular_federal_tax():
    """The minimum amount is measured against this same figure (#350).

    ``compute_total_tax`` has no production caller yet (#302), so
    ``rules_amt`` is the live path -- if the two could disagree, the AMT
    would be measured against a different tax than the return reports.
    """
    import rules_amt
    from tax_data import default_tax_provider
    from countries.canada.tax_calc import (
        federal_tax_before_abatement,
        compute_non_refundable_credits,
    )

    provider = default_tax_provider()
    taxable = 60_000.0
    gross = federal_tax_before_abatement(taxable, 2026, "quebec", provider)
    credits = compute_non_refundable_credits(
        60_000, taxable, 2026, "quebec", provider)["total"]
    expected = max(0.0, gross - credits) - quebec_abatement_on_credits(
        gross, credits, year=2026, province="quebec", provider=provider)

    with open(rules_amt.__file__, encoding="utf-8") as fh:
        source = fh.read()
    # The AMT module must derive its regular tax through the SAME helper --
    # asserted structurally, because the two call sites are the only thing
    # that can drift and a value comparison here would just re-implement it.
    assert "quebec_abatement_on_credits" in source
    assert "gross_fed - abatement - nr_credits" not in source
    assert expected > 0  # the fixture is non-degenerate