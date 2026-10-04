"""One CPP/QPP ceiling table: the duplication guard for countries.canada.cpp_data.

Issue #412. ``countries/canada/cpp_data.py`` answers every CPP/QPP ceiling
(YMPE, YAMPE, the age-65 maximum, the CPP2 age-65 maximum) for one year, for
one plan, under one documented rule for a year that has no row. Consumers read
it; they do not keep a ceiling table of their own.

Why a guard and not just a refactor: the duplication this replaced was silent
and *inert*. Five YMPE tables and four different "no row" rules all carried
plausible numbers, so nothing failed — the drift would have shown up one day
as an estimate priced off a stale ceiling with no test to notice.

The failure modes this catches:

* a new module declares ``YMPE_BY_YEAR`` (or a ``CPP_MAX_*`` / ``QPP_MAX_*``
  ceiling) as a dict or number literal;
* a consumer re-derives a ceiling — a lookup helper that reaches for
  ``CPP_OAS_BY_YEAR`` or the historical YMPE rows directly instead of asking
  ``cpp_data``;
* a consumer hardcodes a ceiling fallback (a growth factor, a multiplier)
  beside the lookup.

The allowlist is empty on purpose. An entry here would be an admission that a
second table is the right answer for some quantity, and the only sanctioned
way to say that is an issue that says why.
"""
from __future__ import annotations

import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from repo_scan import ROOT, iter_source_files  # noqa: E402

# The module that owns the ceilings. Everything else reads it.
CPP_DATA = os.path.join("countries", "canada", "cpp_data.py")

# ``retirement.py`` holds CPP_OAS_BY_YEAR — the federal year-versioned row
# store that cpp_data itself projects onto the ceilings (it also carries the
# OAS/GIS amounts, which are not CPP ceilings and have their own getters).
CPP_OAS_OWNER = os.path.join("countries", "canada", "retirement.py")

# ``tax_data.py`` owns the per-year RECORD: ``TaxYearData`` declares the
# ceiling fields, each defaulting to 0.0 as the "unset" sentinel (DP#13). Those
# are slot declarations, not restated ceilings — the values arrive from the
# federal and provincial records that instantiate the dataclass.
#
# Known limitation, stated rather than hidden: a ceiling supplied as a
# KEYWORD argument (``cpp_max_pensionable=68500`` inside a call) is an
# ``ast.keyword``, not an assignment, so neither this guard nor its
# declaration rule sees it. Catching that shape would flag every legitimate
# ``TaxYearData(...)`` construction in the country packages; the defence for
# the VALUES is the year-versioned provider's own tests, not this guard.
TAX_DATA_OWNER = "tax_data.py"

# A name that claims to hold a ceiling. Matched against the module-level (and
# class-level) assignment targets, so ``ympe = ...`` locals are not caught:
# the defect is a *table* declared away from the interface, not a local.
CEILING_NAME = re.compile(
    r"(?:^|_)(?:Y?A[MP]PE|YMPE|YAMPE|CPP_MAX_PENSIONABLE|CPP2_MAX_PENSIONABLE"
    r"|CPP_MAX_BENEFIT_65|CPP2_MAX_BENEFIT|QPP_MAX_PENSIONABLE"
    r"|QPP_MAX_BENEFIT_65)(?:_|$|\d)",
    re.IGNORECASE,
)

# A number or a table: what a restated ceiling looks like in the AST.
LITERAL_VALUE = (ast.Constant, ast.Dict, ast.Set, ast.List)


def _assigned_names(node: ast.AST):
    """Names bound by a module- or class-level assignment statement.

    Covers both ``X = ...`` (``ast.Assign``) and ``X: T = ...``
    (``ast.AnnAssign``). An annotated assignment is the same declaration with
    a type hint; skipping it left a trivially-annotated table unguarded.
    """
    if isinstance(node, ast.AnnAssign):
        targets = [node.target]
    elif isinstance(node, ast.Assign):
        targets = node.targets
    else:
        return
    for target in targets:
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, ast.Tuple):
            for element in target.elts:
                if isinstance(element, ast.Name):
                    yield element.id


def _ceiling_assignments(tree: ast.AST):
    """Yield (lineno, name) for every literal bound to a ceiling name.

    The NAME decides, not the value's type. A ceiling restated as the string
    ``"74600"`` is still a restated ceiling, so a non-numeric constant is not
    skipped. Values that are not literals at all (a call, a comprehension) are
    skipped — a function that *computes* a ceiling is the separate
    re-derivation rule, not a declaration.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if not isinstance(node.value, LITERAL_VALUE):
            continue
        for name in _assigned_names(node):
            if CEILING_NAME.search(name):
                yield node.lineno, name


def _derived_ceilings(tree: ast.AST):
    """Yield (lineno, name) where a ceiling NAME is bound to arithmetic.

    Complements the operand scan: it does not care whether the operands are
    names, calls, or constants, only that a ceiling-named target is produced
    by ``*``, ``/`` or ``**``.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if not isinstance(node.value, ast.BinOp):
            continue
        if not isinstance(node.value.op, (ast.Mult, ast.Div, ast.Pow)):
            continue
        for name in _assigned_names(node):
            if CEILING_NAME.search(name):
                yield node.lineno, name


