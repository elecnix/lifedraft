"""First-home buyers' tax credits (issue #368).

A household can declare a first-home purchase through ``first_home_purchases[]``
or buy a principal residence through ``properties[].purchase``. Until now the
engine moved only the FHSA qualifying withdrawal and the Home Buyers' Plan
from such a purchase -- **none of the tax credits were booked**, so every
first-time buyer paid too much tax in the purchase year. For a Quebec
household in 2024 that is up to **$2,652.50** of overstated cash; from 2026 a
further up to **$5,875** of refundable relief.

All three credits share one trigger, one eligibility test and one cap, so they
live together here rather than being re-spelled by the federal and Quebec
aggregators (DP#10).

Verified statutory values, from primary sources:

* **Federal Home Buyers' Amount**, CRA line 31270 / ITA s.118.05(3).
  $10,000 for 2022 and later ($5,000 before), claimed at the year's lowest
  federal rate, non-refundable. First-time means neither the buyer **nor the
  spouse** lived in a home they owned in the purchase year or the four
  preceding years. **The credit cannot be split unless both spouses are
  eligible.**
  https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-31270-home-buyers-amount.html

* **Quebec home buyers' tax credit**, TP-1 line 396 (form TP-752.HA-V).
  Maximum $1,400 per qualifying home, non-refundable, limited to Quebec tax
  otherwise payable, splittable among the eligible claimants.
  https://www.revenuquebec.ca/documents/en/formulaires/tp/TP-752.HA-V(2024-10).pdf

* **Quebec refundable credit for access to homeownership**, Ministère des
  Finances bulletin 2026-2, from the 2026 taxation year. 100% of the first
  $5,000 of municipal transfer duties plus 25% of the next $3,500 (maximum
  $5,875), reduced by 2.35% of the basis of imposition above $750,000 so it
  is nil at $1,000,000. Paid even when Quebec tax is zero.
  https://cdn-contenu.quebec.ca/cdn-contenu/adm/min/finances/publications-adm/Bulletins/EN/BULEN_2026-2.pdf

DP#3: pure functions. DP#20: every amount is year-versioned data, read from the
provider -- no rate or ceiling is hardcoded here.
"""
from __future__ import annotations

from typing import Optional

from tax_data import TaxDataProvider

# The CRA and the Quebec bulletin both test the year of acquisition plus the
# FOUR preceding calendar years, so the window spans five. Named for the
# lookback rather than the span, because "four preceding years" is the
# statutory phrase -- but the window below is deliberately year+1.
FIRST_TIME_LOOKBACK_YEARS = 4

# The CRA lists the federal amount as $5,000 before 2022 and $10,000 from 2022.
# For those earlier years there is no year record to carry a value, so the
# legislated pre-2022 amount is stated here rather than invented per call.
PRE_2022_HOME_BUYERS_AMOUNT = 5_000.0


def _federal_record(year: int, provider: Optional[TaxDataProvider]):
    if provider is None:
        provider = TaxDataProvider()
    return provider.get_year_data(year, "canada", "federal")


def _is_quebec(province: Optional[str]) -> bool:
    return province is not None and province.lower() in ("quebec", "qc")


def _quebec_record(year: int, provider: TaxDataProvider):
    """The Quebec year record, for either the long or the short province key.

    The contract accepts both ``quebec`` and ``qc``, and the package registers
    the short form as an alias, so both already resolve. Normalising here keeps
    one spelling at the call sites; it is tidiness, not a fix.
    """
    return provider.get_year_data(year, "canada", "quebec")


def lowest_federal_rate(year: int,
                        provider: Optional[TaxDataProvider] = None) -> float:
    """The year's lowest federal rate -- the rate the CRA credits the amount at.

    Read from ``federal_brackets[0].rate`` rather than hardcoded: the repo
    already carries it year-versioned, and it moves (0.15 for 2023/2024, 0.145
    for 2025, 0.14 for 2026). A literal here would be wrong the year it changed.
    """
    record = _federal_record(year, provider)
    brackets = getattr(record, "federal_brackets", None)
    if not brackets:
        raise ValueError(
            f"No federal brackets are registered for {year}, so the home "
            f"buyers' amount cannot be priced. Load the year-versioned federal "
            f"record rather than assuming a rate (DP#32)."
        )
    return float(brackets[0].rate)


