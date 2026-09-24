"""A death-transition rule that no production path reaches must not look finished.

Issue #213 (detection scope, per the owner's comment); wiring is #72.

Four death-transition rules exist as pure functions, carry unit tests, and are
exported, but no production code path calls any of them: survivor CPP/QPP
(``compute_survivor_benefit``), HBP on death (``HBPAccount.on_death``), FHSA on
death (``FHSAAccount.on_death``; the issue calls it ``FHSA.on_death``, the real
class is ``FHSAAccount``), and locked-in on death
(``LockedInAccount.death_disposition`` -> ``death_benefit_disposition``). The
fold has no death path, so when a household member is assumed to die before the
horizon the trajectory is unchanged by the death. Wiring them needs the
mid-horizon mortality lifecycle, which is #72. This guard makes the gap
*detectable* instead (DP#11: a unit test calling the rule directly says nothing
about who calls it; DP#28: programs exit on a schedule, and death is one; DP#32:
absence must be loud).

WHAT THE GUARD DOES
-------------------
1. **Discovery** (``discover_death_rules``): an AST scan of every production
   (non-test) ``.py`` file for top-level functions and direct class methods whose
   snake_case name carries a death token (``DEATH_TOKENS``). It runs
   independently of the triage record, so deleting a triage row can never delete
   the check (the "missing input deleting an obligation" trap).
2. **Reachability** (``DeathRuleGraph``): a BFS from the production entry points
   (``call_graph.ENTRY_MODULES``), method-precise (see below).
3. **Classification** (``classify``): every discovered rule must be reached or
   triaged; a triaged rule that is now reached is *stale*; a triaged row that
   names no discovered rule is a *ghost*. The triage record is consulted only to
   excuse a finding, never to decide what counts as a death rule.

WHY NOT REUSE ``CallGraph`` AS-IS
---------------------------------
``call_graph.CallGraph`` (the #710/#711/#712/#702 guard) deliberately
over-approximates: instantiating a class marks **every** method on it reached.
That is right at module granularity, but wrong here. Production instantiates
both ``HBPAccount`` and ``FHSAAccount`` (``simulation_state.
_apply_first_home_to_account``), so the base graph reports
``HBPAccount.on_death`` and ``FHSAAccount.on_death`` as REACHED, and a guard
built on it would start with two of the four dead rules falsely cleared.
``DeathRuleGraph`` overrides only ``_walk``: instantiating a class reaches the
class node and its dunder methods, and a plain method is reached only when its
name is referenced as an attribute (``x.on_death``, or ``getattr(x,
'on_death')``) inside reached code.

WHAT A GREEN RESULT DOES NOT PROVE
----------------------------------
Discovery sees only top-level defs and direct class methods whose snake_case
tokens hit ``DEATH_TOKENS``. It misses camelCase names, nested classes, defs
under module-level ``if`` blocks, and death rules named without a death token
(a hypothetical ``deemed_disposition`` or ``spousal_rollover``). Method reach is
name-granular: a reached ``.on_death`` anywhere reaches every class's
``on_death``. So a finding is strong evidence of a dead death rule, and a clean
result is weaker evidence that no death rule anywhere is dead.

THE TRIAGE RECORD IS NOT AN ALLOWLIST
-------------------------------------
Per AGENTS.md: "When a guard fires, fix the code -- do not add an allowlist
entry." ``TRIAGED_UNREACHED_DEATH_RULES`` is a record of known, filed debt. A row
needs an open issue and a mechanism, and it cannot rot silently: a row whose
rule becomes reached fails as stale, a row naming a missing rule fails as a
ghost, and deleting a row while its rule is still dead fails as untriaged.
"""
from __future__ import annotations

import ast
import collections
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, Mapping, Set, Tuple

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from call_graph import CallGraph, ENTRY_MODULES, MODULE_BODY, module_name  # noqa: E402,F401
from repo_scan import ROOT, iter_source_files  # noqa: E402

Node = Tuple[str, str]  # (module, qualname)

