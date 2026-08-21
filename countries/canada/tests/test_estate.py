"""Estate / deemed-disposition after-tax legacy tests.

Epic #603 Track C Phase 2c (#600): ``compute_estate`` now requires an explicit
``EstatePlan``. There is deliberately no default, because every field it carries
used to be a silent assumption that resolved in the FAVOURABLE direction — so a
test (or a caller) that has not decided must say so, not inherit a flattering
answer.

``_declined()`` below is the plan that reproduces the pre-Phase-2c arithmetic
EXACTLY (two separate terminal returns, 50/50 non-reg split), which is why every
pre-existing assertion in this file is unchanged. NOTE the name: the old code
*documented* itself as the spousal-rollover case but *computed* the
declined-rollover case — see estate.py's module docstring. Only the label is now
honest; the numbers never moved.
"""
from tax_data import default_tax_provider
import pytest

from countries.canada.estate import (
    EstateInputError,
    EstatePlan,
    TerminalReturn,
    compute_estate,
    couple_terminal_returns,
    tax_on_registered_at_death,
)

BRK = default_tax_provider().get_combined_brackets(2026, 'quebec')


def _estate(*, plan, registered_primary=0.0, registered_spouse=0.0, **kw):
    """Couple-level adapter: compute_estate now takes a per-member LIST of
    terminal returns (#705). These couple tests still express themselves as a
    primary/spouse pair, so this builds the death-ordered two-return list via
    the same ``couple_terminal_returns`` mapper objective.py uses, then calls
    compute_estate. Byte-identical to the pre-#705 two-scalar call."""
    members = couple_terminal_returns(
        registered_primary=registered_primary,
        registered_spouse=registered_spouse, plan=plan)
    return compute_estate(members=members, plan=plan, **kw)


def _declined(**overrides):
    """The elections that reproduce the pre-Phase-2c arithmetic: nothing rolls,
    so each spouse's assets are taxed on their own terminal return, and the
    non-registered pot is split 50/50 (the old hardcoded guess)."""
    base = dict(spousal_rollover=False, tfsa_successor_holder=True,
                non_reg_primary_share=0.5, primary_dies_first=True,
                registered_rolled_fraction=0.0, non_reg_rolled_fraction=0.0)
    base.update(overrides)
    return EstatePlan(**base)


def _elected(**overrides):
    """A full spousal rollover: the first-to-die's assets move onto the
    survivor's return, so everything is taxed once, together."""
    return _declined(spousal_rollover=True, registered_rolled_fraction=1.0,
                     non_reg_rolled_fraction=1.0, **overrides)


def test_tfsa_and_house_pass_tax_free():
    """TFSA and a DESIGNATED principal residence incur no deemed-disposition tax."""
    e = _estate(registered_primary=0, registered_spouse=0,
                       tfsa=200_000, non_reg_fmv=0, non_reg_acb=0,
                       house_equity=500_000, debts=0, brackets=BRK,
                       plan=_declined())
    assert e.total_tax == 0
    assert e.net_estate == 700_000


def test_registered_is_fully_taxed_at_death():
    """A registered balance is taxed as ordinary income; net < gross."""
    e = _estate(registered_primary=600_000, registered_spouse=0,
                       tfsa=0, non_reg_fmv=0, non_reg_acb=0,
                       house_equity=0, debts=0, brackets=BRK,
                       plan=_declined())
    assert e.registered_tax > 0
    # Quebec top rates make a $600k terminal return effective rate ~40-50%.
    assert 0.35 < e.registered_tax / 600_000 < 0.55
    assert e.net_estate == pytest.approx(600_000 - e.registered_tax)


def test_splitting_registered_between_spouses_lowers_tax():
    """Two $300k terminal returns are taxed less than one $600k return."""
    one = tax_on_registered_at_death(600_000, BRK)
    two = 2 * tax_on_registered_at_death(300_000, BRK)
    assert two < one


def test_non_reg_taxes_only_the_gain():
    """Only accrued gain (FMV − ACB) at 50% inclusion is taxed, not principal."""
    no_gain = _estate(registered_primary=0, registered_spouse=0, tfsa=0,
                             non_reg_fmv=100_000, non_reg_acb=100_000,
                             house_equity=0, debts=0, brackets=BRK,
                             plan=_declined())
    assert no_gain.non_reg_tax == 0
    with_gain = _estate(registered_primary=0, registered_spouse=0, tfsa=0,
                               non_reg_fmv=100_000, non_reg_acb=40_000,
                               house_equity=0, debts=0, brackets=BRK,
                               plan=_declined())
    assert with_gain.non_reg_tax > 0


