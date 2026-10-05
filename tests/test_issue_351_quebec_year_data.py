"""Issue #351: Quebec 2023/2024 left most program parameters at 0.0.

`QuebecTaxData.year_2023` and `year_2024` did not set the senior, HSF, RAMQ and
work-premium parameters that `year_2025`/`year_2026` carry. Every reader fell
back to the `TaxYearData` default of `0.0`, so the age/retirement credit, senior
assistance, the individual HSF and the drug premium all came out **$0 with no
error** -- a silent zero of exactly the kind DP#32 exists to prevent, and one
that no failing test could see because nothing raises.

## The values

All from Finances Quebec, *Parameters of the personal income tax system for
2024*, **Table 3** (official PDF), with the 2023 column alongside:

| Parameter | 2024 | 2023 |
|---|---|---|
| Age amount | 3,798 | 3,614 |
| Amount for a person living alone | 2,069 | 1,969 |
| Retirement income amount | 3,374 | 3,211 |
| Line-361 reduction threshold | 40,925 | 38,945 |
| Senior assistance threshold, single | 27,065 | 25,755 |
| Senior assistance threshold, couple | 44,015 | 41,885 |
| Senior assistance reduction rate | 5.31% | 5.16% |
| HSF first income bracket threshold | 17,630 | 16,780 |
| HSF second income bracket threshold | 61,315 | 58,350 |
| Work premium maximum, person living alone | 1,152.34 | 1,095.27 |
| Work premium maximum, couple | 1,797.07 | 1,709.61 |
| Work premium reduction threshold, one adult | 12,334 | 11,842 |
| Work premium reduction threshold, couple | 19,092 | 18,338 |

The line-361 reduction rate (18.75%) and the 14% conversion rate do not change
between years; the senior assistance maximum stays $2,000 per eligible person.
The RAMQ maximum annual premium rose from $731 to $744 on 1 July 2024, and
prorating the two halves gives **$737.50**, following the half-year convention
the 2025 value of 755 already uses.

**Also fixed:** `year_2024.basic_personal_amount` was 17,183, which is the 2023
figure. The official 2024 amount is **18,056**.

## What is NOT fixed, and is named here rather than left silent

The employer-side HSF rates (`qc_fss_employer_*`), the charitable-donation top
bracket (`qc_charitable_donation_rate_top` / `_top_threshold`), and
`qc_work_premium_growth_rate` / `_excluded_single` / `_excluded_couple` are
**still unset** for these years. They are not in Table 3 as quoted and I did not
source them, so they remain 0.0. They do not affect any figure the acceptance
test asserts, but a caller reading them still gets a silent zero -- the guard
below deliberately covers only the fields this issue sources, so it cannot be
read as clearing the rest.

**A second error this turned up:** `year_2023.basic_personal_amount` is
**15,980**, while the issue's own reasoning puts the 2023 value at 17,183. That
is out of this issue's scope (it flagged only 2024) and is not changed here, but
it looks like the same class of error one year earlier and should be checked.
"""
import pytest

from countries.canada.provinces.quebec import tax_data as qd
from countries.canada.provinces.quebec.quebec_credits import (
    quebec_age_amount_credit, quebec_drug_insurance_premium,
    quebec_health_services_fund_individual, quebec_senior_assistance_credit,
)


class TestTheAcceptanceFigures:
    """The issue's six assertions, exactly."""

    def test_age_amount_credit_2024(self):
        got = quebec_age_amount_credit(43518, 65, lives_alone=True,
                                       retirement_income=26000, year=2024)
        assert got == pytest.approx(1226, abs=1)

    def test_senior_assistance_credit_2024(self):
        got = quebec_senior_assistance_credit(60795, eligible_persons=2,
                                              is_couple=True, year=2024)
        assert got == pytest.approx(3109, abs=1)

    def test_health_services_fund_at_the_first_bracket(self):
        assert quebec_health_services_fund_individual(43518, year=2024) == 150

    def test_health_services_fund_above_the_second_bracket(self):
        got = quebec_health_services_fund_individual(88204, year=2024)
        assert got == pytest.approx(419, abs=1)

    def test_drug_insurance_premium_2024(self):
        assert quebec_drug_insurance_premium(year=2024) == pytest.approx(
            737.50, abs=0.5)

    def test_the_2024_basic_personal_amount(self):
        """Was 17,183 -- the 2023 value. See the issue's Table 3 note."""
        assert qd.QuebecTaxData.year_2024().basic_personal_amount == 18056


