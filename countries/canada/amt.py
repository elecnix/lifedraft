#!/usr/bin/env python3
"""Alternative Minimum Tax (AMT) — Federal calculation for Canada.

AMT ensures taxpayers pay a minimum amount of tax, especially those
with large deductions (RRSP, capital gains, stock options). The 2024
federal budget significantly changed AMT parameters.

DP#2: AMT parameters belong in data (year-versioned), not hardcoded.
DP#3: All functions are pure — same inputs always produce same outputs.
DP#8: AMT parameters are a dataclass passed to computation functions.
DP#20: Parameters are year-versioned; simulate across tax years.
DP#27: AMT affects deduct-later strategy accuracy for high-income scenarios.

References:
    CRA AMT guide: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/alternative-minimum-tax.html
    CRA Form T691 (AMT): https://www.canada.ca/en/revenue-agency/services/forms-publications/forms/t691.html
    ITA Part I, Division F (s.127.5–127.6): AMT computation
    ITA s.127.52 (2024 amendments): adjusted taxable income add-backs
    2024 Federal Budget (enacted): AMT exemption increase to $173,205 (2024),
        rate 20.5% (up from 15%), broadened base.
    Pre-2024: Exemption $40,000 for individuals, rate 15%.

The minimum amount is `20.5% x (adjusted taxable income - basic exemption)`.
The basic exemption is a flat subtraction: it is **not** phased out as income
rises. (The 25%/dollar phase-out this module used to apply was the US AMT's
design, mistakenly transplanted here — see `AMTParameters.for_year`.)

MODELLED as of #747 (the three follow-ups #710 deliberately deferred):
  * The 50%-of-non-refundable-credits reduction to the minimum amount
    (``compute_amt(..., nonrefundable_credits=...)``; ITA s.127.531).
  * The 7-year AMT carry-forward (``carry_forward_amt_credit``; ITA s.120.2):
    AMT paid in excess of regular tax is a credit recoverable in a later year
    where regular tax exceeds the minimum amount, expiring after 7 years.
  * Quebec's separate `impot minimum de remplacement` (``compute_quebec_imr``;
    19%, its own exemption; Revenu Québec TP-776.42).

Verified 2024+ AMT base (ITA s.127.52(1), as amended; CRA T691):
    - (d)  Capital gains: the ss.38(a)/(b) fraction is "read as a reference to
           '1/1'" — 100% inclusion vs the regular 50%. THE dominant trigger.
    - (h)  Employee stock option benefit: the s.110(1)(d) deduction is denied,
           so the benefit is 100% included.
    - (i)  Non-capital loss carryovers (s.111(1)): only 1/2 deductible.
    - (j)  Only 1/2 of s.8(1) employment deductions, and only 1/2 of s.20(1)(c)
           to (f) and (bb) **interest and carrying charges incurred to earn
           income from property** (the leveraged-investing / Smith-Manoeuvre
           case) and investment counsel fees.
           Production source (#142): rules_amt.py passes the household's
           declared ``deductible_management_fee_annual`` totals (ITA
           s.20(1)(e)) here via ``carrying_charges`` -- until then this
           half-add-back priced a preference on a deduction the ordinary tax
           path never granted.
    - AMT rate raised from 15% to 20.5%; exemption raised to the 29%-bracket
      threshold (s.127.51).

    ** RRSP deductions are NOT an AMT preference item. ** They appear nowhere in
    s.127.52(1). This module used to add them back in full, citing "s.127.51" —
    a provision that sets the rate and exemption and says nothing about the base.
    That fabrication is removed (#710): an RRSP deduction reduces regular taxable
    income and AMTI alike, so a large RRSP contribution moves a taxpayer no
    closer to AMT.

STILL NOT MODELLED (declared, not silently omitted — DP#32):
    - The s.110.6 capital gains deduction (30% inclusion, para (d.1)/(d.2)).
    (The 50%-of-credits reduction, the 7-year carry-forward, and Quebec's IMR
    were the #710 deferrals and are now modelled — see the MODELLED note above.)

Usage:
    from countries.canada.amt import AMTParameters, compute_amt, total_tax_with_amt

    params = AMTParameters.for_year(2024)
    result = compute_amt(
        regular_tax=30000,
        adjusted_income=400000,
        params=params,
    )
    # result = {'amt_owing': ..., 'amt_surcharge': ..., 'amt_carryforward': ...}
"""