# A def is a death-transition rule when one of its snake_case tokens is here.
# Tokens, not substrings, so `subsidies` / `studies` cannot match `die`.
# `estate` is deliberately NOT a token: it names terminal valuation (the
# objective's after-tax estate, model-fidelity helpers), not a death transition,
# and would sweep in roughly fifteen helpers that are not death rules.
DEATH_TOKENS = frozenset({
    "death", "deaths", "die", "dies", "died", "dying",
    "survivor", "survivors", "surviving",
    "deceased", "decedent",
    "widow", "widowed", "widower",
    "bereavement", "successor", "posthumous",
})


def _is_test_like(relpath_or_module: str) -> bool:
    """True for a test file's basename: ``test_*.py``, ``*_test.py``, ``conftest.py``.

    Accepts a relpath (``a/test_x.py``) or a dotted module (``a.test_x``).
    """
    if relpath_or_module.endswith(".py"):
        base = os.path.basename(relpath_or_module)[:-3]
    else:
        base = relpath_or_module.rsplit(".", 1)[-1]
    return base.startswith("test_") or base.endswith("_test") or base == "conftest"


def is_death_shaped(def_name: str) -> bool:
    """True when the def's snake_case tokens intersect ``DEATH_TOKENS``."""
    tokens = set(def_name.lower().strip("_").split("_"))
    return bool(tokens & DEATH_TOKENS)


def discover_death_rules(root: str = ROOT) -> Dict[Node, str]:
    """Every death-shaped top-level def / direct method in production source.

    Returns ``{(module, qualname): "relpath:lineno"}``. Parses WITHOUT a
    try/except: an unparseable production file raises rather than silently
    shrinking the set (DP#32; ``CallGraph._build_facts`` swallows it). Never
    reads the triage record.
    """
    found: Dict[Node, str] = {}
    for relpath in sorted(iter_source_files(root)):
        if _is_test_like(relpath):
            continue
        with open(os.path.join(root, relpath), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=relpath)
        mod = module_name(relpath)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if is_death_shaped(node.name):
                    found[(mod, node.name)] = f"{relpath}:{node.lineno}"
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and is_death_shaped(sub.name)):
                        found[(mod, f"{node.name}.{sub.name}")] = f"{relpath}:{sub.lineno}"
    return found


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


