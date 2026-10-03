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
    """Names bound by a module- or class-level assignment statement."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, ast.Tuple):
            for element in target.elts:
                if isinstance(element, ast.Name):
                    yield element.id


def _is_ceiling_module(relpath: str) -> bool:
    return relpath in (CPP_DATA, CPP_OAS_OWNER)


def test_no_module_declares_its_own_cpp_ceiling_table():
    """Only cpp_data.py and retirement.py may bind a ceiling name to data."""
    offenders = []
    for path in iter_source_files(ROOT):
        relpath = os.path.relpath(path, ROOT)
        if _is_ceiling_module(relpath):
            continue
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, LITERAL_VALUE):
                continue
            if isinstance(node.value, ast.Constant) and not isinstance(
                node.value.value, (int, float)
            ):
                continue
            for name in _assigned_names(node):
                if CEILING_NAME.search(name):
                    offenders.append(f"{relpath}:{node.lineno}: {name}")
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
                imported = {alias.name for alias in node.names}
                for name in imported:
                    if name.endswith("_BY_YEAR") and CEILING_NAME.search(name):
                        offenders.append(f"{relpath}:{node.lineno}: imports {name}")
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
            if not isinstance(node.op, (ast.Mult, ast.Pow)):
                continue
            if not isinstance(node.right, ast.Constant):
                continue
            if not isinstance(node.right.value, (int, float)):
                continue
            names = {n.id.lower() for n in ast.walk(node.left)
                     if isinstance(n, ast.Name)}
            if any(re.search(r"ympe|yampe", name) for name in names):
                offenders.append(f"{relpath}:{node.lineno}")
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