from dataclasses import dataclass

# =============================================================================
# AMT Parameters — Year-Versioned Data (DP#2, DP#8, DP#20)
# =============================================================================

@dataclass(frozen=True)
class AMTParameters:
    """AMT parameters for a specific tax year.

    DP#2: Configuration belongs in input, not in code.
    DP#8: Compose through data — parameters are a dataclass.
    DP#20: Data is year-versioned.

    Key AMT parameters that change by year:
    - exemption: AMT basic exemption. 2024+: the lower limit of the 29% federal
      bracket, *derived from the provider's bracket table* rather than restated
      here (see ``for_year``). Pre-2024: a flat, unindexed $40,000.
    - amt_rate: AMT rate (20.5% for 2024+ (enacted); 15% for pre-2024)

    The 2024 federal budget changes (effective 2024 tax year):
    - Exemption raised from $40k to the 29%-bracket threshold ($173,205 in 2024)
    - AMT rate increased from 15% to 20.5% (ITA s.127.51, Budget 2023/2024)
    - Carry-forward period: 7 years

    Per issue #360, the default AMT rate for 2024+ is the enacted 20.5%.
    The 15% rate applies to pre-2024 years (historical accuracy). Construct with
    ``amt_rate=0.15`` to compare against the old regime.

    Attributes:
        year: Tax year
        exemption: AMT basic exemption amount ($)
        amt_rate: AMT rate (decimal, e.g., 0.205 for 2024+)
        exemption_phaseout_start: AMTI above which the exemption phases out.
            Canada has NO such phase-out (rate is 0.0) — the fields exist so a
            jurisdiction that *does* phase one out can express it as data
            (DP#8) rather than by forking the computation.
        exemption_phaseout_rate: Rate the exemption is reduced per $1 above the
            threshold. **0.0 for Canada, every year.**
        carryforward_years: Years the AMT credit can be carried forward.
            The cross-year application lives in ``carry_forward_amt_credit``
            (#747): AMT paid is recovered against regular tax in a later year,
            expiring after this many years (ITA s.120.2).
    """
    year: int
    exemption: float
    amt_rate: float
    exemption_phaseout_start: float
    exemption_phaseout_rate: float
    carryforward_years: int = 7

    # Pre-2024 AMT: flat $40,000 exemption, never indexed, 15% rate
    # (ITA s.127.51 as it read before the Budget 2023 amendments).
    PRE_2024_EXEMPTION: float = 40000.0
    PRE_2024_RATE: float = 0.15
    POST_2024_RATE: float = 0.205

    @classmethod
    def for_year(cls, year: int, provider=None) -> "AMTParameters":
        """Get AMT parameters for a specific tax year.

        DP#2/DP#12/DP#20: the exemption is *derived from the bracket data the
        provider already holds*, not hardcoded here. For 2024+ the basic
        exemption is, by statute, the lower limit of the second-from-top
        federal bracket — the 29% bracket (ITA s.127.51 as amended by Budget
        2023; CRA T691). That threshold lives in ``tax_data`` already, so an
        AMT constant table would be a second, silently-diverging copy of it.
        Verified against the provider's own federal bracket table:

            2025 -> 177,882   (CRA: 2025 AMT basic exemption = $177,882)
            2026 -> 181,440   (== the provider's `bpa_phaseout_threshold`,
                               which is the same statutory threshold)

        Args:
            year: Tax year (e.g., 2026)
            provider: TaxDataProvider supplying the federal bracket table.
                Constructed on demand when omitted.

        Returns:
            AMTParameters for that year
        """
        if year < 2024:
            return cls(
                year=year,
                exemption=cls.PRE_2024_EXEMPTION,
                amt_rate=cls.PRE_2024_RATE,
                exemption_phaseout_start=cls.PRE_2024_EXEMPTION,
                exemption_phaseout_rate=0.0,
                carryforward_years=7,
            )

        if provider is None:
            from tax_data import TaxDataProvider
            provider = TaxDataProvider()

        exemption = cls.basic_exemption_for(year, provider)
        return cls(
            year=year,
            exemption=exemption,
            amt_rate=cls.POST_2024_RATE,
            exemption_phaseout_start=exemption,
            # The Canadian AMT basic exemption is NOT phased out: the minimum
            # amount is `rate x (ATI - exemption)`, full stop (CRA T691; PwC/EY/
            # CIBC/MNP all state the same formula; CFFP, Univ. de Sherbrooke,
            # states explicitly that the exemption "n'est pas reduite" with
            # income). The 25%/dollar phase-out this field used to carry was the
            # *US* AMT's design, and it entered here by confusion with the
            # federal **BPA** phase-out (ITA s.118(1.1)) — which runs over
            # exactly the same $173,205–$246,752 (2024) range, because both are
            # pegged to the 4th- and top-bracket thresholds. They are different
            # rules. See `basic_personal_amount_credit` for the real phase-out.
            exemption_phaseout_rate=0.0,
            carryforward_years=7,
        )

    @staticmethod
    def basic_exemption_for(year: int, provider) -> float:
        """The 2024+ AMT basic exemption: the lower limit of the 29% federal
        bracket (ITA s.127.51 as amended).

        DP#32: if the bracket table cannot supply that threshold, this raises
        rather than substituting a plausible-looking number. An AMT exemption
        that silently defaulted would silently switch AMT off for exactly the
        high-income taxpayers it exists to catch.
        """
        brackets = provider._load_year(year, "canada", "federal").federal_brackets
        if not brackets:
            raise ValueError(
                f"No federal bracket data for {year}: cannot derive the AMT basic "
                f"exemption (ITA s.127.51 pegs it to the 29% bracket threshold)."
            )
        ordered = sorted(brackets, key=lambda b: b.min_income)
        for bracket in ordered:
            if abs(bracket.rate - 0.29) < 1e-9:
                return float(bracket.min_income)
        raise ValueError(
            f"Federal bracket table for {year} has no 29% bracket; the AMT basic "
            f"exemption (ITA s.127.51) is defined as its lower limit. "
            f"Rates present: {[b.rate for b in ordered]}"
        )


