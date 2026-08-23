"""The PRINCIPAL residence, and the registered charge against it.

This is ``cfg['property']`` (singular) -- distinct from ``cfg['properties']``
(plural, ``contract_property``), and the split is the contract's own: the
family home's value reaches the engine through ``house_value`` / LTV / charge
math rather than through the ``property_equities`` list, and its mortgage
AMORTIZES where a non-principal property's secured share is a static snapshot.

Two things live here:

* ``map_property_config`` -- the whole ``cfg['property']`` block: house value,
  appreciation rate, the mortgage's balance/rate/amortization and its
  deductible tranches, the OSFI B-20 charge-limit refusals (#664/#689), the
  HELOC's own SIGNED rate (#654), and the line of credit (#689).
* ``_map_principal_sale`` -- a declared mid-horizon sale of the family home
  (#956 bite E), carried onto ``cfg['property']['principal_sale']`` for the
  principal's own disposition rule. Absent means the home is held to the
  horizon: a strict no-op (DP#32).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from contract_errors import ContractAdaptationError
from contract_people import _owner_shares


def _map_principal_sale(principal: Optional[Dict], mortgage: Optional[Dict],
                        heloc: Optional[Dict], primary_id: Optional[str],
                        spouse_id: Optional[str],
                        family_pre_window: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Issue #956 bite E: map a declared SALE of the PRINCIPAL residence onto
    the config seam the principal's own rule reads.

    The principal residence is a `kind="principal"` property in
    `doc["properties"]`; the schema's `property_sale` block (Bite B) is reused
    verbatim -- a declared `sale` here means the household sells the principal
    in `sale.year` (or the calendar year of `sale.date`), the home + its
    mortgage + any HELOC/SM secured against it leave the balance sheet from
    the sale year, and the net proceeds (gross value less the discharged debt,
    selling costs, and the disposition tax) are invested into the portfolio
    (non-reg). Omitted/null (the golden household and every hold case) -> the
    principal is held to the horizon -> this returns None -> a strict no-op
    (DP#32): the golden invariant is unchanged by construction.

    Returns the same shape Bite B carries on a non-principal `sale` so the
    disposition rule reads one consistent contract:

      - `year`: the sale calendar year (the sweepable numeric `year` leaf
        Bite C added, else `int(date[:4])` -- an explicit `is not None` test,
        never `sale.get("year") or int(...)` (DP#32 forbids `x or DEFAULT`)).
      - `selling_costs`: the couple's share of the one-time disposition costs
        (realtor/notary/inspection); null `selling_costs` is a real $0, read
        explicitly (DP#32).
      - `owner_roles`: each taxed member's share of the property (Canada has
        no joint filing -> each spouse's gain bands against their own return),
        mirroring the non-principal sale's owner_roles spelling exactly.
      - `designated_principal_residence_years`: the PRE periods, carried raw
        so the rule apportionments the gain (ITA s.40(2)(b)). A principal
        designated for its whole ownership is FULLY PRE-exempt -> tax ~ 0.
      - `value_share`: the couple's share of the gross value (the principal's
        FULL value at the couple's ownership % -- the BASE ownership-year value
        the sale realizes; when the principal declares `appreciation_rate`
        (#963 bite F) the disposition rule compounds this base to the sale year,
        so a downsize/sell realizes the GROWN home, not the static figure).
      - `acb_share`: the couple's share of the adjusted cost base. A null
        `acb` means "no accrued gain yet" (bought at value) -> `value_share`
        (the disposition gain collapses to 0, DP#32: never 0.0 as a fallback).

    `secured_share` is NOT carried: the principal's mortgage AMORTIZES in the
    engine (the non-principal path's `secured_share` is a static snapshot of
    a mortgage the engine does not amortize -- it cannot amortize, the
    non-principal mortgage is not in the amortization schedule). The debt
    discharged at the principal's sale is the LIVE year-N balance the rule
    reads off `YearWorkingState` (`new_mortgage_balance` / `new_heloc_balance`
    / `new_sm_heloc`), not a config-time snapshot -- carrying a snapshot here
    would under-state the retired debt (the mortgage has amortized down) and
    break the conservation identity.
    """
    if principal is None:
        return None
    sale = principal.get("sale")
    if sale is None:
        return None
    shares = _owner_shares(principal["owner"])
    couple = [pid for pid in (primary_id, spouse_id) if pid is not None]
    couple_share = sum(frac for pid, frac in shares.items() if pid in couple)
    # A principal the couple does not own cannot be sold by them -- refuse
    # loudly (DP#32) rather than silently carrying a 0-share sale that would
    # inject no proceeds and discharge no debt (a no-op masquerading as a
    # disposition, the exact silent-zero failure this repo exists to prevent).
    if couple_share <= 0:
        raise ContractAdaptationError(
            f"Property {principal['id']!r} declares a sale but the couple owns "
            f"a 0 share of it -- the household cannot sell a home it does not "
            f"own. Declare the owner, or remove the sale (DP#32)."
        )
    # The sale calendar year: the sweepable numeric `year` leaf when present
    # (Bite C), else `int(date[:4])`. Explicit `is not None` -- never
    # `sale.get("year") or int(...)` (DP#32: a year of 0 is a real value the
    # schema permits; `or` would mask it). Exactly one of `year`/`date` is
    # guaranteed by the schema's `property_sale` oneOf.
    syear = (sale["year"] if sale.get("year") is not None
             else int(sale["date"][:4]))
    sc = sale.get("selling_costs")
    selling_costs = (sc if sc is not None else 0.0) * couple_share
    owner_roles = {
        role: shares.get(pid, 0.0)
        for role, pid in (("primary", primary_id), ("spouse", spouse_id))
        if pid is not None and shares.get(pid, 0.0) > 0
    }
    value_share = principal["value"]["amount"] * couple_share
    acb = principal.get("acb")
    acb_share = (acb if acb is not None else value_share) * couple_share
    sale_entry: Dict[str, Any] = {
        "year": syear,
        "selling_costs": selling_costs,
        "owner_roles": owner_roles,
        "designated_principal_residence_years":
            principal.get("designated_principal_residence_years", []),
        "value_share": value_share,
        "acb_share": acb_share,
    }
    # Issue #969: carry the family-level PRE window onto the sale so the
    # disposition rule prices the gain against the FAMILY denominator (ITA
    # s.40(2)(b)), not this property's own span in isolation. Carried ONLY
    # when a genuine family contest exists (>=2 properties with designations);
    # ``None`` (absent) is the byte-identical legacy sentinel -- a single-
    # property household falls back to its own window, unchanged (DP#32).
    if family_pre_window is not None:
        sale_entry["family_pre_window"] = family_pre_window
    # Issue #963 (epic #956 bite F): carry the principal's `appreciation_rate`
    # onto the sale so the disposition rule can price the APPRECIATED gross
    # value at the sale year (a downsize/sell realizes the GROWN home, not the
    # static `value_share`). `value_share` above is the base (ownership-year)
    # value; the rule compounds it by `(1 + rate) ** (syear - start_year)` and
    # uses the appreciated value as the sale's gross (the gain base and the
    # proceeds leg both use it), while `acb_share` stays at cost (appreciation
    # does not change ACB -- DP#19). Carried only when declared so a sale
    # with no rate realizes the static `value_share` byte-identical to bite E
    # (DP#32): the rule's absence-test reads `value_share` directly.
    appreciation_rate = principal.get("appreciation_rate")
    if appreciation_rate is not None:
        sale_entry["appreciation_rate"] = appreciation_rate
    return sale_entry


