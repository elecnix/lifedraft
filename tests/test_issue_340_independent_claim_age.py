"""#340: stopping work and starting benefits are separate events.

The engine used one switch, `retirement_age`, to do BOTH: it stopped all
employment income AND started every retirement benefit. The law separates
them. CPP and QPP can be claimed from **60** whether or not the person is
still working; OAS starts at **65** (or later if deferred) with no work test.

So a member who declared `cpp_start_age = 60` with `retirement_age = 65`
received nothing until 65. The engine already knew the amount — it simply
withheld it, which made claiming early while working unrepresentable.

Verified on `main` at c949015:

    member: birth_year 1966, retirement_age 65, cpp_start_age 60,
            cpp_monthly_estimated 1000, oas_start_age 65

    2027 (age 61) -> cpp 0.0,  oas 0.0     <- withheld despite a 60 claim
    2031 (age 65) -> cpp 7680.0, oas 8800.0 <- the correct 36% early reduction

7680 is 12,000 x (1 - 60 x 0.006), so the figure was always right; only the
timing was wrong.

Sources: ESDC, "When to start your CPP retirement pension"; Retraite Québec,
"Working while receiving a retirement pension".
"""
from countries.canada.retirement_transition import member_retirement_income

OAS_MAX = 8_800.0
OAS_THRESHOLD = 93_454.0


def test_cpp_can_be_claimed_at_60_while_still_working():
    """The issue's acceptance case (1): CPP alongside the salary at 61."""
    member = {"birth_year": 1966, "retirement_age": 65, "cpp_start_age": 60,
              "cpp_monthly_estimated": 1000, "oas_start_age": 65}
    r = member_retirement_income(member, 2027, OAS_MAX, OAS_THRESHOLD, 0.0)
    assert r.retired is False, "the member has NOT stopped working"
    # 12,000 x (1 - 60 months x 0.006) = 7,680 -- the early-claim reduction.
    assert r.cpp == 7680.0
    assert r.oas == 0.0, "OAS is not payable before 65"


def test_oas_can_be_claimed_at_65_before_retirement_age():
    """The issue's acceptance case (2): OAS at 65 with retirement_age 68."""
    member = {"birth_year": 1960, "retirement_age": 68, "oas_start_age": 65}
    before = member_retirement_income(member, 2024, OAS_MAX, OAS_THRESHOLD, 0.0)
    during = member_retirement_income(member, 2025, OAS_MAX, OAS_THRESHOLD, 0.0)
    assert before.oas == 0.0 and before.retired is False
    assert during.oas == OAS_MAX, "OAS must not wait for retirement_age 68"
    assert during.retired is False, "the member is still working"


def test_working_still_controls_the_retired_flag():
    """``retired`` keeps meaning "has left work" -- the fold's income gate.

    Changing the BENEFIT gate must not change what the flag reports, or every
    member who claims early would silently stop earning.
    """
    member = {"birth_year": 1966, "retirement_age": 65, "cpp_start_age": 60,
              "cpp_monthly_estimated": 1000}
    early = member_retirement_income(member, 2027, OAS_MAX, OAS_THRESHOLD, 0.0)
    late = member_retirement_income(member, 2031, OAS_MAX, OAS_THRESHOLD, 0.0)
    assert early.retired is False
    assert late.retired is True
    assert early.cpp == late.cpp, "the same claim pays the same amount either way"


def test_nothing_is_paid_before_the_claim_age():
    """A member who has claimed nothing yet still receives nothing."""
    member = {"birth_year": 1970, "retirement_age": 65, "cpp_start_age": 65,
              "cpp_monthly_estimated": 1000, "oas_start_age": 65}
    r = member_retirement_income(member, 2027, OAS_MAX, OAS_THRESHOLD, 0.0)
    assert r.cpp == 0.0
    assert r.oas == 0.0


def test_absent_claim_ages_still_default_to_65():
    """DP#32: an absent key falls back to 65; this must not become 'pay now'."""
    member = {"birth_year": 1975, "retirement_age": 65,
              "cpp_monthly_estimated": 1000}
    young = member_retirement_income(member, 2031, OAS_MAX, OAS_THRESHOLD, 0.0)
    assert young.cpp == 0.0 and young.oas == 0.0, "age 56, nothing payable"