"""Shared AST-scanning helpers for the DESIGN_PRINCIPLES.md enforcement tests
(issue #586 -- "make the design principles executable").

Not a test module itself (no ``test_`` prefix) -- imported by
``test_dp32_zero_fallback.py`` and ``test_dp18_dead_write.py``.

These helpers walk the first-party source tree (everything except ``tests/``,
virtualenvs, caches, and packaging metadata) and return raw AST findings. Each
test module is responsible for interpreting those findings against its own
curated allowlist -- this module makes no judgment about which findings are
bugs.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from typing import Iterator, List

# Repo root: two levels up from this file (tests/architecture/repo_scan.py).
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Directories that are never first-party source: virtualenvs, caches, VCS,
# packaging metadata, and the tests tree itself (DP#32/DP#18 govern
# production code; test files legitimately use `.get(...) or DEFAULT` to
# build fixtures and are out of scope for this scan).
EXCLUDE_DIR_NAMES = {
    ".venv", "venv", "__pycache__", ".git", ".pytest_cache",
    "canadian_financial_optimizer.egg-info", "node_modules", "tests",
    ".pi",
    # `pip install ".[dev]"` (CI's non-editable install path) leaves a
    # `build/lib/...` copy of the source tree as a build-backend side effect;
    # `dist/` similarly holds built wheels/sdists. Both are byte-identical
    # duplicates of first-party source under a different path prefix -- scan
    # the real source, not its build artifacts.
    "build", "dist",
}


def iter_source_files(root: str = ROOT) -> Iterator[str]:
    """Yield paths (relative to ``root``) of every first-party ``.py`` file."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDE_DIR_NAMES and not d.startswith(".")
        ]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                yield os.path.relpath(full, root)


def _parse(root: str, relpath: str) -> ast.AST | None:
    full = os.path.join(root, relpath)
    try:
        with open(full, encoding="utf-8") as f:
            src = f.read()
        return ast.parse(src, filename=relpath)
    except SyntaxError:
        return None


@dataclass(frozen=True)
class Finding:
    """One AST match. ``snippet`` (the unparsed source of the matched node)
    is the identity used for allowlisting -- it survives line-number drift
    from unrelated edits elsewhere in the file, unlike a bare line number.
    """
    file: str
    line: int
    snippet: str

    @property
    def key(self) -> tuple:
        return (self.file, self.snippet)