def stock_option_amt_addback(
    stock_option_benefit: float,
    regular_deduction_rate: float = 0.50,
) -> float:
    """AMT add-back for employee stock option benefits (ITA s.110(1)(d)/(d.1)).

    For regular tax, a qualifying employee stock option benefit is included in
    income but a 50% deduction (s.110(1)(d)) effectively taxes only half the
    benefit. For AMT purposes (ITA s.127.52, as amended 2024) the full benefit
    is included — i.e. the entire stock option deduction that was claimed for
    regular tax is added back to AMTI.

    This function returns the dollar add-back = benefit × regular_deduction_rate,
    which is exactly the deduction that must be reversed for AMT. Feed the result
    into ``amt_adjusted_income(..., stock_option_deduction=...)``.

    Standard case (DP#10): qualifying options with the 50% deduction. The
    deduction rate is a parameter so non-standard cases (e.g. CCPC shares under
    s.110(1)(d.1), or the post-2021 $200k vesting cap that can deny the 50%
    deduction) can be modelled by passing the applicable rate.

    Args:
        stock_option_benefit: The employment benefit from exercising options
            (FMV at exercise minus exercise price).
        regular_deduction_rate: Fraction deducted for regular tax (default 50%).

    Returns:
        Dollar amount to add back to AMTI for the stock option preference.
    """
    if stock_option_benefit <= 0:
        return 0.0
    return stock_option_benefit * regular_deduction_rate


# =============================================================================
# Pure Functions — AMT Computation (DP#3)
# =============================================================================

