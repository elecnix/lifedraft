"""An explicit currency value type (issue #909, stage 1).

Currency crosses the core interface today as a bare ``float`` -- indistinguishable
at the type level from a rate, a ratio, a count, or another jurisdiction's money.
``Money`` makes a dollar amount its own type so the arithmetic that must not happen
CANNOT happen: you cannot add a rate to a balance, add CAD to GBP, or multiply two
dollar amounts.

Design (settled from the #909 thread; DP#32 -- no silent coercion):

- **Representation: integer minor units (cents).** Stored amounts are exact
  integers, so equality is an exact integer compare -- this is what lets the
  golden-invariant / trajectory tests stop riding on fragile IEEE-754 float
  equality (stage 3). Cents, not ``Decimal``: the optimizer sweep does millions
  of these operations and integer arithmetic keeps it cheap.

- **Currency tag travels with the value** (default ``"CAD"``). The core stays
  currency-agnostic (DP#25); the tag rides on the amount. ``+``/``-``/compare
  across currencies raise -- there is no exchange rate here to make it meaningful.

- **Period (annual vs monthly) is deliberately NOT part of Money.** It is a real
  bug source, but folding it in here would be feature creep (#909 non-goal);
  flows keep modelling period explicitly.

Allowed arithmetic: Money ± Money (same currency); Money × dimensionless and
dimensionless × Money (a scalar rate/fraction/count); Money ÷ dimensionless →
Money; Money ÷ Money (same currency) → a dimensionless ``float`` ratio; unary
``-`` and ``abs``. Forbidden (raises ``TypeError``): Money × Money, Money ± a
bare number, and any cross-currency combination.

This module is stage 1 only -- the type and its arithmetic. It is not yet wired
into the fold; migration is staged (issue #909) so the golden invariant moves
exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Union

# The default (and, until the multi-country fold lands, only) currency. Kept as a
# plain string tag rather than an enum so a new jurisdiction needs no change here
# (DP#25): the tag is data, registered by whoever mints the Money.
DEFAULT_CURRENCY = "CAD"

Scalar = Union[int, float]


def _require_same_currency(a: "Money", b: "Money", op: str) -> None:
    if a.currency != b.currency:
        raise TypeError(
            f"cannot {op} across currencies: {a.currency} and {b.currency}. "
            f"There is no exchange rate at the core interface -- convert "
            f"explicitly before combining (issue #909)."
        )


def _reject_money_operand(x: object, op: str) -> None:
    """A dimensionless operand must not itself be Money (that would be the
    forbidden Money×Money / Money÷... case), and must be a real number, never a
    bare bool masquerading as 0/1."""
    if isinstance(x, Money):
        raise TypeError(
            f"cannot {op} two Money values -- a dollar amount times/over a "
            f"dollar amount is not a dollar amount (issue #909)."
        )
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise TypeError(
            f"Money can only be {op} by a dimensionless number (a rate, "
            f"fraction, or count), not {type(x).__name__}."
        )


@dataclass(frozen=True, order=False)
class Money:
    """A currency amount in exact integer minor units (cents), tagged with a
    currency. Immutable -- every operation returns a new ``Money``."""

    cents: int
    currency: str = field(default=DEFAULT_CURRENCY)

    def __post_init__(self) -> None:
        # DP#32: the stored amount is EXACT integer cents. Constructing a Money
        # from a fractional cent is a caller bug, not something to silently round
        # -- rounding happens once, explicitly, in from_dollars().
        if isinstance(self.cents, bool) or not isinstance(self.cents, int):
            raise TypeError(
                f"Money.cents must be an int (exact minor units), not "
                f"{type(self.cents).__name__}; use Money.from_dollars() to round "
                f"a dollar amount."
            )
        if not self.currency:
            raise ValueError("Money.currency must be a non-empty currency tag.")

    # ── Constructors ────────────────────────────────────────────────────────
    @classmethod
    def from_dollars(cls, amount: Scalar, currency: str = DEFAULT_CURRENCY) -> "Money":
        """Round a dollar amount to the nearest cent, half-up, ONCE at the edge.

        Half-up (not banker's rounding) is the ordinary convention for booking a
        currency amount, and stating it here is the point: rounding is a single
        explicit act at the boundary, never a silent coercion inside the fold."""
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise TypeError(
                f"Money.from_dollars needs a number of dollars, not "
                f"{type(amount).__name__}."
            )
        cents = int(Decimal(str(amount)).scaleb(2).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return cls(cents=cents, currency=currency)

    @classmethod
    def from_cents(cls, cents: int, currency: str = DEFAULT_CURRENCY) -> "Money":
        """Wrap an exact integer number of minor units directly."""
        return cls(cents=cents, currency=currency)

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> "Money":
        return cls(cents=0, currency=currency)

    # ── Views ───────────────────────────────────────────────────────────────
    @property
    def dollars(self) -> float:
        """The amount in dollars as a float. LOSSY by construction -- use only at
        the reporting edge, never to route back into the fold."""
        return self.cents / 100.0

    # ── Arithmetic ──────────────────────────────────────────────────────────
    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            raise TypeError(
                "cannot add a bare number to Money -- wrap it with "
                "Money.from_dollars() so the units are explicit (issue #909)."
            )
        _require_same_currency(self, other, "add")
        return replace(self, cents=self.cents + other.cents)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            raise TypeError(
                "cannot subtract a bare number from Money -- wrap it with "
                "Money.from_dollars() so the units are explicit (issue #909)."
            )
        _require_same_currency(self, other, "subtract")
        return replace(self, cents=self.cents - other.cents)

    def __mul__(self, factor: Scalar) -> "Money":
        """Money × dimensionless (a rate/fraction/count). The product is rounded
        half-up to the nearest cent so the result stays an exact amount."""
        _reject_money_operand(factor, "multiply")
        cents = int(Decimal(self.cents * Decimal(str(factor))).quantize(
            Decimal(1), rounding=ROUND_HALF_UP))
        return replace(self, cents=cents)

    __rmul__ = __mul__

    def __truediv__(self, divisor: Union["Money", Scalar]) -> Union["Money", float]:
        """Money ÷ Money (same currency) → a dimensionless ratio; Money ÷
        dimensionless → Money (rounded half-up)."""
        if isinstance(divisor, Money):
            _require_same_currency(self, divisor, "divide")
            if divisor.cents == 0:
                raise ZeroDivisionError("division by a zero Money amount")
            return self.cents / divisor.cents
        _reject_money_operand(divisor, "divide")
        if divisor == 0:
            raise ZeroDivisionError("division of Money by zero")
        cents = int(Decimal(self.cents / Decimal(str(divisor))).quantize(
            Decimal(1), rounding=ROUND_HALF_UP))
        return replace(self, cents=cents)

    def __neg__(self) -> "Money":
        return replace(self, cents=-self.cents)

    def __abs__(self) -> "Money":
        return replace(self, cents=abs(self.cents))

    def __bool__(self) -> bool:
        return self.cents != 0

    # ── Comparison ──────────────────────────────────────────────────────────
    # __eq__/__hash__ come from the frozen dataclass (cents + currency), so
    # Money(100,"CAD") != Money(100,"GBP") and neither equals a bare number.
    # Ordering is defined by hand so it can reject a cross-currency compare
    # rather than silently ordering by cents.
    def __lt__(self, other: "Money") -> bool:
        _require_same_currency(self, _as_money(other, "compare"), "compare")
        return self.cents < other.cents

    def __le__(self, other: "Money") -> bool:
        _require_same_currency(self, _as_money(other, "compare"), "compare")
        return self.cents <= other.cents

    def __gt__(self, other: "Money") -> bool:
        _require_same_currency(self, _as_money(other, "compare"), "compare")
        return self.cents > other.cents

    def __ge__(self, other: "Money") -> bool:
        _require_same_currency(self, _as_money(other, "compare"), "compare")
        return self.cents >= other.cents

    # ── Formatting ──────────────────────────────────────────────────────────
    def __str__(self) -> str:
        sign = "-" if self.cents < 0 else ""
        whole, frac = divmod(abs(self.cents), 100)
        return f"{sign}${whole:,}.{frac:02d} {self.currency}"

    def __repr__(self) -> str:
        return f"Money(cents={self.cents}, currency={self.currency!r})"


def _as_money(other: object, op: str) -> Money:
    if not isinstance(other, Money):
        raise TypeError(
            f"cannot {op} Money with {type(other).__name__} -- wrap the amount "
            f"with Money.from_dollars() (issue #909)."
        )
    return other
