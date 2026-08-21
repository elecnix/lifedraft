#!/usr/bin/env python3
"""Rental-property income — net rental income + ITA s.20(1)(c) interest deduction.

Epic #690 bite 2 (DP#10/DP#25): the tax law that governs a rental property's
income is Canadian, so it lives in the Canada jurisdiction module, not in the
generic fold (``simulation.py``). The fold keeps only the household-structure
PLUMBING — reading each ``kind="rental"`` property's declared facts off the
internal config, resolving the owner to a taxed role, and per-role accumulation.
None of that is tax law.

The tax law this module encodes (issue #693):

- **Net rental income is ordinary income (CRA form T776).** Gross rent, less the
  operating expenses incurred to earn it, is the NET rental income, and it is
  fully taxable at the owner's marginal rate — no dividend gross-up, no
  capital-gains 50% inclusion, the same treatment as employment income.

- **Mortgage interest on an income-producing property is deductible (ITA
  s.20(1)(c)).** Money borrowed to acquire a rental property is borrowed to earn
  income, so its interest reduces net rental income. This is the structural fact
  that makes a rental interesting: the SAME household's borrowing to fund an
  RRSP/TFSA is expressly NON-deductible (ITA s.18(11)), while borrowing for a
  rental is deductible. The engine does not re-invent this distinction — it reads
  the ruling straight out of ``debt.is_interest_deductible`` for
  ``DebtPurpose.RENTAL_EXPENSE`` (DP#9: one spelling of the s.20(1)(c) rule, not
  two), so a future change to that rule flows here automatically.

CCA / recapture (#694), the per-year principal-residence exemption (#695), a
mid-horizon purchase (#696) and short-term-rental business-income treatment
(#697) are later bites. This module deliberately does NOT depreciate: net rental
income here can go negative (a deductible rental loss), and CCA — which by CRA
rule cannot CREATE or deepen a rental loss — plugs in on top without restructuring
this net-income figure.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — ITA s.20(1)(c), CRA T776
    ITA s.20(1)(c) — interest deductibility (income-producing use)
    CRA form T776 — Statement of Real Estate Rentals (net rental income)
"""

from dataclasses import dataclass

from countries.canada.debt import DebtPurpose, is_interest_deductible


@dataclass(frozen=True)
class RentalIncomeEffect:
    """The ITA classification of ONE rental property's income for ONE tax year.

    ``operating_income`` is gross rent minus operating expenses (the T776 figure
    BEFORE financing); ``deductible_interest`` is the mortgage interest the ITA
    s.20(1)(c) ruling admits against it. Every figure is at the WHOLE-property
    level — the fold applies the owner's share and role when it attributes these
    to a taxed member.
    """
    gross_rent: float
    operating_expenses: float
    deductible_interest: float

    @property
    def operating_income(self) -> float:
        """Gross rent less operating expenses — net rental income BEFORE the
        s.20(1)(c) interest deduction (CRA T776 line before financing costs)."""
        return self.gross_rent - self.operating_expenses

    @property
    def net_rental_income(self) -> float:
        """The taxable net rental income: gross rent less operating expenses
        less deductible mortgage interest. Ordinary income at the marginal rate;
        may be negative (a deductible rental loss)."""
        return self.operating_income - self.deductible_interest


def classify_rental_income(gross_rent_annual: float,
                           expenses_annual: float,
                           mortgage_interest_annual: float) -> RentalIncomeEffect:
    """Classify one rental property's income for one tax year under the ITA.

    ``gross_rent_annual`` and ``expenses_annual`` are the declared T776 facts;
    ``mortgage_interest_annual`` is the interest paid this year on debt secured
    against the property (``0.0`` for a mortgage-free rental). The interest is
    admitted only to the extent ITA s.20(1)(c) allows it — read straight out of
    ``debt.is_interest_deductible(DebtPurpose.RENTAL_EXPENSE)`` (deductible at
    proportion 1.0 for an income-producing rental) rather than restated here, so
    the deductibility fact has exactly one home (DP#9).
    """
    ruling = is_interest_deductible(DebtPurpose.RENTAL_EXPENSE)
    deductible_interest = (
        mortgage_interest_annual * ruling['proportion']
        if ruling['deductible'] else 0.0
    )
    return RentalIncomeEffect(
        gross_rent=gross_rent_annual,
        operating_expenses=expenses_annual,
        deductible_interest=deductible_interest,
    )


def net_rental_income(gross_rent_annual: float,
                      expenses_annual: float,
                      mortgage_interest_annual: float) -> float:
    """Taxable net rental income = gross rent − operating expenses − deductible
    mortgage interest (ITA s.20(1)(c)). Pure convenience over
    :func:`classify_rental_income` for callers that need only the scalar."""
    return classify_rental_income(
        gross_rent_annual, expenses_annual, mortgage_interest_annual
    ).net_rental_income