class DeathRuleGraph(CallGraph):
    """``CallGraph`` with a method-precise walk.

    Same facts, same roots, same name resolver (``_targets``/``_resolve``, with
    re-export chasing) as the base; only ``_walk`` differs:

    * Edges come from every ``Name`` load and every ``name.attr`` load in a
      reached scope (a superset of the base's calls: a function passed as a
      callback is reached too). Imports are never edges, and string constants
      (``__all__``, docstrings) are never references.
    * Reaching a class (instantiation, annotation, ``Cls.attr``) reaches the class
      node and its DUNDER methods only, not its plain methods.
    * A plain method ``m`` is reached when ``.m`` is loaded as an attribute on any
      receiver, or named by ``getattr(x, 'm')`` / ``hasattr(x, 'm')``, inside a
      reached scope. Resolution is by NAME: every class defining ``m`` is reached.

    That last point is the residual over-approximation. If #72 wires one
    ``on_death`` (say HBP's), every other class's ``on_death`` reads reached too.
    Before deleting a triage row because the stale test fired, confirm that THIS
    rule is wired, not merely a same-named method on another class.
    """

    def _scope_refs(self, tree: ast.AST):
        """Per-scope refs, attributed exactly as ``CallGraph._collect_calls`` does."""
        refs: Dict[str, Set[Tuple[str, object]]] = collections.defaultdict(set)
        method_refs: Dict[str, Set[str]] = collections.defaultdict(set)

        def record(node: ast.AST, scope: str) -> None:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                refs[scope].add((node.id, None))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                method_refs[scope].add(node.attr)
                if isinstance(node.value, ast.Name):
                    refs[scope].add((node.attr, node.value.id))
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("getattr", "hasattr")
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                attr = node.args[1].value
                method_refs[scope].add(attr)
                if isinstance(node.args[0], ast.Name):
                    refs[scope].add((attr, node.args[0].id))

        def visit(node: ast.AST, scope: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name if scope == MODULE_BODY else scope)
                    continue
                if isinstance(child, ast.ClassDef):
                    for sub in child.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            visit(sub, f"{child.name}.{sub.name}")
                        else:
                            visit(sub, scope)
                    continue
                record(child, scope)
                visit(child, scope)

        visit(tree, MODULE_BODY)
        return refs, method_refs

    def _walk(self) -> Set[Node]:
        refs_by_mod = {}
        methods_by_name: Dict[str, list] = collections.defaultdict(list)
        for mod, f in self.facts.items():
            if _is_test_like(mod):
                continue
            refs_by_mod[mod] = self._scope_refs(f.tree)
            for cls, meths in f.methods.items():
                for meth in meths:
                    methods_by_name[meth].append((mod, f"{cls}.{meth}"))

        roots: list = []
        for entry in self.entry_modules:
            f = self.facts.get(entry)
            if f is None or _is_test_like(entry):
                continue
            roots.append((entry, MODULE_BODY))
            roots.extend(
                (entry, name) for name in f.definitions if not name.startswith("_")
            )

        seen: Set[Node] = set()
        queue: collections.deque = collections.deque()

        def add(node: Node) -> None:
            if node in seen or node[0] not in refs_by_mod:
                return
            seen.add(node)
            queue.append(node)
            tf = self.facts[node[0]]
            if tf.definitions.get(node[1]) == "class":
                for meth in tf.methods.get(node[1], ()):
                    if _is_dunder(meth):
                        add((node[0], f"{node[1]}.{meth}"))

        for r in roots:  # a root that is a class reaches its dunders too
            add(r)

        while queue:
            mod, qual = queue.popleft()
            refs, method_refs = refs_by_mod[mod]
            for name, receiver in refs.get(qual, ()):
                for target in self._targets(mod, name, receiver):
                    add(target)
            for meth in method_refs.get(qual, ()):
                for target in methods_by_name.get(meth, ()):
                    add(target)
        return seen


# ---------------------------------------------------------------------------
# The triage record. NOT an allowlist: every row is known, filed debt with an
# open issue and a mechanism. Adding a row is a reviewed diff that needs both;
# "When a guard fires, fix the code -- do not add an allowlist entry"
# (AGENTS.md). A row whose rule becomes reached fails as stale and must be
# deleted in the same PR that wires it.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Triage:
    issues: Tuple[str, ...]
    mechanism: str
    note: str


TRIAGED_UNREACHED_DEATH_RULES: Dict[Node, Triage] = {
    ("countries.canada.cpp_sharing", "compute_survivor_benefit"): Triage(
        issues=("#213", "#72"),
        mechanism="no death path in the fold",
        note=(
            "Survivor CPP/QPP is never computed: when a spouse dies before the "
            "horizon the deceased's CPP keeps paying and the survivor receives "
            "no survivor pension. The contract also has no field for a "
            "survivor's pension ($defs/benefit_claim), so wiring needs a schema "
            "addition too (#213)."
        ),
    ),
    ("countries.canada.hbp_rules", "HBPAccount.on_death"): Triage(
        issues=("#213", "#72"),
        mechanism="no death path in the fold",
        note=(
            "An open Home Buyers' Plan balance is never included in the "
            "deceased's income nor assumed by a surviving spouse on death."
        ),
    ),
    ("countries.canada.fhsa", "FHSAAccount.on_death"): Triage(
        issues=("#213", "#72"),
        mechanism="no death path in the fold",
        note=(
            "An FHSA is never disposed of on the holder's death: no successor "
            "holder transfer, no taxable inclusion. (The issue names it "
            "FHSA.on_death; the class is FHSAAccount.)"
        ),
    ),
    ("countries.canada.locked_in_account", "LockedInAccount.death_disposition"): Triage(
        issues=("#213", "#72"),
        mechanism="no death path in the fold",
        note=(
            "A LIRA/LIF balance is never settled on the holder's death: no "
            "spousal rollover, no taxable lump sum to the estate."
        ),
    ),
    ("countries.canada.locked_in_account", "death_benefit_disposition"): Triage(
        issues=("#213", "#72"),
        mechanism="no death path in the fold",
        note=(
            "Its only production-source caller is "
            "LockedInAccount.death_disposition, itself unreached (dead calling "
            "dead). Reachability is transitive from the entry points, so it is "
            "recorded as its own row."
        ),
    ),
}

