"""A function-level call graph over first-party source, for reachability guards.

Not a test module (no ``test_`` prefix) -- imported by
``test_unreached_rule_modules.py`` (issues #710/#711/#712/#702).

WHY FUNCTION-LEVEL AND NOT MODULE-LEVEL
---------------------------------------
The defect this exists to catch is a rule that is *implemented, unit-tested, and
called by nothing in production*. A module-level import graph cannot see it, and
would have cleared every one of #710/#711/#712 as "reached":

  * ``countries.canada.amt`` is imported (and called!) by
    ``countries.canada.tax_calc`` -- but only from inside ``compute_total_tax``,
    which is itself never called by production. A module-granular graph sees the
    edge ``tax_calc -> amt``, sees that ``tax_calc`` is reached (production does
    call it, for brackets), and concludes AMT is live. It is not. The engine
    books zero AMT in every run.

  * ``countries.canada.pension_split_optimizer`` has exactly one caller:
    ``countries.canada.cpp_sharing`` -- which is itself unreached. Dead calling
    dead. Only a graph rooted at the *production entry points* and walked
    transitively can tell you that neither is alive.

So the nodes here are ``(module, qualname)`` pairs, the edges are **calls**, and
reachability is a BFS from the production entry points. An ``import`` alone is
never an edge: ``countries/canada/__init__.py`` re-exports every rule module in
the package, and if a bare re-export conferred reachability this guard would
certify the entire dead surface as live. That re-export barrel is precisely the
hole -- see #711's own evidence line, "only re-exported; never invoked".

WHAT IT DELIBERATELY OVER-APPROXIMATES (i.e. errs toward "reached")
------------------------------------------------------------------
A guard that cries wolf gets an allowlist entry and then gets ignored, so every
ambiguity here resolves in favour of calling something *reached*:

  * Every public top-level def in an entry-point module is a root (not just the
    ones some other root calls).
  * Instantiating a class marks **all** its methods reachable -- we do not try to
    prove which methods a caller actually invokes on the instance.
  * An unresolvable receiver (``obj.method()`` where ``obj``'s type is unknown)
    produces no edge, but neither does it produce a finding: findings come only
    from a module having *no* reached public entry point at all.

The consequence is that this scan under-reports (a module can be flagged reached
on a technicality) and does not over-report. A finding is therefore strong
evidence of dead code; a clean result is weaker evidence of live code. That
asymmetry is the right one for a build gate.
"""
from __future__ import annotations

import ast
import collections
import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple

from repo_scan import ROOT, iter_source_files

# The production entry points: a "real run" is one of these. `optimize.py` and
# `simulate.py` are the two CLI front doors; `simulation.py` owns the year loop
# and `simulation_rules.py` the rule fold it dispatches (DP#26 -- `run` is a
# fold, so the fold's rules are production roots even though nothing calls them
# by name: `run_rules` dispatches them out of the RULES registry).
#
# The `rules_*` modules are that same fold: when `simulation_rules.py` was split
# by domain, every `apply_*` rule moved into one of them, and they are roots for
# exactly the reason `simulation_rules` is -- `run_rules` dispatches them out of
# the registry, so no caller names them. They are listed EXPLICITLY rather than
# globbed: this guard's whole point is that "reached" must be a claim someone
# made on purpose, and a pattern would silently adopt any future `rules_*.py`
# file as a production root without anyone deciding it is one.
ENTRY_MODULES: Tuple[str, ...] = (
    "optimize",
    "simulate",
    "simulation",
    "simulation_rules",
    "rules_amt",
    "rules_contributions",
    "rules_debt",
    "rules_disposition",
    "rules_drawdown",
    "rules_growth",
    "rules_leverage",
    "rules_registered_plans",
    "rules_retirement_income",
    "rules_solvency",
    "rules_tuition_credit",
)

MODULE_BODY = "<module>"

Node = Tuple[str, str]  # (module, qualname)


def module_name(relpath: str) -> str:
    """`countries/canada/amt.py` -> `countries.canada.amt`."""
    dotted = relpath[:-3].replace(os.sep, ".")
    if dotted.endswith(".__init__"):
        dotted = dotted[: -len(".__init__")]
    return dotted


@dataclass
class _ModuleFacts:
    tree: ast.AST
    # local name -> (source module, original name), from `from X import y [as z]`
    from_imports: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    # local alias -> dotted module, from `import x.y [as z]`
    module_aliases: Dict[str, str] = field(default_factory=dict)
    # public top-level defs: name -> "func" | "class"
    definitions: Dict[str, str] = field(default_factory=dict)
    # class name -> its method names
    methods: Dict[str, Set[str]] = field(default_factory=dict)
    # qualname -> {(callee_name, receiver_or_None)}
    calls: Dict[str, Set[Tuple[str, Optional[str]]]] = field(default_factory=dict)


