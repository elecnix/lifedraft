"""#336: the Quebec senior assistance credit parameters for 2023 and 2024.

The refundable *crédit d'impôt pour le soutien aux aînés* (TP-1 line 463) pays
up to $2,000 per person aged 70 or over at year end, reduced by a rate on
family income above a threshold that depends on marital status.

The parameters were populated only for 2025 and 2026. For 2023 and 2024 the
fields defaulted to 0.0, so the helper computed nothing for those years --
a Quebec couple aged 70+ with modest income was shown receiving zero, for
every year, with no error anywhere.

Measured on `main` at c949015, for the CFFP condo case household (family
income $60,795, both spouses 73):

| year | before | now |
|---|---|---|
| 2023 | 0.0 | **3,024.24** |
| 2024 | 0.0 | **3,108.98** |

The CFFP's own figure for 2024 is **$3,108**:
``4,000 - 0.0531 x (60,795 - 44,015) = 3,108.98``.

Source: Ministère des Finances du Québec, *Paramètres du régime d'imposition
des particuliers pour l'année d'imposition YYYY*, tableau 3, "Montant pour le
soutien des aînés". The table's own footnote states the reduction rate is
*"revalorisé chaque année"* -- revalued annually, NOT indexed -- so it is
stated per year rather than derived from the threshold indexation.
"""
import pytest

from countries.canada.provinces.quebec import quebec_senior_assistance_credit

# Published thresholds and rates (see module docstring).
THRESHOLDS = {
    2023: dict(single=25_755, couple=41_885, rate=0.0516),
    2024: dict(single=27_065, couple=44_015, rate=0.0531),
    2025: dict(single=27_835, couple=45_270, rate=0.0540),
    2026: dict(single=28_405, couple=46_200, rate=0.0547),
}

CFFP_FAMILY_INCOME = 60_795.0


@pytest.mark.parametrize("year", [2023, 2024])
def test_the_2023_and_2024_parameters_are_no_longer_zero(year):
    """The headline: these years computed $0 before."""
    credit = quebec_senior_assistance_credit(
        CFFP_FAMILY_INCOME, eligible_persons=2, is_couple=True, year=year
    )
    assert credit > 0.0
    expected = 4_000 - THRESHOLDS[year]["rate"] * (
        CFFP_FAMILY_INCOME - THRESHOLDS[year]["couple"]
    )
    assert credit == pytest.approx(expected, abs=0.01)


def test_2024_reproduces_the_cffp_figure():
    """CFFP "Déménagement en copropriété" concludes $3,108 for this household."""
    credit = quebec_senior_assistance_credit(
        CFFP_FAMILY_INCOME, eligible_persons=2, is_couple=True, year=2024
    )
    assert credit == pytest.approx(3_108, abs=1.0)


@pytest.mark.parametrize("year,params", sorted(THRESHOLDS.items()))
def test_every_parameter_is_read_from_the_record(year, params):
    """The numbers are data, not literals in the arithmetic (DP#2/DP#20)."""
    from tax_data import TaxDataProvider

    record = TaxDataProvider()._load_year(year, "canada", "quebec")
    assert record.qc_senior_assistance_max_per_person == 2_000
    assert record.qc_senior_assistance_threshold_single == params["single"]
    assert record.qc_senior_assistance_threshold_couple == params["couple"]
    assert record.qc_senior_assistance_reduction_rate == pytest.approx(
        params["rate"], abs=1e-9
    )


@pytest.mark.parametrize("year", sorted(THRESHOLDS))
def test_income_below_the_applicable_threshold_is_the_full_maximum(year):
    """No reduction until family income passes the threshold that APPLIES.

    The threshold depends on marital status, so the comparison must use the
    single one when ``is_couple=False`` and the couple one when it is True --
    pairing the couple income with the single threshold (as I first wrote it)
    tests a reduction that is correctly applied.
    """
    params = THRESHOLDS[year]
    for income in (0.0, params["single"]):
        assert quebec_senior_assistance_credit(
            income, eligible_persons=1, is_couple=False, year=year
        ) == pytest.approx(2_000.0), (year, income, "single")
    for income in (0.0, params["couple"]):
        assert quebec_senior_assistance_credit(
            income, eligible_persons=2, is_couple=True, year=year
        ) == pytest.approx(4_000.0), (year, income, "couple")


def test_the_couple_threshold_is_used_when_a_spouse_is_eligible():
    """The same income pays less for a couple than for a single person."""
    year = 2024
    # Above the SINGLE threshold (27,065 in 2024) but below the couple's
    # (44,015), so the two paths must differ.
    income = 35_000.0
    single = quebec_senior_assistance_credit(
        income, eligible_persons=1, is_couple=False, year=year)
    couple = quebec_senior_assistance_credit(
        income, eligible_persons=2, is_couple=True, year=year)
    assert single < 2_000.0
    assert couple == pytest.approx(4_000.0)


def test_a_household_with_no_eligible_person_gets_nothing():
    """70 or over is the whole test, and it is answered before any lookup."""
    assert quebec_senior_assistance_credit(
        CFFP_FAMILY_INCOME, eligible_persons=0, year=2024) == 0.0