def amt_adjusted_income(
    taxable_income: float,
    taxable_capital_gains: float = 0,
    capital_gains_inclusion: float = 0.50,
    carrying_charges: float = 0,
    employment_deductions: float = 0,
    stock_option_deduction: float = 0,
    non_capital_loss_deducted: float = 0,
) -> float:
    """Adjusted taxable income (AMTI) — ITA s.127.52(1), 2024+ regime.

    AMTI starts from **taxable income** and adds back the *specific* preference
    items s.127.52(1) enumerates. The list is closed: a deduction that is not in
    it is allowed against AMT exactly as it is against regular tax.

    THE ADD-BACKS THIS MODELS (statutory text, not a paraphrase):

      (d) capital gains — the fractions in ss.38(a)/(b) are "read as a reference
          to '1/1'", i.e. **100% inclusion** vs the regular 50%. We add back the
          untaxed remainder of the gain.
      (h) the s.110(1)(d) **employee stock option deduction is denied** (only
          s.110(2) and 7/5 of 110(1)(d.01)/110.6(2)/(2.1) survive), i.e. the
          benefit is 100% included.
      (i) non-capital loss carryovers under ss.111(1)(a)/(c)/(d)/(e): "the lesser
          of (A) **1/2** of all amounts deducted for the year"...
      (j) "in computing the individual's income for the year, the individual
          deducted **1/2 of the amount deducted for the year under**
            (i) paragraphs 8(1)(c) to (e), (g) to (l.2) and (p) to (t)  [employment]
            (ii) **paragraphs 20(1)(c) to (f) and (bb)** in respect of an amount
                 borrowed or paid"  [interest / carrying charges to earn income
                 from property, and investment counsel fees]

    ** RRSP DEDUCTIONS ARE NOT ADDED BACK. **

    This function previously added `rrsp_deduction` back in full, citing
    "ITA s.127.51". That was **fabricated** — s.127.51 sets the rate and
    exemption and says nothing about add-backs, and the RRSP deduction (s.60(i))
    appears NOWHERE in s.127.52(1)'s list. AMT was built to catch tax shelters,
    flow-throughs, capital gains and stock options — not registered retirement
    savings. An RRSP deduction reduces regular taxable income and AMTI alike, so
    a large RRSP contribution does not move the taxpayer toward AMT at all.

    That error was not cosmetic: wired into the engine it invented a ~$33k
    minimum tax for a household making a large RRSP contribution, which would
    have cut a real projected refund by ~21% for no legal reason. Removed, with
    the whole `rrsp_deduction` parameter (DP#9: no shims). See #710.

    DP#3: Pure function — same inputs always produce same output.

    Args:
        taxable_income: Taxable income (line 26000) — ALREADY net of the RRSP
            deduction, and that is correct.
        taxable_capital_gains: The TAXABLE half of capital gains, as it is
            already included in ``taxable_income`` (i.e. gain x inclusion rate).
        capital_gains_inclusion: The REGULAR inclusion rate used to produce
            ``taxable_capital_gains`` (0.50). Used to gross the gain back up to
            the 100% AMT inclusion; passing the rate rather than assuming it
            keeps this correct if the statutory rate moves (DP#2/DP#20).
        carrying_charges: Interest and financing expenses deducted under
            s.20(1)(c)-(f) and (bb) — HALF is added back, per (j)(ii).
        employment_deductions: s.8(1) deductions — HALF added back, per (j)(i).
        stock_option_deduction: s.110(1)(d) deduction claimed for regular tax —
            added back in FULL, per (h).
        non_capital_loss_deducted: s.111(1) loss carryovers deducted — HALF added
            back, per (i).

    Returns:
        AMT-adjusted taxable income (AMTI).

    Raises:
        ValueError: if ``capital_gains_inclusion`` is not a usable rate. DP#32 —
            a zero inclusion rate would make the gross-up divide by zero, and
            silently treating it as "no gain" would switch off the single
            biggest AMT trigger there is.
    """
    amti = taxable_income

    # (d) 100% capital gains inclusion. `taxable_capital_gains` is the portion
    # already sitting in taxable_income at the regular rate; the add-back is the
    # remainder of the same gain.
    if taxable_capital_gains:
        if not 0 < capital_gains_inclusion <= 1:
            raise ValueError(
                "capital_gains_inclusion must be in (0, 1] to gross taxable "
                f"capital gains up to the AMT's 100% inclusion; got "
                f"{capital_gains_inclusion!r}"
            )
        full_gain = taxable_capital_gains / capital_gains_inclusion
        amti += full_gain - taxable_capital_gains

    # (j)(ii) half of s.20(1)(c)-(f)/(bb) interest and carrying charges
    amti += 0.5 * carrying_charges

    # (j)(i) half of s.8(1) employment deductions
    amti += 0.5 * employment_deductions

    # (h) the whole s.110(1)(d) stock option deduction
    amti += stock_option_deduction

    # (i) half of s.111(1) non-capital loss carryovers
    amti += 0.5 * non_capital_loss_deducted

    return max(0.0, amti)


