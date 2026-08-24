#!/usr/bin/env python3
"""Issue #963 (epic #956 bite F): principal-residence appreciation.

Bite A (#958) appreciates NON-principal properties. The PRINCIPAL residence
flows via ``house_value`` / ``prop_cfg`` and was STATIC -- its value never grew
over the horizon, which biases every sell/downsize analysis (#956 Bite D):
selling a static home to invest the proceeds looks favourable ONLY because the
home you give up never appreciates. This bite adds a per-home
``appreciation_rate`` to the principal (mirroring Bite A), applied so the home's
value at calendar year Y = ``base_value * (1 + rate) ** (Y - start_year)``
EVERYWHERE the home's value is read:

  - the annual LTV / charge-room math (``apply_sm_readvance`` /
    ``apply_margin_heloc_interest`` read ``_principal_value_for_year``);
  - Bite E's principal sale gross (``_principal_disposition_for`` compounds
    ``value_share`` to the sale year, so a downsize/sell realizes the GROWN
    home);
  - the estate's terminal deemed-disposition FMV (``objective._estate_call_args``
    compounds ``house_value`` to the terminal calendar year).

The principal's value is NOT in ``total_assets`` (it flows via ``house_value``
/ charge math, off the balance sheet -- see ``SimState.total_assets``), so
appreciation does NOT move terminal ``total_assets``; it moves the ESTATE
(``principal_residence_fmv``) and the charge room, and a Bite E sale's
proceeds. Absence-safe (DP#32): absent/0.0 rate => the static ``house_value``
byte-identical (the golden invariant ``9709753.139463063`` is unchanged).

These tests run the real engine (``FamilySimulation.run``), so the
money-conservation invariant suite (``trajectory_invariants.assert_run_
invariants``, wired into ``run()``) is enforced automatically on every run
here. All fixtures use fabricated ids and round numbers (DP#4/DP#15); no real
figure, name, or account enters the repo (DP#15).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from rules_leverage import _principal_value_for_year
from rules_disposition import _principal_disposition_for
from simulation_config import charge_room_for_readvance
from objective import compute_after_tax_estate

from test_input_contract import _load_example, _two_generation_subset
from test_golden_trajectory_581 import golden_household_config, _run
import contract_schema


# ──────────────────────────────────────────────────────────────────────────
# Fixtures: the shipped example's principal residence (joint p1/p2, value
# 650k, fully PRE-designated, mortgage + HELOC secured against it), with an
# optional appreciation_rate and an optional dated SALE. The example projects
# 50 years from start_year 2026, so results[i] is calendar year 2026 + i.
# ──────────────────────────────────────────────────────────────────────────

_PRINCIPAL_VALUE = 650_000      # the shipped example's principal residence value
_SALE_2031 = {"year": 2031, "selling_costs": 30_000}
_SALE_YEAR_INDEX = 5             # 2031 - 2026


def _base_doc():
    """The shipped two-generation example, validated (the sub-family the
    adapter can honestly map onto the two-adults-plus-children engine)."""
    doc = _two_generation_subset(_load_example())
    contract_schema.validate_contract(doc)
    return doc


def _set_principal_rate(doc, rate):
    """Set ``appreciation_rate`` on the kind=principal property."""
    doc = copy.deepcopy(doc)
    for prop in doc["properties"]:
        if prop["kind"] == "principal":
            prop["appreciation_rate"] = rate
    return doc


def _add_principal_sale(doc, sale):
    """Add a ``sale`` block to the principal residence (the kind=principal
    property). Reuses the SAME ``property_sale`` schema block Bite B/E define."""
    doc = copy.deepcopy(doc)
    for prop in doc["properties"]:
        if prop["kind"] == "principal":
            prop["sale"] = sale
    return doc


def _run_doc(doc):
    """Validate -> map to internal config -> run the real engine (which
    enforces the money-conservation invariant suite on every run)."""
    contract_schema.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run(), legacy


# ============================================================================
# 1. The per-year value function — where ranking correctness lives.
# ============================================================================
class PrincipalValueForYear(unittest.TestCase):
    """The compounding math and its absence-safety (DP#32)."""

    def _cfg(self, **kw):
        return SimulationConfig(house_value=800_000, start_year=2026, **kw)

    def test_no_rate_is_static_house_value(self):
        # DP#32: absent rate => the static house_value, never reads the exponent.
        cfg = self._cfg()  # appreciation_rate defaults to None
        self.assertEqual(_principal_value_for_year(cfg, 2071), 800_000)

    def test_zero_rate_is_static_house_value(self):
        cfg = self._cfg(appreciation_rate=0.0)
        self.assertEqual(_principal_value_for_year(cfg, 2071), 800_000)

    def test_value_compounds_from_start_year(self):
        cfg = self._cfg(appreciation_rate=0.03)
        # 800k @3% for 5yr (2031 - 2026)
        self.assertAlmostEqual(_principal_value_for_year(cfg, 2031),
                              800_000 * 1.03 ** 5, places=6)

    def test_start_year_equals_base_value(self):
        # years == 0 => appreciated value == base value (no growth yet).
        cfg = self._cfg(appreciation_rate=0.03)
        self.assertEqual(_principal_value_for_year(cfg, 2026), 800_000)

    def test_negative_rate_depreciates(self):
        cfg = self._cfg(appreciation_rate=-0.05)
        self.assertAlmostEqual(_principal_value_for_year(cfg, 2028),
                              800_000 * 0.95 ** 2, places=6)

    def test_before_start_year_clamps_to_base(self):
        # A cal_year before start_year => 0 years held => base value (defensive).
        cfg = self._cfg(appreciation_rate=0.03)
        self.assertEqual(_principal_value_for_year(cfg, 2020), 800_000)


# ============================================================================
# 2. The mapper carries the principal's appreciation_rate (absence-safe).
# ============================================================================
class MapperCarriesRate(unittest.TestCase):
    """``input_contract.to_internal_config`` carries the principal's
    ``appreciation_rate`` onto ``cfg['property']['appreciation_rate']`` only
    when declared; absent => the key is absent (byte-identical, DP#32)."""

    def test_absent_rate_not_carried(self):
        legacy = ic.to_internal_config(_base_doc())
        self.assertNotIn('appreciation_rate', legacy['property'],
                         "absent rate must not be carried (DP#32: None round-trips "
                         "to absent, not a literal null)")
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIsNone(cfg.appreciation_rate)

    def test_declared_rate_carried(self):
        doc = _set_principal_rate(_base_doc(), 0.03)
        legacy = ic.to_internal_config(doc)
        self.assertEqual(legacy['property']['appreciation_rate'], 0.03)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(cfg.appreciation_rate, 0.03)

    def test_rate_round_trips_through_to_dict(self):
        # DP#24: a declared rate survives a load->modify->save cycle; None
        # round-trips to absent (not an explicit null).
        doc = _set_principal_rate(_base_doc(), 0.03)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        out = cfg.to_dict()
        self.assertEqual(out['property']['appreciation_rate'], 0.03)
        cfg2 = SimulationConfig.from_dict(out)
        self.assertEqual(cfg2.appreciation_rate, 0.03)
        # absent stays absent
        cfg_none = SimulationConfig.from_dict(ic.to_internal_config(_base_doc()))
        self.assertNotIn('appreciation_rate', cfg_none.to_dict()['property'])


# ============================================================================
# 2b. The mapper carries the principal's appreciation_rate into the ESTATE
#     block (issue #963 bite F fix): the estate's deemed-disposition reads
#     the rate from the estate block itself (self-describing, DP#9), not a
#     pointer to the property block.
# ============================================================================
class EstateBlockCarriesRate(unittest.TestCase):
    """``contract_estate._map_estate`` carries the principal's
    ``appreciation_rate`` onto the estate block as
    ``principal_residence_appreciation_rate`` and into the ``property_gains``
    principal entry, only when declared; absent => not carried (DP#32). The
    mapper carries the RATE (never the appreciated value) because the terminal
    year is a simulation-result fact (``start_year + len(results) - 1``),
    unknown at map time."""

    def test_absent_rate_not_carried_on_estate(self):
        legacy = ic.to_internal_config(_base_doc())
        self.assertNotIn(
            'principal_residence_appreciation_rate', legacy['estate'],
            "absent rate must not be carried onto the estate block (DP#32)")

    def test_declared_rate_carried_on_estate(self):
        legacy = ic.to_internal_config(_set_principal_rate(_base_doc(), 0.03))
        self.assertEqual(
            legacy['estate']['principal_residence_appreciation_rate'], 0.03)

    def test_estate_rate_round_trips_through_to_dict(self):
        # DP#24: the estate block (an opaque dict the config round-trips
        # verbatim) carries the rate through a load->modify->save cycle.
        doc = _set_principal_rate(_base_doc(), 0.03)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        out = cfg.to_dict()
        self.assertEqual(
            out['estate']['principal_residence_appreciation_rate'], 0.03)
        # absent stays absent
        cfg_none = SimulationConfig.from_dict(ic.to_internal_config(_base_doc()))
        self.assertNotIn(
            'principal_residence_appreciation_rate', cfg_none.to_dict()['estate'])

    def test_plan_carries_rate(self):
        # EstatePlan.principal_residence_appreciation_rate is threaded through
        # plan_from_config, so the deemed-disposition plan is self-describing.
        from objective import plan_from_config
        legacy = ic.to_internal_config(_set_principal_rate(_base_doc(), 0.03))
        plan = plan_from_config(legacy)
        self.assertEqual(plan.principal_residence_appreciation_rate, 0.03)
        # absent => None (the field's default, byte-identical)
        plan_none = plan_from_config(ic.to_internal_config(_base_doc()))
        self.assertIsNone(plan_none.principal_residence_appreciation_rate)


# ============================================================================
# 2c. The property_gains principal entry carries the rate (multi-property
#     households, issue #695). The cottage carries no rate (non-principal
#     appreciation is Bite A's concern, not the estate's PRE allocation).
# ============================================================================
def _two_property_doc(rate):
    """The shipped example + a cottage designated for non-overlapping years,
    so the per-property PRE allocation engages (property_gains is built). The
    principal needs an ACB here because its gain stays partly taxable when
    the cottage takes some designation years."""
    doc = _base_doc()
    doc["properties"].append({
        "id": "cottage", "kind": "recreational",
        "value": {"amount": 300_000, "as_of": "2026-06-30"},
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "acb": 200_000,
        "designated_principal_residence_years": [
            {"from": "1998-01-01", "to": "2002-12-31"}],
    })
    for p in doc["properties"]:
        if p["kind"] == "principal":
            p["acb"] = 400_000
            if rate is not None:
                p["appreciation_rate"] = rate
    contract_schema.validate_contract(doc)
    return doc


class PropertyGainsCarriesRate(unittest.TestCase):
    """``_map_pre_property_gains`` carries the principal's
    ``appreciation_rate`` onto the principal's gain entry (so the estate's
    deemed disposition can compound it); the cottage entry carries none."""

    def test_principal_entry_carries_rate(self):
        legacy = ic.to_internal_config(_two_property_doc(0.03))
        gains = legacy['estate']['property_gains']
        self.assertIsNotNone(gains, "a two-property household with "
                                     "designations must build property_gains")
        principal = next(g for g in gains if g['is_principal'])
        cottage = next(g for g in gains if not g['is_principal'])
        self.assertEqual(principal.get('appreciation_rate'), 0.03)
        self.assertNotIn('appreciation_rate', cottage,
                         "a non-principal property's appreciation is Bite A's "
                         "concern, not the estate's PRE allocation")

    def test_absent_rate_not_carried_in_property_gains(self):
        legacy = ic.to_internal_config(_two_property_doc(None))
        gains = legacy['estate']['property_gains']
        principal = next(g for g in gains if g['is_principal'])
        self.assertNotIn('appreciation_rate', principal, "absent => not carried")


# ============================================================================
# 3. The estate's terminal FMV rises with appreciation (absent => unchanged).
# ============================================================================
class EstateAppreciates(unittest.TestCase):
    """A principal declaring ``appreciation_rate`` has a HIGHER after-tax
    estate than a static one (the grown home's FMV enters the gross estate);
    absent/0.0 => byte-identical estate (DP#32)."""

    def test_appreciated_estate_exceeds_static(self):
        base_doc = _base_doc()
        res_base, legacy_base = _run_doc(base_doc)
        res_appr, legacy_appr = _run_doc(_set_principal_rate(base_doc, 0.03))
        estate_base = compute_after_tax_estate(res_base, legacy_base).net_estate
        estate_appr = compute_after_tax_estate(res_appr, legacy_appr).net_estate
        self.assertGreater(estate_appr, estate_base,
                           "an appreciating principal must raise the after-tax "
                           "estate (its grown FMV enters the gross estate)")
        # The principal is NOT in total_assets, so terminal total_assets is
        # unchanged by appreciation (the home's value is off the balance sheet).
        self.assertEqual(res_appr[-1].total_assets, res_base[-1].total_assets)

    def test_zero_rate_is_byte_identical_estate(self):
        base_doc = _base_doc()
        res_base, legacy_base = _run_doc(base_doc)
        res_zero, legacy_zero = _run_doc(_set_principal_rate(base_doc, 0.0))
        self.assertEqual(compute_after_tax_estate(res_zero, legacy_zero).net_estate,
                         compute_after_tax_estate(res_base, legacy_base).net_estate)

    def test_appreciated_fmv_matches_compounding(self):
        # The terminal-year FMV the estate sees = base * (1+rate)^(years held).
        base_doc = _base_doc()
        res_appr, legacy_appr = _run_doc(_set_principal_rate(base_doc, 0.03))
        cfg = SimulationConfig.from_dict(legacy_appr)
        years = len(res_appr) - 1
        expected_fmv = _PRINCIPAL_VALUE * 1.03 ** years
        # The estate's principal_residence_fmv feeds the gross estate; recover
        # it from the estate result's house_equity + mortgage (the gross FMV
        # less the terminal mortgage = house_equity). Compare the grown leg.
        estate = compute_after_tax_estate(res_appr, legacy_appr)
        self.assertAlmostEqual(estate.house_equity + res_appr[-1].mortgage_balance,
                              expected_fmv, places=4)

    def test_estate_reads_carried_rate_from_estate_block(self):
        # Issue #963 bite F fix: the objective layer compounds using the rate
        # CARRIED on the estate block (self-describing, DP#9), not a pointer to
        # the property block. Prove it by setting the rate ONLY on the estate
        # block (leaving property.appreciation_rate absent) and showing the
        # estate still rises. An overlay that moves the estate rate without
        # rebuilding the property block must still flow through.
        base_doc = _base_doc()
        res_base, legacy_base = _run_doc(base_doc)
        legacy_appr = copy.deepcopy(legacy_base)
        # Inject the rate ONLY on the estate block; strip it from property.
        legacy_appr.setdefault('estate', {})['principal_residence_appreciation_rate'] = 0.03
        legacy_appr.get('property', {}).pop('appreciation_rate', None)
        cfg_appr = SimulationConfig.from_dict(legacy_appr)
        res_appr = FamilySimulation(cfg_appr).run()
        estate_base = compute_after_tax_estate(res_base, legacy_base).net_estate
        estate_appr = compute_after_tax_estate(res_appr, legacy_appr).net_estate
        self.assertGreater(estate_appr, estate_base,
                           "the estate must rise from the rate carried on the "
                           "estate block alone -- the deemed disposition reads "
                           "cfg['estate'], not cfg['property']")

    def test_property_block_rate_still_flows_as_fallback(self):
        # Robustness: when the estate block does NOT carry the rate (e.g. an
        # overlay that sets property.appreciation_rate without rebuilding the
        # estate block), the objective layer falls back to the property block.
        base_doc = _base_doc()
        res_base, legacy_base = _run_doc(base_doc)
        legacy_appr = copy.deepcopy(legacy_base)
        legacy_appr.setdefault('property', {})['appreciation_rate'] = 0.03
        # estate block intentionally does NOT carry the rate
        cfg_appr = SimulationConfig.from_dict(legacy_appr)
        res_appr = FamilySimulation(cfg_appr).run()
        estate_base = compute_after_tax_estate(res_base, legacy_base).net_estate
        estate_appr = compute_after_tax_estate(res_appr, legacy_appr).net_estate
        self.assertGreater(estate_appr, estate_base,
                           "the property-block rate is the fallback path; an "
                           "overlay that moves only property.appreciation_rate "
                           "must still raise the estate")


# ============================================================================
# 4. Bite E's sale realizes the APPRECIATED value (a sell/downsize prices the
#    grown home), and the realized gain grows by the accrued appreciation.
# ============================================================================
class SaleRealizesAppreciatedValue(unittest.TestCase):
    """With Bite E, selling the principal realizes the APPRECIATED gross value
    at the sale year; the realized gain grows by exactly the accrued
    appreciation (ACB stays at cost, DP#19). Absent rate => byte-identical to
    bite E (DP#32)."""

    def test_sale_proceeds_and_gain_grow_with_appreciation(self):
        doc = _add_principal_sale(_base_doc(), _SALE_2031)
        res_base, _ = _run_doc(doc)
        res_appr, _ = _run_doc(_set_principal_rate(doc, 0.03))
        i = _SALE_YEAR_INDEX
        # The sale-year proceeds (P_net) and realized gain both grow.
        self.assertGreater(res_appr[i].principal_sale_proceeds_invested,
                           res_base[i].principal_sale_proceeds_invested)
        self.assertGreater(res_appr[i].principal_sale_realized_gain,
                           res_base[i].principal_sale_realized_gain)
        # The realized gain = accrued appreciation (ACB == value at base, so
        # gain = appreciated_value - base_value = base*((1+r)^years - 1)).
        # The couple owns 100% of the principal (joint p1/p2 @ 50/50), so
        # value_share == the full principal value.
        expected_gain = _PRINCIPAL_VALUE * (1.03 ** (2031 - 2026) - 1)
        self.assertAlmostEqual(res_appr[i].principal_sale_realized_gain,
                              expected_gain, places=4)

    def test_sale_without_rate_is_byte_identical_to_bite_e(self):
        doc = _add_principal_sale(_base_doc(), _SALE_2031)
        res_base, _ = _run_doc(doc)
        res_zero, _ = _run_doc(_set_principal_rate(doc, 0.0))
        i = _SALE_YEAR_INDEX
        self.assertEqual(res_zero[i].principal_sale_proceeds_invested,
                         res_base[i].principal_sale_proceeds_invested)
        self.assertEqual(res_zero[i].principal_sale_realized_gain,
                         res_base[i].principal_sale_realized_gain)
        self.assertEqual(res_zero[i].principal_sale_disposition_tax,
                         res_base[i].principal_sale_disposition_tax)

    def test_helper_compounds_value_share_to_sale_year(self):
        # The pure helper compounds value_share to the sale year; ACB stays at
        # cost, so the gain grows by the accrued appreciation.
        sale = {"year": 2031, "selling_costs": 30_000, "owner_roles": {},
                "designated_principal_residence_years": [],
                "value_share": 650_000.0, "acb_share": 650_000.0,
                "appreciation_rate": 0.03}
        figures = _principal_disposition_for(
            sale, 2031, mortgage_balance=300_000.0, heloc_balance=0.0,
            sm_heloc=0.0, brackets=[], primary_taxable_income=0.0,
            spouse_taxable_income=0.0, start_year=2026)
        expected_value = 650_000.0 * 1.03 ** (2031 - 2026)
        self.assertAlmostEqual(figures['realized_gain'],
                              expected_value - 650_000.0, places=4)

    def test_helper_without_rate_ignores_start_year(self):
        # Absent rate => start_year is not required (absence-safe, DP#32).
        sale = {"year": 2031, "selling_costs": 0.0, "owner_roles": {},
                "designated_principal_residence_years": [],
                "value_share": 650_000.0, "acb_share": 400_000.0}
        figures = _principal_disposition_for(
            sale, 2031, mortgage_balance=300_000.0, heloc_balance=0.0,
            sm_heloc=0.0, brackets=[], primary_taxable_income=0.0,
            spouse_taxable_income=0.0)  # no start_year
        self.assertAlmostEqual(figures['realized_gain'], 250_000.0, places=6)

    def test_helper_appreciation_without_start_year_raises(self):
        # A direct caller that declares appreciation_rate MUST pass start_year
        # (the rule does, from ctx.config.start_year). Refusing loudly rather
        # than silently compounding against an unknown base (DP#32).
        sale = {"year": 2031, "selling_costs": 0.0, "owner_roles": {},
                "designated_principal_residence_years": [],
                "value_share": 650_000.0, "acb_share": 400_000.0,
                "appreciation_rate": 0.03}
        with self.assertRaises(ValueError):
            _principal_disposition_for(
                sale, 2031, mortgage_balance=300_000.0, heloc_balance=0.0,
                sm_heloc=0.0, brackets=[], primary_taxable_income=0.0,
                spouse_taxable_income=0.0)  # no start_year -> raises


# ============================================================================
# 5. The annual LTV / charge-room math reads the appreciated value (a grown
#    home has more collateral to re-borrow against).
# ============================================================================
class ChargeRoomAppreciates(unittest.TestCase):
    """``apply_sm_readvance`` / ``apply_margin_heloc_interest`` price the charge
    against the APPRECIATED value, so a grown home has more readvance room."""

    def test_charge_room_grows_with_appreciation(self):
        # Direct unit test of the charge-room primitive fed the appreciated
        # value: at a 5% rate over 10yr, an 800k home's charge limit (80%) grows
        # from 640k to 800k*1.05**10*0.8.
        cfg_static = SimulationConfig(house_value=800_000, start_year=2026)
        cfg_appr = SimulationConfig(house_value=800_000, start_year=2026,
                                     appreciation_rate=0.05)
        cal_year = 2036  # 10 years on
        hv_static = _principal_value_for_year(cfg_static, cal_year)
        hv_appr = _principal_value_for_year(cfg_appr, cal_year)
        room_static = charge_room_for_readvance(
            house_value=hv_static, mortgage_balance=200_000,
            drawn_revolving=0.0, charge_ltv_limit=0.80, heloc_ltv_limit=0.65)
        room_appr = charge_room_for_readvance(
            house_value=hv_appr, mortgage_balance=200_000,
            drawn_revolving=0.0, charge_ltv_limit=0.80, heloc_ltv_limit=0.65)
        self.assertGreater(room_appr, room_static,
                           "an appreciating home must grow the charge room")
        self.assertAlmostEqual(room_appr, hv_appr * 0.80 - 200_000, places=6)


# ============================================================================
# 6. Golden invariant: absent rate => byte-identical (DP#32). The golden
#    fixture's legacy `property` dict never carries `appreciation_rate`.
# ============================================================================
class GoldenInvariantHeld(unittest.TestCase):
    """The golden household declares no appreciation_rate; its terminal
    total_assets must be byte-exact (``9709753.139463063``)."""

    def test_golden_total_assets_byte_exact(self):
        results = _run(golden_household_config())
        self.assertEqual(repr(results[-1].total_assets), '9709753.139463063')

    def test_golden_config_has_no_appreciation_rate(self):
        cfg = SimulationConfig.from_dict(golden_household_config())
        self.assertIsNone(cfg.appreciation_rate,
                           "the golden fixture's legacy property dict never "
                           "carries appreciation_rate (DP#32: absent => static)")

    def test_declared_zero_rate_on_golden_is_byte_identical(self):
        # A declared 0.0 rate is a real value (DP#32: zero is a value, not a
        # fallback) and must also leave the golden trajectory byte-identical.
        cfg = golden_household_config()
        cfg['property']['appreciation_rate'] = 0.0
        results = _run(cfg)
        self.assertEqual(repr(results[-1].total_assets), '9709753.139463063')


if __name__ == '__main__':
    unittest.main()