def test_net_estate_is_gross_minus_tax_minus_debt():
    """Invariant: net = tfsa + house + registered + non_reg − debt − tax."""
    e = _estate(registered_primary=300_000, registered_spouse=200_000,
                       tfsa=150_000, non_reg_fmv=100_000, non_reg_acb=50_000,
                       house_equity=700_000, debts=80_000, brackets=BRK,
                       plan=_declined())
    assert e.gross_estate == 300_000 + 200_000 + 150_000 + 100_000 + 700_000 - 80_000
    assert e.net_estate == pytest.approx(e.gross_estate - e.total_tax)
    assert 0 < e.effective_tax_rate < 1


# ── epic #603 Track C Phase 2c (#600): the five elections are real inputs ──


def test_compute_estate_refuses_to_run_without_an_explicit_plan():
    """DP#32: the caller must DECIDE. There is no default EstatePlan, because
    every field it carries used to default to the favourable branch."""
    with pytest.raises(EstateInputError, match='explicit EstatePlan'):
        compute_estate(members=[TerminalReturn(registered=100_000)], tfsa=0,
                       non_reg_fmv=0, non_reg_acb=0, house_equity=0, debts=0,
                       brackets=BRK, plan=None)


def test_electing_the_spousal_rollover_costs_MORE_tax_than_declining_it():
    """#600's headline, and the correction of a backwards docstring.

    A rollover moves the first-to-die's registered plan onto the SURVIVOR's
    terminal return, so both pots run the progressive brackets ONCE, together —
    bracket compression. Declining it gives each spouse their own return, each
    starting from $0. The rollover buys DEFERRAL, not exemption, and at these
    balances the deferral costs real money.
    """
    args = dict(registered_primary=600_000, registered_spouse=500_000, tfsa=0,
                non_reg_fmv=0, non_reg_acb=0, house_equity=0, debts=0,
                brackets=BRK)
    elected = _estate(**args, plan=_elected())
    declined = _estate(**args, plan=_declined())

    assert elected.registered_tax > declined.registered_tax
    # One combined $1.1M return vs a $600k return + a $500k return.
    assert elected.registered_tax == pytest.approx(
        tax_on_registered_at_death(1_100_000, BRK))
    assert declined.registered_tax == pytest.approx(
        tax_on_registered_at_death(600_000, BRK)
        + tax_on_registered_at_death(500_000, BRK))
    assert declined.net_estate > elected.net_estate


def test_a_partial_rollover_can_beat_BOTH_extremes():
    """A per-account ``rollover_overrides`` entry that disagrees with the
    household default (the shipped example does exactly this) is a PARTIAL
    rollover — and it is not merely "somewhere between" the two booleans. It can
    be strictly BETTER than both, which is precisely the planning lever #600
    describes ("declined to use up a low-income spouse's brackets") and which no
    single boolean could ever express.

    With everything in one spouse's name, both extremes pile the whole balance
    onto ONE terminal return (rolling it all over, or leaving it all where it is
    — symmetric). Rolling only HALF splits it across two returns, each running
    the progressive brackets from $0. Progressive tax rewards the balanced split.
    """
    args = dict(registered_primary=400_000, registered_spouse=0, tfsa=0,
                non_reg_fmv=0, non_reg_acb=0, house_equity=0, debts=0,
                brackets=BRK)
    declined = _estate(**args, plan=_declined())
    elected = _estate(**args, plan=_elected())
    half = _estate(**args, plan=_declined(spousal_rollover=True,
                                                 registered_rolled_fraction=0.5))

    # Both extremes put the full $400k on a single return, so they tie...
    assert declined.registered_tax == pytest.approx(elected.registered_tax)
    # ...and splitting it across two returns strictly beats them.
    assert half.registered_tax < declined.registered_tax
    assert half.net_estate > declined.net_estate
    assert half.net_estate > elected.net_estate


