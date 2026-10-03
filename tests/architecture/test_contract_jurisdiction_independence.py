"""The contract ingestion layer must not DECIDE jurisdiction policy.

Issue #364 (`[refactor]` deepen the CPP estimate module). The adapter
``contract_people.py`` used to own the CPP program: which plan a province
runs (``"qpp" if residency["province"] == "quebec" else "cpp"``), where the
contributory period ends when a household models an early retirement, and how
the base tier and the CPP2 tier aggregate into one monthly figure. Those are
program facts, so they moved into ``countries/canada/cpp_estimator.py`` and the
adapter now makes ONE call and maps the answer.

This module is the enforcement half of that move. It states, as AST rules:

* **DP#25** — no ``contract_*.py`` mapper imports ``countries.*`` at module
  scope. Lazy, function-scope imports of a jurisdiction's domain math remain
  legal and are NOT banned here: the contract layer IS Canada's ingestion
  point by construction, and four mappers (accounts / estate / people /
  property) already call into Canada modules for exactly that reason. A ban
  at ANY scope would force this repository to rewrite four more adapters and
  is therefore deliberately NOT claimed here. What it DOES claim is the half
  that is free and true today: no mapper may need a jurisdiction package
  merely to be IMPORTED.

* **DP#10** — no mapper may SELECT a contributory plan by a hardcoded literal.
  Which plan a province runs is a question the CPP module answers
  (``resolve_plan``); an adapter that answers it with a ternary has taken the
  program's policy back.

* **DP#10** — the base+CPP2 aggregation is stated once, in the module that
  owns the two tiers. Any other file naming ``cpp2_age_65_monthly`` has
  re-derived it.

Two widenings are known-blocked and deliberately not asserted here, so this
guard does not pretend to be broader than it is:

1. A blanket ban on province-name literals in ``contract_*.py`` is refused by
   ``contract_accounts.py`` (``"is_quebec_resident": purchase_province ==
   "quebec"``) — a real, separate Canada policy decision living in the
   ingestion layer. It is out of scope for the CPP deepening.
2. A ban on ``countries`` imports at ANY scope — see above.
"""
import ast
import glob
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: The contributory plan identifiers the CPP module owns. ``"cpp"`` also
#: appears as a CONTRACT KEY name (``benefits.cpp`` / ``entitlements.cpp``),
#: which is mapping and stays legal — this guard fires only where a plan name
#: is COMPARED or used to CHOOSE, never where it is looked up.
PLAN_NAMES = frozenset({"cpp", "qpp"})


def _contract_mappers():
    return sorted(
        os.path.basename(p)
        for p in glob.glob(os.path.join(REPO, "contract_*.py"))
    )


def _tree(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _plan_selections(tree):
    """Nodes that CHOOSE a plan: a plan name as a DIRECT operand.

    Deliberately shallow. ``("cpp", "oas")`` inside a key-presence check
    (``any(benefits.get(k) for k in ...)``) is a CONTRACT KEY lookup — the
    document says ``benefits.cpp``, and reading it is this layer's whole job.
    What is not this layer's job is a plan name sitting where a DECISION goes:
    a comparator, or a ternary branch.
    """
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            for operand in operands:
                if (isinstance(operand, ast.Constant)
                        and operand.value in PLAN_NAMES):
                    found.append((node.lineno, operand.value))
        elif isinstance(node, ast.IfExp):
            for branch in (node.body, node.orelse):
                if (isinstance(branch, ast.Constant)
                        and branch.value in PLAN_NAMES):
                    found.append((node.lineno, branch.value))
    return found


CPP_ESTIMATOR = os.path.join("countries", "canada", "cpp_estimator.py")


class TestContractLayerHasNoModuleScopeJurisdictionImport(unittest.TestCase):
    """DP#25: a mapper must import without a jurisdiction package present."""

    def test_no_contract_mapper_imports_countries_at_module_scope(self):
        offenders = []
        for name in _contract_mappers():
            for node in _tree(name).body:
                modules = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
                elif isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                modules = [m for m in modules if m.split(".")[0] == "countries"]
                if modules:
                    offenders.append(f"{name}:{node.lineno} {modules}")
        self.assertEqual(
            offenders, [],
            "a contract mapper imports `countries.*` at module scope, so the "
            "ingestion layer cannot load without a jurisdiction package "
            f"(DP#25): {offenders}",
        )

    def test_the_guarded_file_set_is_not_empty(self):
        """A guard that silently matches nothing is not a guard."""
        self.assertGreaterEqual(len(_contract_mappers()), 10)


class TestContractLayerDoesNotSelectAContributoryPlan(unittest.TestCase):
    """DP#10: the CPP module answers 'which plan', not the adapter."""

    def test_no_contract_mapper_compares_or_branches_on_a_plan_name(self):
        offenders = {}
        for name in _contract_mappers():
            found = _plan_selections(_tree(name))
            if found:
                offenders[name] = found
        self.assertEqual(
            offenders, {},
            "a contract mapper selects a CPP/QPP plan by hardcoded literal; "
            "that decision belongs to countries/canada/cpp_estimator.py "
            f"(DP#10): {offenders}",
        )

    def test_the_removed_ternary_shape_is_still_recognised(self):
        """Guard the GUARD: the exact shape this refactor deleted must fail.

        A rule that cannot fail is indistinguishable from no rule, so the
        canonical counter-example is parsed and asserted to trip — and the
        contract-key lookup it must NOT confuse itself with is asserted to
        pass, which is the other half of "the detector is sharp".
        """
        counter_example = ast.parse(
            'plan = "qpp" if residency["province"] == "quebec" else "cpp"'
        )
        self.assertTrue(
            _plan_selections(counter_example),
            "the plan-selection detector no longer recognises the hardcoded "
            "ternary it was written for — the guard has gone blind",
        )
        key_lookup = ast.parse(
            'if any(benefits.get(k) for k in ("cpp", "oas")):\n    x = 1'
        )
        self.assertFalse(
            _plan_selections(key_lookup),
            "the detector now fires on a contract KEY lookup "
            "(`benefits.get(\"cpp\")`), which is mapping, not a decision",
        )


class TestCppAggregationRuleHasOneHome(unittest.TestCase):
    """DP#10: base tier + CPP2 tier is stated once, where the tiers live."""

    def _names_cpp2_age_65(self, rel):
        tree = _tree(rel)
        return [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and n.attr == "cpp2_age_65_monthly"
        ]

    def test_only_the_cpp_estimator_aggregates_the_cpp2_tier(self):
        owner = CPP_ESTIMATOR
        self.assertTrue(
            self._names_cpp2_age_65(owner),
            f"{owner} must be where the CPP2 tier is summed",
        )
        offenders = {}
        for path in sorted(glob.glob(os.path.join(REPO, "*.py"))):
            rel = os.path.relpath(path, REPO)
            if rel == owner:
                continue
            lines = self._names_cpp2_age_65(rel)
            if lines:
                offenders[rel] = lines
        self.assertEqual(
            offenders, {},
            "a root-layer module re-derives the base+CPP2 aggregation; it is "
            "stated once in countries/canada/cpp_estimator.py "
            f"(DP#10): {offenders}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()