"""Shared example-document helpers for tests that need a MINIMAL
(illustrative-block-free) example document.

The shipped ``schema/example.json`` is a POPULATED 4-generation household.
Several of its blocks are OPTIONAL ILLUSTRATIVE declarations whose ONLY
purpose is schema-coverage illustration -- a populated instance so the
leaf-reachability guard (``tests/test_schema_coverage.py``) sees the leaf.
They are REAL inputs the engine prices, so a test that loads the raw example
to pin a DIFFERENT feature's mapping gets confounded by them: the
illustrative block moves the internal config the test did not ask for, and
the test's byte-identical / relative assertions drift.

Each feature's own test file used to hand-strip the blocks it did not want,
duplicating the strip list in three places (#1075, #792, #139) -- so a NEW
feature added to the example forced a coordinated edit across all three, and
a strip forgotten in one file surfaced as a confounded assertion later
(exactly how #139's ``transaction_costs`` block confounded #1075 and #792
until each hand-stripped it).

``minimal_example()`` strips them all in ONE place: a future feature adds its
illustrative block to the example AND to this one helper, not to N test files.
Each test then adds back ONLY the block its feature tests (the contract's own
"declare what you mean" discipline, applied to fixtures -- a test that pins
feature X starts from a clean slate and declares X, not a slate that already
quietly declares X, Y, and Z).

The blocks stripped are the OPTIONAL illustrative ones (the example's
``transaction_costs`` (#139), the ``refi_50k`` refinance option's
``advance_split`` (#792), and the ``refi_100k`` refinance option's
``deployment_schedule_years`` / ``parking_rate`` (#74)).
A block a test is actively testing is added back BY THE TEST after the strip,
never left to the example's illustration. The two-generation sub-family trim
(``_two_generation_subset``) is applied first (the adapter only maps the
two-adults-plus-children sub-family -- see ``test_input_contract.py``).

Importable by both ``unittest``-style and pytest-style test modules (it is a
plain module, not a test file -- the ``_`` prefix keeps pytest from collecting
it as a test).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict

# Self-bootstrap: ensure THIS directory (tests/) is on sys.path so the
# ``test_input_contract`` import below resolves regardless of the calling
# test file's own sys.path manipulation (some test files insert only the repo
# root + tests/architecture, not tests/ itself). Importing this module once
# makes tests/ importable for the rest of the session.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Reuse the canonical example loader + two-generation trim already shared
# across the test suite (tests/test_input_contract.py owns them). Imported
# here so the strip list lives in ONE place and the loader/trim do not.
from test_input_contract import _load_example, _two_generation_subset  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = REPO_ROOT / "schema" / "example.json"


def minimal_example() -> Dict:
    """The shipped example, trimmed to the two-generation sub-family the
    adapter maps, with EVERY optional illustrative block stripped so a test
    that loads it to pin a DIFFERENT feature starts from a clean slate.

    Each test adds back ONLY the block its feature tests. The strip is
    defensive (``.pop(..., None)``): a block the example does not carry is a
    no-op, so a future example that drops an illustration does not break this
    helper, and a future feature adds its block here in ONE place.

    Returns a DEEP copy (the caller may mutate it freely without touching the
    shared example on disk or other tests' copies).
    """
    doc = _two_generation_subset(_load_example())
    # Top-level illustrative block: the one-time transaction_costs (#139).
    doc.pop("transaction_costs", None)
    # The refinance options' illustrative deployment-timing / parking-rate /
    # advance-split blocks (#74/#137/#792). Stripped from EVERY refinance
    # option (the shipped example carries advance_split on refi_50k and a
    # deployment schedule on refi_100k; stripping all is defensive and
    # matches the "clean slate" contract -- a test that needs one adds it
    # back explicitly).
    refi_opts = doc.get("decisions", {}).get("mortgage", {}).get(
        "refinance_options", [])
    for opt in refi_opts:
        opt.pop("deployment_lag_months", None)
        opt.pop("parking_rate", None)
        opt.pop("advance_split", None)
        opt.pop("deployment_schedule_years", None)
    # Defensive: strip any illustrative per-liability cash_back block (#1075)
    # the example may later carry, so a test that pins the multitranche /
    # cash_back feature adds it back explicitly rather than inheriting the
    # example's illustration. A no-op today (the example carries none).
    for liab in doc.get("liabilities", []):
        liab.pop("cash_back", None)
        liab.pop("cash_back_total", None)
    return doc
