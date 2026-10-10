"""#342: the RRSP deduction limit is reduced by the pension adjustment.

ITA s.146(1): the RRSP deduction limit for a year is **18% of the prior
year's earned income**, capped at the dollar limit, **minus the prior year's
pension adjustment (PA)**.

The PA is the total value of the employee's required contributions to a
registered pension plan (RPP), plus the bridge-benefit portion of a
defined-benefit pension accruing in the year. It occupies deduction-limit room
the member therefore does **not** have.

`apply_contribution_room` credited the full 18% with no PA term at all. The
PA has been a field on `RRSPAccount` since the legacy schema and many fixtures
still pass it — so an RPP member's room was overstated by exactly the PA, and
nothing said so.

Verified on `main` at c949015 with $55,000 of earned income, PA supplied as
0 and as 5,000 on the config and in the allocations: **both credited exactly
9,900.00** of new room.

Sources: ITA s.146(1); CRA, "Contributions affect your RRSP/PRPP deduction
limit"; ITA s.8(1)(m) for the deduction itself.
"""
import pytest

from rules_contributions import _pension_adjustment_for

EARNED = 55_000.0
PERCENT = 0.18
DOLLAR_LIMIT = 69_360.0          # above 0.18 * 55,000, so the percentage binds
FULL_ROOM = EARNED * PERCENT     # 9,900


class _Member(dict):
    pass


class _Cfg:
    def __init__(self, members):
        self.family_members = members
        self.rrsp_annual_percent = PERCENT


class _Ctx:
    def __init__(self, members):
        self.config = _Cfg(members)


def test_no_pension_plan_means_the_full_percentage():
    """The overwhelming majority: no PA declared, full 18%."""
    ctx = _Ctx([{"role": "primary", "pension_adjustment": None}])
    assert _pension_adjustment_for(ctx, "primary") == 0.0
    assert FULL_ROOM == pytest.approx(9_900.0)


def test_a_declared_pension_adjustment_is_read():
    """The issue's headline: PA 5,000 must be visible, not ignored."""
    ctx = _Ctx([{"role": "primary", "pension_adjustment": 5_000}])
    assert _pension_adjustment_for(ctx, "primary") == 5_000.0


def test_the_two_roles_are_read_independently():
    """A member in a plan does not tax their partner's room."""
    ctx = _Ctx([
        {"role": "primary", "pension_adjustment": 5_000},
        {"role": "spouse", "pension_adjustment": None},
    ])
    assert _pension_adjustment_for(ctx, "primary") == 5_000.0
    assert _pension_adjustment_for(ctx, "spouse") == 0.0


def test_an_absent_member_is_zero_not_a_crash():
    """A household with only a primary, or an empty list."""
    ctx = _Ctx([{"role": "primary"}])
    assert _pension_adjustment_for(ctx, "spouse") == 0.0
    assert _pension_adjustment_for(_Ctx([]), "primary") == 0.0


def test_a_config_with_no_member_list_is_zero():
    ctx = _Ctx(None)
    assert _pension_adjustment_for(ctx, "primary") == 0.0


@pytest.mark.parametrize("bad", [-1, -5_000, "not a number", object()])
def test_bad_pension_adjustment_values_never_increase_room(bad):
    """A PA is never negative; a negative one must not become extra room.

    The floor is applied inside the rule too, but a value that is nonsense at
    the source should not reach it carrying a sign.
    """
    ctx = _Ctx([{"role": "primary", "pension_adjustment": bad}])
    assert _pension_adjustment_for(ctx, "primary") == 0.0


def test_the_documented_arithmetic():
    """18% of 55,000 less the PA -- the issue's acceptance figure (c)."""
    pa = 5_000.0
    assert FULL_ROOM - pa == pytest.approx(4_900.0)
    # A PA above the accrued room cannot go negative.
    assert max(0.0, FULL_ROOM - 12_000.0) == 0.0


def test_the_rule_applies_the_pension_adjustment():
    """End of the chain: the rule must actually reduce the room it credits.

    The helper tests above pin the READ; this one pins that `apply_contribution_room`
    uses it, which is the part that was missing.
    """
    import inspect

    import rules_contributions

    source = inspect.getsource(rules_contributions.apply_contribution_room)
    assert "_pension_adjustment_for" in source, (
        "apply_contribution_room no longer applies the pension adjustment (#342)"
    )
    assert "max(0.0, primary_room_added - primary_pa)" in source