def map_property_config(principal: Optional[Dict], mortgage: Optional[Dict],
                        heloc: Optional[Dict], credit_facility: Optional[Dict],
                        primary_id: str, spouse_id: Optional[str],
                        family_pre_window: Optional[int]) -> Dict[str, Any]:
    """The internal ``cfg['property']`` block: the principal residence, the
    charge registered against it, and every facility carved out of that
    charge.

    Also the ONE place the charge-limit arithmetic runs at load time
    (#664/#689): a document whose declared mortgage + HELOC limit + secured
    line of credit already exceeds OSFI B-20's combined LTV ceiling is refused
    HERE, at the contract boundary, so every caller of ``to_internal_config``
    gets the refusal whether or not an LTV overlay is ever applied downstream.

    ``family_pre_window`` is the family-level principal-residence-exemption
    denominator (#969), computed once by the caller so the principal's sale and
    every non-principal sale price their gain against the SAME window."""
    prop_cfg: Dict[str, Any] = {
        "house_value": principal["value"]["amount"] if principal else 0,
    }
    # Issue #963 (epic #956 bite F): the principal residence's REAL annual
    # appreciation rate, mirroring Bite A's `appreciation_rate` on a
    # non-principal property. The schema declares this leaf on EVERY property
    # (incl. kind=principal), so it is already permitted on the principal --
    # only the MAPPER + the consumers needed to honor it. Carried only when
    # declared so a household with no rate (incl. the golden fixture, whose
    # legacy `property` dict never carries this key) round-trips byte-identical
    # to today (DP#32): the consumers' absence-test returns the static
    # `house_value` and never read `appreciation_rate`. The base value is
    # `house_value` itself (the principal is held from the projection start,
    # never a dated mid-horizon purchase); the consumers compound
    # `house_value * (1 + rate) ** (cal_year - start_year)`. A sweepable
    # numeric leaf: range it via sensitivity.sweeps (e.g.
    # properties.N.appreciation_rate: [0, 0.03, 0.05]) so a sell/keep
    # conclusion is robust to the home-appreciation assumption rather than
    # hostage to a static-value guess that systematically favours selling.
    if principal is not None:
        appreciation_rate = principal.get("appreciation_rate")
        if appreciation_rate is not None:
            prop_cfg["appreciation_rate"] = appreciation_rate
    if mortgage:
        prop_cfg["mortgage_balance"] = mortgage["balance"]["amount"]
        prop_cfg["mortgage_rate"] = mortgage["rate"]
        if "amortization" in mortgage:
            prop_cfg["amortization_years"] = mortgage["amortization"]["years"]
            # mortgage["amortization"]["payment_monthly"] / .renewal_date /
            # .term_start_date are NOT mapped here (epic #603 Track C Phase
            # 2b finding): the legacy keys they used to target
            # (property.current_payment_monthly/.renewal_date/
            # .contract_start_date) were confirmed dead and deleted from the
            # legacy schema in Phase 2a (test_schema_coverage.py's history);
            # SimulationConfig.from_dict never read them either. Mapping a
            # real contract fact onto a key nothing reads is the DP#18/DP#32
            # "written, not applied" failure this epic exists to end -- so
            # this mapper does not populate them (measured, not asserted: see
            # tests/architecture/test_contract_reachability.py).
        # Issue #1075 (data-model half): the sum of the balances of every
        # kind=mortgage tranche whose new `deductible` flag is set (their
        # interest is deductible under ITA s.20(1)(c) -- borrowed for an
        # income-producing non-registered investment), and -- alongside it --
        # those tranches' EXACT annual interest (each balance * its OWN
        # rate). Surfaced on the config (SimulationConfig
        # .deductible_mortgage_balance / .deductible_mortgage_interest) so
        # the s.20(1)(c) interest pricing (issue #850) can price the
        # declared tranches' TRUE interest instead of tracing it. The
        # interest is NOT deductible_mortgage_balance * mortgage_rate: the
        # rate here is the balance-weighted average of ALL tranches, which
        # coincides with the deductible tranches' own-rate sum only when
        # every tranche shares one rate (DP#32: never price deductible
        # interest off a blended rate). Both emitted only when > 0: a
        # household with no deductible tranche (the golden fixture and every
        # pre-#1075 contract) round-trips byte-identical (DP#32 -- absence
        # of the flag is "not deductible", never a fabricated zero key).
        deductible_mortgage_balance = mortgage.get("deductible_balance", 0.0)
        if deductible_mortgage_balance > 0:
            prop_cfg["deductible_mortgage_balance"] = deductible_mortgage_balance
        deductible_mortgage_interest = mortgage.get("deductible_interest", 0.0)
        if deductible_mortgage_interest > 0:
            prop_cfg["deductible_mortgage_interest"] = deductible_mortgage_interest

    # Issue #956 bite E (principal-residence disposition): a declared
    # mid-horizon SALE of the PRINCIPAL residence. The principal is
    # deliberately excluded from `_map_owned_properties` (its value reaches
    # the annual side via `house_value`/LTV/charge math, not via
    # `property_equities` in total_assets), so Bite B's `property_disposition`
    # rule cannot sell it -- the principal's sale is a separate disposition
    # with its own rule. The contract surface reuses the SAME `property_sale`
    # schema block Bite B defines (the principal is a `kind="principal"`
    # property in `doc["properties"]`, and the schema permits `sale` on any
    # property); this mapper carries it onto `prop_cfg["principal_sale"]`
    # (the principal's own config seam, distinct from the non-principal
    # `properties[]` path). The sale year is sweepable via the same numeric
    # `year` leaf Bite C added (sensitivity.sweeps.properties.N.sale.year),
    # so the optimizer can rank sell-TIMING. Exactly the same shape Bite B
    # carries on a non-principal `sale`: {year, selling_costs, owner_roles,
    # designated_principal_residence_years, value_share, acb_share} -- the
    # rule reads these off the config. `secured_share` is NOT carried here:
    # the principal's mortgage AMORTIZES (the non-principal path's static
    # `secured_share` is a snapshot of a mortgage the engine does not
    # amortize), so the discharged debt is the LIVE year-N balance the rule
    # reads off `ws` at the sale year, not a config-time snapshot.

    principal_sale = _map_principal_sale(
        principal, mortgage, heloc, primary_id, spouse_id,
        family_pre_window=family_pre_window)
    if principal_sale is not None:
        prop_cfg["principal_sale"] = principal_sale

    # issue #689: does the credit facility secure against THIS property's
    # charge at all? null collateral (unsecured) or collateral pointing at a
    # different property both mean "no" -- an unsecured line is genuinely
    # ADDITIONAL borrowing capacity, outside the registered charge, and it
    # must not be folded into a check it was never part of (that would
    # understate the household's real, unsecured capacity in exactly the
    # direction #689 exists to fix).
    credit_facility_secured_here = bool(
        credit_facility and principal and credit_facility.get("collateral") == principal["id"]
    )

    if heloc or credit_facility_secured_here:
        # issue #664/#689: a readvanceable mortgage, its HELOC, and any
        # SECURED line of credit against the same property are carved out
        # of ONE registered charge with ONE combined limit -- refuse loudly
        # (DP#32) if the document itself declares secured debt that already
        # exceeds it, rather than silently modeling a >80% LTV facility.
        # Checked here, at the ONE contract-loading boundary, so every
        # caller of to_internal_config gets the refusal regardless of
        # whether an LTV overlay is ever applied downstream
        # (apply_ltv_overlay/apply_overlay re-check after any overlay that
        # grows the mortgage further, per their own docstrings).
        from simulation_config import (
            charge_limit as _charge_limit, heloc_revolving_limit as _heloc_revolving_limit,
            OSFI_B20_CHARGE_LTV_MAX, OSFI_B20_REVOLVING_LTV_MAX, _CHARGE_TOLERANCE,
        )
        house_value = principal["value"]["amount"]
        declared_mortgage = mortgage["balance"]["amount"] if mortgage else 0
        declared_heloc_limit = heloc["limit"] if heloc else 0
        declared_cf_limit = credit_facility["limit"] if credit_facility_secured_here else 0
        combined_limit = _charge_limit(house_value, OSFI_B20_CHARGE_LTV_MAX)
        combined_secured = declared_mortgage + declared_heloc_limit + declared_cf_limit
        if combined_secured > combined_limit + _CHARGE_TOLERANCE:
            raise ContractAdaptationError(
                f"Declared mortgage (${declared_mortgage:,.0f}) + HELOC limit "
                f"(${declared_heloc_limit:,.0f})"
                + (f" + secured line of credit limit (${declared_cf_limit:,.0f})"
                   if credit_facility_secured_here else "")
                + f" = ${combined_secured:,.0f} secured debt exceeds the charge "
                f"registered against {principal['id']!r} (${combined_limit:,.0f} = "
                f"{OSFI_B20_CHARGE_LTV_MAX:.0%} of ${house_value:,.0f} house value "
                f"-- OSFI B-20's legal maximum LTV for an uninsured combined loan "
                f"plan). Every facility secured against the SAME property shares "
                f"ONE registered charge -- this is not a valid combination (#664/#689)."
            )
        if heloc:
            revolving_limit = _heloc_revolving_limit(house_value, OSFI_B20_REVOLVING_LTV_MAX)
            if declared_heloc_limit > revolving_limit + _CHARGE_TOLERANCE:
                raise ContractAdaptationError(
                    f"Declared HELOC limit (${declared_heloc_limit:,.0f}) on "
                    f"{principal['id']!r} exceeds the revolving-only ceiling "
                    f"(${revolving_limit:,.0f} = {OSFI_B20_REVOLVING_LTV_MAX:.0%} of "
                    f"${house_value:,.0f} house value) -- OSFI B-20 caps the "
                    f"readvanceable/revolving segment of a combined loan plan at "
                    f"65% LTV independent of the 80% combined cap; lending between "
                    f"65% and 80% must be amortizing and non-readvanceable (#664). "
                    f"(A secured line_of_credit is NOT subject to this specific "
                    f"sub-ceiling -- it is not the mortgage-paired readvance "
                    f"mechanism OSFI's 65% figure targets -- only to the 80% "
                    f"combined cap above.)"
                )

    if heloc:
        prop_cfg["margin_available"] = heloc["limit"]
        prop_cfg["heloc_readvance"] = heloc.get("readvanceable", False)
        # issue #654: the HELOC's OWN declared rate/rate_type -- direct
        # indexing (not .get()), same as mortgage["rate"] above, because
        # the schema REQUIRES both on every kind=heloc liability, so their
        # absence here would mean validate_contract() was skipped, and
        # that must raise loudly (KeyError), never default. Mapped to
        # property.heloc_rate/.heloc_rate_type (SimulationConfig.heloc_rate/
        # .heloc_rate_type -> FamilySimulation's heloc_path), the ONLY
        # spelling the engine actually reads (#654 found and closed the
        # gap this comment used to describe: assumptions.rate_paths.heloc
        # -> assumptions_cfg["heloc_rate"] below was never read by
        # SimulationConfig.from_dict either -- "scenario_discovery.py's
        # real HELOC-rate consumer" was true only for scenario_discovery's
        # OWN strategy-discovery heuristics, never for the engine that
        # actually prices HELOC/SM interest, which is simulation.py's
        # build_heloc_path -- previously wired ONLY off property.
        # mortgage_rate, the #654/#595B bug).
        prop_cfg["heloc_rate"] = heloc["rate"]
        prop_cfg["heloc_rate_type"] = heloc["rate_type"]
        # heloc["balance"]/.capitalize_interest/.deductibility/
        # .qc_carryforward_opening are NOT mapped to a `legacy["heloc"]`
        # block (there used to be one here). Confirmed (epic #603 Track C
        # Phase 2b) that legacy["heloc"] has ZERO production readers:
        # SimulationConfig dropped its heloc_data field in Phase 2a (DP#9,
        # "HELOCConfig had zero production callers") and nothing else
        # reads cfg['heloc'] either (grepped clean). Writing this block
        # was producing confident-looking output that reached no decision
        # -- exactly the DP#32 failure this epic exists to end, so it is
        # deleted, not carried forward as a duplicate declaration. The
        # opening drawn balance is deliberately NOT mapped: issue #577
        # makes SimState.initial() always start heloc_balance at 0 (undrawn)
        # by design, regardless of any declared opening balance -- a draw is
        # something the simulation decides, not a fact the household states
        # (see simulation_state.py's SimState.initial docstring).

    if credit_facility:
        # issue #689: makes the facility reachable by the engine at all --
        # SimulationConfig.credit_facility_limit/_rate/_rate_type/_secured,
        # consumed by simulation_rules.apply_solvency (issue #679's
        # liquidation waterfall's second rung) and by
        # trajectory_invariants.check_total_secured_debt_within_charge (only
        # when secured). The opening drawn balance is deliberately NOT
        # mapped -- same reasoning as heloc.balance above (#577): whether
        # this facility is ever drawn is a simulation decision (only the
        # #679 waterfall draws it, in a shortfall year), never a fact read
        # off this field. An undrawn facility therefore starts, and stays,
        # undrawn until a real shortfall reaches it.
        prop_cfg["credit_facility_limit"] = credit_facility["limit"]
        prop_cfg["credit_facility_rate"] = credit_facility["rate"]
        prop_cfg["credit_facility_rate_type"] = credit_facility["rate_type"]
        prop_cfg["credit_facility_secured"] = credit_facility_secured_here
    return prop_cfg
