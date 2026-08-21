"""The AMT tax BASE is the one in the statute — not a fabricated one (#710/#754).

`amt.py` added the RRSP deduction back in full to adjusted taxable income,
citing "ITA s.127.51". Both halves of that are wrong: s.127.51 sets the *rate and
exemption* and says nothing about the base, and the base provision — s.127.52(1)
— has a **closed list** of add-backs that does not include the RRSP deduction
(s.60(i)). AMT catches tax shelters, flow-throughs, capital gains and stock
options; not registered retirement savings.

This was almost shipped. PR #750 originally wired the module into the fold and
reported that a $400k earner contributing $300k to an RRSP owed $33,377 of AMT —
a 21% haircut on a real household's projected refund, for a tax that does not
exist. These tests exist so that never comes back.

The statutory add-backs, verbatim from s.127.52(1):

  (d) the ss.38(a)/(b) fractions are "read as a reference to '1/1'"
      -> 100% capital gains inclusion (vs the regular 50%)
  (h) only s.110(2) and 7/5 of 110(1)(d.01)/110.6(2)/(2.1) survive
      -> the s.110(1)(d) stock option deduction is DENIED
  (i) s.111(1) loss carryovers: "the lesser of (A) 1/2 of all amounts deducted"
  (j) "the individual deducted 1/2 of the amount deducted for the year under
      (i) paragraphs 8(1)(c) to (e), (g) to (l.2) and (p) to (t),
      (ii) paragraphs 20(1)(c) to (f) and (bb) in respect of an amount borrowed"
      -> half of employment deductions, and half of interest / carrying charges
         incurred to earn income from property

https://laws-lois.justice.gc.ca/eng/acts/i-3.3/section-127.52.html
"""
from __future__ import annotations

import inspect

import pytest

from countries.canada.amt import AMTParameters, amt_adjusted_income, total_tax_with_amt
from countries.canada.tax_calc import compute_non_refundable_credits, federal_tax


YEAR = 2026
EXEMPTION_2026 = 181_440  # the 29% federal bracket threshold (ITA s.127.51)


def _regular_federal_tax(taxable: float) -> float:
    """Federal tax after abatement and credits — what the minimum amount is
    measured against (CRA T691)."""
    after_abatement = federal_tax(taxable, YEAR, "quebec")
    credits = compute_non_refundable_credits(taxable, taxable, YEAR, "quebec")["total"]
    return max(0.0, after_abatement - credits)


def _surcharge(taxable_income: float, **addbacks) -> float:
    return total_tax_with_amt(
        regular_tax=_regular_federal_tax(taxable_income),
        taxable_income=taxable_income,
        params=AMTParameters.for_year(YEAR),
        **addbacks,
    )["amt_surcharge"]


# ---------------------------------------------------------------------------
# The RRSP deduction is NOT an AMT preference item
# ---------------------------------------------------------------------------

def test_rrsp_deduction_is_not_even_expressible_as_an_addback():
    """The parameter is GONE, not defaulted to zero (DP#9: no shims).

    An `rrsp_deduction=0` default would leave the fabrication one keyword away
    from returning. A caller that still passes it must fail loudly.
    """
    for fn in (amt_adjusted_income, total_tax_with_amt):
        assert "rrsp_deduction" not in inspect.signature(fn).parameters

    with pytest.raises(TypeError):
        amt_adjusted_income(taxable_income=100_000, rrsp_deduction=300_000)


def test_an_rrsp_deduction_does_not_change_amti():
    """AMTI is computed from TAXABLE income, which is already net of the RRSP
    deduction — and stays that way. The deduction reduces regular taxable income
    and AMTI alike, so it moves the taxpayer no closer to AMT."""
    # $400k earner contributing $300k: taxable income is $100k, and that IS the
    # AMT base. Nothing is added back.
    assert amt_adjusted_income(taxable_income=100_000) == 100_000


def test_the_fabricated_33k_surcharge_is_gone():
    """THE regression this file exists for.

    PR #750's original headline: a $400k earner deducting $300k of RRSP owed
    $33,377 of AMT, cutting their refund from $159,930 to $126,553. With the
    statutory base their AMTI is $100,000 — far below the $181,440 exemption —
    so the minimum amount is zero and they owe NO AMT at all.
    """
    assert _surcharge(taxable_income=100_000) == 0


@pytest.mark.parametrize("income,deduction", [
    (250_000, 100_000), (400_000, 300_000), (600_000, 500_000),
])
def test_no_rrsp_contribution_of_any_size_triggers_amt(income, deduction):
    """Both sides of a threshold that does not exist (DP#17). However large the
    contribution, the AMT base is just what is left as taxable income."""
    assert _surcharge(taxable_income=income - deduction) == 0