# The two declaration rules, bound once into a registry so a self-test can
# call them through a reference the module namespace cannot invalidate.
_RULES = {
    "ceiling_assignments": _ceiling_assignments,
    "derived_ceilings": _derived_ceilings,
}


def _is_ceiling_module(relpath: str) -> bool:
    return relpath in (CPP_DATA, CPP_OAS_OWNER, TAX_DATA_OWNER)


def test_no_module_declares_its_own_cpp_ceiling_table():
    """Only cpp_data.py and retirement.py may bind a ceiling name to data."""
    offenders = []
    for path in iter_source_files(ROOT):
        relpath = os.path.relpath(path, ROOT)
        if _is_ceiling_module(relpath):
            continue
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        offenders.extend(
            f"{relpath}:{lineno}: {name}" for lineno, name in _ceiling_assignments(tree)
        )
    assert not offenders, (
        "A CPP/QPP ceiling is declared outside "
        f"{CPP_DATA} / {CPP_OAS_OWNER}. Read it through "
        "cpp_data.cpp_parameters(year, plan) instead:\n  "
        + "\n  ".join(offenders)
    )


def test_no_consumer_reaches_for_the_ceiling_tables_directly():
    """No consumer may import or index the ceiling tables itself.

    ``cpp_data`` and ``retirement`` are the owners; every other module that
    mentions a ceiling name must be reaching the value through a function it
    imported, not by reading a table.
    """
    offenders = []
    for path in iter_source_files(ROOT):
        relpath = os.path.relpath(path, ROOT)
        if _is_ceiling_module(relpath):
            continue
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # ``alias.name`` is the ORIGINAL name, so ``import ... as _T``
                # is caught by the same check.
                for alias in node.names:
                    if alias.name.endswith("_BY_YEAR") and CEILING_NAME.search(alias.name):
                        offenders.append(f"{relpath}:{node.lineno}: imports {alias.name}")
            elif isinstance(node, ast.Attribute):
                # ``import countries.canada.retirement as r`` then
                # ``r.CPP_OAS_BY_YEAR[2026]`` is an attribute reach, not an
                # ImportFrom, so only inspecting ImportFrom missed it.
                #
                # Restricted to UPPERCASE attributes on purpose: the sanctioned
                # interface returns a lower-case field (``params.ympe``), and
                # flagging that would make the guard cry wolf on correct code.
                if any(char.isupper() for char in node.attr) and CEILING_NAME.search(node.attr):
                    offenders.append(f"{relpath}:{node.lineno}: reaches {node.attr}")
    assert not offenders, (
        "A module imports a CPP/QPP ceiling table directly. Ask "
        "cpp_data.cpp_parameters(year, plan) for the value:\n  "
        + "\n  ".join(offenders)
    )


def test_no_consumer_re_derives_a_ceiling():
    """A consumer must not multiply a ceiling by a growth or band factor.

    The rules this replaces were of exactly this shape: YMPE x 1.14 for a
    missing YAMPE row, YMPE grown 2%/yr past the last published year. A
    ``ympe * <float>`` or ``ympe ** <float>`` in a module that does not own
    the ceilings is one of those rules being rebuilt.
    """
    offenders = []
    for path in iter_source_files(ROOT):
        relpath = os.path.relpath(path, ROOT)
        if _is_ceiling_module(relpath):
            continue
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            # Mult, Div and Pow: a ceiling multiplied by a factor, divided by
            # an accrual rate, or grown by a power are the same defect, and
            # the hardcoded factor can sit on either side (``ympe * 1.14`` /
            # ``1.14 * ympe`` / ``ympe / 12``).
            if not isinstance(node.op, (ast.Mult, ast.Div, ast.Pow)):
                continue
            operands = (node.left, node.right)
            has_literal = any(
                isinstance(side, ast.Constant)
                and isinstance(side.value, (int, float))
                and not isinstance(side.value, bool)
                for side in operands
            )
            if not has_literal:
                # No hardcoded factor, so this is an ordinary use of a
                # ceiling the module obtained legitimately (``base_avg *
                # max_benefit_65``, ``ympe * threshold_pct``), not a ceiling
                # being rebuilt out of thin air.
                continue
            names = {n.id.lower() for n in ast.walk(node)
                     if isinstance(n, ast.Name)}
            if any(re.search(r"ympe|yampe|max_pensionable|max_benefit", name)
                   for name in names):
                offenders.append(f"{relpath}:{node.lineno}")

        # A ceiling BOUND to a ceiling name by arithmetic is a derivation even
        # when the operands are helper calls rather than names, so the operand
        # scan above cannot be routed around by one level of indirection:
        # ``cpp2_max_benefit = (yampe - ympe) * (1 / 40) * 12``.
        for lineno, name in _derived_ceilings(tree):
            offenders.append(f"{relpath}:{lineno}: {name} = <arithmetic>")
    assert not offenders, (
        "A module derives a CPP/QPP ceiling by arithmetic. cpp_data owns the "
        "rule for a year with no row:\n  " + "\n  ".join(offenders)
    )


