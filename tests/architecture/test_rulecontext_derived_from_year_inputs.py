"""Issue #231 slice 2 enforcement: ``RuleContext`` is DERIVED from ``YearInputs``.

Slice 1 bundled the year step's per-call inputs into a frozen ``YearInputs``,
but the tax rules still read a hand-listed ``RuleContext`` whose shared fields
were spelled out one by one at the construction site. Slice 2 projects those
fields by name (``RuleContext.from_year_inputs``), so adding a rule no longer
means editing two parallel lists.

The failure this guard exists for is silent, not loud: a projection that drops a
field leaves ``RuleContext`` falling back to that field's default, and a rule
reading it then gets a plausible wrong value -- the exact defect class this
codebase exists to catch. So the guard pins the *partition*, not the spelling::

    RuleContext fields == shared(YearInputs)  disjoint-union  RULE_CONTEXT_EXPLICIT_FIELDS
    YearInputs fields  == shared(RuleContext) disjoint-union  YEAR_INPUTS_ONLY

Both allowlists are checked for stale entries (a member no longer present on the
expected side), so neither can silently grow nor silently rot -- the same
discipline as the DP#32 guards.

Control run before this file was written: adding a 57th ``YearInputs`` field
that ``RuleContext`` does not carry makes ``test_rule_context_partition_matches
_year_inputs`` fail; removing ``year`` from ``RULE_CONTEXT_EXPLICIT_FIELDS``
makes it fail too. Restoring either turns it green.
"""
from __future__ import annotations

import dataclasses
import types

import pytest

from rule_registry import RULE_CONTEXT_EXPLICIT_FIELDS, RuleContext
from simulation_state import YearInputs


# ``YearInputs`` fields that are fold-internal, not rule inputs -- deliberately
# absent from ``RuleContext``. The deployment/transaction carries are surfaced
# on the year result for observability, and the child/extra-adult blocks are
# grown by the fold body itself. Pinned here so a NEW ``YearInputs`` field
# forces a decision (carry it into ``RuleContext``, or list it here with a
# reason) instead of drifting silently.
YEAR_INPUTS_ONLY = frozenset({
    "deployment_lag_cost",
    "deployment_schedule_cost",
    "transaction_cost_year0",
    "child_allocation_pcts",
    "child_gift_amounts",
    "child_loan_amounts",
    "extra_adult_accounts",
})


def _names(cls) -> frozenset:
    return frozenset(f.name for f in dataclasses.fields(cls))


def _sentinel_inputs() -> YearInputs:
    """One ``YearInputs`` with a distinct, non-default value on every field.

    Values are unique strings (types are not checked at runtime), so a dropped
    field would leave ``RuleContext`` at its 0.0/False/None default and the
    identity assertions below would catch it.
    """
    values = {f.name: f"<{f.name}>" for f in dataclasses.fields(YearInputs)}
    # Exercise the resolved-int path as well as the None fallback.
    values["calendar_year"] = 2031
    return YearInputs(**values)


# ── partition ────────────────────────────────────────────────────────────────

def test_rule_context_partition_matches_year_inputs():
    rc = _names(RuleContext)
    yi = _names(YearInputs)
    shared_rc = rc - RULE_CONTEXT_EXPLICIT_FIELDS
    shared_yi = yi - YEAR_INPUTS_ONLY
    assert shared_rc == shared_yi, (
        "RuleContext and YearInputs disagree on the shared field set: "
        f"on RuleContext only={sorted(shared_rc - shared_yi)}; "
        f"on YearInputs only={sorted(shared_yi - shared_rc)}"
    )


def test_rule_context_explicit_fields_are_rule_context_only():
    rc = _names(RuleContext)
    yi = _names(YearInputs)
    assert RULE_CONTEXT_EXPLICIT_FIELDS <= rc, (
        "stale RULE_CONTEXT_EXPLICIT_FIELDS entries (not RuleContext fields): "
        f"{sorted(RULE_CONTEXT_EXPLICIT_FIELDS - rc)}"
    )
    assert RULE_CONTEXT_EXPLICIT_FIELDS.isdisjoint(yi), (
        "a field supplied explicitly by the caller may not also exist on "
        f"YearInputs: {sorted(RULE_CONTEXT_EXPLICIT_FIELDS & yi)}"
    )


def test_year_inputs_only_fields_are_year_inputs_only():
    rc = _names(RuleContext)
    yi = _names(YearInputs)
    assert YEAR_INPUTS_ONLY <= yi, (
        f"stale YEAR_INPUTS_ONLY entries (not YearInputs fields): "
        f"{sorted(YEAR_INPUTS_ONLY - yi)}"
    )
    assert YEAR_INPUTS_ONLY.isdisjoint(rc), (
        "a fold-internal YearInputs field must not also be a RuleContext field "
        f"unless it moves into the derivation: {sorted(YEAR_INPUTS_ONLY & rc)}"
    )


# ── projection is field-for-field identical ──────────────────────────────────

def test_projection_copies_every_shared_field_unchanged():
    inputs = _sentinel_inputs()
    ctx = RuleContext.from_year_inputs(
        inputs, year=7, amt_credit_opening=("amt",), qc_imr_credit_opening=("qc",))
    for name in sorted(_names(RuleContext) - RULE_CONTEXT_EXPLICIT_FIELDS):
        assert getattr(ctx, name) is getattr(inputs, name), name


def test_explicit_fields_come_from_the_call_not_the_inputs():
    ctx = RuleContext.from_year_inputs(
        _sentinel_inputs(), year=7,
        amt_credit_opening=("amt",), qc_imr_credit_opening=("qc",))
    assert ctx.year == 7
    assert ctx.amt_credit_opening == ("amt",)
    assert ctx.qc_imr_credit_opening == ("qc",)


def test_calendar_year_none_falls_back_to_the_index():
    """``YearInputs`` documents None as "use the 0-based index" (#343)."""
    values = {f.name: f"<{f.name}>" for f in dataclasses.fields(YearInputs)}
    values["calendar_year"] = None
    ctx = RuleContext.from_year_inputs(YearInputs(**values), year=42)
    assert ctx.calendar_year == 42


def test_projection_refuses_a_missing_input_field_loudly():
    """Drift is an ``AttributeError``, never a silent default (DP#32)."""
    shared = {f.name: 0.0 for f in dataclasses.fields(RuleContext)
              if f.name not in RULE_CONTEXT_EXPLICIT_FIELDS}
    del shared["living_costs"]
    with pytest.raises(AttributeError, match="living_costs"):
        RuleContext.from_year_inputs(types.SimpleNamespace(**shared), year=0)


def test_rule_context_stays_frozen():
    ctx = RuleContext.from_year_inputs(_sentinel_inputs(), year=7)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.living_costs = 1.0