# ---------------------------------------------------------------------------
# What the statute DOES add back
# ---------------------------------------------------------------------------

def test_capital_gains_are_100pct_included_and_this_is_the_real_trigger():
    """s.127.52(1)(d): the 38(a)/(b) fraction is read as '1/1'.

    A $1,000,000 gain is $500,000 of taxable income under the regular 50%
    inclusion, but the WHOLE $1,000,000 in AMTI:

        minimum   = (1,000,000 - 181,440) x 20.5%  =  167,805
        regular   = federal tax on 500,000 (QC, after credits)
        surcharge = the shortfall

    This is the only add-back big enough to make AMT bite in this engine.
    """
    gain = 1_000_000
    taxable = gain * 0.50

    amti = amt_adjusted_income(
        taxable_income=taxable,
        taxable_capital_gains=taxable,
        capital_gains_inclusion=0.50,
    )
    assert amti == gain, "the untaxed half of the gain is added back"

    surcharge = _surcharge(
        taxable_income=taxable,
        taxable_capital_gains=taxable,
        capital_gains_inclusion=0.50,
    )
    expected_minimum = (gain - EXEMPTION_2026) * 0.205
    assert expected_minimum == pytest.approx(167_804.8, abs=0.5)
    assert surcharge == pytest.approx(expected_minimum - _regular_federal_tax(taxable))
    assert surcharge == pytest.approx(54_046, abs=50)


def test_carrying_charges_are_only_half_added_back():
    """s.127.52(1)(j)(ii): 1/2 of s.20(1)(c)-(f)/(bb) interest. The Smith
    Manoeuvre / leveraged-investing case."""
    assert amt_adjusted_income(
        taxable_income=300_000, carrying_charges=100_000,
    ) == 350_000


def test_carrying_charges_alone_cannot_trigger_amt_at_any_leverage():
    """A consequence worth pinning, because it is the reason AMT stays unwired.

    Only HALF the interest is added back, so AMTI = taxable + interest/2, which
    is always BELOW gross income — while regular federal rates (26-33%) exceed
    the 20.5% AMT rate. So carrying charges can never clear the exemption on
    their own, even at absurd leverage.
    """
    for income, interest in [
        (250_000, 200_000), (600_000, 200_000), (1_000_000, 400_000),
    ]:
        taxable = income - interest
        assert _surcharge(taxable_income=taxable, carrying_charges=interest) == 0, (
            f"income {income:,} with {interest:,} of deductible interest should "
            f"owe no AMT — a half add-back cannot outrun the 29% bracket"
        )


def test_stock_option_deduction_is_denied_in_full():
    """s.127.52(1)(h): the s.110(1)(d) deduction does not survive for AMT, so the
    benefit is 100% included — unlike carrying charges, which are halved."""
    assert amt_adjusted_income(
        taxable_income=200_000, stock_option_deduction=50_000,
    ) == 250_000


def test_non_capital_losses_are_half_added_back():
    """s.127.52(1)(i)."""
    assert amt_adjusted_income(
        taxable_income=200_000, non_capital_loss_deducted=40_000,
    ) == 220_000


def test_a_zero_inclusion_rate_raises_rather_than_silently_erasing_the_gain():
    """DP#32: grossing a gain up to 100% divides by the inclusion rate. Treating
    a nonsense rate as 'no gain' would switch off the single biggest AMT trigger
    there is — silently."""
    with pytest.raises(ValueError):
        amt_adjusted_income(
            taxable_income=100_000, taxable_capital_gains=50_000,
            capital_gains_inclusion=0,
        )


# ---------------------------------------------------------------------------
# The exemption is derived from data, not invented
# ---------------------------------------------------------------------------

def test_exemption_is_the_29pct_bracket_threshold_from_tax_data():
    """ITA s.127.51 pegs the basic exemption to the lower limit of the 29%
    federal bracket — a figure `tax_data` already holds. Deriving it means there
    is no second, silently-diverging copy of the same number (DP#2/DP#12/DP#20).
    """
    from tax_data import TaxDataProvider

    provider = TaxDataProvider()
    brackets = provider._load_year(YEAR, "canada", "federal").federal_brackets
    bracket_29 = next(b for b in brackets if abs(b.rate - 0.29) < 1e-9)

    params = AMTParameters.for_year(YEAR, provider)
    assert params.exemption == bracket_29.min_income == EXEMPTION_2026
    assert params.amt_rate == 0.205
    assert params.exemption_phaseout_rate == 0.0  # Canada has no phase-out