class TestThe2023Column:
    """The same calls agree with Table 3's 2023 column."""

    def test_the_2023_parameters_are_the_2023_column(self):
        y = qd.QuebecTaxData.year_2023()
        assert y.qc_age_amount == 3614
        assert y.qc_living_alone_amount == 1969
        assert y.qc_retirement_income_amount == 3211
        assert y.qc_age_credit_reduction_threshold == 38945
        assert y.qc_senior_assistance_threshold_single == 25755
        assert y.qc_senior_assistance_threshold_couple == 41885
        assert y.qc_senior_assistance_reduction_rate == pytest.approx(0.0516)
        assert y.qc_fss_individual_exemption == 16780
        assert y.qc_fss_individual_second_threshold == 58350
        assert y.qc_work_premium_max_single == pytest.approx(1095.27)
        assert y.qc_work_premium_max_couple == pytest.approx(1709.61)

    def test_the_2024_parameters_are_the_2024_column(self):
        y = qd.QuebecTaxData.year_2024()
        assert y.qc_age_amount == 3798
        assert y.qc_living_alone_amount == 2069
        assert y.qc_retirement_income_amount == 3374
        assert y.qc_age_credit_reduction_threshold == 40925
        assert y.qc_senior_assistance_threshold_single == 27065
        assert y.qc_senior_assistance_threshold_couple == 44015
        assert y.qc_senior_assistance_reduction_rate == pytest.approx(0.0531)
        assert y.qc_fss_individual_exemption == 17630
        assert y.qc_fss_individual_second_threshold == 61315
        assert y.qc_work_premium_max_single == pytest.approx(1152.34)
        assert y.qc_work_premium_max_couple == pytest.approx(1797.07)

    def test_the_year_invariant_rates_are_set_in_both_years(self):
        for year in (qd.QuebecTaxData.year_2023(), qd.QuebecTaxData.year_2024()):
            assert year.qc_age_credit_reduction_rate == pytest.approx(0.1875)
            assert year.qc_non_refundable_credit_rate == pytest.approx(0.14)


class TestNoLoadedQuebecYearSilentlyReturnsZero:
    """The guard the issue asks for: a year added later WITHOUT these fields
    must fail loudly here rather than quietly return $0 downstream."""

    #: Every field this issue sources. NOT the full parameter set -- see the
    #: module docstring for what is deliberately still unset.
    SOURCED = (
        'qc_age_amount', 'qc_living_alone_amount', 'qc_retirement_income_amount',
        'qc_age_credit_reduction_threshold', 'qc_age_credit_reduction_rate',
        'qc_non_refundable_credit_rate',
        'qc_senior_assistance_max_per_person',
        'qc_senior_assistance_threshold_single',
        'qc_senior_assistance_threshold_couple',
        'qc_senior_assistance_reduction_rate',
        'qc_fss_individual_exemption', 'qc_fss_individual_second_threshold',
        'qc_fss_individual_first_cap', 'qc_fss_individual_rate',
        'qc_fss_individual_max',
        'qc_work_premium_max_single', 'qc_work_premium_max_couple',
        'qc_work_premium_reduction_threshold_single',
        'qc_work_premium_reduction_threshold_couple',
        'qc_work_premium_reduction_rate',
        'qc_drug_insurance_max_premium',
        'basic_personal_amount',
    )

    def test_every_sourced_field_is_non_zero_in_every_loaded_year(self):
        missing = []
        for year in qd.QuebecTaxData.all_years():
            for field in self.SOURCED:
                if not getattr(year, field, 0):
                    missing.append(f'{year.year}.{field}')
        assert missing == [], (
            "A Quebec year is missing a parameter this issue sourced, so the "
            "program reading it returns a silent $0 (DP#32). Add the value from "
            "Finances Quebec's parameters table for that year: " + ', '.join(missing)
        )