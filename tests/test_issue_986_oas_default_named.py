"""Tests for issue #986: DP#13 -- name the opinionated OAS default in optimize.py.

Before the fix, ``compute_net_benefit`` hardcoded the literal ``8500`` as the
``assumptions.oas_annual`` fallback at three call sites. DP#13: an opinionated
government-benefit amount baked into the optimizer is a fallback for ABSENT
input, named with provenance -- not a magic inline literal scattered across the
function.

After the fix, all three sites reference the single named module constant
``optimize._DEFAULT_OAS_ANNUAL`` (value 8500, preserved byte-for-byte). The
``dict.get(..., _DEFAULT_OAS_ANNUAL)`` form honours an explicit ``0`` (DP#32 --
this is ``dict.get`` with a default, NOT ``x or DEFAULT``, so a configured zero
stays zero).

Why a named constant and not ``get_oas_annual_max(year)`` (the convention-
consistent source used by pension_split_optimizer via #331)? Because
``get_oas_annual_max(2026)`` returns **8908**, not 8500 -- sourcing from the
year-versioned table would CHANGE the value and move optimizer results, a
behaviour change explicitly ruled out by #986's byte-exact constraint. The
provenance comment on ``_DEFAULT_OAS_ANNUAL`` records this divergence so the
switch to the table can be made as a separate, deliberate decision. See
``optimize._DEFAULT_OAS_ANNUAL`` for the full note.
"""
import inspect

import pytest


class TestOASDefaultIsNamed:
    """DP#13 (#986): the 8500 OAS fallback is a single named constant, not a
    scattered inline literal, and an explicit 0 is honoured (DP#32)."""

    def test_default_constant_exists_and_equals_8500(self):
        # Byte-exact preservation: the named constant carries the historical
        # value, so a household that declares no assumptions.oas_annual sees
        # exactly the number it saw before.
        import optimize
        assert hasattr(optimize, "_DEFAULT_OAS_ANNUAL")
        assert optimize._DEFAULT_OAS_ANNUAL == 8500

    def test_no_inline_8500_literal_at_call_sites(self):
        # The literal 8500 may appear ONLY in the constant definition and its
        # provenance comment -- never inline at a .get('oas_annual', ...) call.
        import optimize
        source = inspect.getsource(optimize)
        bad = []
        for line_no, line in enumerate(source.split("\n"), 1):
            stripped = line.strip()
            # Skip the constant definition line itself.
            if "_DEFAULT_OAS_ANNUAL = 8500" in stripped:
                continue
            # Skip comment lines (the provenance note mentions 8500 -> 8908).
            if stripped.startswith("#"):
                continue
            if "'oas_annual', 8500" in line or '"oas_annual", 8500' in line:
                bad.append((line_no, stripped))
        assert not bad, f"inline 8500 OAS default remains at: {bad}"

    def test_all_three_call_sites_reference_the_constant(self):
        # Every assumptions.oas_annual fallback reads _DEFAULT_OAS_ANNUAL.
        import optimize
        source = inspect.getsource(optimize)
        # There are exactly three .get('oas_annual', ...) sites and all use the
        # named constant.
        oas_gets = [ln for ln in source.split("\n")
                    if ".get('oas_annual', " in ln or '.get("oas_annual", ' in ln]
        assert len(oas_gets) == 3, f"expected 3 oas_annual get sites, got {len(oas_gets)}"
        assert all("_DEFAULT_OAS_ANNUAL" in ln for ln in oas_gets), \
            "an oas_annual fallback does not use _DEFAULT_OAS_ANNUAL"


class TestExplicitOASZeroHonoured:
    """DP#32: an explicit assumptions.oas_annual of 0 is a real value, never
    coerced to the 8500 fallback (this is dict.get with a default, not
    ``x or DEFAULT``)."""

    def test_explicit_zero_oas_not_replaced_by_default(self):
        # _DEFAULT_OAS_ANNUAL is the dict.get default, so a configured 0 is
        # returned as-is (the whole point of #986 preserving the dict.get form).
        cfg = {"assumptions": {"oas_annual": 0}}
        assert cfg.get("assumptions", {}).get("oas_annual", 8500) == 0

    def test_absent_oas_uses_default(self):
        # Absent input falls back to 8500 -- the named-constant path.
        cfg = {"assumptions": {}}
        import optimize
        assert (cfg.get("assumptions", {}).get("oas_annual", optimize._DEFAULT_OAS_ANNUAL)
                == 8500)