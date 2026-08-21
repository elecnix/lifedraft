"""DP#13 enforcement: no person-specific ``birth_year`` default in library code.

Issue #741: several library functions/dataclasses shipped a specific human
birth year as a DEFAULT parameter value -- ``optimize_claiming_ages(...,
birth_year: int = 1979)`` and ``CPPSharingInput(primary_birth_year=1960,
spouse_birth_year=1962)``. A caller that omits the argument does not get an
error; it silently gets a fully plausible 47-/66-/64-year-old, and every
downstream age-dependent computation (CPP/QPP claiming age, OAS clawback
timing, RRIF minimums, LIF maximums) then produces a confident number for
the wrong person. That is exactly the defect class this repo exists to
eliminate (DP#32: absence must fail loudly, not default to a real-looking
value), wearing the DP#13 coat (no person-specific defaults).

## The rule

No dataclass field **and** no function parameter whose name contains
``birth_year`` may carry an int default inside ``[1900, 2010]`` -- the range
that reads as a real person's birth year -- unless the source line carries a
``DP#13`` marker comment documenting it as a clearly-fabricated placeholder.

## What passes, what fails

- ``= 0`` (the sentinel used by ``locked_in_account.py`` and, after #741,
  ``cpp_sharing.py``): outside the range. Passes. ``0`` is not a birth year;
  the consumer validates and raises on it (DP#32: fail loudly on use).
- ``= 2000  # DP#13: clearly dated placeholder year`` (``lsif_credit.py``):
  inside the range, but carries the marker. Passes -- it is the established
  clearly-fabricated-placeholder pattern.
- ``= 1979`` / ``= 1960`` / ``= 1962`` with no marker: inside the range,
  no marker. **Fails.** This is the smell #741 is about.

## The one allowlisted instance

``countries/canada/retirement.py``'s ``MemberRetirementData.birth_year`` defaults
to ``1979`` with only a ``# DP#1`` comment. It is a real violation of this
rule. It is allowlisted here, with a citation, because:

  - The DP#15 guard's own docstring (``test_dp15_no_personal_data.py``)
    already notes this default is relied upon as generic fixture data in
    well over a hundred call sites, and that changing it "needs its own
    verified change, not a name-swap" -- it is out of scope for #741 (which
    names only ``claiming_age_optimizer.py`` and ``cpp_sharing.py``) and was
    deliberately not touched by this fix.
  - The allowlist is citation-gated (``test_allowlisted_entries_cite_a_reason``)
    and drift-gated (``test_allowlist_has_no_stale_entries``): if the field
    is ever removed or its default moves out of ``[1900, 2010]``, the entry
    goes stale and the build fails until someone removes it -- so the
    carve-out cannot silently outlive the violation.

Growing this allowlist to make a NEW ``= 19xx`` default go green is exactly
how the original bugs got in (AGENTS.md: "When a guard fires, fix the code
-- do not add an allowlist entry"). File a separate issue and fix the call
sites instead.
"""
from __future__ import annotations

import repo_scan


# The one known, separately-tracked instance. See the module docstring for
# why this is allowlisted rather than fixed in #741, and the two tests below
# for what keeps the carve-out honest.
_ALLOWLIST = {
    ("countries/canada/retirement.py", "birth_year: int = 1979"): {
        "reason": (
            "MemberRetirementData.birth_year=1979 is relied upon as generic "
            "fixture data in 100+ call sites (per the DP#15 guard's own "
            "note); remediation needs its own verified change and is out of "
            "scope for #741, which names only claiming_age_optimizer.py and "
            "cpp_sharing.py. Track separately; do not grow this allowlist."
        ),
    },
}


