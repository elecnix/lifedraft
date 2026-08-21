#!/usr/bin/env python3
"""Capital Cost Allowance (CCA) on a rental property — depreciation with a
delayed trigger (issue #694, epic #690 bite 3).

Epic #690 bite 3 (DP#10/DP#25): the tax law that governs depreciation of a
rental building is Canadian, so it lives in the Canada jurisdiction module, not
the generic fold (``simulation.py``). The fold keeps only the household-structure
PLUMBING — reading each rental property's declared CCA election off the internal
config, tracking the running UCC in ``jurisdiction_state['canada']``, and
splitting the year's claim to each taxed owner. None of that is tax law.

The tax law this module encodes (issue #694), grounded in CRA's published rules
(Canada.ca "Capital cost allowance for rental property" / ITA s.13, s.20(1)(a)):

- **CCA is a DECLINING-BALANCE deduction against rental income.** A residential
  rental building is typically Class 1 at a **4% declining-balance rate**, but
  the correct class depends on facts this tool does not collect (build type,
  acquisition date, accelerated-investment eligibility), so the rate is
  CONFIGURATION (``cca.rate``), never a hardcoded 4% (DP#2/DP#12).

- **The half-year rule** applies in the year of acquisition: only 50% of a net
  addition to the class is eligible for CCA that first year.

- **CCA cannot CREATE or DEEPEN a rental loss** — it may reduce net rental income
  to zero but never push it negative. So the claim is capped at the net rental
  income BEFORE CCA, not a free-standing deduction (this is the whole asymmetry
  with the s.20(1)(c) interest deduction in ``rental_income``, which CAN run the
  rental to a loss).

- **CCA is a NON-CASH deduction.** It lowers TAXABLE income (and so the tax
  bill) without consuming cash — that is the deferral benefit. The cost comes
  due at disposition.

- **Recapture on disposition is fully taxable ORDINARY income (100% inclusion)**
  — NOT the 50% capital-gains inclusion. If the proceeds (up to the original
  capital cost) exceed the remaining undepreciated capital cost (UCC), the
  difference is recaptured as ordinary income (ITA s.13(1)). This is the single
  largest asymmetry: **recapture is taxed WORSE than the capital gain on the
  same property**, because it has no 50% inclusion. A rental held at death is
  deemed disposed at FMV (ITA s.70(5)), so recapture applies at the estate too,
  not only on an arm's-length sale.

- A **terminal loss** (ITA s.20(16)) is the symmetric case: UCC remaining above
  the proceeds when the class empties out is deductible against ordinary income.

Any capital APPRECIATION above the original capital cost is an ordinary capital
gain (50% inclusion), computed from the ACB — a DIFFERENT base than the UCC — and
must NOT be conflated with the recapture. ``recapture_on_disposition`` returns
the two separately; the estate already taxes the whole (FMV − ACB) gain via
``estate.compute_estate``, so the wiring consumes only ``recapture`` /
``terminal_loss`` from here and leaves the capital-gain half to that path (DP#9:
one spelling of the capital-gains rule).

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — ITA s.13, s.20(1)(a), s.20(16)
    ITA s.13(1) — recapture of CCA on disposition (100% ordinary income)
    ITA s.20(16) — terminal loss
    CRA — Capital cost allowance (CCA) for rental property (Class 1, 4%)
"""


def cca_claim(ucc_opening: float, rate: float,
              net_rental_income_before_cca: float,
              is_acquisition_year: bool = False) -> float:
    """The CCA a rental property may claim for one tax year (ITA s.20(1)(a)).

    Declining balance: the maximal claim is ``rate`` of the opening UCC, halved
    in the acquisition year (the half-year rule on a net addition). CRA lets a
    taxpayer claim ANY amount up to that maximum, but the binding constraint
    here is that **CCA cannot create or deepen a rental loss** — the claim is
    capped at the net rental income before CCA (``max(0, ...)``), so a rental
    running at a loss claims ZERO (not a negative "deduction", and never a
    loss-widening one). Returns ``0.0`` for an empty/negative class or a
    non-positive rate (nothing to depreciate).

    Args:
        ucc_opening: undepreciated capital cost at the start of the year (>= 0).
        rate: declining-balance CCA rate (e.g. 0.04 for Class 1), config not
            hardcoded (DP#2/DP#12).
        net_rental_income_before_cca: gross rent − operating expenses −
            deductible interest (the T776 figure BEFORE CCA); the loss-widening
            guard caps the claim at ``max(0, this)``.
        is_acquisition_year: True in the year the property is acquired, applying
            the half-year rule (only 50% of the class is eligible).

    Returns:
        The CCA claimed this year (>= 0), never exceeding the loss-widening cap.
    """
    if ucc_opening <= 0.0 or rate <= 0.0:
        return 0.0
    eligible_base = ucc_opening * (0.5 if is_acquisition_year else 1.0)
    maximal = eligible_base * rate
    room = max(0.0, net_rental_income_before_cca)  # CCA cannot create a loss
    return min(maximal, room)


def ucc_after_claim(ucc_opening: float, cca_claimed: float) -> float:
    """The undepreciated capital cost carried to next year: the running
    declining-balance ledger (DP#19 — track cost basis from day one). The claim
    reduces the class pool dollar-for-dollar."""
    return ucc_opening - cca_claimed


def recapture_on_disposition(proceeds: float, original_capital_cost: float,
                             ucc_remaining: float) -> dict:
    """The CCA consequences of disposing of a rental property (ITA s.13(1) /
    s.20(16)).

    A disposition (arm's-length sale OR the ITA s.70(5) deemed disposition at
    death) settles the class. Three mutually-informing figures come out, from
    two DIFFERENT bases that must not be conflated:

    - ``recapture``: previously-claimed CCA clawed back as ORDINARY income
      (100% inclusion). Proceeds are counted only up to the original capital
      cost for this purpose; the excess over cost is a capital gain, not
      recapture. ``recapture = max(0, min(proceeds, cost) − UCC)``.
    - ``terminal_loss``: the symmetric case — UCC still above the (cost-capped)
      proceeds when the class empties, deductible against ordinary income.
      ``terminal_loss = max(0, UCC − min(proceeds, cost))``. Recapture and
      terminal loss are mutually exclusive (at most one is non-zero).
    - ``capital_gain``: appreciation ABOVE the original capital cost
      (``max(0, proceeds − cost)``), a CAPITAL gain (50% inclusion), computed
      here only for completeness/symmetry. The estate path already taxes the
      whole (FMV − ACB) capital gain, so the WIRING must NOT re-add this — it
      would double-count (DP#9). Consume ``recapture``/``terminal_loss`` only.

    Args:
        proceeds: disposition proceeds (FMV at death for a deemed disposition).
        original_capital_cost: the building's capital cost — the recapture
            ceiling (recapture never exceeds total CCA ever claimed).
        ucc_remaining: undepreciated capital cost at disposition.

    Returns:
        ``{'recapture', 'terminal_loss', 'capital_gain'}`` — all >= 0.
    """
    proceeds_for_recapture = min(proceeds, original_capital_cost)
    recapture = max(0.0, proceeds_for_recapture - ucc_remaining)
    terminal_loss = max(0.0, ucc_remaining - proceeds_for_recapture)
    capital_gain = max(0.0, proceeds - original_capital_cost)
    return {
        'recapture': recapture,
        'terminal_loss': terminal_loss,
        'capital_gain': capital_gain,
    }