def _is_get_or_getattr_call(node: ast.AST) -> bool:
    """True if ``node`` is ``X.get(...)`` or ``getattr(...)``."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "get":
        return True
    if isinstance(f, ast.Name) and f.id == "getattr":
        return True
    return False


def find_get_or_default(root: str = ROOT) -> List[Finding]:
    """DP#32: find ``X.get(k[, default]) or FALLBACK`` and
    ``getattr(obj, name[, default]) or FALLBACK`` -- the truthiness
    fallthrough that conflates "absent" with "present and falsy" (``0``,
    ``''``, ``[]``, ``{}``, ``False``).

    Matches any ``or``-chain whose *first* operand is a ``.get(...)`` or
    ``getattr(...)`` call. This is deliberately a syntactic pattern match,
    not a semantic one -- see the docstring of
    ``test_dp32_zero_fallback.py`` for what it does and does not prove, and
    why it flags some sites #606 lists as harmless.
    """
    findings = []
    for relpath in iter_source_files(root):
        tree = _parse(root, relpath)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                if node.values and _is_get_or_getattr_call(node.values[0]):
                    try:
                        snippet = ast.unparse(node)
                    except Exception:
                        snippet = "<unparse-failed>"
                    findings.append(Finding(relpath, node.lineno, snippet))
    return findings


def _is_empty_mutable_default(node: ast.AST) -> bool:
    """True if ``node`` is a fresh empty-container literal: ``{}``, ``[]``,
    ``set()``/``dict()``/``list()`` with no arguments."""
    if isinstance(node, ast.Dict):
        return len(node.keys) == 0
    if isinstance(node, ast.List):
        return len(node.elts) == 0
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in ("dict", "list", "set") and not node.args and not node.keywords
    return False


def find_mutated_default_writes(root: str = ROOT) -> List[Finding]:
    """DP#18: find ``expr.get(key, {})[...] = value`` (or ``.attr = value``)
    -- mutating the *default* a dict-``get`` returns is a dead write whenever
    the key was actually absent, because ``.get`` returns a fresh throwaway
    container in that case, not a reference into ``expr``. The write only
    "works" by accident, when the key already happened to be present.

    This is a distinct DP#18 shape from the sensitivity-overlay dead-write
    (#591): #591 wrote to the wrong *key*; this pattern writes into a value
    that was never connected to the config object in the first place.
    """
    findings = []
    for relpath in iter_source_files(root):
        tree = _parse(root, relpath)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            target = node.targets[0] if len(node.targets) == 1 else None
            if target is None:
                continue
            # target is `<call>[...]` or `<call>.attr`
            if isinstance(target, ast.Subscript):
                base = target.value
            elif isinstance(target, ast.Attribute):
                base = target.value
            else:
                continue
            if not (isinstance(base, ast.Call) and isinstance(base.func, ast.Attribute)
                    and base.func.attr == "get"):
                continue
            if len(base.args) < 2:
                continue
            if not _is_empty_mutable_default(base.args[1]):
                continue
            try:
                snippet = ast.unparse(node)
            except Exception:
                snippet = "<unparse-failed>"
            findings.append(Finding(relpath, node.lineno, snippet))
    return findings


def _birth_year_default_findings_in(node: ast.AST, relpath: str, lines: List[str]) -> List[Finding]:
    """Yield DP#13 findings for one function's parameters.

    Internal helper for ``find_birth_year_person_specific_defaults``.
    """
    out: List[Finding] = []
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return out
    args = node.args
    posargs = list(args.posonlyargs) + list(args.args)
    defaults = list(args.defaults)
    # `defaults` aligns to the LAST len(defaults) positional/posonly args.
    paired = list(zip(posargs[len(posargs) - len(defaults):], defaults))
    # kwonly args: `kw_defaults` uses None for "no default".
    for arg, dflt in zip(args.kwonlyargs, args.kw_defaults):
        if dflt is not None:
            paired.append((arg, dflt))
    for arg, dflt in paired:
        if not (arg and 'birth_year' in arg.arg):
            continue
        if not (isinstance(dflt, ast.Constant) and isinstance(dflt.value, int)
                and 1900 <= dflt.value <= 2010):
            continue
        marker_line = lines[dflt.lineno - 1] if dflt.lineno - 1 < len(lines) else ''
        if 'DP#13' in marker_line:
            continue  # documented clearly-fabricated placeholder
        snippet = f"{arg.arg}: "
        if arg.annotation is not None:
            snippet += f"{ast.unparse(arg.annotation)} "
        snippet += f"= {ast.unparse(dflt)}"
        out.append(Finding(relpath, dflt.lineno, snippet))
    return out


def find_birth_year_person_specific_defaults(root: str = ROOT) -> List[Finding]:
    """DP#13/#741: find dataclass fields and function parameters whose name
    contains ``birth_year`` and whose default is an int in ``[1900, 2010]`` --
    the range that reads as a real person's birth year -- WITHOUT a ``DP#13``
    marker comment on the source line.

    A ``*birth_year*`` default of ``0`` (the sentinel used by
    ``locked_in_account.py`` / ``cpp_sharing.py`` that fails loudly on use) is
    outside the range and passes. A clearly-fabricated placeholder like
    ``= 2000  # DP#13: clearly dated placeholder year`` (``lsif_credit.py``)
    is inside the range but carries the marker and passes. A bare ``= 1979``
    or ``= 1960`` with no marker fails -- a caller that omits the argument
    silently gets a plausible-but-wrong person (DP#32: absence must fail
    loudly, not default to a real-looking value).

    Returns raw findings; the test module allowlists the one known, separately
    tracked instance (``retirement.py``'s widely-relied-upon default, see
    ``test_dp13_birth_year_defaults.py``).
    """
    findings: List[Finding] = []
    for relpath in iter_source_files(root):
        tree = _parse(root, relpath)
        if tree is None:
            continue
        full = os.path.join(root, relpath)
        try:
            with open(full, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for node in ast.walk(tree):
            # Dataclass-style annotated field: `birth_year: int = 1979`
            if (isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and 'birth_year' in node.target.id
                    and node.value is not None
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, int)
                    and 1900 <= node.value.value <= 2010):
                marker_line = (lines[node.lineno - 1]
                               if node.lineno - 1 < len(lines) else '')
                if 'DP#13' in marker_line:
                    continue
                findings.append(Finding(relpath, node.lineno,
                                        ast.unparse(node)))
            # Function parameters with a person-specific default.
            findings.extend(_birth_year_default_findings_in(node, relpath, lines))
    return findings


###############################################################################
# RULE-ORDER COUPLING SCANNER  (see tests/architecture/test_coupling_guard.py)
###############################################################################


@dataclass(frozen=True)
class WsAccess:
    """One ``ws.<field>`` read or write found inside a registered rule (or a
    helper the rule transitively calls).

    ``kind`` is ``'read'`` or ``'write'``.  ``ws.field += x`` (AugAssign) is
    both read and write, so the scanner emits TWO findings for one statement.
    The scanner deduplicates by ``(rule_name, field, kind)``: a rule that
    touches the same field in three branches still registers as one *reader*
    of that field, and one *writer* if it also writes it.  This is the unit
    the ordering check reasons about -- "does the rule that PRODUCES a field
    precede the rule that CONSUMES it?" -- not the raw line count.
    """
    file: str
    rule_name: str
    field: str
    kind: str  # 'read' or 'write'
    line: int
    snippet: str


def _rule_name_from_decorator(dec: ast.expr):
    """Extract the registered rule name from a ``@rule("name")`` call, or
    ``None`` if ``dec`` is not that shape."""
    if isinstance(dec, ast.Call):
        if isinstance(dec.func, ast.Name) and dec.func.id == "rule":
            if dec.args and isinstance(dec.args[0], ast.Constant):
                return dec.args[0].value
    return None


def _collect_func_defs(func_node: ast.AST, acc: dict) -> None:
    """Collect every ``FunctionDef`` / ``AsyncFunctionDef`` reachable from
    ``func_node`` (itself + any nested helpers) into ``acc`` keyed by name.

    Nested defs shadow nothing in this codebase (rule helpers have unique
    names within their module), so registering them alongside the same-file
    top-level functions is sufficient for transitive-call resolution.
    """
    for child in ast.walk(func_node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.name not in acc:
                acc[child.name] = child


def _transitive_callee_names(start: str, all_funcs: dict, seen: set | None = None) -> set:
    """Return ``{start} + every function reachable by direct calls from start``
    within the same file.  ``all_funcs`` is ``{name: FunctionDef}``.

    Recurses through same-file helper functions so that a ``ws.field`` access
    buried inside a helper called by a rule is attributed to that rule.
    Cross-file imports are NOT resolved: pricing/calc helpers imported from
    other modules take ``ws`` values as arguments and never access ``ws``
    directly (verified by the field count matching YearWorkingState's total).
    """
    if seen is None:
        seen = set()
    if start not in all_funcs or start in seen:
        return seen
    seen.add(start)
    for node in ast.walk(all_funcs[start]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            _transitive_callee_names(node.func.id, all_funcs, seen)
    return seen


def _scan_ws_accesses_in_func(func_node: ast.AST, rule_name: str, file: str) -> List[WsAccess]:
    """Return every ``ws.<field>`` read and write inside ``func_node``.

    - ``ws.field`` in Load ctx  -> read
    - ``ws.field`` in Store ctx -> write  (e.g. ``ws.field = x``)
    - ``ws.field += x`` (AugAssign target) -> BOTH read and write, because
      AugAssign loads the current value before storing the sum.
    """
    out: List[WsAccess] = []
    for node in ast.walk(func_node):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ws"
        ):
            kind = "write" if isinstance(node.ctx, ast.Store) else "read"
            try:
                snippet = ast.unparse(node)
            except Exception:
                snippet = f"ws.{node.attr}"
            out.append(WsAccess(
                file, rule_name, node.attr, kind, node.lineno, snippet))
        if (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Attribute)
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == "ws"
        ):
            try:
                snippet = ast.unparse(node.target)
            except Exception:
                snippet = f"ws.{node.target.attr}"
            out.append(WsAccess(
                file, rule_name, node.target.attr, "read", node.lineno, snippet))
            out.append(WsAccess(
                file, rule_name, node.target.attr, "write", node.lineno, snippet))
    return out


def find_ws_field_accesses(root: str = ROOT) -> List[WsAccess]:
    """Coupling-guard scan (RULE_ORDER enforcement, DP#18 seam).

    Finds every ``ws.<field>`` read and write across all ``rules_*.py`` files.
    For each ``@rule("name")``-decorated function it resolves the *transitive*
    set of same-file callees (helper functions called in the rule body,
    including nested ``def``s) and scans all of them for ``ws.<field>``
    accesses.  ``AugAssign`` targets (``ws.field += ...``) count as both a
    read and a write.

    The result is deduplicated by ``(rule_name, field, kind)``.  Only
    ``rules_*.py`` files are scanned: every registered rule lives in one of
    them (see ``simulation_rules.py``'s import list), and every rule function
    is a top-level ``@rule(...)`` definition -- the ``@rule`` decorator in
    ``rule_registry`` is the single registration gate, so scanning for it is
    sound and can't miss a rule.
    """
    findings: List[WsAccess] = []
    for relpath in iter_source_files(root):
        if not (relpath.startswith("rules_") and relpath.endswith(".py")):
            continue
        tree = _parse(root, relpath)
        if tree is None:
            continue

        top_funcs: dict[str, ast.AST] = {}
        rule_fns: dict[str, ast.AST] = {}
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                top_funcs[node.name] = node
                for dec in node.decorator_list:
                    rn = _rule_name_from_decorator(dec)
                    if rn:
                        rule_fns[rn] = node

        for rule_name, rule_fn in rule_fns.items():
            all_funcs: dict[str, ast.AST] = dict(top_funcs)
            _collect_func_defs(rule_fn, all_funcs)

            callees = _transitive_callee_names(rule_fn.name, all_funcs)

            seen_pairs: set = set()
            for fname in callees:
                func = all_funcs[fname]
                for acc in _scan_ws_accesses_in_func(func, rule_name, relpath):
                    key = (rule_name, acc.field, acc.kind)
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    findings.append(acc)
    return findings


def diff_against_allowlist(findings: List[Finding], allowlist: dict) -> tuple:
    """Compare scan ``findings`` against a ``{(file, snippet): {...}}``
    allowlist. Returns ``(unlisted, stale)``:

    - ``unlisted``: findings present in the scan but not in the allowlist
      (a NEW, un-triaged violation -- the build must fail so it gets
      classified explicitly).
    - ``stale``: allowlist entries that no longer match any current finding
      (the violation was fixed, or the code moved, without the allowlist
      being updated -- the build must fail so the entry gets removed or
      corrected, which is what makes the allowlist "impossible to grow
      silently": it can't silently stay stale either).
    """
    found_keys = {f.key for f in findings}
    allowed_keys = set(allowlist.keys())
    unlisted = [f for f in findings if f.key not in allowed_keys]
    stale = sorted(allowed_keys - found_keys)
    return unlisted, stale
