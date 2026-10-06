"""Detector for issue #302's first slice: the RRSP refund has ONE spelling.

The slice's own words: "`apply_rrsp_deduction` already writes
`ws.rrsp_deduction_savings` and `ws.spouse_deduction_savings` in both branches.
Make `apply_rrsp_refund_heloc_paydown` and the `YearResult.rrsp_tax_savings`
builder read those fields instead of recomputing the product. ... Add a static
guard in the same slice: no production file other than `rules_contributions.py`
may multiply `*_rrsp_actual` by a marginal rate."

The defect this pins is a *fourth* spelling appearing. Before the slice the same
flat product was written in three places -- `rules_contributions`, the HELOC
paydown rule, and the `YearResult` builder -- so a fix to the valuation (issue
#286) that landed in only one of them left the other two paying, and reporting,
the old uncapped refund. Two of the three now read the fields; this keeps the
third from coming back, and keeps the two readers from drifting off again.

Both directions are asserted, because either one alone is satisfiable by a
broken engine:

* nobody outside `rules_contributions.py` multiplies a `*_rrsp_actual` by a rate
  (a re-spelling), and
* the two consumers still READ the fields the writer publishes (a refactor that
  deletes the consumption would leave the refund computed nowhere, which the
  first half alone would happily accept).
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from repo_scan import ROOT, iter_source_files  # noqa: E402

# The ONE file allowed to turn a contribution into a tax saving -- the rule that
# owns the contribution ledger and the marginal rates it is valued at.
WRITER = "rules_contributions.py"

# The consumers that must read the writer's fields rather than re-derive them.
READERS = {
    "rules_leverage.py": "rrsp_deduction_savings",
    "simulation_state.py": "rrsp_deduction_savings",
}

_RATE_HINTS = ("rate", "marginal", "pct")


def _names_in(node: ast.AST) -> set:
    """Every identifier mentioned anywhere under ``node``, attributes included."""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
    return out


def _rrsp_refund_products(tree: ast.AST):
    """Every ``<something *_rrsp_actual> * <something rate-ish>`` in the module."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
            continue
        left, right = _names_in(node.left), _names_in(node.right)
        for contribution_side, rate_side in ((left, right), (right, left)):
            if any("rrsp_actual" in n for n in contribution_side) and any(
                    any(h in n.lower() for h in _RATE_HINTS) for n in rate_side):
                yield node


def _production_modules():
    for relpath in sorted(iter_source_files(ROOT)):
        if os.path.basename(relpath).startswith("test_") or "tests/" in relpath:
            continue
        if not relpath.endswith(".py"):
            continue
        with open(os.path.join(ROOT, relpath), encoding="utf-8") as handle:
            yield relpath, handle.read()


def test_no_second_spelling_of_the_rrsp_refund():
    offences = []
    for relpath, source in _production_modules():
        if os.path.basename(relpath) == WRITER:
            continue
        try:
            tree = ast.parse(source, filename=relpath)
        except SyntaxError:
            continue
        for node in _rrsp_refund_products(tree):
            offences.append(f"{relpath}:{node.lineno}")
    assert not offences, (
        "issue #302: these lines re-derive the RRSP refund by multiplying a "
        "*_rrsp_actual contribution by a marginal rate. The refund has ONE "
        "spelling -- `rules_contributions.apply_rrsp_deduction` writes "
        "`ws.rrsp_deduction_savings` / `ws.spouse_deduction_savings`, and every "
        "other consumer reads them. A second spelling is how the pre-#302 code "
        "paid and reported a stale, uncapped refund after #286 fixed only one "
        f"site. Offending lines: {offences}"
    )


@pytest.mark.parametrize("relpath,field", sorted(READERS.items()))
def test_the_consumers_still_read_the_writers_field(relpath, field):
    """The other half: the guard above is satisfied by simply deleting the
    consumption, which would leave the refund computed nowhere at all."""
    path = os.path.join(ROOT, relpath)
    if not os.path.exists(path):
        pytest.skip(f"{relpath} is not in this tree")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    assert field in source, (
        f"issue #302: {relpath} no longer reads `{field}`. The RRSP refund is "
        f"published once, by rules_contributions, and consumed here -- dropping "
        f"the read does not single-spell the refund, it deletes it."
    )
