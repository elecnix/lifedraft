#!/usr/bin/env python3
"""The business-use portion of a property: Capital Cost Allowance on it, and
the price of claiming it (issue #377).

A household where one member runs a business out of part of the principal
residence -- a home office, a shop in the front rooms -- has two things to model
that the engine could not before this module existed:

1. **The claim.** A business-use portion of a home is depreciable: CRA's
   business-use-of-home page states CCA on the business-use part is an expense
   of the business (T2125 line 22200). The claim is the SAME declining-balance
   claim the rental path already makes (:func:`countries.canada.cca.cca_claim`,
   ITA s.13 / s.20(1)(a)) against the SAME T2125 base the self-employment
   segment already computes -- one spelling of both (DP#9), so the two paths
   cannot drift apart.

2. **What the claim costs.** Claiming CCA on the business fraction of a
   principal residence ends that property's principal-residence treatment.
   Income Tax Folio S1-F3-C2 para 2.59-2.60: CRA treats the property as keeping
   principal-residence status only while "no CCA is claimed on the property";
   once it is, "the deemed disposition rule is applied as of the time at which
   the income-producing use commenced" (ITA s.45(1)(c)), so the business
   fraction is settled at FMV, reacquired at that FMV, and its gain from the
   change in use onward carries NO principal-residence exemption. The
   depreciation is clawed back as ORDINARY income (100% inclusion, ITA
   s.13(1)) on a later sale or on the s.70(5) deemed disposition at death.

That asymmetry is the whole point of the module. Folding a CCA amount into
``income.expenses_annual`` by hand -- the workaround before #377 -- keeps the
first half and silently drops the second, so the engine OVERSTATES the value of
claiming CCA on a home, which CRA's own guidance makes a usually poor choice.

Timing of the first claim
-------------------------
The claim is recognized from the first FULL YEAR AFTER ``change_in_use_year``:
``change_in_use_year`` is the year ITA s.45(1)(c) deems the business fraction
disposed of and reacquired at FMV, and the issue's own acceptance criterion is
a claim of ``rate x opening_ucc`` "in a full year after the conversion year".

The **half-year rule** (ITA s.20(1)(b)) is deliberately NOT applied, and that is
a position, not an oversight: ``opening_ucc`` is the declared undepreciated
capital cost of the business fraction tracked from day one (DP#19 -- track cost
basis from day one), not a net addition to the class arriving in the change
year, so there is no half-year base to halve. This is the same position the
rental path takes for a declared running UCC (#694). A household whose class
genuinely starts empty mid-projection declares the lower ``opening_ucc``.

Nothing here reads the household: every function is a pure function of the
declared facts (DP#3, DP#25 -- the tax law lives in this module, the fold only
does the plumbing).
"""


def business_use_claim(opening_ucc: float, rate: float,
                       role_net_business_income: float,
                       change_in_use_year: int,
                       cal_year: int) -> float:
    """The CCA the business-use portion claims for one calendar year.

    Delegates the arithmetic to :func:`countries.canada.cca.cca_claim` -- the
    same declining-balance claim the rental building makes -- and adds the one
    rule specific to a change in use: nothing is claimed in or before the year
    the business use began (``change_in_use_year``). From the first full year
    after it, the claim is ``rate`` of the opening UCC, capped at the role's net
    business income before CCA, because CCA cannot create or deepen a business
    loss (ITA s.20(1)(a) -- the same cap the rental path applies).

    Args:
        opening_ucc: undepreciated capital cost of the BUSINESS FRACTION at the
            start of the year (>= 0).
        rate: declining-balance CCA rate (configuration, never a hardcoded 4% --
            DP#2/DP#12).
        role_net_business_income: the declaring role's net self-employment
            income for the year BEFORE this claim (the T2125 figure).
        change_in_use_year: the CALENDAR year the business use began.
        cal_year: the calendar year being priced.

    Returns:
        The CCA claimed this year (>= 0). 0.0 in and before the change-in-use
        year (the block is inert until its trigger year, DP#16) and 0.0 for a
        role with no net business income to shelter it.
    """
    if cal_year <= change_in_use_year:
        return 0.0
    from countries.canada.cca import cca_claim
    return cca_claim(opening_ucc, rate, role_net_business_income,
                     is_acquisition_year=False)


def business_fraction_cost_and_value(value_now: float,
                                     value_at_change_in_use: float,
                                     fraction: float) -> tuple:
    """The BUSINESS FRACTION's ``(acb, fmv)`` -- the cost base ITA s.45(1)(c) fixed
    for it, and its value in the year being priced.

    s.45(1)(c) deems the business fraction disposed of and reacquired at FMV on
    the date the income-producing use began, so its cost base is its value ON
    THAT DATE, not the original purchase price of the whole property. Everything
    it has gained since -- ``fmv - acb`` -- is an ordinary capital gain with NO
    principal-residence exemption (Income Tax Folio S1-F3-C2 para 2.59-2.60,
    which is also what makes claiming CCA on the portion a usually poor choice).

    The estate prices that gain through its existing
    ``property_gain_bases`` seam -- a list of ``(fmv, acb, taxable_fraction)``
    triples -- so this returns the pair rather than a pre-subtracted gain: the
    seam takes a property's value and cost, and handing it a synthesised
    ``(gain, 0)`` would put a cost base in the record that the property never
    had. A property that has not appreciated since the change in use yields
    ``fmv == acb`` and therefore a gain of 0, which is the honest answer -- a
    static house has no gain to shelter.

    Args:
        value_now: the PROPERTY's value in the year being priced.
        value_at_change_in_use: the same property's value in the year the
            business use began -- the deemed-disposition value.
        fraction: the business-use share of the property (0.25 = a quarter).

    Returns:
        ``(fraction * value_at_change_in_use, fraction * value_now)``.
    """
    return fraction * value_at_change_in_use, fraction * value_now
