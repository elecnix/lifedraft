"""Architecture guard for #437: the non-reg DISTRIBUTION rate must be threaded
everywhere the combined non-reg return is.

``non_reg_after_tax_return`` bundles the after-tax distribution with capital
appreciation; ``non_reg_after_tax_distribution`` carries the distribution half
alone, and it is the ONLY thing that may join cost basis (DP#19). The #437
double taxation existed for exactly one reason -- ACB never grew, so every
reinvested distribution stayed inside ``balance - acb`` and was realized as
capital gain at withdrawal.

The failure mode this guards against is not the fix regressing; it is a NEW
call site (``simulation.py`` has several ways to build ``YearInputs``) that
passes ``non_reg_after_tax_return`` and forgets the distribution. That call site
would silently produce the old, double-taxing behaviour while every existing
test still passed on the paths they exercise.

Not a text search for a comment: the check is structural -- every keyword
argument that supplies the combined return must be accompanied by the
distribution argument in the same call.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SIMULATION_PY = os.path.join(REPO_ROOT, "simulation.py")

COMBINED = "non_reg_after_tax_return"
DISTRIBUTION = "non_reg_after_tax_distribution"


def _calls_passing(tree, kwarg):
    """Every ast.Call in ``tree`` that supplies ``kwarg=...``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == kwarg:
                    yield node.lineno


def test_every_call_passing_the_combined_rate_passes_the_distribution():
    with open(SIMULATION_PY, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    combined_lines = sorted(_calls_passing(tree, COMBINED))
    assert combined_lines, (
        f"{COMBINED}= no longer appears in simulation.py -- if the non-reg "
        "growth rate moved, update this guard with it rather than deleting it")

    distribution_lines = set(_calls_passing(tree, DISTRIBUTION))

    unthreaded = [line for line in combined_lines
                  if line not in distribution_lines]
    assert not unthreaded, (
        f"simulation.py passes {COMBINED}= at line(s) {unthreaded} without "
        f"the matching {DISTRIBUTION}=. That call site would grow the pot "
        "correctly but leave its cost basis untouched, reintroducing #437: "
        "every reinvested distribution stays inside balance - acb and is "
        "taxed a second time as a capital gain on withdrawal. Thread both, or "
        "neither -- there is no correct middle here.")


def _combined_rate_names(node):
    """Every reference to the combined growth rate under ``node``.

    Walks the whole expression subtree rather than testing ``node.value``'s own
    type: an ACB increment is written as ``pre * dist_rate`` or
    ``pre * ctx.non_reg_after_tax_return`` -- a BinOp, an Attribute, a Call -- so
    an ``isinstance(node.value, ast.Name)`` test silently matches nothing and
    the guard can never fail.

    Both ``Name.id`` and ``Attribute.attr`` are matched, because they are
    different kinds of node holding the same forbidden identifier: the
    attribute name of ``ctx.non_reg_after_tax_return`` lives in
    ``Attribute.attr``, which ``ast.walk`` never yields as a ``Name``.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and COMBINED in sub.id:
            yield sub
        elif isinstance(sub, ast.Attribute) and COMBINED in sub.attr:
            yield sub


def test_the_distribution_rate_is_never_added_to_acb_by_capital_appreciation():
    """``rules_growth`` may add the DISTRIBUTION to ACB, never the combined
    rate. Growth is capital-appreciation-inclusive, so adding it to cost basis
    would erase a household's entire unrealized gain -- the opposite error from
    #437, and one that would make every drawdown tax-free."""
    import ast as _ast
    path = os.path.join(REPO_ROOT, "rules_growth.py")
    with open(path, encoding="utf-8") as handle:
        tree = _ast.parse(handle.read())

    offenders = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.AugAssign):
            targets = [node.target]
        elif isinstance(node, _ast.Assign):
            targets = list(node.targets)
        else:
            continue
        if not any(isinstance(t, _ast.Attribute) and t.attr.endswith("_acb")
                   for t in targets):
            continue
        for sub in _combined_rate_names(node.value):
            offenders.append(node.lineno)

    assert not offenders, (
        f"rules_growth.py adds the combined growth rate to a cost basis at "
        f"line(s) {offenders}. Capital appreciation is unrealized and is "
        "never income; only the after-tax DISTRIBUTION may join cost basis "
        "(DP#19). Adding the combined rate would make the pot's unrealized "
        "gain permanently zero and tax every drawdown at the lowest rate.")