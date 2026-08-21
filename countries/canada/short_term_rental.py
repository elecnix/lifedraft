#!/usr/bin/env python3
"""Short-term rental (Airbnb-style) — business-income treatment, the GST/HST
small-supplier threshold, and a Quebec/Montreal jurisdictional legality gate.

Epic #690 bite 6 (DP#10/DP#25): a short-term rental (STR) is a DIFFERENT animal
from the long-term rental of #693, both fiscally and legally, and every rule
that makes it different is Canadian (Quebec/Montreal), so it lives in the Canada
jurisdiction module — never in the generic fold (``simulation.py``) and never
restated in the contract adapter, which only PLUMBS the household structure and
CALLS the rulings here (DP#25).

What this module encodes (issue #697):

- **Business income, not property income.** Short-stay accommodation with
  material services (cleaning between stays, linens) shifts CRA's
  characterization from PASSIVE property income (#693, CRA form T776) to ACTIVE
  business income (ITA s.9). The NET arithmetic — gross rent less operating
  expenses less deductible interest — is the SAME figure #693 already computes
  (so it is NOT re-spelled here, DP#9: :func:`classify_str_income` delegates the
  net to :func:`rental_income.classify_rental_income`); what differs is the
  *classification* (``income_type == "business_income"``) and the obligations
  that ride on it.

- **GST/HST/QST registration above the $30,000 small-supplier threshold.**
  Short-term accommodation is a TAXABLE supply (unlike the EXEMPT supply of
  long-term residential rent), so a host whose taxable revenue EXCEEDS the
  $30,000/year small-supplier threshold (ETA s.148) must register for and remit
  GST/HST (and QST in Quebec) — a filing obligation a plain long-term rental
  never has. This module flags the obligation; it does not compute the remittance
  (the tax is collected from the guest, not borne by the host).

- **A jurisdictional legality gate (the headline safety rule).** A tool whose
  product is a number the user cannot independently verify MUST NOT model an
  illegal activity as a routine, always-available income stream (AGENTS.md: "a
  plausible answer from absent data is worse than a crash"). So a declared STR
  must state its municipality/borough AND its registration status, and the engine
  REFUSES (loud, DP#32 — never a silent "assume legal") to model positive STR
  income where it cannot confirm the activity is permitted:

    * three Montreal boroughs FULLY BAN short-term rental (Lachine,
      Saint-Laurent, Saint-Léonard) — an STR there is refused outright;
    * a Quebec STR requires CITQ (Corporation de l'industrie touristique du
      Québec) registration — an STR without a declared CITQ registration is
      refused, because the tool cannot confirm the use is permitted.

  This banned-borough set is a POINT-IN-TIME snapshot of third-party summaries
  (see references), deliberately a SHORT, cited, refusing list rather than a
  silent allowlist that rots: an unlisted borough is NOT asserted "legal", it is
  admitted only when a CITQ registration is declared, and the ``jurisdiction``
  the household states is carried through for audit. A production system should
  replace this snapshot with fetched, cached, current Ville de Montréal zoning
  bylaws + the official CITQ/RBQ registry (DP#12).

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — ITA s.9 (business income),
        ETA s.148 (GST/HST small-supplier threshold), Loi 67 / CITQ registration
    Ville de Montréal borough zoning bylaws (STR prohibition) — point-in-time
    Sources (web search, current as of issue #697 filing):
        https://silaws.com/2026/05/07/airbnb-short-term-rental-quebec-legal-guide-2026-en/
        https://www.guestable.com/blog/montreal-short-term-rental-regulations/
        https://reserver.ca/en/blog/loi-67-citq
"""

from dataclasses import dataclass

from countries.canada.rental_income import (
    RentalIncomeEffect,
    classify_rental_income,
)

#: ETA s.148 small-supplier threshold — a host whose taxable supplies EXCEED
#: this in a year must register for GST/HST (and QST in Quebec). Stable since
#: the GST's 1991 introduction.
GST_HST_SMALL_SUPPLIER_THRESHOLD = 30000.0

#: Montreal boroughs that FULLY BAN short-term rental. A point-in-time snapshot
#: of third-party summaries (issue #697), NOT an authoritative zoning map — a
#: short, cited, REFUSING list (DP#12: a production system fetches the current
#: Ville de Montréal bylaws instead). An unlisted borough is not asserted legal;
#: it is admitted only with a declared CITQ registration (see
#: :func:`assess_str_legality`).
STR_BANNED_JURISDICTIONS = frozenset({
    "montreal_lachine",
    "montreal_saint_laurent",
    "montreal_saint_leonard",
})