def test_no_person_specific_birth_year_default():
    """Every ``*birth_year*`` default in ``[1900, 2010]`` without a ``DP#13``
    marker must be triaged in ``_ALLOWLIST``. A NEW, unlisted one is a build
    failure -- that is the mechanism that makes DP#13 load-bearing for this
    defect class instead of decorative."""
    findings = repo_scan.find_birth_year_person_specific_defaults()
    unlisted, _ = repo_scan.diff_against_allowlist(findings, _ALLOWLIST)
    if unlisted:
        lines = "\n".join(
            f"  {f.file}:{f.line}: {f.snippet}"
            for f in sorted(unlisted, key=lambda f: (f.file, f.line))
        )
        raise AssertionError(
            "DP#13/#741: found a `*birth_year*` default inside [1900, 2010] "
            "with no `DP#13` marker that is not triaged in _ALLOWLIST. A "
            "missing birth year must fail loudly (DP#32), not default to a "
            "real-looking person. Either make the parameter required, use "
            "the `= 0` sentinel + validate-on-use (see locked_in_account.py "
            "/ cpp_sharing.py), or -- for a demo/__main__ placeholder only -- "
            "a clearly-fabricated year with a `# DP#13:` comment "
            "(see lsif_credit.py):\n" + lines
        )


def test_allowlist_has_no_stale_entries():
    """Every allowlisted `*birth_year*` default must still exist verbatim in
    the source. A stale entry means the violation was fixed (or the default
    moved out of [1900, 2010]) without anyone removing the carve-out -- which
    is the silent-outlive failure mode this allowlist must be immune to."""
    findings = repo_scan.find_birth_year_person_specific_defaults()
    _, stale = repo_scan.diff_against_allowlist(findings, _ALLOWLIST)
    if stale:
        lines = "\n".join(f"  {file}: {snippet!r}" for file, snippet in stale)
        raise AssertionError(
            "DP#13/#741: _ALLOWLIST entry no longer matches any source "
            "finding (the underlying default changed). Remove it from "
            "tests/architecture/test_dp13_birth_year_defaults.py:\n" + lines
        )


def test_allowlisted_entries_cite_a_reason():
    """An allowlist entry without a real reason is exactly the silent
    carve-out AGENTS.md forbids -- guard against it the same way
    test_dp32_reviewed_harmless_entries_cite_a_reason does."""
    for key, meta in _ALLOWLIST.items():
        reason = meta.get("reason", "")
        assert len(reason) >= 40, (
            f"{key}: _ALLOWLIST entry must cite why it is carved out, got {reason!r}"
        )


# ── Self-tests: prove the detector actually detects, using synthetic ASTs ──
# only (no real personal data -- DP#15).

def _scan_source(src: str, relpath: str = "<synthetic>"):
    import ast
    import os
    import tempfile
    tmp = tempfile.mkdtemp()
    full = os.path.join(tmp, relpath + ".py")
    with open(full, "w", encoding="utf-8") as f:
        f.write(src)
    return [
        f for f in repo_scan.find_birth_year_person_specific_defaults(root=tmp)
        if f.file.endswith(relpath + ".py")
    ]


def test_detector_flags_a_bare_person_specific_default():
    src = "def f(birth_year: int = 1979):\n    return birth_year\n"
    findings = _scan_source(src, "flagged")
    assert len(findings) == 1, findings
    assert "birth_year: int = 1979" in findings[0].snippet


def test_detector_passes_the_zero_sentinel():
    src = "@dataclass\nclass C:\n    birth_year: int = 0\n"
    assert _scan_source(src, "zero") == []


def test_detector_passes_a_d13_marked_placeholder():
    src = "@dataclass\nclass C:\n    birth_year: int = 2000  # DP#13: clearly dated placeholder year\n"
    assert _scan_source(src, "marked") == []


def test_detector_passes_a_year_outside_the_real_person_range():
    src = "def f(birth_year: int = 1899):\n    return birth_year\n"
    assert _scan_source(src, "old") == []


def test_detector_flags_a_dataclass_field_default():
    src = "@dataclass\nclass Member:\n    birth_year: int = 1962\n"
    findings = _scan_source(src, "field")
    assert len(findings) == 1, findings
    assert "birth_year: int = 1962" in findings[0].snippet


def test_detector_catches_owner_birth_year_name_variant():
    """The rule is name-contains-`birth_year`, so `owner_birth_year` is in scope."""
    src = "@dataclass\nclass Fund:\n    owner_birth_year: int = 1960\n"
    findings = _scan_source(src, "owner")
    assert len(findings) == 1, findings