def compute_amt(
    regular_tax: float,
    adjusted_income: float,
    params: AMTParameters,
    nonrefundable_credits: float = 0.0,
) -> dict[str, float]:
    """Compute AMT liability as a pure function.

    The AMT calculation:
    1. Compute AMTI (adjusted_income parameter, pre-computed)
    2. Subtract AMT exemption (with phaseout for high AMTI)
    3. Apply AMT rate to get the GROSS tentative amount
    4. Reduce it by 50% of eligible non-refundable credits -> the MINIMUM AMOUNT
    5. Compare the minimum amount with regular tax
    6. If minimum > regular tax: pay the minimum, carry the excess forward 7 years
    7. If minimum ≤ regular tax: no AMT surcharge, pay regular tax

    THE 50%-OF-CREDITS REDUCTION (#747, ITA s.127.531; CRA T691). The minimum
    amount is not the gross ``rate x (ATI - exemption)``: the statute allows
    **50% of the taxpayer's eligible non-refundable credits** against it
    (``minimum amount = 20.5% x (ATI - exemption) - 50% x credits``). Passing
    ``nonrefundable_credits`` applies that reduction; the default 0.0 means "no
    credit offset supplied" and reproduces the gross minimum. The wired fold
    passes the same federal non-refundable credits the regular tax was computed
    net of, so the comparison is apples-to-apples (both sides carry the credits).

    Returns dict where:
    - amt_owing: The AMT amount owed (0 if AMT doesn't apply)
    - amt_surcharge: Extra tax due to AMT above regular tax (0 if none)
    - amt_carryforward: Amount carried forward as credit (same as surcharge)

    DP#3: Pure function — same inputs always produce same output.
    DP#8: Parameters passed as data, not read from globals.

    Args:
        regular_tax: Regular federal tax liability after credits and abatement (for AMT comparison)
        adjusted_income: AMT-adjusted taxable income (from amt_adjusted_income)
        params: AMT parameters for the tax year
        nonrefundable_credits: Eligible non-refundable credits; 50% reduces the
            minimum amount (ITA s.127.531). Default 0.0 = no reduction.

    Returns:
        Dict with amt_base, amt_tentative, minimum_amount, amt_owing,
        amt_surcharge, amt_carryforward, carryforward_years, effective_exemption
    """
    # Compute effective exemption (with phaseout for high AMTI)
    effective_exemption = params.exemption
    if params.exemption_phaseout_rate > 0 and adjusted_income > params.exemption_phaseout_start:
        phaseout_amount = (adjusted_income - params.exemption_phaseout_start) * params.exemption_phaseout_rate
        effective_exemption = max(0, params.exemption - phaseout_amount)

    # AMT base: AMTI minus exemption
    amt_base = max(0, adjusted_income - effective_exemption)

    # Gross tentative amount, then the s.127.531 minimum amount net of 50% of
    # eligible non-refundable credits. Floored at 0: the credit offset cannot
    # make the minimum amount negative.
    amt_tentative = amt_base * params.amt_rate
    minimum_amount = max(0.0, amt_tentative - 0.5 * nonrefundable_credits)

    # Compare the minimum amount with regular tax
    if minimum_amount > regular_tax:
        amt_owing = minimum_amount
        amt_surcharge = minimum_amount - regular_tax
    else:
        amt_owing = 0.0
        amt_surcharge = 0.0

    # AMT carryforward: excess AMT paid above regular tax can be carried forward
    # as a credit for up to 7 years -- see carry_forward_amt_credit for the
    # cross-year application that actually recovers it (#747, ITA s.120.2).
    amt_carryforward = amt_surcharge

    return {
        'amt_base': amt_base,
        'amt_tentative': amt_tentative,
        'minimum_amount': minimum_amount,
        'amt_owing': amt_owing,
        'amt_surcharge': amt_surcharge,
        'amt_carryforward': amt_carryforward,
        'carryforward_years': params.carryforward_years,
        'effective_exemption': effective_exemption,
    }