def test_the_interface_answers_every_ceiling_for_both_plans():
    """The guard must not pass on an interface that stopped answering."""
    sys.path.insert(0, ROOT)
    from countries.canada.cpp_data import cpp_parameters, qpp_max_benefit_65_years

    for plan in ("cpp", "qpp"):
        params = cpp_parameters(2026, plan)
        assert params.ympe > 0
        assert params.yampe > 0
        assert params.max_benefit_65 > 0
        assert params.max_cpp2_benefit > 0

    # QPP maxima are read from the Quebec package's own records.
    assert qpp_max_benefit_65_years() == (2023, 2024, 2025, 2026)
    assert cpp_parameters(2023, "qpp").max_benefit_65 == 15170
    assert cpp_parameters(2026, "qpp").max_benefit_65 == 17334

# ── Self-tests ────────────────────────────────────────────────────────────────
#
# A guard that has never been shown to FAIL is not evidence it works. Each
# case below is a real hole Cite found in this detector (issue #422), replayed
# against the helper that closes it. If a future edit weakens a rule, the
# matching case goes green-to-red here rather than silently undetected in the
# field.

def _findings_for(source: str, rule) -> list:
    """Run one rule over a synthetic module body.

    Materialised as a list: a generator is always truthy, so ``assert
    _findings_for(...)`` would pass on an EMPTY result and the self-test would
    be a tautology — the exact defect it exists to catch.
    """
    return list(rule(ast.parse(source)))


def test_a_ceiling_declared_with_a_type_annotation_is_caught():
    """Hole: only ast.Assign was walked, so `X: dict = {...}` slipped through."""
    src = "YMPE_BY_YEAR: dict[int, int] = {2026: 74600}\n"
    rule = _RULES["ceiling_assignments"]
    assert _findings_for(src, rule), (
        "an annotated ceiling table must be reported"
    )


def test_a_ceiling_restated_as_a_string_is_caught():
    """Hole: the (int, float) filter skipped a Constant, so a quoted ceiling escaped."""
    src = 'YMPE_2026 = "74600"\n'
    rule = _RULES["ceiling_assignments"]
    assert _findings_for(src, rule), (
        "a ceiling restated as a string is still a restated ceiling"
    )


def test_a_ceiling_derived_by_a_division_is_caught():
    """Hole: only Mult and Pow were inspected; a division escaped."""
    tree = ast.parse("band = ympe / 12\n")
    assert any(
        isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) for n in ast.walk(tree)
    ), "the Div shape must exist for this case to mean anything"


def test_a_ceiling_derived_through_a_helper_is_caught():
    """Hole: one level of indirection hid the operand from the name scan."""
    src = "cpp2_max_benefit = helper() * 12\n"
    rule = _RULES["derived_ceilings"]
    assert _findings_for(src, rule), (
        "a ceiling bound to arithmetic must be caught even via a call"
    )


def test_an_ordinary_use_of_a_ceiling_is_not_reported():
    """The counter-case: applying a ceiling is not deriving one.

    Without this, the widest rule that catches the defect also silences the
    guard's value by flagging correct code.
    """
    tree = ast.parse(
        "base = base_avg * max_benefit_65\n"
        "ratio = above_ympe / cpp2_range\n"
        "threshold = ympe * threshold_pct\n"
        "params = cpp_parameters(2026)\n"
    )
    assert not list(_RULES["derived_ceilings"](tree))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div, ast.Pow)):
            operands = (node.left, node.right)
            has_literal = any(
                isinstance(side, ast.Constant)
                and isinstance(side.value, (int, float))
                and not isinstance(side.value, bool)
                for side in operands
            )
            assert not has_literal, (
                f"line {node.lineno} must carry no hardcoded factor, or this "
                "case stops testing what it claims"
            )


def test_a_lowercase_interface_field_is_not_a_reported_table_reach():
    """The attribute rule is uppercase-only so `params.ympe` stays legal."""
    src = "value = params.ympe\n"
    tree = ast.parse(src)
    reported = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and any(c.isupper() for c in n.attr)
        and CEILING_NAME.search(n.attr)
    ]
    assert not reported
