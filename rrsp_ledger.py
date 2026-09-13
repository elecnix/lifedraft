#!/usr/bin/env python3
"""RRSP ledger — plain-list per-contribution deduction ledger (DP#25).

Jurisdiction-agnostic. The canonical per-contribution RRSP deduction
ledger. Stores entries as plain dicts so the simulation fold carries
no Canada-specific class.
"""


class RRSPListLedger:
    """Plain-list RRSP ledger wrapper (jurisdiction-agnostic, DP#25).

    The canonical per-contribution RRSP deduction ledger. Stores entries as
    plain dicts (DP#25: jurisdiction-agnostic) so the simulation fold carries
    no Canada-specific class. The dead countries.canada.rrsp_ledger clone that
    once shadowed this was removed (#744, DP#9).
    """
    def __init__(self, entries: list = None):
        self._entries = entries or []

    @property
    def contributions(self):
        """Access entries list."""
        return self._entries

    @contributions.setter
    def contributions(self, value):
        self._entries = value

    def undeducted_total(self) -> float:
        """Total undeducted contribution amount."""
        return sum(e['amount'] for e in self._entries if not e['deducted'])

    def total_deducted(self) -> float:
        """Total deducted contribution amount."""
        return sum(e['amount'] for e in self._entries if e['deducted'])

    def total_tax_savings(self) -> float:
        """Total tax savings from all deducted contributions."""
        return sum(
            e['amount'] * (e.get('deduction_marginal_rate') or 0)
            for e in self._entries if e['deducted']
        )

    def add_contribution(self, year: int, amount: float, role: str = 'primary'):
        """Add a contribution entry."""
        self._entries.append({
            'year': year, 'amount': amount, 'role': role,
            'deducted': False, 'deduction_year': None,
            'deduction_marginal_rate': None,
        })

    def clone(self) -> 'RRSPListLedger':
        """Return an independent copy (shallow dict copies of entries).

        Issue #1059: entries are flat dicts of scalars, so a shallow copy
        per entry ({**e}) is sufficient.  The returned ledger shares no
        mutable containers with self (DP#26).
        """
        return RRSPListLedger([{**e} for e in self._entries])

    def claim_all_deductions(self, year: int, marginal_rate: float):
        """Claim all undeducted contributions."""
        for e in self._entries:
            if not e['deducted']:
                e['deducted'] = True
                e['deduction_year'] = year
                e['deduction_marginal_rate'] = marginal_rate

    def claim_deferred_deduction(self, year: int, income: float, brackets: list,
                                 bracket_target: float = 0.0) -> dict:
        """Claim the deduct-later slice for ``year`` at bracket-fill rates (DP#19).

        Deducts undeducted primary/spousal contributions down to
        ``bracket_target`` (the income floor the contributor wants to keep taxed
        at the higher bracket). Each claimed slice is valued at the marginal
        rate of the income band it removes, not a flat top rate (issue #546):
        as income is drawn toward ``bracket_target`` the slices fall through
        progressively lower brackets.

        Mutates the ledger (marks entries deducted, splitting partial claims).
        Returns a dict with ``savings``, ``amount`` deducted, and ``claims`` —
        a per-entry list of (year, amount, rate) for output surfacing.
        """
        from tax_calculator import deduction_value, marginal_rate

        undeducted = self.undeducted_total()
        if undeducted <= 0:
            return {'savings': 0.0, 'amount': 0.0, 'claims': []}

        if bracket_target <= 0 and income > 0:
            mtr = marginal_rate(income, brackets)
            for i in range(len(brackets) - 1, 0, -1):
                if brackets[i]['min'] < income and brackets[i]['rate'] >= mtr - 0.10:
                    bracket_target = brackets[i]['min']
                    break
            if bracket_target <= 0:
                bracket_target = brackets[3]['min'] if len(brackets) > 3 else 50000

        amount_to_deduct = min(undeducted, max(0.0, income - bracket_target))
        if amount_to_deduct <= 0:
            return {'savings': 0.0, 'amount': 0.0, 'claims': []}

        running_income = income
        remaining = amount_to_deduct
        total_savings = 0.0
        claims = []
        for entry in list(self._entries):
            if remaining <= 0:
                break
            if entry['deducted'] or entry['role'] not in ('primary', 'spousal'):
                continue
            claim_from = min(remaining, entry['amount'])
            slice_savings = deduction_value(running_income, claim_from, brackets)
            slice_rate = slice_savings / claim_from if claim_from else 0.0
            total_savings += slice_savings
            running_income -= claim_from
            remaining -= claim_from
            claims.append({'year': entry['year'], 'amount': claim_from,
                           'rate': slice_rate})
            if claim_from < entry['amount']:
                entry['amount'] -= claim_from
                self.add_contribution(year=entry['year'], amount=claim_from,
                                      role=entry['role'])
                self._entries[-1]['deducted'] = True
                self._entries[-1]['deduction_year'] = year
                self._entries[-1]['deduction_marginal_rate'] = slice_rate
            else:
                entry['deducted'] = True
                entry['deduction_year'] = year
                entry['deduction_marginal_rate'] = slice_rate

        return {'savings': total_savings, 'amount': amount_to_deduct,
                'claims': claims}

    def __len__(self):
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)


def ledger_undeducted_total(ledger: list) -> float:
    """Compute the total undeducted amount in an RRSP ledger.

    Compute the total undeducted amount in an RRSP ledger.
    The ledger is a list of dicts with 'deducted' and 'amount' keys.
    """
    return sum(e['amount'] for e in ledger if not e['deducted'])


def ledger_total_claimed(ledger: list, year: int = None) -> float:
    """Compute the total claimed deductions in an RRSP ledger.

    Args:
        ledger: List of contribution dicts
        year: If provided, only count deductions claimed in this year
    """
    total = 0.0
    for e in ledger:
        if e['deducted']:
            if year is None or e.get('deduction_year') == year:
                total += e['amount']
    return total


def ledger_total_tax_savings(ledger: list) -> float:
    """Compute the total tax savings from all deducted contributions in an RRSP ledger."""
    return sum(
        e['amount'] * (e.get('deduction_marginal_rate') or 0)
        for e in ledger if e['deducted']
    )