ISSUE_213_KEYS: Tuple[Node, ...] = (
    ("countries.canada.cpp_sharing", "compute_survivor_benefit"),
    ("countries.canada.hbp_rules", "HBPAccount.on_death"),
    ("countries.canada.fhsa", "FHSAAccount.on_death"),
    ("countries.canada.locked_in_account", "LockedInAccount.death_disposition"),
    ("countries.canada.locked_in_account", "death_benefit_disposition"),
)

_ISSUE_RE = re.compile(r"^#\d+$")


def classify(discovered, reached, triaged: Mapping):
    """``(untriaged_unreached, stale, ghosts)``, each sorted.

    ``triaged`` only EXCUSES a finding; it never decides what is a death rule.
    """
    untriaged = sorted(n for n in discovered if n not in reached and n not in triaged)
    stale = sorted(n for n in triaged if n in reached)
    ghosts = sorted(n for n in triaged if n not in discovered)
    return untriaged, stale, ghosts


def _fmt(node: Node) -> str:
    return f"{node[0]}:{node[1]}"


# ---------------------------------------------------------------------------
# Production tree
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def graph():
    return DeathRuleGraph()


@pytest.fixture(scope="module")
def discovered():
    return discover_death_rules()


def test_every_death_transition_rule_is_reached_or_triaged(graph, discovered):
    untriaged, _, _ = classify(set(discovered), graph.reached, TRIAGED_UNREACHED_DEATH_RULES)
    assert not untriaged, (
        "Death-transition rule(s) with no production caller and no triage row. "
        "Wire it (the death path is #72) or add a cited Triage row with an open "
        "issue and a mechanism:\n" + "\n".join(
            f"  {_fmt(n)}  ({discovered[n]})" for n in untriaged)
    )


def test_triaged_death_rules_are_not_stale(graph):
    _, stale, _ = classify(set(), graph.reached, TRIAGED_UNREACHED_DEATH_RULES)
    assert not stale, (
        "Triaged death-transition rule(s) now reached from production; delete "
        "the row and update #213/#72. Method reach is by NAME, so first confirm "
        "that THIS rule is wired, not merely a same-named method on another "
        "class (deleting the row for a still-dead rule is a silent false green):\n"
        + "\n".join(f"  {_fmt(n)}" for n in stale)
    )


def test_triaged_entries_name_real_death_rules(graph, discovered):
    _, _, ghosts = classify(set(discovered), set(), TRIAGED_UNREACHED_DEATH_RULES)
    lines = []
    for mod, qual in ghosts:
        f = graph.facts.get(mod)
        if "." in qual:
            cls, meth = qual.split(".", 1)
            exists = f is not None and meth in f.methods.get(cls, ())
        else:
            exists = f is not None and qual in f.definitions
        why = ("present but not death-shaped (no DEATH_TOKENS token)" if exists
               else "absent from production source (renamed, deleted, or typo)")
        lines.append(f"  {mod}:{qual} -- {why}")
    assert not ghosts, "Triage row(s) naming no discovered death rule:\n" + "\n".join(lines)


def test_every_triaged_entry_cites_an_issue_and_a_mechanism():
    bad = []
    for node, t in TRIAGED_UNREACHED_DEATH_RULES.items():
        if not t.issues:
            bad.append(f"{_fmt(node)}: cites no issue")
        for issue in t.issues:
            if not _ISSUE_RE.match(issue):
                bad.append(f"{_fmt(node)}: malformed issue {issue!r} (want '#NNN')")
        if not t.mechanism.strip():
            bad.append(f"{_fmt(node)}: no mechanism")
    assert not bad, "Uncited triage row(s):\n" + "\n".join(bad)


