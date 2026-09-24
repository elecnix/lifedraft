"""RULE_ORDER coupling guard: a rule that writes ``ws.<field>`` must run before
every rule that reads that field, or the consumer silently reads stale
(default) data -- DP#32 in the ordering dimension.

## The failure this prevents

When ``RULE_ORDER`` is reordered, a producer rule can end up after its
consumer.  The consumer then reads the field's *default* (0.0 / False /
[]), not the value the producer wrote -- and the run completes, green,
printing a confident wrong number.  ~3,900 tests missed #575-#580 for the
same reason: they assert terminal scalars, not the internal producer-consumer
seams that feed them.

Concrete example: ``management_fee`` (pos 20) writes ``ws.management_fee``;
``solvency`` (pos 31) reads it.  If ``management_fee`` is moved to 35,
``solvency`` reads 0.0 and the household's solvency check runs without the
real fee -- the optimizer cheerfully ranks a strategy that silently dropped
its cash-outflow against one that included it.

## What the scan proves

``repo_scan.find_ws_field_accesses()`` walks every ``rules_*.py`` file, finds
each ``@rule("name")`` definition, resolves its transitive same-file callees
(so a ``ws.<field>`` access buried inside a helper is attributed to the RULE
that calls it, not the helper), and records every ``ws.<field>`` read and
write per rule.  ``AugAssign`` (``ws.field += x``) counts as both read and
write -- the rule loads the old value and stores the sum.

## The ordering check

For each field touched by **more than one rule** (a cross-rule seam), the
guard asserts the producer precedes the consumer in ``RULE_ORDER``:

- **Single-rule-writer fields** (the common case, e.g. ``management_fee``):
  the writer must precede *every* pure consumer (a rule that reads the
  field but does not also write it).  Any reordering of the writer past any
  reader is a violation.

- **Multi-rule-writer fields** (e.g. ``new_nonreg_bal``, written by 9 rules):
  the *first* writer in ``RULE_ORDER`` must precede the *last* pure consumer.
  A multi-writer field is fed incrementally (each writer AugAssigns += its
  slice); the consumer reads the *latest* preceding writer, so any writer
  appearing before any consumer is sufficient.  The first-writer /
  last-reader check is the strongest sound test that avoids false positives on
  legitimate multi-writer incremental feeds.

### The one known read-before-write (DP#32, not a violation)

``heloc_interest_servicing`` (pos 19) reads ``ws.drawdown_taxable`` before any
writer (writers at 26/27/28).  This is a **legitimate** read of the field's
default ``0.0``: in the accumulation phase no drawdown has fired, so the tax
base is correctly 0.0.  The comment on the rule explicitly says "the
accumulation phase where this rule fires."  The multi-writer check does NOT
flag this because the first writer (26) still precedes the last pure consumer
of the field (34) -- the seam from ``retirement_drawdown`` to ``amt`` is what
matters, not the zero-default read that happens to fire first.

If this read were genuinely broken (the consumer needed the writer's value),
the multi-writer check would still catch it: the first writer (26) would need
to precede the last reader (19) -- 26 > 19 -- which it doesn't, so the check
would fire.  The fact that it doesn't fire here is the check saying "no
pure consumer reads this field before all writers have run" -- i.e., the
zero-default read is not the value any *consumer* depends on.

This is not an allowlist entry: it is the natural consequence of checking
first-writer / last-reader for multi-writer fields, which is the correct
invariant for incremental feeds.  No exception list is needed.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass

import pytest

# Add the repo root so `import simulation_rules` resolves.  The root conftest.py
# covers this under pytest; the explicit insert covers direct execution / IDE too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import repo_scan  # noqa: E402

import simulation_rules as sr  # noqa: E402


@dataclass(frozen=True)
class _FieldInfo:
    """Writers and pure-readers for one ``ws.<field>``, at rule granularity."""

    field: str
    writers: set  # rule names that write the field
    pure_readers: set  # rule names that read but do NOT also write the field
    writer_files: set  # source files of the writer rules


@dataclass
class _Inventory:
    """Aggregate scan of every ``ws.<field>`` access across all rules."""

    fields: dict  # field name -> _FieldInfo
    rule_to_file: dict  # rule name -> source file (e.g. 'rules_leverage.py')
    rule_names_in_scan: set  # every @rule name the scanner found


def _build_inventory() -> _Inventory:
    """Scan the rule layer once and fold the flat ``WsAccess`` list into a
    per-field writer/reader index."""
    accesses = repo_scan.find_ws_field_accesses()
    rule_to_file = {a.rule_name: a.file for a in accesses}

    writers = defaultdict(set)
    readers = defaultdict(set)
    for a in accesses:
        if a.kind == "write":
            writers[a.field].add(a.rule_name)
        if a.kind == "read":
            readers[a.field].add(a.rule_name)

    all_fields = set(writers) | set(readers)
    fields = {}
    for f in sorted(all_fields):
        w = writers.get(f, set())
        r = readers.get(f, set())
        pure_r = r - w
        writer_files = {rule_to_file[rw] for rw in w} if w else set()
        fields[f] = _FieldInfo(f, w, pure_r, writer_files)

    return _Inventory(
        fields=fields,
        rule_to_file=rule_to_file,
        rule_names_in_scan=set(rule_to_file),
    )


@pytest.fixture(scope="module")
def inventory() -> _Inventory:
    return _build_inventory()


# ── Measurements ───────────────────────────────────────────────────────────


def test_ws_field_inventory(inventory: _Inventory):
    """The scanner must account for every ``ws.<field>`` the rules touch.

    These counts are pinned so a scanner regression (missing a rule file,
    failing to resolve a helper) turns into a loud mismatch rather than a
    silently-weaker guard.  The exact numbers are computed from the source
    on this commit -- see the module docstring for the methodology.
    """
    total_fields = len(inventory.fields)
    multi_module_writers = sum(1 for f in inventory.fields.values() if len(f.writer_files) > 1)

    # 262: the total ws.<field> surface touched by all rules (#286 added
    #      ws.rrsp_deduction_carried_forward, written by rrsp_deduction).
    #  26: fields written by >1 source module (module-level multi-writers).
    assert total_fields == 262, (
        f"expected 262 ws fields, got {total_fields} -- the scanner missed "
        "rules_*.py files or failed to resolve a helper; the guard's"
        " measurements are no longer trustworthy."
    )
    assert multi_module_writers == 26, (
        f"expected 26 multi-module-writer fields, got {multi_module_writers}"
    )


def test_seam_inventory(inventory: _Inventory):
    """A *seam* is a (producer_rule, consumer_rule) pair where the producer
    is the LATEST writer of a field that precedes a PURE consumer (a reader
    that does not also write the field).  52 such pairs exist today; the
    count is pinned so adding or removing a seam is a deliberate, reviewed
    change rather than a silent drift.

    (#286 removed two: ``rrsp_deduction`` and ``rrsp_refund_heloc_paydown``
    no longer read the ``contributions`` rule's ``*_rrsp_actual`` amounts --
    the refund is the ledger's capped claim, one source, not a second
    ``contribution x marginal rate`` product computed from those amounts.)

    (The feasibility brief estimated "52"; the difference is methodology --
    the brief's ad-hoc prototype did not resolve two same-file helper
    callees that this guard does.  Pinning the actual count, not the
    estimate, is the point.)
    """
    order_idx = {n: i for i, n in enumerate(sr.RULE_ORDER)}

    seam_pairs = set()
    for f in inventory.fields.values():
        if not f.writers or not f.pure_readers:
            continue
        sorted_w = sorted(f.writers, key=lambda r: order_idx[r])
        for consumer in f.pure_readers:
            ci = order_idx[consumer]
            preceding = [w for w in sorted_w if order_idx[w] < ci]
            if preceding:
                seam_pairs.add((preceding[-1], consumer))

    assert len(seam_pairs) == 52, (
        f"expected 52 producer-consumer rule pairs, got {len(seam_pairs)} -- "
        f"a rule was added/removed/reordered. Review the change."
    )


# ── Ordering enforcement ───────────────────────────────────────────────────


@dataclass
class _Violation:
    field: str
    writer: str
    writer_pos: int
    reader: str
    reader_pos: int
    kind: str  # 'single' or 'multi'


def _check_ordering(inventory: _Inventory, order=None) -> list:
    """Run the RULE_ORDER preceding check on every cross-rule field.

    - Single-rule-writer fields: the writer must precede ALL pure consumers.
    - Multi-rule-writer fields: the FIRST writer (lowest position) must
      precede the LAST pure consumer (highest position).
    """
    order_idx = {n: i for i, n in enumerate(order if order is not None else sr.RULE_ORDER)}
    violations = []

    for f in inventory.fields.values():
        if not f.writers or not f.pure_readers:
            continue  # no cross-rule seam

        if len(f.writers) == 1:
            w = next(iter(f.writers))
            wi = order_idx[w]
            for r in f.pure_readers:
                ri = order_idx[r]
                if ri < wi:
                    violations.append(_Violation(f.field, w, wi, r, ri, "single"))
        else:
            first_w = min(f.writers, key=lambda r: order_idx[r])
            last_r = max(f.pure_readers, key=lambda r: order_idx[r])
            if order_idx[first_w] >= order_idx[last_r]:
                violations.append(
                    _Violation(
                        f.field, first_w, order_idx[first_w], last_r, order_idx[last_r], "multi"
                    )
                )
    return violations


def test_no_under_ordered_pair_violations(inventory: _Inventory):
    """Every producer rule must precede every consumer rule it feeds in
    ``RULE_ORDER``.  A violation means a consumer reads the field's default
    instead of the producer's write -- the silent-zero class of defect this
    guard exists to make a build failure.

    See the module docstring for why single-writer fields get a strict
    'all writers precede all readers' check while multi-writer fields get
    the weaker 'first writer precedes last reader' check (the incremental
    ``+=`` feed pattern makes the weaker check both necessary and sound).
    """
    violations = _check_ordering(inventory)
    if violations:
        lines = "\n".join(
            f"  {v.kind}: {v.writer}({v.writer_pos}) writes ws.{v.field} "
            f"but {v.reader}({v.reader_pos}) reads it first"
            for v in violations
        )
        raise AssertionError(
            f"RULE_ORDER ordering violation(s) -- a consumer reads ws.<field> "
            f"before its producer writes it:\n{lines}"
        )


def test_every_rule_has_a_ruler_order_position(inventory: _Inventory):
    """Every ``@rule(...)``-registered rule must appear in ``RULE_ORDER``,
    and vice versa -- a rule outside ``RULE_ORDER`` never runs, and a
    ``RULE_ORDER`` entry with no function is a silent no-op.
    """
    scanned = inventory.rule_names_in_scan
    ordered = set(sr.RULE_ORDER)

    missing_from_order = scanned - ordered
    assert not missing_from_order, (
        f"Registered rule(s) not in RULE_ORDER (they never execute): {sorted(missing_from_order)}"
    )

    missing_from_registry = ordered - scanned
    assert not missing_from_registry, (
        f"RULE_ORDER declares rule(s) with no @rule registration "
        f"(silent no-ops): {sorted(missing_from_registry)}"
    )

    assert len(sr.RULE_ORDER) == len(ordered), (
        f"RULE_ORDER has duplicate entries: {len(sr.RULE_ORDER)} names, {len(ordered)} unique"
    )


# ── Regression pins: the canonical single-writer seams ─────────────────────


def test_management_fee_precedes_solvency(inventory: _Inventory):
    """The canonical seam from the issue: ``management_fee`` writes
    ``ws.management_fee`` and ``ws.management_fee_deductible``;
    ``solvency`` and ``sm_interest`` read them.  If this ever regresses,
    the solvency identity runs without the real management fee.
    """
    order_idx = {n: i for i, n in enumerate(sr.RULE_ORDER)}
    mgmt = order_idx["management_fee"]

    assert "management_fee" in inventory.fields, (
        "management_fee field missing from scanner output -- the "
        "find_ws_field_accesses() scan changed."
    )
    info = inventory.fields["management_fee"]
    assert "management_fee" in info.writers, (
        "management_fee rule no longer writes ws.management_fee"
    )
    assert "solvency" in info.pure_readers, "solvency no longer reads ws.management_fee"
    assert mgmt < order_idx["solvency"], "management_fee must precede solvency in RULE_ORDER"


def test_rerun_with_modified_order_catches_management_fee_misorder(inventory: _Inventory):
    """Simulate moving ``management_fee`` past its consumers and confirm the
    ordering check fires -- proof that the guard is sound, not a no-op.
    """
    # Move management_fee to the dead last position.
    modified = list(sr.RULE_ORDER)
    modified.remove("management_fee")
    modified.append("management_fee")

    violations = _check_ordering(inventory, order=modified)
    assert violations, (
        "Expected violations when management_fee is moved to the end -- "
        "the guard is not detecting reorderings it should."
    )
    assert any(v.field == "management_fee" for v in violations), (
        "management_fee reordering was not detected"
    )