def total_tax_with_amt(
    regular_tax: float,
    taxable_income: float,
    taxable_capital_gains: float = 0,
    capital_gains_inclusion: float = 0.50,
    carrying_charges: float = 0,
    employment_deductions: float = 0,
    stock_option_deduction: float = 0,
    non_capital_loss_deducted: float = 0,
    nonrefundable_credits: float = 0,
    params: AMTParameters = None,
    year: int = 2026,
) -> dict[str, float]:
    """Total federal tax including any AMT surcharge: ``max(regular, minimum)``.

    Combines ``amt_adjusted_income`` and ``compute_amt``. The add-back arguments
    are exactly s.127.52(1)'s list — see ``amt_adjusted_income``. There is no
    ``rrsp_deduction`` parameter, because an RRSP deduction is not an AMT
    preference item (#710); it was removed rather than deprecated (DP#9).

    ``nonrefundable_credits`` reduces the minimum amount by 50% (ITA s.127.531,
    #747); pass the same federal non-refundable credits ``regular_tax`` is net
    of. Default 0 leaves the minimum gross.

    DP#3: Pure function — same inputs always produce same output.

    Args:
        regular_tax: Regular FEDERAL tax after abatement and credits — the
            figure the minimum amount is measured against (CRA T691).
        taxable_income: Taxable income (already net of the RRSP deduction).
        taxable_capital_gains: Taxable (regular-inclusion) capital gains already
            in ``taxable_income``. **This is the dominant AMT trigger.**
        capital_gains_inclusion: Regular inclusion rate behind that figure.
        carrying_charges: s.20(1)(c)-(f)/(bb) interest — half added back.
        employment_deductions: s.8(1) deductions — half added back.
        stock_option_deduction: s.110(1)(d) deduction — added back in full.
        non_capital_loss_deducted: s.111(1) carryovers — half added back.
        params: AMT parameters (loaded for ``year`` when omitted).
        year: Tax year (used if params is None).

    Returns:
        Dict with total_tax, regular_tax, amt_surcharge, amt_carryforward,
        carryforward_years, adjusted_income, amt_details
    """
    if params is None:
        params = AMTParameters.for_year(year)

    # Compute AMTI
    adjusted = amt_adjusted_income(
        taxable_income=taxable_income,
        taxable_capital_gains=taxable_capital_gains,
        capital_gains_inclusion=capital_gains_inclusion,
        carrying_charges=carrying_charges,
        employment_deductions=employment_deductions,
        stock_option_deduction=stock_option_deduction,
        non_capital_loss_deducted=non_capital_loss_deducted,
    )

    # Compute AMT
    amt_result = compute_amt(
        regular_tax=regular_tax,
        adjusted_income=adjusted,
        params=params,
        nonrefundable_credits=nonrefundable_credits,
    )

    total_tax = regular_tax + amt_result['amt_surcharge']

    return {
        'total_tax': total_tax,
        'regular_tax': regular_tax,
        'amt_surcharge': amt_result['amt_surcharge'],
        'amt_carryforward': amt_result['amt_carryforward'],
        'minimum_amount': amt_result['minimum_amount'],
        'carryforward_years': amt_result['carryforward_years'],
        'adjusted_income': adjusted,
        'amt_details': amt_result,
    }


# =============================================================================
# 7-year AMT carry-forward — ITA s.120.2 (issue #747)
# =============================================================================

@dataclass(frozen=True)
class AMTCredit:
    """A minimum-tax credit arising from AMT paid in ``year`` (ITA s.120.2).

    AMT paid in excess of regular tax is not a permanent cost: it becomes a
    credit recoverable against regular tax in a later year, to the extent that
    year's regular tax exceeds its own minimum amount, and it expires 7 years
    after the year it arose. Tracking the year each credit arose is what makes
    the 7-year expiry expressible (DP#28: programs enter AND exit on a schedule).
    """
    year: int
    amount: float


