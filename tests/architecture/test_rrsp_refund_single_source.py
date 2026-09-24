#!/usr/bin/env python3
"""Issue #286 detector: the RRSP refund has ONE source.

Before #286 the deduct-now refund was ``(p_rrsp_actual + s_rrsp_actual) *
primary_marginal_rate`` -- an uncapped flat product spelled out in three
places (the deduction rule, ``YearResult.rrsp_tax_savings`` and the
HELOC-paydown refund). Fixing one site and leaving another is exactly how the
overstated refund would come back, so this scan fails on ANY production
multiplication that pairs an ``*rrsp_actual*`` operand with a
``*marginal_rate*`` operand. The refund must be read from the ledger's capped
claim (``ws.rrsp_deduction_savings + ws.spouse_deduction_savings``).

It also forbids a dollar-sized numeric literal (>= 1000) anywhere in
``rrsp_ledger.py``: the deduct-later target once fell back to a hard-coded
``50000`` (DP#2/#13 -- an opinion, not a fact); the cap and targets must come
from the loaded brackets.
"""

import ast
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKIP_DIRS = {'.venv', '.git', 'node_modules', '__pycache__', 'tests', 'build', 'dist'}


def _production_files():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs
                   if d not in _SKIP_DIRS and not d.endswith('.egg-info')
                   and not d.startswith('.')]
        rel_root = os.path.relpath(root, REPO_ROOT)
        if rel_root != '.' and not rel_root.split(os.sep)[0] == 'countries':
            continue
        for f in files:
            if f.endswith('.py'):
                yield os.path.join(root, f)


def _identifiers(node):
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
    return names


def flat_refund_products(source: str):
    """Line numbers of every ``<...rrsp_actual...> * <...marginal_rate...>``
    multiplication in ``source`` (either operand order)."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            left, right = _identifiers(node.left), _identifiers(node.right)

            def has(ids, needle):
                return any(needle in i for i in ids)
            if ((has(left, 'rrsp_actual') and has(right, 'marginal_rate'))
                    or (has(right, 'rrsp_actual') and has(left, 'marginal_rate'))):
                hits.append(node.lineno)
    return hits


def dollar_literals(source: str):
    return [n.lineno for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Constant)
            and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
            and abs(n.value) >= 1000]


class TestRRSPRefundSingleSource(unittest.TestCase):
    def test_detector_catches_the_pre_286_shapes(self):
        """Self-test: the scan must flag the exact shapes #286 removed."""
        old = (
            "a = (ws.p_rrsp_actual + ws.s_rrsp_actual) * ctx.primary_marginal_rate\n"
            "b = ws.sp_rrsp_actual * spouse_marginal_rate\n"
            "c = primary_marginal_rate * (p_rrsp_actual)\n"
        )
        self.assertEqual(flat_refund_products(old), [1, 2, 3])
        self.assertEqual(dollar_literals(
            "t = brackets[3]['min'] if len(brackets) > 3 else 50000\n"), [1])

    def test_no_flat_refund_product_in_production(self):
        offenders = []
        for path in _production_files():
            with open(path, encoding='utf-8') as f:
                src = f.read()
            for line in flat_refund_products(src):
                offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{line}")
        self.assertEqual(
            offenders, [],
            "An RRSP contribution is multiplied by a marginal rate -- the "
            "uncapped flat refund #286 removed. Read the refund from the "
            "ledger's capped claim (ws.rrsp_deduction_savings + "
            "ws.spouse_deduction_savings) instead.")

    def test_scan_reaches_the_three_former_sites(self):
        """Guard the guard: the files that held the flat product are in the
        scanned set (a scan that silently skipped them would pass)."""
        scanned = {os.path.relpath(p, REPO_ROOT) for p in _production_files()}
        for rel in ('rules_contributions.py', 'rules_leverage.py',
                    'simulation_state.py', 'rrsp_ledger.py',
                    os.path.join('countries', 'canada', 'hbp_rules.py')):
            self.assertIn(rel, scanned)

    def test_no_dollar_literal_in_rrsp_ledger(self):
        with open(os.path.join(REPO_ROOT, 'rrsp_ledger.py'), encoding='utf-8') as f:
            lines = dollar_literals(f.read())
        self.assertEqual(
            lines, [],
            "rrsp_ledger.py carries a dollar-sized literal -- a bracket target "
            "or cap must be derived from the loaded brackets (DP#2/#13, #286).")


if __name__ == '__main__':
    unittest.main()
