"""Issue #371: the student-loan-interest credits (ITA s.118.62 federal; TP-1
line 385 Quebec) and the interest carry-forward both regimes use.

The gap this closes: the engine accepts ``liabilities[kind=student_loan]`` and
amortizes it (#763), but the interest it computes each year went into debt
service and nowhere else -- no federal credit, no Quebec credit. A recent
graduate repaying a government loan paid more tax in every projected repayment
year than the statute requires.

Three things are load-bearing and each is asserted rather than assumed:

* **The rate is year-versioned data, not a constant.** The issue's acceptance
  figures ($250 of 2024 interest -> $31.31 federal, $50.00 Quebec) come from the
  15% federal lowest rate that applied in 2024; the engine's own data says 15%
  for 2023-2024, 14.5% for 2025 and 14% for 2026, and the credit follows it. A
  hardcoded 15% would pass the acceptance test and be wrong in 2026.
* **A Quebec resident's federal credit is abated.** ITA s.120(2) is computed on
  the tax remaining AFTER the non-refundable credits, so every federal credit is
  worth 16.5% less to a Quebec resident. This engine models the abatement by
  scaling the federal bracket rates, so its tax is already abated and the credit
  must be scaled by (1 - abatement) to reproduce the statute against it.
* **Both regimes carry forward INTEREST, not credit dollars.** Federal: CRA line
  31900 allows the year and the five preceding years. Quebec: Revenu Quebec's
  line-385 page allows unused interest to be carried to future years with no
  expiring window. A single credit balance could not express that difference.

DP#15: every figure here is fabricated and round-numbered.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.student_loan_credit import (
    FEDERAL_CARRY_FORWARD_YEARS,
    consume_interest_fifo,
    eligible_interest,
    student_loan_interest_credit,
)
from tax_data import default_tax_provider

# The issue's acceptance figures, for a 2024 tax year.
INTEREST = 250.0
ACCEPTANCE_YEAR = 2024
FEDERAL_2024 = 31.3125      # 250 x 15% x (1 - 16.5%)
QUEBEC_2024 = 50.0          # 250 x 20%


class TestTheTwoCredits:
    def test_the_issue_acceptance_figures(self):
        """A Quebec resident's $250 of 2024 interest: $31.31 federal (abated)
        and $50.00 Quebec."""
        federal, quebec = student_loan_interest_credit(
            INTEREST, ACCEPTANCE_YEAR, default_tax_provider(), province='quebec')
        assert federal == pytest.approx(FEDERAL_2024)
        assert quebec == pytest.approx(QUEBEC_2024)

    def test_the_federal_rate_is_the_years_own_not_a_constant(self):
        """The federal lowest rate fell 15% -> 14.5% (2025) -> 14% (2026). A
        hardcoded 15% would pass the acceptance test above and overstate every
        later year."""
        provider = default_tax_provider()
        credits = {
            year: student_loan_interest_credit(
                INTEREST, year, provider, province='ontario')[0]
            for year in (2024, 2025, 2026)
        }
        assert credits[2024] == pytest.approx(INTEREST * 0.15)
        assert credits[2025] == pytest.approx(INTEREST * 0.145)
        assert credits[2026] == pytest.approx(INTEREST * 0.14)
        assert credits[2024] > credits[2025] > credits[2026]

    def test_a_quebec_resident_keeps_only_the_abated_federal_credit(self):
        """s.120(2) against this engine's model: the abatement is already in the
        bracket rates, so the credit is scaled by (1 - abatement)."""
        provider = default_tax_provider()
        qc = student_loan_interest_credit(INTEREST, ACCEPTANCE_YEAR, provider,
                                          province='quebec')[0]
        on = student_loan_interest_credit(INTEREST, ACCEPTANCE_YEAR, provider,
                                          province='ontario')[0]
        assert qc == pytest.approx(on * (1.0 - 0.165))
        assert qc < on

    def test_only_a_quebec_resident_gets_the_provincial_credit(self):
        provider = default_tax_provider()
        for province in ('ontario', 'alberta', None):
            _, quebec = student_loan_interest_credit(
                INTEREST, ACCEPTANCE_YEAR, provider, province=province)
            assert quebec == 0.0

    def test_no_interest_is_no_credit(self):
        provider = default_tax_provider()
        assert student_loan_interest_credit(0.0, 2026, provider,
                                            province='quebec') == (0.0, 0.0)
        assert student_loan_interest_credit(-100.0, 2026, provider,
                                            province='quebec') == (0.0, 0.0)

    def test_a_year_with_no_rate_data_yields_no_credit(self, monkeypatch):
        """DP#32: no rate data -> no credit, never a guessed 15%.

        The guard is pinned by making the data layer genuinely raise, because
        the real data layer does NOT raise for a year outside its tables -- it
        projects from the nearest one (DP#20), so an out-of-table year would
        otherwise silently return a projected rate and this branch would never
        be exercised. Reaching it here proves the branch is real, and the
        projection policy itself is the data layer's business, not this
        module's.
        """
        import countries.canada.student_loan_credit as mod
        import countries.canada.tax_calc as tax_calc

        def _boom(year, provider):
            raise ValueError("no data for that year")

        monkeypatch.setattr(tax_calc, '_load_fed_data', _boom)
        assert mod.student_loan_interest_credit(
            INTEREST, 2026, default_tax_provider(), province='quebec') == (0.0, 0.0)


class TestTheDataLayerEdges:
    """The two ways the Quebec data can be absent, and the default provider."""

    def test_the_provider_defaults_when_none_is_passed(self):
        """A caller that passes no provider gets the real one -- and the real
        one's 2024 rate is the issue's 15%."""
        federal, quebec = student_loan_interest_credit(
            INTEREST, ACCEPTANCE_YEAR, None, province='quebec')
        assert federal == pytest.approx(FEDERAL_2024)
        assert quebec == pytest.approx(QUEBEC_2024)

    def test_a_quebec_table_that_raises_yields_no_provincial_credit(self, monkeypatch):
        """The provider failing to load the QUEBEC table is not a reason to guess
        a 20% rate or a 16.5% abatement: both fall to 0.0, so the credit is the
        UN-abated federal one (DP#32 -- absence produces nothing, it is not
        filled in). Only the Quebec load is broken here; the federal table still
        answers, which is the case a real data gap would produce."""
        provider = default_tax_provider()
        real = provider._load_year

        def _quebec_only(year, country, province):
            if province == 'quebec':
                raise ValueError("no Quebec table for that year")
            return real(year, country, province)

        monkeypatch.setattr(provider, '_load_year', _quebec_only)
        federal, quebec = student_loan_interest_credit(
            INTEREST, ACCEPTANCE_YEAR, provider, province='quebec')
        assert quebec == 0.0
        assert federal == pytest.approx(INTEREST * 0.15)

    def test_a_missing_quebec_table_yields_no_provincial_credit(self, monkeypatch):
        """The same case through the OTHER absence shape: the loader returns
        nothing rather than raising. Both reach `_quebec_rates(None)`."""
        provider = default_tax_provider()
        real = provider._load_year

        def _quebec_only(year, country, province):
            if province == 'quebec':
                return None
            return real(year, country, province)

        monkeypatch.setattr(provider, '_load_year', _quebec_only)
        federal, quebec = student_loan_interest_credit(
            INTEREST, ACCEPTANCE_YEAR, provider, province='quebec')
        assert quebec == 0.0
        assert federal == pytest.approx(INTEREST * 0.15)


class TestTheInterestLedger:
    """The carry-forward is a schedule of INTEREST, per payment year."""

    def test_the_federal_window_is_the_year_and_the_five_before_it(self):
        ledger = {2019: 100.0, 2021: 100.0, 2026: 100.0}
        # 2026: the window is [2021, 2026] -- the 2019 interest is lost.
        assert eligible_interest(ledger, 2026, federal=True) == pytest.approx(200.0)
        # 2027: the window is [2022, 2027] -- now the 2021 interest is lost too.
        assert eligible_interest(ledger, 2027, federal=True) == pytest.approx(100.0)

    def test_the_quebec_window_never_closes(self):
        """Revenu Quebec's line-385 page carries unused interest to future
        years with no expiring window, so the same ledger stays whole."""
        ledger = {2019: 100.0, 2021: 100.0, 2026: 100.0}
        assert eligible_interest(ledger, 2027, federal=False) == pytest.approx(300.0)
        assert eligible_interest(ledger, 2040, federal=False) == pytest.approx(300.0)

    def test_the_window_boundary_is_inclusive(self):
        """CRA line 31900 says 'the year or any of the five preceding years', so
        the fifth year back is still eligible."""
        ledger = {2020: 50.0}
        assert FEDERAL_CARRY_FORWARD_YEARS == 5
        assert eligible_interest(ledger, 2025, federal=True) == pytest.approx(50.0)
        assert eligible_interest(ledger, 2026, federal=True) == 0.0

    def test_consumption_takes_the_oldest_interest_first(self):
        """FIFO, because the federal window expires the OLDEST interest first:
        spending it first is what keeps the claim as large as the statute allows."""
        ledger = {2024: 100.0, 2026: 50.0}
        assert consume_interest_fifo(ledger, 120.0) == {2026: 30.0}

    def test_consumption_never_goes_negative(self):
        ledger = {2024: 100.0}
        assert consume_interest_fifo(ledger, 250.0) == {}
        assert consume_interest_fifo(ledger, 0.0) == {2024: 100.0}
        assert consume_interest_fifo({}, 100.0) == {}


class TestTheAcceptanceScenarioEndToEnd:
    """The issue's acceptance test is a full-year amount: the engine must
    produce the two credits for the loan's owner. That is asserted through the
    fold in the engine test below; here the arithmetic the rule composes is
    pinned so a wiring change cannot quietly alter it."""

    def test_a_quebec_residents_full_year(self):
        federal, quebec = student_loan_interest_credit(
            INTEREST, ACCEPTANCE_YEAR, default_tax_provider(), province='quebec')
        assert federal + quebec == pytest.approx(81.3125)

    def test_a_non_qualifying_loan_produces_nothing(self):
        """The credit is on a QUALIFYING government loan only. The contract says
        so explicitly and refuses a student_loan that does not declare it; a
        caller that reaches the credit arithmetic with no qualifying interest has
        no interest to claim, which is $0 -- not a missing input."""
        assert student_loan_interest_credit(
            0.0, ACCEPTANCE_YEAR, default_tax_provider(), province='quebec') == (0.0, 0.0)