def carry_forward_amt_credit(
    opening,
    current_year: int,
    surcharge_paid: float,
    recoverable_room: float,
    carryforward_years: int = 7,
):
    """Pure fold step for the 7-year AMT carry-forward (ITA s.120.2).

    One year of the cross-year AMT credit balance, computed as a pure function of
    the opening balance and this year's figures (DP#3/DP#26 — state flows through
    the fold, nothing is read off ``self``). The order is exactly the statute's:

    1. **Expire.** A credit that arose in year Y is claimable in Y+1 .. Y+7 and
       is lost after that. Credits older than ``carryforward_years`` are dropped
       before anything is recovered.
    2. **Recover.** In a year where regular tax exceeds the minimum amount, that
       excess (``recoverable_room = max(0, regular_tax - minimum_amount)``,
       s.120.2(3)) can be applied against regular tax, oldest credit first (so a
       credit about to expire is used before a fresher one). The recovered dollar
       amount reduces the year's tax.
    3. **Book.** This year's own AMT surcharge (``surcharge_paid``, the excess of
       the minimum amount over regular tax) becomes a fresh credit for
       ``current_year``. It cannot be recovered this year — a year that PAYS AMT
       has ``recoverable_room == 0`` by construction — so booking last is safe.

    Args:
        opening: iterable of ``AMTCredit`` — the unused credits carried in.
        current_year: the calendar/tax year being folded.
        surcharge_paid: this year's AMT surcharge (0 if AMT did not bite).
        recoverable_room: ``max(0, regular_tax - minimum_amount)`` this year.
        carryforward_years: the expiry window (7, ITA s.120.2).

    Returns:
        ``(recovered, closing)`` — the dollar credit applied to reduce this
        year's tax, and the surviving list of ``AMTCredit`` (oldest first) to
        carry into next year.
    """
    # 1. Expire credits older than the carry-forward window. A credit from year
    # Y survives while current_year - Y <= carryforward_years (claimable through
    # Y+7 inclusive).
    live = [
        c for c in opening
        if c.amount > 0 and (current_year - c.year) <= carryforward_years
    ]

    # 2. Recover oldest-first, capped by this year's recoverable room.
    room = max(0.0, recoverable_room)
    recovered = 0.0
    closing = []
    for credit in sorted(live, key=lambda c: c.year):
        if room <= 0:
            closing.append(credit)
            continue
        taken = min(credit.amount, room)
        recovered += taken
        room -= taken
        leftover = credit.amount - taken
        if leftover > 0:
            closing.append(AMTCredit(credit.year, leftover))

    # 3. Book this year's new surcharge as a fresh credit.
    if surcharge_paid > 0:
        closing.append(AMTCredit(current_year, surcharge_paid))

    return recovered, closing


# =============================================================================
# Quebec impôt minimum de remplacement (IMR) — Revenu Québec TP-776.42 (#747)
# =============================================================================

