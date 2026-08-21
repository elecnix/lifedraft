"""Net-target (tax-aware) drawdown tests — plan_drawdown_net."""
import pytest

from countries.canada.retirement_transition import plan_drawdown_net

ORDER = ['tfsa', 'non_reg', 'rrsp', 'lif', 'lira']


def _canada(**kw):
    base = {'tfsa_primary_balance': 0, 'tfsa_spouse_balance': 0,
            'rrsp_balance': 0, 'spousal_rrsp_balance': 0, 'spouse_rrsp_balance': 0,
            'lif_balance': 0, 'lira_balance': 0, 'fhsa_balance': 0}
    base.update(kw)
    return base


def test_tfsa_delivers_face_value():
    """A tax-free TFSA withdrawal draws exactly the net need — no gross-up."""
    plan = plan_drawdown_net(50_000, ORDER, _canada(tfsa_primary_balance=200_000),
                             non_reg_balance=0, non_reg_acb=0, marginal_rate=0.45)
    assert plan.total_withdrawn == pytest.approx(50_000)
    assert plan.taxable_withdrawn == 0


def test_rrsp_is_grossed_up_for_tax():
    """A fully-taxable RRSP draw is grossed up: gross = net / (1 − rate)."""
    plan = plan_drawdown_net(50_000, ORDER, _canada(rrsp_balance=500_000),
                             non_reg_balance=0, non_reg_acb=0, marginal_rate=0.40)
    assert plan.total_withdrawn == pytest.approx(50_000 / (1 - 0.40))
    assert plan.taxable_withdrawn == pytest.approx(plan.total_withdrawn)


def test_order_respected_tax_free_first():
    """With TFSA first, a small need never touches the RRSP."""
    plan = plan_drawdown_net(30_000, ORDER,
                             _canada(tfsa_primary_balance=100_000, rrsp_balance=100_000),
                             non_reg_balance=0, non_reg_acb=0, marginal_rate=0.45)
    assert plan.balance_deltas.get('rrsp_balance', 0) == 0
    assert plan.balance_deltas['tfsa_primary_balance'] == pytest.approx(-30_000)


def test_over_draw_regression_vs_gross():
    """Net mode does NOT over-draw when the lead source is tax-free.

    The legacy gross path would withdraw ~net/(1−rate) even from a tax-free TFSA;
    net mode withdraws only the need. This is the early-retirement fix.
    """
    plan = plan_drawdown_net(100_000, ORDER, _canada(tfsa_primary_balance=300_000),
                             non_reg_balance=0, non_reg_acb=0, marginal_rate=0.50)
    assert plan.total_withdrawn == pytest.approx(100_000)  # not 200_000


def test_falls_through_to_next_source_when_depleted():
    """When TFSA can't cover the net need, the remainder comes from RRSP."""
    plan = plan_drawdown_net(50_000, ORDER,
                             _canada(tfsa_primary_balance=20_000, rrsp_balance=500_000),
                             non_reg_balance=0, non_reg_acb=0, marginal_rate=0.40)
    assert plan.balance_deltas['tfsa_primary_balance'] == pytest.approx(-20_000)
    # Remaining $30k net grossed up from RRSP.
    assert plan.balance_deltas['rrsp_balance'] == pytest.approx(-30_000 / (1 - 0.40))


def test_zero_need_draws_nothing():
    plan = plan_drawdown_net(0, ORDER, _canada(rrsp_balance=100_000),
                             non_reg_balance=0, non_reg_acb=0, marginal_rate=0.4)
    assert plan.total_withdrawn == 0
