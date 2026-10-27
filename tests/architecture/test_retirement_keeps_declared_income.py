"""Detector for issue #445: the retirement transition must not DISCARD declared
income.

The failure this pins is the one the repo was rebuilt to eliminate: a dated
``income_segments`` window that runs past ``retirement_age`` was blended into
the year's income and then zeroed, so the document declared wages to 2030 and
the engine reported a member with none -- silently, because the run stayed
green. ``tests/test_issue_445_post_retirement_income.py`` asserts the BEHAVIOUR
through the fold; this asserts the SHAPE, so the zeroing cannot be reintroduced
by a refactor that keeps every existing test green (for instance by "simplifying"
the transition back to ``primary_income = 0.0`` in a household whose fixtures
happened to declare no dated window).

The rule being pinned, stated positively so it can be read without the diff:

    Retiring stops the UNDATED base salary and keeps the income the document
    dates. Both fold paths (the yearly step and the monthly one) must do this
    by delegating to ``simulation._dated_income_for_year``.

Mechanically: no ``if <role>_retired:`` block in ``simulation.py`` may assign
``0.0`` to that role's income, earned income or self-employment slice. A
scan cannot prove the delegation happens -- that is the behavioural test's
job -- but it can prove the discard does not, which is the half that silently
loses a declaration.
"""

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SIMULATION = os.path.join(ROOT, "simulation.py")

ROLES = ("primary", "spouse")
ZEROED_FIELDS = ("income", "earned_income", "self_emp")


def _retirement_blocks(tree: ast.AST):
    """Every ``if <role>_retired:`` block in the module, with its role."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        names = []
        if isinstance(test, ast.Name):
            names = [test.id]
        elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            names = [n.id for n in ast.walk(test.operand)
                     if isinstance(n, ast.Name)]
        for role in ROLES:
            if f"{role}_retired" in names:
                yield role, node


def test_simulation_does_not_zero_a_retired_members_income():
    """The core detector: no retirement gate assigns $0 to a member's income."""
    with open(SIMULATION) as handle:
        tree = ast.parse(handle.read(), filename=SIMULATION)
    offences = []
    for role, node in _retirement_blocks(tree):
        for statement in ast.walk(node):
            targets = []
            if isinstance(statement, ast.Assign):
                targets = [t.id for t in statement.targets
                           if isinstance(t, ast.Name)]
            for target in targets:
                for field in ZEROED_FIELDS:
                    if target in (f"{role}_{field}",):
                        offences.append(
                            f"{SIMULATION}:{statement.lineno} zeroes "
                            f"{target!r} inside the {role}'s retirement gate -- "
                            f"issue #445 requires the transition to keep the "
                            f"income the document dates "
                            f"(simulation._dated_income_for_year)")
    assert not offences, "\n".join(offences)


def test_both_fold_paths_delegate_to_the_dated_income_helper():
    """The transition exists on two paths (the yearly step and the monthly
    one). Wiring it on only one makes the two silently disagree on a retired
    member's income -- the parity the monthly/yearly tests compare, asserted
    here at the shape level so it is pinned even where no fixture exercises
    it."""
    with open(SIMULATION) as handle:
        source = handle.read()
    calls = source.count("_dated_income_for_year(")
    # 1 = the def + 4 = one call per role (primary/spouse) on each of the two
    # fold paths.
    assert calls == 5, (
        "expected the helper defined once and called for BOTH roles on BOTH "
        f"fold paths (5 occurrences), found {calls}")


@pytest.mark.parametrize("module", ["rules_retirement_income.py",
                                    "rules_amt.py"])
def test_no_rule_re_zeroes_income_outside_the_transition(module):
    """The rules downstream read the POST-transition income, so neither may
    re-derive a zeroed version of its own -- that is what kept the retirement
    path pricing the drawdown as if the member had no income at all."""
    path = os.path.join(ROOT, module)
    with open(path) as handle:
        tree = ast.parse(handle.read(), filename=path)
    offences = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = [n.id for n in ast.walk(node.test)
                 if isinstance(n, ast.Name)]
        if not any(name.endswith("_retired") for name in names):
            continue
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if (isinstance(target, ast.Name)
                        and "income" in target.id
                        and isinstance(statement.value, ast.Constant)
                        and statement.value.value == 0.0):
                    offences.append(
                        f"{module}:{statement.lineno} assigns "
                        f"{target.id!r} = 0.0 inside a retirement gate")
    assert not offences, "\n".join(offences)