"""Issue #361: the QPP's claiming-age rules are not the CPP's.

**Deferral runs to 72, not 70.** Retraite Quebec: "It will increase by 0.7% for
each month that has passed since you turned 65, up to a maximum of 58.8% for a
pension that begins when you turn 72." The engine clamped every claim at 70, so
a Quebec member deferring to 72 was paid the age-70 factor -- understated by
about $2,016/year for life on a $1,000/month estimate.

**The early reduction is not flat.** Retraite Quebec: "the adjustment factor
varies between -0.5% and -0.6% per month of anticipation, in proportion to the
level of the base plan pension" -- so a claim at 60 costs 30% to 36%, where the
engine charged a flat 36% to everyone.

Both figures are quoted in the issue from the primary sources and cited in
``countries/canada/claim_age``; neither is taken from memory (per #273).

**What must not change:** every non-Quebec household, including the golden
invariant. ``plan`` defaults to ``'cpp'`` at every layer, and the CPP path
reproduces the previous arithmetic exactly.
"""
import pytest

from countries.canada.claim_age import (
    CPP_MAX_AGE, QPP_MAX_AGE, claiming_age_factor, max_claim_age,
    qpp_early_monthly_rate,
)
from countries.canada.retirement_transition import cpp_from_estimate


class TestTheSourcedFormula:
    def test_a_small_pension_gets_the_floor_rate(self):
        assert qpp_early_monthly_rate(0.0, 1_000.0) == pytest.approx(0.005)

    def test_the_maximum_pension_gets_the_ceiling_rate(self):
        assert qpp_early_monthly_rate(1_000.0, 1_000.0) == pytest.approx(0.006)

    def test_an_intermediate_pension_interpolates(self):
        """Half the maximum -> the midpoint of 0.5% and 0.6%, the linear
        reading of 'in proportion to the level of the base plan pension'."""
        assert qpp_early_monthly_rate(500.0, 1_000.0) == pytest.approx(0.0055)

    def test_a_pension_above_the_maximum_is_capped_not_extrapolated(self):
        assert qpp_early_monthly_rate(2_000.0, 1_000.0) == pytest.approx(0.006)

    def test_a_missing_maximum_is_refused_not_assumed(self):
        """DP#32: the maximum comes from the year-versioned table, and its
        ABSENCE must not silently apply the top rate to every pension."""
        for missing in (None, 0.0, -1.0):
            with pytest.raises(ValueError):
                qpp_early_monthly_rate(1_000.0, missing)


class TestTheAcceptanceFigures:
    def test_a_quebec_claim_at_72_pays_the_full_588_percent(self):
        """12 x 1,000 x (1 + 84 x 0.007) = 19,056."""
        got = cpp_from_estimate(1_000, start_age=72, claim_age=72,
                                plan='qpp', max_pension_at_65=1_500.0)
        assert got == pytest.approx(19_056.0)
        assert got == pytest.approx(12 * 1_000 * 1.588)

    def test_nothing_flows_before_the_claim(self):
        assert cpp_from_estimate(1_000, start_age=72, claim_age=71,
                                 plan='qpp', max_pension_at_65=1_500.0) == 0.0

    def test_a_maximum_pension_claimed_at_60_loses_36_percent(self):
        got = cpp_from_estimate(1_000, start_age=60, claim_age=60,
                                plan='qpp', max_pension_at_65=1_000.0)
        assert got == pytest.approx(12_000 * 0.64)

    def test_an_intermediate_pension_claimed_at_60_matches_the_formula(self):
        """The proportional rule, end to end through the real function.

        ``pension_at_65`` is the ANNUAL figure (12 x the monthly estimate), so a
        $1,000/month estimate is $12,000/year. Against a $100,000/year maximum
        that is a ratio of 0.12, giving 0.5% + 0.12 x 0.1% = 0.512%/month, and
        a 60-month reduction of 30.72% -- NOT a flat 36%.

        (An earlier draft of this test asserted ~30% by choosing inputs whose
        ratio was 0.12 rather than near 0; the code was right and the test's
        premise was wrong, which is the same mistake as the income-shock
        finding on #450. The floor itself is asserted on the rate function
        above, where it can be stated exactly.)
        """
        got = cpp_from_estimate(1_000, start_age=60, claim_age=60,
                                plan='qpp', max_pension_at_65=100_000.0)
        rate = 0.005 + 0.12 * 0.001
        assert got == pytest.approx(12_000 * (1 - 60 * rate))
        assert got == pytest.approx(12_000 * 0.6928)
        # And it is measurably better than the flat CPP treatment.
        cpp = cpp_from_estimate(1_000, start_age=60, claim_age=60)
        assert got > cpp


