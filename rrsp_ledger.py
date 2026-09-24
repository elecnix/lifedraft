#!/usr/bin/env python3
"""RRSP ledger — plain-list per-contribution deduction ledger (DP#25).

Jurisdiction-agnostic. The canonical per-contribution RRSP deduction
ledger. Stores entries as plain dicts so the simulation fold carries
no Canada-specific class.
"""


def lowest_taxed_floor(brackets: list) -> float:
    """The income below which a deduction saves no tax (issue #286).

    The ``min`` of the lowest bracket taxed at a positive rate, taken from the
    LOADED brackets (DP#2/#13: never a hard-coded figure). Raises
    ``ValueError`` on empty brackets or brackets with no positive rate --
    a cap that cannot be derived must fail loudly, not default to zero.
    """
    if not brackets:
        raise ValueError("lowest_taxed_floor: no tax brackets supplied (#286).")
    for b in brackets:
        if b['rate'] > 0:
            return b['min']
    raise ValueError(
        "lowest_taxed_floor: no bracket has a positive rate, so no deduction "
        "can save tax and the useful-deduction cap is undefined (#286).")


def current_bracket_floor(income: float, brackets: list) -> float:
    """The ``min`` of the bracket a deduction from ``income`` first removes
    income from: the bracket with ``min < income <= max`` (``max`` 0 means
    unbounded, as in ``tax_calculator.tax_on_income``). ``income`` at or
    below the first bracket's floor returns that floor. Raises ``ValueError``
    on empty brackets (issue #286)."""
    if not brackets:
        raise ValueError("current_bracket_floor: no tax brackets supplied (#286).")
    for b in brackets:
        upper = b['max'] if b['max'] else float('inf')
        if income <= upper:
            return b['min']
    return brackets[-1]['min']


# Issue #286: the largest remainder (in dollars) a claim treats as float
# noise rather than money. Summing and subtracting non-round contribution
# amounts leaves residues around 1e-13; splitting one off as an "undeducted"
# entry would report a phantom carry-forward. A micro-dollar is far below any
# amount a return reports (cents) and far above float residue at these
# magnitudes.
SPLIT_EPSILON = 1e-6


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

    def _claimed_in_year(self, year: int, roles: tuple) -> float:
        """Amount of ``roles``' contributions already deducted in ``year``.

        Issue #286: two engine steps can claim against the SAME tax year (the
        monthly path's year-0 lump step, then the regular year-0 step). The
        per-year cap must see what the first step already claimed, or the
        second would claim the same income headroom again and re-create the
        impossible refund this function exists to prevent."""
        return sum(e['amount'] for e in self._entries
                   if e['deducted'] and e['deduction_year'] == year
                   and e['role'] in roles)

    def _undeducted_for(self, roles: tuple) -> float:
        return sum(e['amount'] for e in self._entries
                   if not e['deducted'] and e['role'] in roles)

    def _claim_down_to(self, year: int, income: float, floor: float,
                       brackets: list, roles: tuple) -> dict:
        """Claim ``roles``' undeducted contributions, oldest first, until
        ``income`` (less what was already claimed this year) reaches ``floor``.

        Each claimed slice is valued at the bracket-fill rate of the income
        band it removes (``deduction_value``), never at a flat top rate
        (issue #546 / #286). Partial entries are split with the amount
        conserved: the claimed part becomes a new deducted entry, the rest
        stays undeducted. Every undeducted entry of ``roles`` is visited until
        the cap is exhausted -- never only the first match.

        Returns ``savings``, ``amount`` claimed, ``claims`` (per-slice year,
        amount, rate) and ``carried_forward`` (the undeducted total left for
        ``roles`` -- deducted in a later year, never lost).
        """
        from tax_calculator import deduction_value

        already = self._claimed_in_year(year, roles)
        running_income = income - already
        cap = max(0.0, running_income - floor)
        # When the cap covers everything ``roles`` still has undeducted, every
        # entry is claimed WHOLE. Walking a float ``remaining`` down entry by
        # entry would drift below the last entry's amount (the float sum does
        # not reproduce exactly) and split off a sub-cent undeducted stub --
        # a phantom carry-forward the disclosure would then report.
        claim_all = cap >= self._undeducted_for(roles)
        remaining = cap
        amount_claimed = 0.0
        total_savings = 0.0
        claims = []
        for entry in list(self._entries):
            if entry['deducted'] or entry['role'] not in roles:
                continue
            if claim_all:
                claim_from = entry['amount']
            else:
                if remaining <= SPLIT_EPSILON:
                    break
                claim_from = min(remaining, entry['amount'])
                # A remainder below SPLIT_EPSILON is float noise, not an
                # undeducted contribution: claim the entry whole.
                if entry['amount'] - claim_from <= SPLIT_EPSILON:
                    claim_from = entry['amount']
            slice_savings = deduction_value(running_income, claim_from, brackets)
            slice_rate = slice_savings / claim_from if claim_from else 0.0
            total_savings += slice_savings
            running_income -= claim_from
            remaining -= claim_from
            amount_claimed += claim_from
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

        return {'savings': total_savings, 'amount': amount_claimed,
                'claims': claims,
                'carried_forward': self._undeducted_for(roles)}

    def claim_useful_deductions(self, year: int, income: float, brackets: list,
                                roles: tuple) -> dict:
        """Deduct-now (issue #286): claim ``roles``' undeducted contributions
        against ``income`` down to the lowest taxed income -- the point below
        which a deduction saves nothing (ITA s.146(5): the deduction is only
        useful up to the tax it reduces; the rest is carried forward, Schedule
        7). The excess stays undeducted in the ledger and is claimed in later
        years -- never refunded in this year, never lost.
        """
        return self._claim_down_to(year, income, lowest_taxed_floor(brackets),
                                   brackets, roles)

    def claim_deferred_deduction(self, year: int, income: float, brackets: list,
                                 bracket_target: float = 0.0) -> dict:
        """Claim the deduct-later slice for ``year`` at bracket-fill rates (DP#19).

        Deducts undeducted primary/spousal contributions down to
        ``bracket_target`` (the income floor the contributor wants to keep taxed
        at the higher bracket). Each claimed slice is valued at the marginal
        rate of the income band it removes, not a flat top rate (issue #546):
        as income is drawn toward ``bracket_target`` the slices fall through
        progressively lower brackets.

        With no declared target (``bracket_target <= 0``) the target is taken
        from the loaded brackets: the lowest bracket within ten rate points of
        the contributor's marginal rate, else the floor of the bracket the
        income sits in (issue #286: never a hard-coded dollar figure, DP#2/#13).
        Empty brackets raise ``ValueError``.

        Mutates the ledger (marks entries deducted, splitting partial claims).
        Returns a dict with ``savings``, ``amount`` deducted, ``claims`` -- a
        per-entry list of (year, amount, rate) for output surfacing -- and
        ``carried_forward``.
        """
        from tax_calculator import marginal_rate

        if not brackets:
            raise ValueError(
                "claim_deferred_deduction: no tax brackets were supplied, so "
                "the deduct-later target cannot be derived (issue #286, DP#32).")
        if bracket_target <= 0 and income > 0:
            mtr = marginal_rate(income, brackets)
            for i in range(len(brackets) - 1, 0, -1):
                if brackets[i]['min'] < income and brackets[i]['rate'] >= mtr - 0.10:
                    bracket_target = brackets[i]['min']
                    break
            if bracket_target <= 0:
                bracket_target = current_bracket_floor(income, brackets)

        return self._claim_down_to(year, income, bracket_target, brackets,
                                   ('primary', 'spousal'))

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
