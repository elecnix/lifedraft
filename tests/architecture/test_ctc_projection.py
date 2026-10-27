#!/usr/bin/env python3
"""Detector: a Canada Training Credit parameter added to ``TaxYearData`` must be
carried through the forward projection (issue #372).

Why this detector exists
------------------------
``TaxDataProvider._project_from_base`` builds each future year's
``TaxYearData`` by ENUMERATING its fields. A field that is not named in that
call does not silently keep its value -- it is dropped, and the year beyond the
last real data comes back with a default. That is not a hypothetical: it is how
``cpp_max_benefit_65`` silently fell through to a literal ``return 14448`` and
left two readers of one concept 20% apart (#416). The engine cannot see a
projected year disagreeing with a real one unless something checks.

Why it is scoped to ``ctc_*`` and not to every field
----------------------------------------------------
The general form of this check would fail today on twenty-two pre-existing
fields (the whole Ontario/Quebec credit surface plus the CPP/OAS/GIS
maximums), and fixing them here would duplicate issue #416's stack
(PRs #418 and #422, both open drafts) in a PR about the training credit -- six
layers of conflict for a detector that is not this issue's business. So the
guard is scoped to the prefix this issue owns: every ``ctc_`` field on the
dataclass must be named in the projection. It fails the moment somebody adds a
CTC parameter and forgets to project it, which is the regression that matters
here. When #416's stack lands, widening the guard to all fields is a one-line
change to the two constants below.
"""

import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
TAX_DATA = os.path.join(REPO_ROOT, "tax_data.py")

# The field prefix this issue owns. Widen to ``("",)`` when #416's stack has
# projected every field (see the module docstring).
OWNED_PREFIXES = ("ctc_",)


def _dataclass_fields(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TaxYearData":
            return [n.target.id for n in node.body
                    if isinstance(n, ast.AnnAssign)]
    raise AssertionError("TaxYearData dataclass not found in tax_data.py")


def _cached_keywords(source):
    """The keyword names of the ``TaxYearData(...)`` call `_parse_cached`
    BUILDS, when a year is served from the JSON cache.

    Deliberately the constructor's keywords, not every ``.get()`` key in the
    function: a field that `_parse_cached` merely *reads* (for a gate, a log, a
    branch) would satisfy the looser check while the dataclass is still built
    without it, so a cached year would keep the 0.0 default and the credit would
    be denied with the detector green. This mirrors `_projected_keywords`, which
    inspects the constructor call for the same reason (Cite round 6).
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "_parse_cached"):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and getattr(sub.func, "id", "") == "TaxYearData"):
                    return {kw.arg for kw in sub.keywords}
    raise AssertionError(
        "_parse_cached does not build a TaxYearData(...) call -- this detector "
        "is guarding nothing and must be updated in the same change")


def test_every_ctc_field_survives_the_json_cache_path():
    source = _read_tax_data()
    owned = [f for f in _dataclass_fields(source)
             if f.startswith(OWNED_PREFIXES)]
    cached = _cached_keywords(source)
    missing = [f for f in owned if f not in cached]
    assert not missing, (
        f"TaxDataProvider._parse_cached does not read {missing} out of the "
        f"cached year, so a run served from the cache carries the dataclass "
        f"DEFAULT (0.0) instead -- which the CTC's accrual gate reads as 'no "
        f"data, no credit' and silently denies every learner their $250. Add "
        f"the field to the TaxYearData(...) call in _parse_cached (issue #372).")


def _projected_keywords(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "_project_from_base"):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and getattr(sub.func, "id", "") == "TaxYearData"):
                    return [kw.arg for kw in sub.keywords]
    raise AssertionError("_project_from_base / its TaxYearData call not found")


def _read_tax_data():
    with open(TAX_DATA) as handle:
        return handle.read()


def test_every_ctc_field_is_carried_through_the_forward_projection():
    source = _read_tax_data()
    fields = _dataclass_fields(source)
    projected = set(_projected_keywords(source))
    owned = [f for f in fields if f.startswith(OWNED_PREFIXES)]
    assert owned, (
        "no `ctc_` field found on TaxYearData -- if the Canada Training Credit "
        "parameters were renamed or removed, this detector is guarding nothing "
        "and must be updated in the same change (issue #372)")

    missing = [f for f in owned if f not in projected]
    assert not missing, (
        "TaxDataProvider._project_from_base does not carry "
        f"{missing} through the forward projection. A year beyond the last "
        "real data comes back with the dataclass DEFAULT instead of the real "
        "value, so the Canada Training Credit silently changes size (or stops "
        "firing) in any projection that runs past the last published year. "
        "Add the field to the TaxYearData(...) call in _project_from_base, "
        "escalating the money amounts and passing statutory constants through "
        "unchanged (issue #372; the same shape of bug is #416).")


def _mentions_indexation_factor(value):
    """Whether a keyword's argument expression actually USES the projection's
    indexation factor.

    Checks for a ``Name`` node bound to ``factor`` -- the name
    `_project_from_base` binds and no other -- rather than searching
    ``ast.dump`` output for the substring "factor". A substring test is
    satisfied by an expression that merely mentions it (a helper called
    ``_no_factor``, a comment-shaped name, a differently written product), which
    would let a statutory amount be indexed past a detector meant to stop that,
    and would equally mis-fire the mirrored check. A detector is only as good as
    its precision about the thing it claims to detect (Cite round 6).
    """
    return any(isinstance(node, ast.Name) and node.id == "factor"
               for node in ast.walk(value))


# The CTC amounts CRA does NOT index, and the one it does.
CTC_STATUTORY_FIELDS = ("ctc_annual_accrual", "ctc_lifetime_cap")
CTC_INDEXED_FIELDS = ("ctc_working_income_threshold",)


def test_the_ctc_projection_does_not_index_the_statutory_amounts():
    """The $250 annual accrual and the $5,000 lifetime cap are NOT indexed by
    CRA. A projection that escalated them alongside the money amounts would
    inflate the credit on a long horizon -- a plausible, quietly wrong number,
    which is the failure this repo exists to prevent. Only the working-income
    threshold is indexed, so this pins BOTH directions: the statutory pair ride
    through unchanged, and the threshold is escalated with the other money
    amounts."""
    source = _read_tax_data()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_project_from_base"):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Call)
                    and getattr(sub.func, "id", "") == "TaxYearData"):
                continue
            passed = {kw.arg: kw.value for kw in sub.keywords}
            for name in CTC_STATUTORY_FIELDS + CTC_INDEXED_FIELDS:
                # A field absent from the projection call must fail with THIS
                # message, not a bare KeyError -- the first test names it, and a
                # detector is not allowed to be less legible than the failure it
                # guards against.
                assert name in passed, (
                    f"_project_from_base does not pass {name} at all, so the "
                    f"indexation check below has nothing to inspect (issue #372)")
            for name in CTC_STATUTORY_FIELDS:
                assert not _mentions_indexation_factor(passed[name]), (
                    f"_project_from_base indexes TaxYearData.{name}, but the "
                    f"Canada Training Credit's accrual and lifetime cap are "
                    f"statutory amounts CRA does NOT index (issue #372). Only "
                    f"ctc_working_income_threshold is indexed.")
            for name in CTC_INDEXED_FIELDS:
                assert _mentions_indexation_factor(passed[name]), (
                    f"_project_from_base carries TaxYearData.{name} through "
                    f"unchanged, but it IS an indexed CRA threshold. Leaving "
                    f"it flat would understate the working-income test on a "
                    f"long horizon -- a learner who should accrue $250 does "
                    f"not (issue #372).")
