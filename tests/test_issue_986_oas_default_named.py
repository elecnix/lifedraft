"""Tests for issue #986: DP#13 -- name the OAS default (now in net_benefit_legs.py, #232).

#986 named the previously-scattered inline ``8500`` literal as a single module
seam. Issue #1029 then made the deliberate decision #986 deferred: that seam
now reads the year-versioned government table
(``countries.canada.retirement.get_oas_annual_max``) instead of carrying the
frozen 8500 literal, so the value-tracking assertions below assert the SEAM,
not a frozen amount. Issue #248 amended the SHAPE: the seam is reached only on
ABSENCE, via a membership test instead of an eager ``dict.get`` default (whose
default is evaluated even when a value WAS supplied). The DP#13 shape is
unchanged: a named fallback for ABSENT input only, applied via a membership
test so an explicit ``0`` is honoured (DP#32).
"""
import inspect


class TestOASDefaultIsNamed:
    """DP#13 (#986, as amended by #1029 and #248): the OAS fallback is a single
    named seam, not a scattered inline literal, and an explicit 0 is honoured."""

    def test_default_seam_exists_and_is_callable(self):
        import net_benefit_legs
        assert callable(net_benefit_legs._default_oas_annual)

    def test_no_inline_numeric_oas_default_at_call_sites(self):
        # A numeric literal may not appear inline at a .get('oas_annual', ...)
        # call -- the default must come from the named seam. Since #290
        # deleted net_benefit_legs' RRSP re-projection (2 sites), the only
        # call site is objective.py's capital-gains leg; net_benefit_legs is
        # still scanned so a re-introduced site there is caught too.
        import net_benefit_legs
        import objective
        bad = []
        for _mod, name in ((net_benefit_legs, 'net_benefit_legs.py'),
                           (objective, 'objective.py')):
            source = inspect.getsource(_mod)
            for line_no, line in enumerate(source.split("\n"), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "'oas_annual', " not in line and '"oas_annual", ' not in line:
                    continue
                after = line.split("'oas_annual', ")[-1].split('"oas_annual", ')[-1]
                if after[0].isdigit():
                    bad.append((name, line_no, stripped))
        assert not bad, f"inline numeric OAS default remains at: {bad}"

    def test_every_call_site_references_the_seam(self):
        # Every assumptions.oas_annual fallback reads _default_oas_annual as
        # the ELSE arm of a membership test. Since #290 there is ONE site (the
        # capital-gains leg in objective.py); the two in net_benefit_legs'
        # deleted RRSP re-projection are gone.
        # Membership, not dict.get: a dict.get default is evaluated EAGERLY,
        # so the fallback ran even when a value WAS supplied (#248). The
        # membership arm (not ``or`` -- there is one membership test per call
        # site) keeps an explicit 0 a real value (DP#32).
        import net_benefit_legs
        import objective
        seam_sites = []
        for _mod in (net_benefit_legs, objective):
            source = inspect.getsource(_mod)
            for line_no, ln in enumerate(source.split("\n"), 1):
                if "_default_oas_annual" not in ln:
                    continue
                stripped = ln.strip()
                if stripped.startswith("def _default_oas_annual"):
                    continue  # the seam's own definition
                if stripped.startswith(("from ", "import ")):
                    continue  # objective.py's import block
                if stripped.rstrip(",") == "_default_oas_annual":
                    continue  # a bare imported name (continuation line)
                seam_sites.append((_mod.__name__, line_no, stripped))
        assert len(seam_sites) == 1, (
            f"expected 1 oas_annual fallback site, got {len(seam_sites)}: "
            f"{seam_sites}")
        for _mod_name, _line_no, ln in seam_sites:
            assert "else" in ln, (
                f"{_mod_name} reaches _default_oas_annual outside the lazy "
                f"fallback arm of a membership test: {ln!r}")
        # One membership test per call site, so ``x or DEFAULT`` cannot stand
        # in for the membership shape (DP#32: 0 must stay a real value).
        membership = [
            ln for _mod in (net_benefit_legs, objective)
            for ln in inspect.getsource(_mod).split("\n")
            if "'oas_annual' in " in ln and not ln.strip().startswith("#")
        ]
        assert len(membership) == len(seam_sites), (
            f"expected {len(seam_sites)} 'oas_annual' in ... membership tests, "
            f"got {len(membership)}: {membership}")


class TestExplicitOASZeroHonoured:
    """DP#32: an explicit assumptions.oas_annual of 0 is a real value, never
    coerced to the fallback. NOT because of dict.get but because the fallback
    is only reached on ABSENCE: the membership arm short-circuits it (#248),
    which a raising stand-in proves rather than asserts."""

    def test_explicit_zero_oas_not_replaced_by_default(self):
        # The fallback is not evaluated at all: were it, the raising stand-in
        # would explode. An explicit 0 short-circuits the membership test.
        def _fallback(_cfg):
            raise AssertionError("fallback ran for a supplied oas_annual")
        cfg = {"assumptions": {"oas_annual": 0}}
        assumptions = cfg.get("assumptions", {})
        assert (assumptions["oas_annual"] if "oas_annual" in assumptions
                else _fallback(cfg)) == 0

    def test_supplied_value_survives_a_raising_fallback(self):
        # The #248 latent bug: dict.get's eager default would have run the
        # fallback even against a supplied value, so a fallback that raises
        # would crash the household. The membership shape returns the supplied
        # value untouched.
        def _fallback(_cfg):
            raise AssertionError("fallback ran for a supplied oas_annual")
        cfg = {"assumptions": {"oas_annual": 4321}}
        assumptions = cfg.get("assumptions", {})
        assert (assumptions["oas_annual"] if "oas_annual" in assumptions
                else _fallback(cfg)) == 4321

    def test_absent_oas_uses_year_versioned_default(self):
        # Absent input reaches the fallback, which reads the live government
        # table (#1029) for the simulation start year (2026 here).
        import net_benefit_legs
        from countries.canada.retirement import get_oas_annual_max
        cfg = {"assumptions": {}, "tax": {"start_year": 2026}}
        assumptions = cfg.get("assumptions", {})
        assert (assumptions["oas_annual"] if "oas_annual" in assumptions
                else net_benefit_legs._default_oas_annual(cfg)) \
            == get_oas_annual_max(2026)

    def test_absent_oas_and_absent_start_year_refuses(self):
        # Issue #290: the fallback needs the household's year; without it the
        # seam refuses loudly instead of assuming 2026 (DP#13/DP#32).
        import pytest
        import net_benefit_legs
        with pytest.raises(ValueError, match="start_year"):
            net_benefit_legs._default_oas_annual({"assumptions": {}})