def home_buyers_amount_for_year(year: int,
                                provider: Optional[TaxDataProvider] = None) -> float:
    """The federal DOLLAR amount claimable for a qualifying home in ``year``.

    $5,000 before 2022, $10,000 from 2022.

    The YEAR decides, not the record. The provider resolves an unregistered
    year to the NEAREST row, so a 2021 query carries back the 2023 record and
    would otherwise report the post-2022 $10,000 for a year the CRA legislated
    at half that. Deciding on the year first is what stops the carry-forward
    from silently crediting an amount that did not exist.
    """
    if year < 2022:
        return PRE_2022_HOME_BUYERS_AMOUNT
    amount = float(_federal_record(year, provider).home_buyers_amount)
    if amount <= 0:
        # A year whose record carries no amount is a DATA GAP, not a zero
        # credit: the CRA has legislated $10,000 for every year from 2022, so
        # an empty field means the record was not populated. Answering 0 would
        # tell a first-time buyer they get nothing when the statute says they
        # get $10,000, with nothing to say so (DP#32).
        raise ValueError(
            f"No federal home buyers' amount is registered for {year}. The "
            f"federal record must carry home_buyers_amount (CRA line 31270 / "
            f"ITA s.118.05(3)) for {year}; returning 0 here would silently "
            f"deny a first-time buyer their credit (DP#32)."
        )
    return amount