def test_issue_213_rules_are_triaged_with_the_mandated_citation():
    bad = []
    for node in ISSUE_213_KEYS:
        t = TRIAGED_UNREACHED_DEATH_RULES.get(node)
        if t is None:
            bad.append(f"{_fmt(node)}: missing triage row")
            continue
        if "#213" not in t.issues or "#72" not in t.issues:
            bad.append(f"{_fmt(node)}: must cite #213 and #72, cites {t.issues}")
        if t.mechanism != "no death path in the fold":
            bad.append(f"{_fmt(node)}: mechanism {t.mechanism!r}")
    assert not bad, "\n".join(bad)


def test_discovery_covers_the_issue_213_rules(graph, discovered):
    missing = [n for n in ISSUE_213_KEYS if n not in discovered]
    assert not missing, (
        "The detector no longer discovers the #213 rules (narrowed DEATH_TOKENS, "
        "rename, or skipped file) -- a detector that finds nothing must be loud: "
        + ", ".join(_fmt(n) for n in missing)
    )
    absent = sorted({m for m, _ in ISSUE_213_KEYS if m not in graph.facts})
    assert not absent, f"Module(s) missing from the call graph's facts: {absent}"


def test_graph_is_method_precise(graph):
    for mod, cls in (("countries.canada.hbp_rules", "HBPAccount"),
                     ("countries.canada.fhsa", "FHSAAccount")):
        assert graph.is_reached(mod, cls), (
            f"{cls} should be reached: simulation_state._apply_first_home_to_account "
            "instantiates it"
        )
        assert not graph.is_reached(mod, f"{cls}.on_death"), (
            f"{cls}.on_death reads reached. Either instantiation is leaking plain "
            "methods (the base CallGraph does exactly that and would falsely "
            "clear this rule), or production now references `.on_death` (then "
            "the stale test fires too; once #72 wires it, retarget this check "
            "to a method production still never references)."
        )


def test_positive_controls_are_reached(graph, discovered):
    for node in (("countries.canada.estate", "tax_on_capital_gain_at_death"),
                 ("contract_estate", "_assumed_death_date")):
        assert node in discovered, f"positive control {_fmt(node)} not discovered"
        assert node in graph.reached, (
            f"positive control {_fmt(node)} not reached -- the walk may be "
            "reading everything as unreached, which would make the guard vacuous"
        )


# ---------------------------------------------------------------------------
# Synthetic trees: the three sabotage checks as permanent regression tests.
# Parsed only, never imported.
# ---------------------------------------------------------------------------
_ACCT = (
    "class Acct:\n"
    "    def __init__(self):\n"
    "        self.balance = 1000.0\n"
    "\n"
    "    def grow(self):\n"
    "        return self.balance * 2\n"
    "\n"
    "    def on_death(self):\n"
    "        return self.balance\n"
    "\n"
    "\n"
    "def survivor_payout():\n"
    "    return 500.0\n"
)

_RUN = (
    "def run():\n"
    "    from countries.acct import Acct\n"
    "    a = Acct()\n"
    "    a.grow()\n"
)

ON_DEATH = ("countries.acct", "Acct.on_death")
PAYOUT = ("countries.acct", "survivor_payout")


def _write_tree(tmp_path, files: Mapping[str, str]) -> str:
    base = {"countries/__init__.py": "", "countries/acct.py": _ACCT, "simulation.py": _RUN}
    base.update(files)
    for rel, src in base.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return str(tmp_path)


def _analyse(root: str, triaged: Mapping):
    g = DeathRuleGraph(root=root, entry_modules=("simulation",))
    d = discover_death_rules(root)
    return g, d, classify(set(d), g.reached, triaged)


def test_synthetic_uncalled_death_rule_fails_untriaged(tmp_path):
    root = _write_tree(tmp_path, {})
    g, d, (untriaged, stale, ghosts) = _analyse(root, {})
    assert ("countries.acct", "Acct") in g.reached
    assert ("countries.acct", "Acct.grow") in g.reached
    assert ON_DEATH in untriaged and PAYOUT in untriaged
    assert d[ON_DEATH] == "countries/acct.py:8"