class ShortTermRentalNotPermitted(ValueError):
    """Raised when the engine refuses to model an STR the tool cannot confirm is
    legal — a zoning-banned borough, or an STR with no CITQ registration. A loud
    refusal (DP#32), never a silent "assume legal" that would hand the user a
    confident return on an illegal activity."""


@dataclass(frozen=True)
class ShortTermRentalEffect:
    """The ITA classification of ONE short-term rental's income for ONE tax year.

    ``net_rental`` is the SAME net-income arithmetic a long-term rental produces
    (gross − expenses − deductible interest, computed by #693's
    :func:`rental_income.classify_rental_income` — one home for that formula,
    DP#9). What THIS type adds is the STR-specific treatment: the income is ACTIVE
    business income (ITA s.9), not passive property income, and its gross revenue
    is tested against the GST/HST small-supplier threshold.
    """
    net_rental: RentalIncomeEffect
    gross_rent: float
    #: ITA s.9 — active business income, not passive property income (T776).
    income_type: str = "business_income"

    @property
    def net_business_income(self) -> float:
        """Taxable net business income = gross rent − operating expenses −
        deductible interest. Numerically the SAME as a long-term rental's net
        income; its CLASSIFICATION (business, s.9) is what differs."""
        return self.net_rental.net_rental_income

    @property
    def gst_hst_registration_required(self) -> bool:
        """True when gross STR revenue EXCEEDS the $30,000 small-supplier
        threshold (ETA s.148) — the host must register for and remit GST/HST/QST.
        A long-term residential rental (an EXEMPT supply) never triggers this."""
        return self.gross_rent > GST_HST_SMALL_SUPPLIER_THRESHOLD


def classify_str_income(gross_rent_annual: float,
                        expenses_annual: float,
                        mortgage_interest_annual: float) -> ShortTermRentalEffect:
    """Classify one short-term rental's income for one tax year under the ITA.

    The net-income figure is delegated to #693's
    :func:`rental_income.classify_rental_income` (DP#9: the gross − expenses −
    deductible-interest arithmetic and the s.20(1)(c)/business interest
    deductibility have exactly one home). This function adds only what makes an
    STR different: the business-income classification (ITA s.9) and the GST/HST
    small-supplier test on gross revenue.
    """
    net = classify_rental_income(
        gross_rent_annual, expenses_annual, mortgage_interest_annual)
    return ShortTermRentalEffect(net_rental=net, gross_rent=gross_rent_annual)


@dataclass(frozen=True)
class StrLegality:
    """The legality ruling for one declared STR: whether the engine may model
    positive income for it, and the human-readable reason either way."""
    permitted: bool
    reason: str


def assess_str_legality(jurisdiction: str,
                        citq_registered: bool) -> StrLegality:
    """Rule whether a declared STR may be modelled as a legal income stream.

    Refuses (``permitted=False``) when the borough is on the fully-banned list,
    or when no CITQ registration is declared (the tool cannot CONFIRM the use is
    permitted). Admits it only when a CITQ registration is declared AND the
    borough is not banned — never a silent "assume legal" for an unlisted,
    unregistered jurisdiction (DP#32)."""
    if jurisdiction in STR_BANNED_JURISDICTIONS:
        return StrLegality(
            permitted=False,
            reason=(f"short-term rental is zoning-banned in '{jurisdiction}' "
                    f"(Montreal borough full prohibition)"),
        )
    if not citq_registered:
        return StrLegality(
            permitted=False,
            reason=(f"cannot confirm '{jurisdiction}' permits short-term rental: "
                    f"no CITQ registration declared (Loi 67 requires it)"),
        )
    return StrLegality(
        permitted=True,
        reason=(f"CITQ-registered and '{jurisdiction}' is not on the "
                f"banned-borough list"),
    )


def require_str_permitted(jurisdiction: str, citq_registered: bool) -> None:
    """Enforce the legality gate: raise :class:`ShortTermRentalNotPermitted`
    unless :func:`assess_str_legality` admits the STR. The single loud refusal
    point the contract adapter calls before it will map any STR income (DP#25:
    the RULE lives here, the adapter only invokes it)."""
    legality = assess_str_legality(jurisdiction, citq_registered)
    if not legality.permitted:
        raise ShortTermRentalNotPermitted(
            f"Refusing to model short-term-rental income: {legality.reason}. "
            f"This tool will not present a confident return on an activity it "
            f"cannot confirm is legal (declare a permitting jurisdiction and a "
            f"CITQ registration, or model it as a long-term rental instead)."
        )