def federal_home_buyers_amount(
    year: int,
    province: Optional[str] = None,
    claimed_amount: Optional[float] = None,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """The federal home buyers' amount, as a reduction of FEDERAL tax.

    ``claimed_amount`` is the dollar amount this claimant claims for the home
    (the full year's amount, or a share when both spouses are eligible and the
    claim is split). The CRA caps the total across all claimants for one home
    at the year's maximum; the cap is enforced by the caller, which knows how
    many claimants there are.

    **A Quebec resident's federal credit is reduced by the provincial
    abatement.** The CRA computes the refundable Quebec abatement on line
    42900 -- *basic* federal tax, i.e. after the federal non-refundable
    credits (ITA s.120(4)). A federal credit therefore shrinks the 16.5%
    abatement by 16.5% of the credit, so the credit is worth
    ``rate x (1 - 0.165)`` here, not ``rate``. This is the same statutory
    effect issue #350 fixed for the credit ordering, and the 2024 figure the
    issue states ($1,252.50 = 10,000 x 15% x 0.835) depends on it.

    Returns 0 for a household that declared no purchase (the caller simply
    does not invoke this), and 0 outside Quebec's abatement.
    """
    if claimed_amount is None:
        claimed_amount = home_buyers_amount_for_year(year, provider)
    if claimed_amount <= 0:
        return 0.0
    # The CRA caps the total across ALL eligible claimants for one home at the
    # year's maximum (s.118.05(4)). Enforcing it here rather than trusting the
    # caller is what stops a household claiming $15,000 and being credited
    # 150% of the credit -- a real over-credit, not a theoretical one.
    claimed_amount = min(float(claimed_amount),
                         home_buyers_amount_for_year(year, provider))
    # No second guard after the cap: `home_buyers_amount_for_year` RAISES
    # rather than returning 0 for a year it has no amount for, so a capped
    # value of zero is unreachable. An earlier version guarded it anyway, and
    # the coverage gate correctly reported the branch as uncovered -- dead
    # code shaped like a safety net.
    credit = claimed_amount * lowest_federal_rate(year, provider)

    if _is_quebec(province):
        if provider is None:
            provider = TaxDataProvider()
        try:
            abatement = float(_quebec_record(year, provider).provincial_abatement)
        except (ValueError, IndexError, AttributeError):
            # DP#32: with no province record we cannot know the abatement.
            # Returning the UN-abatemented figure would overstate a Quebec
            # resident's credit by 16.5%, and returning zero would understate
            # it. Refuse rather than guess -- the caller can report it.
            raise ValueError(
                f"No Quebec record for {year}, so the provincial abatement on "
                f"the federal home buyers' amount cannot be determined. Load "
                f"the year-versioned Quebec record rather than crediting the "
                f"un-abatemented amount (DP#32)."
            )
        credit *= (1.0 - abatement)
    return credit


def quebec_home_buyers_credit(
    year: int,
    quebec_tax_payable: float = 0.0,
    claimed_amount: Optional[float] = None,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """The Quebec home buyers' tax credit (TP-1 line 396).

    A **non-refundable** maximum per qualifying home, limited to Quebec tax
    otherwise payable -- so the credit can never exceed what this claimant
    actually owes. Splittable among the eligible claimants for the same home;
    ``claimed_amount`` is this claimant's share of the year's maximum.

    $1,400 for 2024 (10,000 x 14%), read from the Quebec record.
    """
    if provider is None:
        provider = TaxDataProvider()
    # DP#32: the line 396 credit has existed since 2018, so an absent maximum
    # is a DATA GAP, not a genuine zero -- unlike the 2026 refundable credit
    # below, which really does not exist for earlier years. An earlier
    # version conflated the two and answered $0 either way, which would deny
    # a Quebec first-time buyer their $1,400 with nothing to say so.
    try:
        maximum = float(_quebec_record(year, provider).qc_home_buyers_credit_max)
    except (ValueError, IndexError, AttributeError) as exc:
        raise ValueError(
            f"No Quebec home buyers' credit maximum is registered for {year}. "
            f"The Quebec record must carry qc_home_buyers_credit_max (TP-1 "
            f"line 396, form TP-752.HA-V) for {year}; returning 0 here would "
            f"silently deny a Quebec first-time buyer their credit (DP#32)."
        ) from exc
    if maximum <= 0:
        raise ValueError(
            f"The Quebec home buyers' credit maximum for {year} is "
            f"{maximum!r}, which is not a valid maximum. The credit has "
            f"existed since 2018, so an empty value is a data gap rather than "
            f"'this household qualifies for nothing' (DP#32)."
        )

    # A zero SHARE and a nil TAX LIABILITY are genuine zeros -- this claimant
    # claims nothing, or owes nothing for the credit to offset.
    if quebec_tax_payable <= 0:
        return 0.0
    share = maximum if claimed_amount is None else min(float(claimed_amount), maximum)
    if share <= 0:
        return 0.0
    # Non-refundable: bounded by the tax this claimant actually owes
    # (form TP-752.HA-V line 5.3 -- "whichever is less").
    return min(share, max(0.0, quebec_tax_payable))


def quebec_homeownership_refundable_credit(
    year: int,
    transfer_duties: float = 0.0,
    duty_basis: float = 0.0,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """The Quebec refundable credit for access to homeownership (2026+).

    **Refundable**, so unlike the two non-refundable credits it is paid even
    when Quebec tax is zero.

        credit = 100% of the first $5,000 of municipal transfer duties
               + 25%  of the next $3,500          (maximum $5,875)

    then reduced by ``2.35%`` of the duty basis above ``$750,000``, so the
    credit is nil once the basis reaches $1,000,000.

    Every band, rate and threshold is year-versioned data. A year with no
    populated bands means the credit does not apply (it starts in 2026), which
    is a genuine zero rather than a missing one.
    """
    if provider is None:
        provider = TaxDataProvider()
    try:
        record = _quebec_record(year, provider)
    except (ValueError, IndexError, AttributeError):
        return 0.0

    full_band = float(record.qc_homeownership_credit_full_rate_band)
    if full_band <= 0:
        # A GENUINE zero, not a gap: the refundable credit begins with the
        # 2026 taxation year, so every earlier year legitimately carries 0.
        # Distinguishing "the program does not exist" from "the record is
        # missing" by the value alone is impossible here, and the programme
        # boundary is the only reading that is right for a pre-2026 year.
        return 0.0

    partial_band = float(record.qc_homeownership_credit_partial_band)
    partial_rate = float(record.qc_homeownership_credit_partial_rate)
    reduction_rate = float(record.qc_homeownership_credit_reduction_rate)
    threshold = float(record.qc_homeownership_credit_reduction_threshold)

    duties = max(0.0, float(transfer_duties))
    credit = min(duties, full_band)                       # 100% of the first band
    excess = duties - full_band
    if excess > 0 and partial_band > 0:
        credit += min(excess, partial_band) * partial_rate   # 25% of the next band

    if duty_basis > threshold > 0:
        credit -= (duty_basis - threshold) * reduction_rate
    return max(0.0, credit)


def is_first_home_buyer(
    year: int,
    buyer_prior_home_years: Optional[set] = None,
    spouse_prior_home_years: Optional[set] = None,
) -> bool:
    """First-time-buyer test over the statutory four-year window.

    The CRA (s.118.05(3)) and the Quebec bulletin (2026-2) both test the
    **spouse** as well as the buyer: neither may have lived in a home they
    owned in the year of acquisition or the four preceding years.
    ``countries/canada/fhsa.py``'s ``FHSA.is_first_home_buyer`` is an FHSA
    eligibility predicate and does not carry the spouse test, so this is a
    separate, wider predicate rather than a reuse.

    The two histories are SEPARATE parameters on purpose. An earlier version
    took ``birth_year`` and ``spouse_birth_year``, read neither, and tested a
    single merged set -- two dead parameters promising a spouse test the code
    never performed. A caller who passed only their own history would have
    silently qualified. Taking them separately makes the spouse half of the
    test impossible to omit.

    Each history is the set of calendar years in which that person declares a
    prior owned-and-occupied home. **Absence is not evidence**: ``None`` means
    "not declared", which is not the same as "declared none", so an
    undeclared history qualifies rather than refusing on a datum nobody
    supplied. The caller is expected to report that difference rather than
    present it as verified.
    """
    window = set(range(year - FIRST_TIME_LOOKBACK_YEARS, year + 1))
    for history in (buyer_prior_home_years, spouse_prior_home_years):
        if history and (set(history) & window):
            return False
    return True