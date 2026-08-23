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
        # Issue #1039: the OPENING POSITION cross-check (#664). The check
        # above validates the facilities' LIMITS -- potential borrowing.
        # This validates the household's TRUE OPENING POSITION: mortgage +
        # actually-drawn HELOC balance <= the registered charge. When the
        # drawn balance is within its own limit it is arithmetically implied
        # by the limits check above (drawn <= limit), so this fires only when
        # a document declares a drawn balance ABOVE its facility's limit that
        # also breaches the charge -- and it fires BEFORE the over-limit
        # refusal below so the household learns about the charge breach (the
        # fact that governs any refinancing) first, never just the
        # bookkeeping error.
        opening_drawn = heloc["balance"]["amount"] if heloc else 0
        if (opening_drawn > 0
                and declared_mortgage + opening_drawn > combined_limit + _CHARGE_TOLERANCE):
            raise ContractAdaptationError(
                f"liability {heloc['id']!r} (kind=heloc) declares an opening "
                f"drawn balance of ${opening_drawn:,.0f}, which together with "
                f"the mortgage (${declared_mortgage:,.0f}) puts ${declared_mortgage + opening_drawn:,.0f} "
                f"of secured debt against {principal['id']!r} -- beyond the "
                f"charge registered on it (${combined_limit:,.0f} = "
                f"{OSFI_B20_CHARGE_LTV_MAX:.0%} of ${house_value:,.0f} house "
                f"value). An opening drawn position is honoured as a true "
                f"starting balance (#1039), but no true position can exceed "
                f"the registered charge securing it (#664). Refusing loudly "
                f"rather than simulating debt no lender could have advanced."
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
        # Issue #1039: an opening DRAWN balance is a TRUE STARTING POSITION,
        # not a refusal and not a silent drop. A household already partway
        # through a borrow-to-invest strategy starts the simulation from its
        # real position: heloc_balance = balance.amount, margin_available =
        # limit - drawn (less standby room), and a margin_tracing derived
        # from the DECLARED deductibility.investment_portion -- the original
        # borrowing's purpose is a historical fact that predates the
        # snapshot, so it is carried in, never re-derived from a simulation
        # decision (#577 governs DRAWS THE ENGINE MAKES; this honours a draw
        # THE HOUSEHOLD ALREADY MADE). DP#32 keeps absence loud: a declared
        # opening balance WITHOUT a deductibility block would leave the trace
        # un-derivable (defaulting it to 0 or 1 would both be fabrications),
        # so that combination still refuses.
        heloc_drawn = heloc["balance"]["amount"]
        heloc_deductibility = heloc.get("deductibility")
        if heloc_drawn > 0 and heloc_deductibility is None:
            raise ContractAdaptationError(
                f"liability {heloc['id']!r} (kind=heloc) declares balance.amount="
                f"{heloc_drawn:,.0f} > 0, an OPENING DRAWN balance, but no "
                f"deductibility block. The engine honours an opening drawn "
                f"position as a true starting balance (#1039) -- but its "
                f"s.20(1)(c) trace cannot be derived: the original borrowing's "
                f"purpose is a historical fact only the document carries, and "
                f"defaulting it to fully-deductible or fully-personal would "
                f"both fabricate a tax position (DP#32). Declare "
                f"deductibility.investment_portion = p (the share of the "
                f"opening balance traced to investment use), or set "
                f"balance.amount = 0 if the facility is actually undrawn."
            )
        if heloc_drawn > heloc["limit"]:
            raise ContractAdaptationError(
                f"liability {heloc['id']!r} (kind=heloc) declares "
                f"balance.amount={heloc_drawn:,.0f} above its own limit "
                f"({heloc['limit']:,.0f}). An opening drawn position is "
                f"honoured as a true starting balance (#1039), but a facility "
                f"cannot be drawn past its declared credit limit -- no lender "
                f"balances that. Fix the balance or the limit; refusing "
                f"loudly rather than simulating a negative undrawn room."
            )
        prop_cfg["margin_available"] = heloc["limit"] - heloc_drawn
        if heloc_drawn > 0:
            # Only written when a draw exists, so absence in the internal
            # dict keeps meaning "undrawn" -- the same >0-only convention
            # deductible_mortgage_balance uses (DP#24/DP#32).
            prop_cfg["heloc_opening_balance"] = heloc_drawn
            prop_cfg["heloc_opening_investment_portion"] = \
                heloc_deductibility["investment_portion"]
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
        # Issue #1036: the three heloc declarations that used to be silently
        # dropped here are now each either READ or REFUSED LOUDLY (DP#32:
        # absence must fail loudly, never default to zero / never drop a
        # declared fact). The schema-coverage DEAD_ALLOWLIST entries for
        # these three are removed in the same PR -- a leaf is no longer
        # allowlisted as dead once the engine consumes or refuses it.
        #
        # 1. capitalize_interest -- READ. Mapped to property.capitalize_interest
        #    and consumed by simulation_rules.apply_margin_heloc_interest: false
        #    = service the drawn-margin interest in CASH (via the existing
        #    heloc_interest_servicing rule); true = capitalize up to the charge,
        #    servicing the rest (the pre-#1036 behaviour). A retiree paying
        #    HELOC interest in cash is no longer modelled as capitalizing it.
        #    The internal-config default (when this key is absent, e.g. every
        #    test that builds the internal dict directly) is True -- byte-
        #    identical to the pre-#1036 capitalization path (DP#32: absence is
        #    the fallback, never a coercion of a supplied value).
        prop_cfg["capitalize_interest"] = heloc["capitalize_interest"]
        # 2. balance (the DRAWN amount) -- HONOURED as the opening position
        #    when > 0 (issue #1039): mapped to property.heloc_opening_balance
        #    (+ property.heloc_opening_investment_portion off the declared
        #    deductibility), read by SimulationConfig.from_dict, seeded into
        #    SimState.heloc_balance / canada.margin_tracing by SimState.initial
        #    -- wired all the way into the engine (DP#18). See the block at
        #    the top of this `if heloc:` for the refusals that keep absence
        #    loud (a draw with no deductibility; a draw above the limit).
        #    balance = 0 (undrawn) remains the documented accepted state
        #    (#577): margin_available then equals the full limit.
        # 3. deductibility -- REFUSED LOUDLY when declared WITHOUT an opening
        #    drawn balance and asserting a deductible portion (> 0). With an
        #    opening draw it IS honoured (the #1039 opening trace); with none,
        #    there is no opening interest to apply it to and future draws are
        #    traced from their borrowing's purpose -- silently dropping the
        #    declaration would be exactly the DP#32 defect, so it refuses
        #    (same stance as the consumer-loan path at the consumer-loan
        #    mapping, whose message instructs 'declare investment_portion=0').
        #    investment_portion=0 is accepted either way -- the safe, accepted
        #    state the user's real contracts declare.
        if (heloc_drawn <= 0 and heloc_deductibility is not None
                and heloc_deductibility["investment_portion"] > 0):
            raise ContractAdaptationError(
                f"liability {heloc['id']!r} (kind=heloc) declares deductibility."
                f"investment_portion={heloc_deductibility['investment_portion']:.4f} "
                f"> 0 while balance.amount is 0 (undrawn). A declared ratio is "
                f"honoured only as the trace of an OPENING drawn balance "
                f"(#1039); with nothing drawn there is no interest to apply it "
                f"to, and future draws are traced from their borrowing's "
                f"purpose -- silently dropping this declaration would be the "
                f"DP#32 defect. Declare investment_portion=0 (personal-use, "
                f"not deductible), declare the real balance.amount with this "
                f"deductibility to honour an opening position, or express a "
                f"draw the engine should make via decisions.borrow_to_invest "
                f"(#1036). Refusing loudly."
            )

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
