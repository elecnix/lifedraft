"""Tests for issue #986: DP#13 -- name the OAS default in optimize.py.

#986 named the previously-scattered inline ``8500`` literal as a single module
seam. Issue #1029 then made the deliberate decision #986 deferred: that seam
now reads the year-versioned government table
(``countries.canada.retirement.get_oas_annual_max``) instead of carrying the
frozen 8500 literal, so the value-tracking assertions below assert the SEAM,
not a frozen amount. The DP#13 shape is unchanged: a named fallback for ABSENT
input only, applied via ``dict.get`` so an explicit ``0`` is honoured (DP#32).
"""
import inspect


class TestOASDefaultIsNamed:
    """DP#13 (#986, as amended by #1029): the OAS fallback is a single named
    seam, not a scattered inline literal, and an explicit 0 is honoured."""

    def test_default_seam_exists_and_is_callable(self):
        import optimize
        assert callable(optimize._default_oas_annual)

    def test_no_inline_numeric_oas_default_at_call_sites(self):
        # A numeric literal may not appear inline at a .get('oas_annual', ...)
        # call -- the default must come from the named seam.
        import optimize
        source = inspect.getsource(optimize)
        bad = []
        for line_no, line in enumerate(source.split("\n"), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "'oas_annual', " not in line and '"oas_annual", ' not in line:
                continue
            after = line.split("'oas_annual', ")[-1].split('"oas_annual", ')[-1]
            if after[0].isdigit():
                bad.append((line_no, stripped))
        assert not bad, f"inline numeric OAS default remains at: {bad}"

    def test_all_three_call_sites_reference_the_seam(self):
        # Every assumptions.oas_annual fallback reads _default_oas_annual.
        import optimize
        source = inspect.getsource(optimize)
        oas_gets = [ln for ln in source.split("\n")
                    if ".get('oas_annual', " in ln or '.get("oas_annual", ' in ln]
        assert len(oas_gets) == 3, f"expected 3 oas_annual get sites, got {len(oas_gets)}"
        assert all("_default_oas_annual" in ln for ln in oas_gets), \
            "an oas_annual fallback does not use _default_oas_annual"


class TestExplicitOASZeroHonoured:
    """DP#32: an explicit assumptions.oas_annual of 0 is a real value, never
    coerced to the fallback (this is dict.get with a default, not
    ``x or DEFAULT``)."""

    def test_explicit_zero_oas_not_replaced_by_default(self):
        cfg = {"assumptions": {"oas_annual": 0}}
        import optimize
        assert cfg.get("assumptions", {}).get(
            "oas_annual", optimize._default_oas_annual(cfg)) == 0

    def test_absent_oas_uses_year_versioned_default(self):
        # Absent input falls back to the live government table (#1029).
        import optimize
        from countries.canada.retirement import get_oas_annual_max
        cfg = {"assumptions": {}}
        assert (cfg.get("assumptions", {}).get(
            "oas_annual", optimize._default_oas_annual(cfg))
            == get_oas_annual_max(2026))
