#!/usr/bin/env python3
"""Private-loan interest — ITA s.20(1)(c) deductibility + s.74.2 attribution.

Epic #795 bite 4 (DP#10/DP#25): the tax law that governs a private (intra-family
or individual-to-individual) loan's interest is Canadian, so it lives in the
Canada jurisdiction module, not in the generic fold (`simulation.py`). The fold
keeps only the household-structure PLUMBING — resolving a person_id to a taxed
role, reading a person's age from their birth_year (DP#1), the external-vs-
internal lender SHAPE check, per-role accumulation, and the two out-of-scope
contradiction warnings (#701/#832). None of that is tax law.

The tax law this module encodes (issues #813/#832):

- **Payability (ITA s.20(1)(c)).** Interest is a deduction to the borrower only
  when it is *paid or payable*, and it is income to the lender only when
  *received or receivable*. A demand loan on which no interest is demanded this
  year (`interest='on_demand'` and not amortizing) produces NO interest tax flow
  at all — it is interest-free financing, not a forced rate x principal split.
  Interest is payable when ``interest == 'paid'`` OR ``repayment == 'amortizing'``
  (scheduled principal-and-interest implies the interest is paid).

- **Deductibility (ITA s.20(1)(c)).** The borrower may deduct the interest only
  when the borrowed money is used to earn income (``use == 'investment'``).
  Personal-use (``consumption``) interest is not deductible.

- **Attribution (ITA s.74.2).** When the lender is a MINOR (under 18), the
  minor's property income (the interest) is attributed back to the BORROWER (the
  transferor of the funds) and taxed in the borrower's hands — the split is
  undone. An adult lender (18+) is exempt from attribution.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — ITA s.20(1)(c), s.74.2
    ITA s.20(1)(c) — interest deductibility (paid/payable, income-producing use)
    ITA s.74.2 — attribution of a related minor's property income
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from countries.canada.attribution import check_attribution, TransferType

# ITA s.74.2: attribution applies to a lender under this age at the tax year.
# The minor-vs-adult DECISION itself is owned by attribution.py (DP#10: that
# module owns ITA s.74.1/s.74.2/s.104.2) and is delegated to
# ``attribution.check_attribution`` below -- not re-spelled here (DP#9). This
# constant remains the module's documented s.74.2 age for the migration guard.
MINOR_ATTRIBUTION_AGE = 18


@dataclass(frozen=True)
class PrivateLoanInterestEffect:
    """The ITA classification of ONE private loan's interest for ONE tax year.

    ``interest`` is ``rate x principal`` when interest is payable this year and
    positive, else ``0.0`` (in which case every other field is inert). The
    ``*_role`` fields name the taxed member (``'primary'`` / ``'spouse'`` /
    ``None``) that the fold should credit; the ``warn_*`` flags mark the two
    contradictions the engine cannot fully model (a child's own tax bracket,
    #701), which the fold surfaces loudly rather than dropping silently (DP#32).
    """
    interest: float
    income_role: Optional[str]      # taxed member who accrues the interest as income
    deduction_role: Optional[str]   # taxed member who may deduct the interest
    warn_adult_child_lender_untaxed: bool
    warn_child_borrower_deduction_unusable: bool


def interest_is_payable(loan: Dict[str, Any]) -> bool:
    """ITA s.20(1)(c): is interest paid/payable on this loan this year?

    True when ``interest == 'paid'`` OR ``repayment == 'amortizing'`` (a
    scheduled principal-and-interest repayment implies the interest is paid).
    The default on-demand demand loan is interest-free financing -> False.
    """
    return loan.get('interest') == 'paid' or loan.get('repayment') == 'amortizing'


def classify_private_loan_interest(
        loan: Dict[str, Any],
        *,
        lender_is_external: bool,
        lender_age: Optional[int],
        lender_role: Optional[str],
        borrower_role: Optional[str],
        borrower_is_child: bool,
) -> PrivateLoanInterestEffect:
    """Classify one private loan's interest for one tax year under the ITA.

    The caller resolves the household-structure facts and passes them in:
    ``lender_is_external`` (the lender is an individual outside the household —
    not a simulated member, so not taxed here), ``lender_age`` (from birth_year,
    or None when unknown), ``lender_role`` / ``borrower_role`` (the taxed member
    each person_id maps to, or None), and ``borrower_is_child`` (the borrower is
    a declared child). This function applies only the tax law:

    - **Lender side.** An INTERNAL, adult, taxed-member lender accrues the
      interest as income (``income_role = lender_role``). ITA s.74.2: an internal
      MINOR lender's interest is attributed to the borrower instead
      (``income_role = borrower_role``). An external lender is never taxed here.
      An adult-child internal lender (18+, exempt from attribution, but with no
      taxed role because the engine does not tax children individually — #701)
      earns the interest untaxed: ``warn_adult_child_lender_untaxed``.

    - **Borrower side.** ITA s.20(1)(c): the borrower deducts the interest only
      for ``use == 'investment'``. A taxed-member borrower gets the deduction; a
      child borrower's deduction has no tax to reduce (#701):
      ``warn_child_borrower_deduction_unusable``.

    When no interest is payable, or it is non-positive, every effect is inert.
    """
    if not interest_is_payable(loan):
        return PrivateLoanInterestEffect(0.0, None, None, False, False)
    interest = float(loan['rate']) * float(loan['principal'])
    if interest <= 0.0:
        return PrivateLoanInterestEffect(0.0, None, None, False, False)

    income_role: Optional[str] = None
    warn_adult_child = False
    if not lender_is_external:
        # ITA s.74.2 (#702): the minor-vs-adult attribution decision is delegated
        # to attribution.check_attribution -- the rule is not re-spelled here
        # (DP#9). A MINOR lender's property income attributes back to the borrower
        # (the transferor); an adult lender (18+) is exempt. The age gate stays
        # here (the fold resolves ages, DP#1): when the age is unknown we do not
        # call the rule, so an unproven minor is treated conservatively as adult.
        attributes_to_borrower = lender_age is not None and check_attribution(
            TransferType.MINOR_CHILD,
            donor_role=borrower_role or "",
            recipient_role=lender_role or "",
            recipient_age=lender_age,
        ).attributed
        if attributes_to_borrower:
            # s.74.2: attribute the minor's interest to the borrower (the
            # transferor). If the borrower is not a taxed member the attribution
            # simply has no taxed target.
            income_role = borrower_role
        elif lender_role is not None:
            income_role = lender_role
        elif lender_age is not None:
            # Adult child (18+): exempt from attribution, so the interest is the
            # child's to tax in their own bracket -- but the engine has no child
            # bracket (#701), so it is earned untaxed. Surface it loudly (DP#32).
            warn_adult_child = True

    deduction_role: Optional[str] = None
    warn_child_borrower = False
    if loan.get('use') == 'investment':  # ITA s.20(1)(c): income-producing use
        if borrower_role is not None:
            deduction_role = borrower_role
        elif borrower_is_child:
            # The interest IS deductible, but the engine does not tax the child
            # (#701), so there is no tax for the deduction to reduce (DP#32).
            warn_child_borrower = True

    return PrivateLoanInterestEffect(
        interest, income_role, deduction_role,
        warn_adult_child, warn_child_borrower)