class CallGraph:
    """Function-level call graph of the first-party tree, rooted at production."""

    def __init__(self, root: str = ROOT, entry_modules: Tuple[str, ...] = ENTRY_MODULES):
        self.root = root
        self.entry_modules = entry_modules
        self.facts: Dict[str, _ModuleFacts] = {}
        self._build_facts()
        self.reached: Set[Node] = self._walk()

    # -- fact collection -------------------------------------------------

    def _build_facts(self) -> None:
        for relpath in sorted(iter_source_files(self.root)):
            mod = module_name(relpath)
            try:
                with open(os.path.join(self.root, relpath), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=relpath)
            except (SyntaxError, UnicodeDecodeError):
                continue
            f = _ModuleFacts(tree=tree)
            self.facts[mod] = f
            pkg = mod.rsplit(".", 1)[0] if "." in mod else ""

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.level:  # relative import
                        base = pkg
                        for _ in range(node.level - 1):
                            base = base.rsplit(".", 1)[0] if "." in base else ""
                        src = f"{base}.{node.module}" if node.module else base
                    else:
                        src = node.module or ""
                    for alias in node.names:
                        f.from_imports[alias.asname or alias.name] = (src, alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        local = alias.asname or alias.name.split(".")[0]
                        f.module_aliases[local] = alias.name

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    f.definitions[node.name] = "func"
                elif isinstance(node, ast.ClassDef):
                    f.definitions[node.name] = "class"
                    f.methods[node.name] = {
                        sub.name for sub in node.body
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }

            self._collect_calls(f, tree)

    @staticmethod
    def _collect_calls(f: _ModuleFacts, tree: ast.AST) -> None:
        """Attribute every Call to the top-level def / method / module body around it."""

        def visit(node: ast.AST, scope: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # A nested def's calls belong to the enclosing top-level scope.
                    visit(child, child.name if scope == MODULE_BODY else scope)
                    continue
                if isinstance(child, ast.ClassDef):
                    for sub in child.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            visit(sub, f"{child.name}.{sub.name}")
                        else:
                            visit(sub, scope)
                    continue
                if isinstance(child, ast.Call):
                    fn = child.func
                    if isinstance(fn, ast.Name):
                        f.calls.setdefault(scope, set()).add((fn.id, None))
                    elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                        f.calls.setdefault(scope, set()).add((fn.attr, fn.value.id))
                visit(child, scope)

        visit(tree, MODULE_BODY)

    # -- name resolution -------------------------------------------------

    def _resolve(self, mod: str, name: str, depth: int = 0) -> Optional[Node]:
        """Resolve `name` as seen in `mod` to the (module, name) that DEFINES it.

        Follows re-export chains, so `from countries.canada import compute_total_tax`
        lands on `countries.canada.tax_calc.compute_total_tax` rather than on the
        `countries/canada/__init__.py` barrel that merely re-exports it.
        """
        if depth > 8:  # cycle guard
            return None
        f = self.facts.get(mod)
        if f is None:
            return None
        if name in f.definitions:
            return (mod, name)
        entry = f.from_imports.get(name)
        if entry:
            src, original = entry
            if src in self.facts:
                return self._resolve(src, original, depth + 1)
        return None

    def _targets(self, mod: str, callee: str, receiver: Optional[str]) -> list[Node]:
        f = self.facts[mod]
        out: list[Node] = []
        if receiver is None:
            hit = self._resolve(mod, callee)
            if hit:
                out.append(hit)
            return out

        # `receiver.callee(...)` -- receiver may be an imported module...
        alias = f.module_aliases.get(receiver)
        if alias and alias in self.facts:
            out.append((alias, callee))

        entry = f.from_imports.get(receiver)
        if entry:
            candidate = f"{entry[0]}.{entry[1]}"
            if candidate in self.facts:  # `from pkg import submodule`; submodule.fn()
                out.append((candidate, callee))
            else:
                # receiver is a CLASS (`Ledger.from_dict(...)`) or a re-exported name
                hit = self._resolve(mod, receiver)
                if hit:
                    out.append(hit)
        return out

    # -- reachability ----------------------------------------------------

    def _walk(self) -> Set[Node]:
        roots: list[Node] = []
        for entry in self.entry_modules:
            f = self.facts.get(entry)
            if f is None:
                continue
            roots.append((entry, MODULE_BODY))
            roots.extend(
                (entry, name) for name in f.definitions if not name.startswith("_")
            )

        seen: Set[Node] = set(roots)
        queue = collections.deque(roots)
        while queue:
            mod, qual = queue.popleft()
            f = self.facts.get(mod)
            if f is None:
                continue
            for callee, receiver in f.calls.get(qual, ()):
                for target in self._targets(mod, callee, receiver):
                    if target in seen or target[0] not in self.facts:
                        continue
                    seen.add(target)
                    queue.append(target)
                    # Instantiating a class reaches every method on it: we do not
                    # try to prove which ones the holder of the instance calls.
                    tf = self.facts[target[0]]
                    if tf.definitions.get(target[1]) == "class":
                        for meth in tf.methods.get(target[1], ()):
                            node = (target[0], f"{target[1]}.{meth}")
                            if node not in seen:
                                seen.add(node)
                                queue.append(node)
        return seen

    # -- queries ---------------------------------------------------------

    def public_entry_points(self, mod: str) -> Set[str]:
        """Public top-level defs (functions and classes) of `mod`."""
        f = self.facts.get(mod)
        if f is None:
            return set()
        return {n for n in f.definitions if not n.startswith("_")}

    def is_reached(self, mod: str, name: str) -> bool:
        return (mod, name) in self.reached

    def reached_entry_points(self, mod: str) -> Set[str]:
        return {n for n in self.public_entry_points(mod) if self.is_reached(mod, n)}
