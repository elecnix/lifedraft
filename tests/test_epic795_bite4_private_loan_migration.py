#!/usr/bin/env python3
"""Epic #795 bite 4 — the private-loan interest tax law (ITA s.20(1)(c)
deductibility + s.74.2 minor-lender attribution) moves out of the generic fold
(`simulation.py`) into the Canada jurisdiction module.

Bite 4's verdict (the sweep): after bites 1-3, the program logic still computed
inline in the fold's prologue was the private-loan interest split. Its *math* —
which interest is payable (s.20(1)(c)), whether a minor lender's interest is
attributed to the borrower (s.74.2), and whether the borrower may deduct it
(s.20(1)(c) income-producing use) — is Canadian tax law and belongs in
`countries.canada` (DP#10 — one module per program; DP#25 — the generic engine
module must not encode a jurisdiction's tax law). This test pins that the
classification now lives in `countries.canada.private_loan_interest` and that
`simulation.py` no longer encodes it (migrated, not duplicated — DP#9).

The *fold-side wiring* stays generic and intentionally-core: resolving a
person_id to a taxed role (primary/spouse), looking a person's age up from
birth_year (DP#1, like `age_in(year)`), the external-vs-internal lender SHAPE
check, per-role accumulation, and the two out-of-engine-scope contradiction
warnings (#701/#832). That is household-structure plumbing, not tax law.

The end-to-end behaviour is already pinned by `test_issue_832_private_loans.py`;
this test adds (1) a unit-level CHARACTERIZATION of `_private_loan_interest_for`'s
exact per-role output vector so the extraction is provably behaviour-preserving,
and (2) the migration assertion.
"""

import inspect
import unittest
from types import SimpleNamespace


PRIMARY = {'role': 'primary', 'id': 'p1', 'birth_year': 1980}
SPOUSE = {'role': 'spouse', 'id': 'p2', 'birth_year': 1982}
SIM_YEAR = 2026  # primary age 46, spouse 44


def _cfg(private_loans, children=None):
    return SimpleNamespace(private_loans=private_loans, children=children or [])


def _split(private_loans, children=None):
    from simulation import _private_loan_interest_for
    return _private_loan_interest_for(
        _cfg(private_loans, children), SIM_YEAR, PRIMARY, SPOUSE)


class TestPrivateLoanInterestCharacterization(unittest.TestCase):
    """Pin the EXACT (p_income, s_income, p_deduction, s_deduction) vector the
    fold helper produces for each ITA case, so moving the tax law into the
    Canada module is provably behaviour-preserving (interest = 5% x $100k =
    $5,000 throughout)."""

    PAID_INV = {'id': 'l1', 'lender': 'p2', 'borrower': 'p1', 'rate': 0.05,
                'principal': 100_000, 'use': 'investment',
                'repayment': 'amortizing', 'interest': 'paid'}

    def test_no_loans_is_all_zeros(self):
        self.assertEqual(_split([]), (0.0, 0.0, 0.0, 0.0))

    def test_spouse_lends_to_primary_investment_paid(self):
        # s.20(1)(c): lender (spouse) taxed on the $5,000; borrower (primary)
        # deducts it (investment use).
        self.assertEqual(_split([self.PAID_INV]), (0.0, 5_000.0, 5_000.0, 0.0))

    def test_consumption_use_gives_no_deduction(self):
        # s.20(1)(c): consumption use -> no borrower deduction; lender still taxed.
        loan = {**self.PAID_INV, 'use': 'consumption'}
        self.assertEqual(_split([loan]), (0.0, 5_000.0, 0.0, 0.0))

    def test_on_demand_produces_no_flow(self):
        # Interest not payable -> no lender income, no borrower deduction.
        loan = {**self.PAID_INV, 'repayment': 'on_demand', 'interest': 'on_demand'}
        self.assertEqual(_split([loan]), (0.0, 0.0, 0.0, 0.0))

    def test_minor_lender_attributes_income_to_borrower(self):
        # s.74.2: a 15-year-old lender's interest is attributed to the BORROWER
        # (primary), who ALSO deducts it (investment) -> nets to zero on the
        # primary. p_income == p_deduction == 5000; spouse untouched.
        child = {'name': 'child_a', 'id': 'ca', 'birth_year': 2011}
        loan = {**self.PAID_INV, 'lender': 'ca'}
        self.assertEqual(_split([loan], [child]), (5_000.0, 0.0, 5_000.0, 0.0))

    def test_external_lender_gives_borrower_deduction_only(self):
        # An external (inline-dict) lender is not a simulated member -> no
        # lender income; only the borrower's investment deduction lands.
        loan = {**self.PAID_INV, 'lender': {'id': 'grandparent_a'}}
        self.assertEqual(_split([loan]), (0.0, 0.0, 5_000.0, 0.0))

    def test_zero_interest_loan_is_a_noop(self):
        loan = {**self.PAID_INV, 'rate': 0.0}
        self.assertEqual(_split([loan]), (0.0, 0.0, 0.0, 0.0))


class TestMigratedNotDuplicated(unittest.TestCase):
    """DP#9 / DP#25: the ITA private-loan tax law left the generic fold module —
    it lives in countries.canada.private_loan_interest, is not re-encoded in
    simulation.py, and is not imported there at module scope."""

    def test_canada_module_exposes_the_classification(self):
        from countries.canada.private_loan_interest import (
            classify_private_loan_interest, MINOR_ATTRIBUTION_AGE,
        )
        self.assertEqual(MINOR_ATTRIBUTION_AGE, 18)  # ITA s.74.2
        self.assertTrue(callable(classify_private_loan_interest))

    def test_simulation_does_not_encode_the_ita_rules(self):
        import simulation
        src = inspect.getsource(simulation)
        # The payability gate and the s.74.2 age comparison are the tax law;
        # they must not be re-spelled in the generic fold.
        self.assertNotIn("loan.get('interest') == 'paid'", src,
                         "simulation.py must not re-encode the s.20(1)(c) "
                         "interest-payability gate — it belongs in "
                         "countries.canada.private_loan_interest (DP#10/DP#25).")
        self.assertNotIn("lender_age < 18", src,
                         "simulation.py must not re-encode the s.74.2 minor "
                         "threshold — it belongs in the Canada module.")

    def test_simulation_has_no_module_scope_canada_symbol(self):
        import simulation
        self.assertFalse(hasattr(simulation, "classify_private_loan_interest"))
        self.assertFalse(hasattr(simulation, "MINOR_ATTRIBUTION_AGE"))


if __name__ == "__main__":
    unittest.main()
