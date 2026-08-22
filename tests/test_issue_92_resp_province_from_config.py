#!/usr/bin/env python3
"""Tests for issue #92: DP#16 -- province='quebec' is hardcoded in RESPChild
construction inside analyze_resp_for_family (resp_rules.py:870), forcing QESI
eligibility regardless of the household's actual province. The trigger data
(province) must be read from the config, not fabricated, so a non-Quebec
household's RESP is not silently over-credited with QESI.

DP#16: modules auto-include on trigger data; the absence of data is the only
disable path. The default remains 'quebec' for a config that supplies no
province (absent input falls back to the historical behaviour -- DP#32), but
an EXPLICIT non-Quebec province must be honoured, exactly as the RESPChild
model's own `province` field already is.

Fabricated round numbers, role-based names only (DP#4/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cfg(province="quebec"):
    return {
        "tax": {"province": province, "country": "canada"},
        "family": {
            "members": [{"gross_income": 100_000}],
            "children": [
                {"name": "child_a", "age": 10},
            ],
        },
        "accounts": {
            "resp_current_balance": 30_000,
            "resp_room_accumulated": 80_000,
            "resp_annual_room_per_child": 3_000,
        },
    }


class TestRespProvinceFromConfig:
    def test_quebec_household_gets_qesi_as_before(self):
        """A Quebec household is byte-identical to the pre-fix behaviour: the
        child is QESI-eligible because province 'quebec' is honoured."""
        from countries.canada.resp_rules import analyze_resp_for_family
        result = analyze_resp_for_family(_cfg("quebec"))
        assert result["child_a"]["qesi_eligible"] is True
        assert result["child_a"]["province"] == "quebec"

    def test_ontario_household_is_not_qesi_eligible(self):
        """An explicitly-Ontario household no longer gets QESI: the province
        is read from the config, not fabricated as Quebec (the #92 defect)."""
        from countries.canada.resp_rules import analyze_resp_for_family
        result = analyze_resp_for_family(_cfg("ontario"))
        assert result["child_a"]["qesi_eligible"] is False
        assert result["child_a"]["province"] == "ontario"

    def test_absent_province_defaults_to_quebec(self):
        """DP#32: a config with no province declared falls back to the
        historical 'quebec' default -- a household that never stated a
        province behaves exactly as before (backward compatible)."""
        from countries.canada.resp_rules import analyze_resp_for_family
        cfg = _cfg("quebec")
        del cfg["tax"]["province"]
        result = analyze_resp_for_family(cfg)
        assert result["child_a"]["province"] == "quebec"
        assert result["child_a"]["qesi_eligible"] is True