class TestTheCppPathIsUnchanged:
    """The golden invariant depends on this, asserted directly."""

    @pytest.mark.parametrize('age', [60, 62, 65, 66, 70])
    def test_cpp_matches_the_flat_rules_at_every_age(self, age):
        got = cpp_from_estimate(1_000, start_age=age, claim_age=age)
        annual = 12_000
        if age < 65:
            expected = annual * (1 - (65 - age) * 12 * 0.006)
        elif age > 65:
            expected = annual * (1 + (age - 65) * 12 * 0.007)
        else:
            expected = annual
        assert got == pytest.approx(expected)

    def test_cpp_still_clamps_at_70(self):
        """A CPP claim at 72 is refused at load, so the arithmetic must not
        invent a factor beyond the plan's own window."""
        assert cpp_from_estimate(1_000, start_age=72, claim_age=72) == \
            pytest.approx(cpp_from_estimate(1_000, start_age=70, claim_age=70))

    def test_cpp_ignores_the_max_pension_argument(self):
        assert cpp_from_estimate(1_000, start_age=60, claim_age=60,
                                 max_pension_at_65=1_000.0) == \
            cpp_from_estimate(1_000, start_age=60, claim_age=60)


class TestThePlanWindow:
    def test_the_two_plans_have_different_latest_ages(self):
        assert CPP_MAX_AGE == 70
        assert QPP_MAX_AGE == 72
        assert max_claim_age('cpp') == 70
        assert max_claim_age('qpp') == 72

    def test_the_qpp_reaches_588_percent_at_72(self):
        assert claiming_age_factor(72, 'qpp', 1_000.0, 1_000.0) == \
            pytest.approx(1.588)


class TestTheContractRefusesAnImpossibleClaim:
    """A non-Quebec claim at 71 or 72 is refused at LOAD, not clamped."""

    def _doc(self, province, claim_age):
        return {
            'as_of': '2024-01-01',
            'jurisdiction': {'country': 'canada', 'province': province},
            'decisions': {'retirement_age': [{'person': 'p1',
                                              'candidate_ages': [65]}]},
            'family': {},
            'people': [{
                'id': 'p1', 'birth_date': '1960-01-01',
                'room': {'rrsp': None, 'tfsa': None, 'fhsa': None},
                'incomes': [], 'relationships': [],
                'entitlements': {'cpp': {'estimated_monthly_at_65': 1000,
                                         'as_of': '2024-01-01',
                                         'claim_age': claim_age}},
            }],
        }

    def test_a_quebec_claim_at_72_is_accepted(self):
        from contract_people import _map_member
        m = _map_member(self._doc('quebec', 72), 'p1', 'primary', {})
        assert m['cpp_start_age'] == 72

    def test_an_ontario_claim_at_72_is_refused_loudly(self):
        from contract_errors import ContractAdaptationError
        from contract_people import _map_member
        with pytest.raises(ContractAdaptationError):
            _map_member(self._doc('ontario', 72), 'p1', 'primary', {})

    def test_an_ontario_claim_at_70_is_still_fine(self):
        from contract_people import _map_member
        m = _map_member(self._doc('ontario', 70), 'p1', 'primary', {})
        assert m['cpp_start_age'] == 70


class TestTheOptimizerCanReach72:
    def test_the_quebec_sweep_reaches_72(self):
        from countries.canada.claiming_age_optimizer import (
            optimize_claiming_ages,
        )
        res = optimize_claiming_ages(cpp_monthly_at_65=1_000, year=2026,
                                     cpp_start_range=(60, 72), plan='qpp')
        ages = {s['cpp_start_age'] for s in res.all_scenarios}
        assert 72 in ages and 71 in ages

    def test_the_cpp_sweep_stops_at_70(self):
        from countries.canada.claiming_age_optimizer import (
            optimize_claiming_ages,
        )
        res = optimize_claiming_ages(cpp_monthly_at_65=1_000, year=2026,
                                     cpp_start_range=(60, 72))
        assert max(s['cpp_start_age'] for s in res.all_scenarios) == 70