def test_who_dies_first_decides_whose_return_the_rolled_assets_land_on():
    """assumptions.mortality is CONSUMED, not assumed. With asymmetric registered
    pots, rolling half of the primary's onto the spouse's return is not the same
    as rolling half of the spouse's onto the primary's."""
    args = dict(registered_primary=800_000, registered_spouse=200_000, tfsa=0,
                non_reg_fmv=0, non_reg_acb=0, house_equity=0, debts=0,
                brackets=BRK)
    p_first = _estate(**args, plan=_declined(
        spousal_rollover=True, primary_dies_first=True,
        registered_rolled_fraction=0.5))
    s_first = _estate(**args, plan=_declined(
        spousal_rollover=True, primary_dies_first=False,
        registered_rolled_fraction=0.5))
    assert p_first.registered_tax != s_first.registered_tax


def test_an_undesignated_principal_residence_is_taxed_on_its_gain():
    """ITA s.40(2)(b) is claimed per property per YEAR. A residence with no
    designated years is ordinary capital property — #600's fourth assumption."""
    args = dict(registered_primary=0, registered_spouse=0, tfsa=0,
                non_reg_fmv=0, non_reg_acb=0, house_equity=650_000, debts=0,
                brackets=BRK)
    designated = _estate(**args, plan=_declined())
    undesignated = _estate(**args, plan=_declined(
        principal_residence_designated=False,
        principal_residence_fmv=650_000, principal_residence_acb=200_000))

    assert designated.taxable_property_tax == 0
    assert undesignated.taxable_property_tax > 0
    assert undesignated.net_estate < designated.net_estate
    # The residence's VALUE is not double-counted: it reaches the estate via
    # house_equity, and the undesignated case only adds its GAIN to the tax.
    assert undesignated.gross_estate == designated.gross_estate


def test_non_principal_property_adds_value_and_is_taxed_on_its_gain():
    """A cottage/rental is ordinary capital property: its value joins the estate
    and its accrued gain is taxed."""
    e = _estate(registered_primary=0, registered_spouse=0, tfsa=0,
                       non_reg_fmv=0, non_reg_acb=0, house_equity=0, debts=0,
                       brackets=BRK,
                       plan=_declined(taxable_property_fmv=310_000,
                                      taxable_property_acb=180_000))
    assert e.taxable_property_gross == 310_000
    assert e.taxable_property_tax > 0
    assert e.gross_estate == 310_000


def test_life_insurance_is_added_tax_free():
    """ITA s.148(1): the death benefit is received tax-free. Absent from the
    model entirely before Phase 2c."""
    e = _estate(registered_primary=0, registered_spouse=0, tfsa=0,
                       non_reg_fmv=0, non_reg_acb=0, house_equity=0, debts=0,
                       brackets=BRK,
                       plan=_declined(life_insurance_death_benefit=500_000))
    assert e.life_insurance == 500_000
    assert e.total_tax == 0
    assert e.net_estate == 500_000


def test_the_real_non_reg_ownership_split_is_used_not_a_5050_guess():
    """#600's third assumption. With asymmetric registered pots the split
    genuinely moves the tax, because it decides which return's marginal band the
    gain stacks onto."""
    args = dict(registered_primary=0, registered_spouse=250_000, tfsa=0,
                non_reg_fmv=400_000, non_reg_acb=100_000, house_equity=0,
                debts=0, brackets=BRK)
    all_primary = _estate(**args, plan=_declined(non_reg_primary_share=1.0))
    all_spouse = _estate(**args, plan=_declined(non_reg_primary_share=0.0))
    # The primary has NO registered income, so their return starts at $0 and the
    # gain runs the low brackets. Piling it on the spouse's $250k return instead
    # taxes it at a higher band.
    assert all_primary.non_reg_tax < all_spouse.non_reg_tax


def test_the_result_carries_the_elections_that_produced_it():
    """DP#32/#585: an assumption that moves the headline must be surfaced. The
    result STATES which elections produced it, so an output surface never has to
    guess (and a reader never has to trust a docstring)."""
    e = _estate(registered_primary=0, registered_spouse=0, tfsa=100_000,
                       non_reg_fmv=0, non_reg_acb=0, house_equity=0, debts=0,
                       brackets=BRK,
                       plan=_elected(tfsa_successor_holder=False))
    assert e.spousal_rollover is True
    assert e.tfsa_shelter_ends is True