def test_synthetic_triaged_rule_that_gains_a_caller_is_stale(tmp_path):
    t = Triage(("#1",), "m", "n")
    root = _write_tree(tmp_path / "method", {"simulation.py": _RUN + "    a.on_death()\n"})
    _, _, (untriaged, stale, _) = _analyse(root, {ON_DEATH: t, PAYOUT: t})
    assert stale == [ON_DEATH] and untriaged == []

    root = _write_tree(tmp_path / "func", {"simulation.py": _RUN + (
        "    from countries.acct import survivor_payout\n"
        "    survivor_payout()\n")})
    _, _, (untriaged, stale, _) = _analyse(root, {ON_DEATH: t, PAYOUT: t})
    assert stale == [PAYOUT] and untriaged == []

    root = _write_tree(tmp_path / "getattr", {"simulation.py": _RUN + (
        "    getattr(a, 'on_death')\n")})
    _, _, (_, stale, _) = _analyse(root, {ON_DEATH: t, PAYOUT: t})
    assert stale == [ON_DEATH]


def test_synthetic_deleted_entry_fails(tmp_path):
    t = Triage(("#1",), "m", "n")
    root = _write_tree(tmp_path, {})
    assert _analyse(root, {ON_DEATH: t, PAYOUT: t})[2] == ([], [], [])
    untriaged, stale, ghosts = _analyse(root, {PAYOUT: t})[2]
    assert untriaged == [ON_DEATH] and stale == [] and ghosts == []


def test_synthetic_dead_calling_dead_is_dead(tmp_path):
    acct = _ACCT.replace(
        "    def on_death(self):\n        return self.balance\n",
        "    def on_death(self):\n        return survivor_payout()\n")
    root = _write_tree(tmp_path, {"countries/acct.py": acct})
    g, _, (untriaged, _, _) = _analyse(root, {})
    assert ON_DEATH not in g.reached and PAYOUT not in g.reached
    assert untriaged == [ON_DEATH, PAYOUT]


def test_synthetic_import_and_reexport_is_not_a_call(tmp_path):
    root = _write_tree(tmp_path, {
        "countries/__init__.py": (
            '"""survivor_payout on_death"""\n'
            "from .acct import survivor_payout\n"
            "__all__ = ['survivor_payout', 'on_death']\n"),
        "simulation.py": _RUN + (
            "    from countries import survivor_payout\n"
            "    note = 'survivor_payout on_death'  # on_death()\n"
            "    return note\n"),
    })
    g, _, (untriaged, _, _) = _analyse(root, {})
    assert PAYOUT not in g.reached and ON_DEATH not in g.reached
    assert untriaged == [ON_DEATH, PAYOUT]


def test_synthetic_test_file_caller_does_not_count(tmp_path):
    caller = (
        "from countries.acct import Acct, survivor_payout\n"
        "def helper():\n"
        "    Acct().on_death()\n"
        "    survivor_payout()\n"
        "def test_on_death_dies():\n"
        "    helper()\n"
    )
    root = _write_tree(tmp_path, {
        "tests/test_acct.py": caller,
        "test_x.py": caller,
        "conftest.py": caller,
        "acct_test.py": caller,
        "simulation.py": _RUN + (
            "    from test_x import helper\n"
            "    helper()\n"
            "    from conftest import helper as h2\n"
            "    h2()\n"
            "    from acct_test import helper as h3\n"
            "    h3()\n"),
    })
    g, d, (untriaged, _, _) = _analyse(root, {})
    assert ON_DEATH not in g.reached and PAYOUT not in g.reached
    assert untriaged == [ON_DEATH, PAYOUT]
    # A death-shaped def in a test file is not a production rule either.
    assert set(d) == {ON_DEATH, PAYOUT}


def test_synthetic_unparseable_file_raises(tmp_path):
    root = _write_tree(tmp_path, {"countries/bad.py": "def on_death(:\n"})
    with pytest.raises(SyntaxError):
        discover_death_rules(root)
