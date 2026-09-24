"""Every production call to an objective's ``evaluate`` passes a cfg
(issue #290).

``ObjectiveFunction.evaluate(results, cfg=None)`` turns a missing cfg into
``{}``. Before #290 the grid / scipy / Monte Carlo / DP optimizer modes called
``objective.evaluate(results)`` -- so every household they ranked was scored
with no members, no province, no start year and no estate elections: the
"parsed, mapped, then never passed" trap. After #290 the default objective
refuses such a cfg for any household with a registered balance, and in the
modes whose candidate loop swallows exceptions into ``-inf`` (scipy, the DP
claim-fraction loop, Monte Carlo) that refusal would silently rank every
candidate last.

This guard is the ARITY half: a syntactic AST scan of first-party source
(``repo_scan.iter_source_files``, tests excluded) that fails when a call
``<receiver>.evaluate(...)`` -- receiver named like an objective, or an
upper-case objective constant such as ``MAX_NET_BENEFIT`` -- passes fewer than
two arguments. The CONTENT half (the cfg is ``objective.objective_cfg`` of the
run's config, and scores stay finite) is behavioural, in
``tests/test_issue_290_net_benefit_registered_tax.py``.
"""
from __future__ import annotations

import ast
import os

import repo_scan


def _receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_objective_receiver(name: str | None) -> bool:
    if name is None:
        return False
    return 'objective' in name.lower() or (name.isupper() and len(name) > 1)


def _evaluate_calls():
    """(file, line, n_args, source) for every objective ``.evaluate`` call."""
    found = []
    for rel in repo_scan.iter_source_files():
        tree = repo_scan._parse(repo_scan.ROOT, rel)
        if tree is None:
            continue
        with open(os.path.join(repo_scan.ROOT, rel), encoding='utf-8') as f:
            src = f.read()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'evaluate'):
                continue
            if not _is_objective_receiver(_receiver_name(node.func.value)):
                continue
            n_args = len(node.args) + len(node.keywords)
            found.append((rel, node.lineno, n_args, ast.get_source_segment(src, node)))
    return found


def test_the_scan_sees_every_optimizer_mode():
    """Non-vacuity: the scan must actually find the call sites it guards."""
    files = {rel for rel, *_ in _evaluate_calls()}
    for expected in ('optimize.py', 'optimizer.py', 'scipy_optimizer.py',
                     'monte_carlo_optimizer.py', 'dp_optimizer.py'):
        assert expected in files, f"scan found no objective.evaluate call in {expected}"


def test_every_objective_evaluate_call_passes_a_cfg():
    offenders = [f"{rel}:{line}: {seg}" for rel, line, n_args, seg in _evaluate_calls()
                 if n_args < 2]
    assert offenders == [], (
        "objective.evaluate called without a cfg -- it would score the household "
        "with cfg={} (no members, province, start year or estate elections). Pass "
        "objective.objective_cfg(config) (issue #290):\n  " + "\n  ".join(offenders))