def test_tfsa_is_tax_free_under_both_designations():
    """Honest negative result, DECLARED rather than faked: the ITA makes a TFSA
    tax-free at its death-date VALUE under BOTH a successor-holder and a
    beneficiary designation. The difference is entirely in POST-death growth,
    which a point-in-time estate snapshot does not reach — so this model must not
    invent a dollar difference here. The gap is disclosed by model_fidelity's
    `estate_is_a_point_in_time_valuation`, not papered over."""
    args = dict(registered_primary=0, registered_spouse=0, tfsa=300_000,
                non_reg_fmv=0, non_reg_acb=0, house_equity=0, debts=0,
                brackets=BRK)
    successor = _estate(**args, plan=_declined(tfsa_successor_holder=True))
    beneficiary = _estate(**args, plan=_declined(tfsa_successor_holder=False))
    assert successor.net_estate == beneficiary.net_estate == 300_000
    # ...but the designation IS carried, so the report can state it.
    assert successor.tfsa_shelter_ends is False
    assert beneficiary.tfsa_shelter_ends is True


@pytest.mark.parametrize('bad', [-0.1, 1.1])
def test_a_share_outside_0_1_is_refused(bad):
    with pytest.raises(EstateInputError):
        _declined(non_reg_primary_share=bad)


# ── #705: compute_estate sums over N terminal returns, not a hardcoded two ──


def test_compute_estate_refuses_an_empty_member_list():
    """A deemed disposition with no members to settle has no estate to tax; the
    empty list is refused loudly rather than returning a plausible $0 (DP#32)."""
    with pytest.raises(EstateInputError, match='at least one terminal return'):
        compute_estate(members=[], tfsa=0, non_reg_fmv=0, non_reg_acb=0,
                       house_equity=0, debts=0, brackets=BRK, plan=_declined())


def test_two_returns_are_byte_identical_to_the_closed_form_split():
    """Generalising to N returns must not move the two-return arithmetic. A
    two-member declined-rollover estate equals, to the last cent, the closed
    form 'each owner is taxed on their own terminal return from $0'."""
    members = couple_terminal_returns(registered_primary=420_000,
                                      registered_spouse=180_000,
                                      plan=_declined())
    e = compute_estate(members=members, tfsa=0, non_reg_fmv=0, non_reg_acb=0,
                       house_equity=0, debts=0, brackets=BRK, plan=_declined())
    expected = (tax_on_registered_at_death(420_000, BRK)
                + tax_on_registered_at_death(180_000, BRK))
    assert e.registered_tax == expected  # exact equality, not approx
    assert e.registered_gross == 600_000


def test_three_generation_estate_produces_three_terminal_returns():
    """#705's point: three generations, each with their own registered pot and
    no rollover, are taxed on THREE separate progressive returns -- not folded
    into a hardcoded two. Each $300k runs the brackets from $0, so the total is
    strictly below one combined $900k return (progressive-bracket compression)."""
    members = [TerminalReturn(registered=300_000),
               TerminalReturn(registered=300_000),
               TerminalReturn(registered=300_000)]
    e = compute_estate(members=members, tfsa=0, non_reg_fmv=0, non_reg_acb=0,
                       house_equity=0, debts=0, brackets=BRK, plan=_declined())
    assert e.registered_gross == 900_000
    assert e.registered_tax == pytest.approx(
        3 * tax_on_registered_at_death(300_000, BRK))
    assert e.registered_tax < tax_on_registered_at_death(900_000, BRK)


def test_a_full_rollover_chain_lands_everything_on_the_last_return():
    """The couple's spousal rollover generalises to an N-member chain: with a
    full rollover at every step, each first-to-die's registered plan rolls
    forward, so a three-generation chain lands all registered value on the FINAL
    terminal return -- one combined progressive run (ITA s.146(8.1))."""
    members = [TerminalReturn(registered=300_000),
               TerminalReturn(registered=300_000),
               TerminalReturn(registered=300_000)]
    e = compute_estate(members=members, tfsa=0, non_reg_fmv=0, non_reg_acb=0,
                       house_equity=0, debts=0, brackets=BRK,
                       plan=_declined(registered_rolled_fraction=1.0))
    assert e.registered_tax == pytest.approx(
        tax_on_registered_at_death(900_000, BRK))