@dataclass(frozen=True)
class QuebecIMRParameters:
    """Quebec's parallel minimum tax parameters for a tax year (TP-776.42).

    Quebec levies its own *impôt minimum de remplacement*, harmonized with the
    2024 federal AMT reform but with its OWN parameters: a **19%** rate (vs 20.5%
    federal) and a standalone basic exemption that, like the federal one, is
    **not** phased out.

    Unlike the federal exemption — which ITA s.127.51 pegs to the 29%-bracket
    threshold the provider already holds — Quebec's is a standalone indexed
    figure with no bracket anchor. Issue #747 refused to invent an unsourced
    constant. So the SOURCED values (Revenu Québec: $175,000 for 2024, $179,990
    for 2025) are held here, and a later year is DERIVED by indexing the last
    sourced value by Quebec's own annual indexation factor — read as the ratio of
    successive Quebec basic personal amounts the provider holds, which Revenu
    Québec indexes by exactly that factor. That derives the figure from data in
    hand rather than fabricating a constant (DP#2/DP#12/DP#20).
    """
    year: int
    rate: float
    exemption: float

    RATE: float = 0.19
    # Revenu Québec TP-776.42, sourced basic exemptions.
    _SOURCED_EXEMPTION = {2024: 175000.0, 2025: 179990.0}

    @classmethod
    def for_year(cls, year: int, provider=None) -> "QuebecIMRParameters":
        return cls(year=year, rate=cls.RATE,
                   exemption=cls.exemption_for(year, provider))

    @classmethod
    def exemption_for(cls, year: int, provider=None) -> float:
        """The Quebec IMR basic exemption for ``year``.

        Sourced years return the published figure. Later years are indexed
        forward from the last sourced year by Quebec's annual indexation factor
        (the QC basic-personal-amount ratio). Years before the harmonized 2024
        regime raise (DP#32): a pre-reform QC IMR is a different rule we do not
        model, and substituting a plausible number would silently mis-tax it.
        """
        if year in cls._SOURCED_EXEMPTION:
            return cls._SOURCED_EXEMPTION[year]
        earliest = min(cls._SOURCED_EXEMPTION)
        if year < earliest:
            raise ValueError(
                f"No Quebec IMR basic exemption for {year}: the harmonized IMR "
                f"regime begins {earliest} (Revenu Québec TP-776.42); a pre-reform "
                f"figure is not modelled and will not be invented."
            )
        if provider is None:
            from tax_data import TaxDataProvider
            provider = TaxDataProvider()
        base_year = max(cls._SOURCED_EXEMPTION)
        exemption = cls._SOURCED_EXEMPTION[base_year]
        for y in range(base_year + 1, year + 1):
            exemption *= _quebec_indexation_factor(y, provider)
        return exemption


def _quebec_indexation_factor(year: int, provider) -> float:
    """Quebec's annual personal-tax indexation factor for ``year``, read as the
    ratio of the Quebec basic personal amount for ``year`` to that of ``year-1``.

    Revenu Québec indexes the basic personal amount and the IMR exemption by the
    same annual factor, so this ratio IS that factor — derived from data the
    provider holds, not a hardcoded constant (DP#12). Raises (DP#32) if either
    year's Quebec BPA is missing rather than assuming no indexation.
    """
    this_year = provider._load_year(year, "canada", "quebec").basic_personal_amount
    prior_year = provider._load_year(year - 1, "canada", "quebec").basic_personal_amount
    if not this_year or not prior_year:
        raise ValueError(
            f"Cannot derive the Quebec indexation factor for {year}: missing the "
            f"Quebec basic personal amount for {year} or {year - 1}."
        )
    return this_year / prior_year


def compute_quebec_imr(
    regular_qc_tax: float,
    adjusted_income: float,
    params: QuebecIMRParameters,
    nonrefundable_credits: float = 0.0,
) -> dict[str, float]:
    """Quebec IMR surcharge as a pure function — the provincial mirror of
    ``compute_amt`` (TP-776.42).

    The Quebec minimum amount is ``19% x (AMTI - QC exemption)``, reduced by 50%
    of eligible Quebec non-refundable credits, and the household pays
    ``max(regular QC tax, that minimum)``. This books only the SURCHARGE
    (minimum - regular QC tax), separate from the federal AMT surcharge because
    they are two distinct taxes measured against two distinct regular taxes.

    Args:
        regular_qc_tax: Regular Quebec provincial tax after QC credits — the
            figure the QC minimum is measured against.
        adjusted_income: AMTI (same s.127.52-style base as the federal side;
            Quebec harmonized its base with the federal reform).
        params: Quebec IMR parameters for the year.
        nonrefundable_credits: eligible QC non-refundable credits; 50% reduces
            the QC minimum amount. Default 0.0 = no reduction.

    Returns:
        Dict with imr_base, imr_minimum, imr_surcharge, imr_carryforward.
    """
    imr_base = max(0.0, adjusted_income - params.exemption)
    imr_minimum = max(0.0, imr_base * params.rate - 0.5 * nonrefundable_credits)
    if imr_minimum > regular_qc_tax:
        imr_surcharge = imr_minimum - regular_qc_tax
    else:
        imr_surcharge = 0.0
    return {
        'imr_base': imr_base,
        'imr_minimum': imr_minimum,
        'imr_surcharge': imr_surcharge,
        'imr_carryforward': imr_surcharge,
    }
