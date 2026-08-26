#!/usr/bin/env python3
"""DP#24's load/save round-trip for ``SimulationConfig``.

Split out of ``simulation_config.py``, where the two halves of the round trip
together ran to ~370 lines of field-by-field mapping and buried the dataclass
they belong to. ``SimulationConfig.from_dict`` / ``.to_dict`` are unchanged
public API; they are one line each and call straight into here.

``config_fields_from_dict`` returns the constructor KWARGS rather than a
``SimulationConfig``, so this module needs no import of the class it serializes
-- the dataclass stays the single owner of its own construction.

The two functions are exact inverses and must stay that way: every field
``config_fields_from_dict`` reads out of a config dict, ``config_to_dict``
writes back, with the same absence conventions (DP#32 -- ``None``/absent is
distinct from ``0``, and an empty list round-trips to *absent*, never to a
fabricated block).
"""

from typing import TYPE_CHECKING, Dict

from charge_limits import OSFI_B20_CHARGE_LTV_MAX, OSFI_B20_REVOLVING_LTV_MAX
from config_access import (
    _estate_block,
    _materialize_return_model_data,
    _reserve_cfg,
    _validate_internal_shape,
    has_readvanceable_facility,
)
from member_config import projection_span

if TYPE_CHECKING:
    from simulation_config import SimulationConfig


def config_fields_from_dict(cfg: Dict) -> Dict:
    """The ``SimulationConfig`` constructor kwargs described by ``cfg``.

    The read half of DP#24's round trip, and the body of
    ``SimulationConfig.from_dict`` -- see that method for what the internal
    dict shape is (and is not).
    """
    _validate_internal_shape(cfg)
    accounts = cfg.get('accounts', {})
    assumptions = cfg.get('assumptions', {})
    horizon_age = assumptions.get('horizon_age')

    return_model_data = _materialize_return_model_data(cfg)
    savings = cfg.get('savings', {})
    prop = cfg.get('property', {})
    family = cfg.get('family', {})

    # Issue #294: per-member retirement_age (default 65). Normalize here so
    # the field is explicit, round-trips via to_dict()'s family.members, and
    # is read uniformly downstream (simulation.py / retirement_transition).
    members = [dict(m) for m in family.get('members', [])]
    for m in members:
        m.setdefault('retirement_age', 65)
        # Issue #699: every member carries a stable entity id. The contract
        # loader (input_contract._map_member) already sets the schema
        # person_id; a config authored directly (tests, ScenarioOverlay)
        # falls back to its role label, which is a stable identity in the
        # two-adult world. member_by_id / the #643 rewrite key off this.
        m.setdefault('id', m.get('role'))

    # DP#8/DP#10 (#241): jurisdiction comes from config data (tax section),
    # not a hardcoded literal in core logic. Falls back to the historical
    # Quebec/Canada default when the config omits it.
    tax = cfg.get('tax', {}) if isinstance(cfg.get('tax', {}), dict) else {}

    start_year = assumptions.get('start_year', 2026)
    return dict(
        projection_years=projection_span(
            horizon_age=horizon_age,
            start_year=start_year,
            members=members,
            projection_years=assumptions.get('projection_years', 10),
        ),
        horizon_age=horizon_age,
        investment_return=assumptions.get('investment_return', 0.07),
        salary_growth=assumptions.get('salary_growth', 0.02),
        inflation=assumptions.get('inflation', 0.025),
        tfsa_growth=assumptions.get('tfsa_growth', 0.02),
        capital_gains_inclusion=assumptions.get('capital_gains_inclusion', 0.50),
        resp_eap_tax_rate=assumptions.get('resp_eap_tax_rate', 0.15),
        resp_eap_taxable_portion=assumptions.get('resp_eap_taxable_portion', 0.60),
        savings_rate=savings.get('rate', 0.0),  # DP#13: personal data, not a default
        house_value=prop.get('house_value', 0),
        # Issue #963 (epic #956 bite F): absence-safe -- .get(key) with no
        # default returns None on a genuinely absent key, never coerces it
        # (DP#32). The golden household's legacy `property` dict never
        # carries this key -> None -> the consumers return static
        # `house_value` and never read this field -> byte-identical.
        appreciation_rate=prop.get('appreciation_rate'),
        mortgage_balance=prop.get('mortgage_balance', 0),
        mortgage_rate=prop.get('mortgage_rate', 0.05),
        # Issue #1075: absence-safe -- .get(key, 0.0) with a real 0
        # default: a household with no deductible tranche keeps 0.0
        # (DP#32), and a declared deductible balance / interest reaches
        # the engine for the #850 pricing to consume. The keys are only
        # ever written by input_contract when > 0, so a legacy dict
        # never carries them.
        deductible_mortgage_balance=prop.get('deductible_mortgage_balance', 0.0),
        deductible_mortgage_interest=prop.get('deductible_mortgage_interest', 0.0),
        ltv_max=prop.get('ltv_max', 0.80),
        amortization_years=prop.get('amortization_years', 13),
        margin_available=prop.get('margin_available', 0),
        # issue #1039: absence-safe -- input_contract writes these keys
        # only when an opening drawn balance is declared, so a legacy
        # dict never carries them and 0.0 is the documented undrawn state
        # (#577), never a coerced zero (DP#32).
        heloc_opening_balance=prop.get('heloc_opening_balance', 0.0),
        heloc_opening_investment_portion=prop.get(
            'heloc_opening_investment_portion', 0.0),
        has_heloc=has_readvanceable_facility(cfg),
        # issue #654: absence-safe -- .get(key) with no default returns
        # None on a genuinely absent key, never coerces it (DP#32).
        heloc_rate=prop.get('heloc_rate'),
        heloc_rate_type=prop.get('heloc_rate_type'),
        cash_out=prop.get('cash_out', 0.0),
        # issue #689: absence-safe, same convention as heloc_rate above.
        credit_facility_limit=prop.get('credit_facility_limit', 0.0),
        credit_facility_rate=prop.get('credit_facility_rate'),
        credit_facility_rate_type=prop.get('credit_facility_rate_type'),
        credit_facility_secured=prop.get('credit_facility_secured', False),
        charge_ltv_limit=prop.get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX),
        heloc_ltv_limit=prop.get('heloc_ltv_limit', OSFI_B20_REVOLVING_LTV_MAX),
        refinance_amortization_years=prop.get('refinance_amortization_years'),
        refinance_advance_deductible_non_reg=prop.get('refinance_advance_deductible_non_reg'),
        # Issue #137: absence-safe -- .get with no default returns 0 (the
        # documented no-lag state) on a genuinely absent key, never coerces a
        # declared value (DP#32). A household that declares no deployment_lag
        # round-trips byte-identical (0 = no lag = today's behaviour).
        deployment_lag_months=prop.get('deployment_lag_months', 0),
        deployment_lag_parking_rate=prop.get('deployment_lag_parking_rate', 0.0),
        # Issue #139: absence-safe -- .get with no default returns 0.0 on a
        # genuinely absent key, never coerces a declared value (DP#32). A
        # household that declares no transaction_costs round-trips byte-
        # identical (0.0 = no net year-0 cost = today's behaviour).
        transaction_cost_year0=prop.get('transaction_cost_year0', 0.0),
        heloc_readvance=prop.get('heloc_readvance', False),
        # Issue #956 bite E: the principal residence's declared sale
        # (absence-safe -- .get with no default returns None on a
        # genuinely absent key, never coerces it; the golden household's
        # legacy dict never carries this key -> None -> no-op, DP#32).
        principal_sale=prop.get('principal_sale'),
        family_members=members,
        children=family.get('children', []),
        private_loans=family.get('private_loans', []),  # issue #813
        gifts=family.get('gifts', []),  # epic #841 bite 3
        first_home_purchases=family.get('first_home_purchases', []),  # issue #704
        zev_purchases=cfg.get('zev_purchases', []),
        rrsp_annual_percent=accounts.get('rrsp_annual_percent', 0.18),
        rrsp_annual_max=accounts.get('rrsp_annual_max', 0),  # DP#13: set from TaxDataProvider
        tfsa_annual_room_per_person=accounts.get('tfsa_annual_room_per_person', 7000),
        resp_current_balance=accounts.get('resp_current_balance', 0),
        resp_study_start_age=accounts.get('resp_study_start_age', 18),
        resp_study_duration_years=accounts.get('resp_study_duration_years', 4),
        resp_used_for_education=accounts.get('resp_used_for_education', True),
        resp_composition=accounts.get('resp_composition', {}),
        deduct_later_bracket_target=assumptions.get('deduct_later_bracket_target', accounts.get('deduct_later_bracket_target', 0)),  # DP#13: set from tax brackets
        country=tax.get('country', cfg.get('country', 'canada')),
        province=tax.get('province', 'quebec'),
        start_year=assumptions.get('start_year', 2026),
        frozen_brackets=assumptions.get('frozen_brackets', False),
        time_step=assumptions.get('time_step', 'yearly'),
        cash_flows=cfg.get('cash_flows', []),
        portfolio_data=cfg.get('portfolio', {}),
        retirement_data=cfg.get('retirement', {}),
        estate_data=_estate_block(cfg),
        non_reg_yield_rate=assumptions.get('non_reg_yield_rate', 0.02),
        return_model_data=return_model_data,
        # issue #293: top-level CRI/LIRA block (separate from RRSP balance).
        lira_data=cfg.get('lira', {}) or {},
        # issue #679: absence-safe -- .get(key) with no default returns
        # None on a genuinely absent key, never coerces it (DP#32).
        living_costs=cfg.get('household_budget', {}).get('living_costs'),
        # Issue #761: absence-safe -- .get with no default returns None on
        # a genuinely absent key, never coerces it (DP#32). 0.0 is a real
        # declarable "all rigid" value that travels through unchanged.
        discretionary_fraction=cfg.get('household_budget', {}).get('discretionary_fraction'),
        # Issue #760: dated, finite-term living-cost segments. Absence-safe
        # -- .get with no default returns [] (a household with no dated
        # segments), never a fabricated entry; the list is only present
        # when the contract declared some (DP#24/DP#32).
        expense_segments=list(cfg.get('household_budget', {}).get('expense_segments', [])),
        # issue #688: the reserve POLICY. Every one of these is None when
        # the household declared no assumptions.emergency_reserve block --
        # which means a $0 reserve, STATED as an absence, not defaulted
        # into existence (DP#32). `target_months: 0` inside a declared
        # block is a different thing: a deliberate "I hold no reserve."
        emergency_reserve_target_months=_reserve_cfg(cfg).get('target_months'),
        emergency_reserve_rate=_reserve_cfg(cfg).get('rate'),
        emergency_reserve_held_in=_reserve_cfg(cfg).get('held_in'),
        emergency_reserve_instrument=_reserve_cfg(cfg).get('instrument'),
        # issue #763: closed-end consumer loans (car_loan/student_loan/
        # personal_loan). Absence-safe -- .get with no default returns []
        # (a household with no consumer debt), never a fabricated entry,
        # and the list is only present when the contract declared some.
        consumer_loans=list(cfg.get('consumer_loans', [])),
        # issue #692: the couple's non-principal properties. Absence-safe --
        # .get with no default returns [] (a household with only a principal
        # residence, or none), never a fabricated entry; the list is only
        # present when the contract declared a couple-owned non-principal
        # property (DP#24/DP#32).
        properties=list(cfg.get('properties', [])),
        # issue #759: fixed-term installment obligations. Absence-safe --
        # .get with no default returns [] (a household with no payment
        # plan), never a fabricated entry; the list is only present when
        # the contract declared some (DP#24/DP#32).
        installments=list(cfg.get('installments', [])),
        # Issue #768: record-only equity grants. .get with no default
        # returns [] (a household with no such grants), never a fabricated
        # entry; the list is only present when the contract declared some
        # (DP#24/DP#32). No rule reads it -- $0 for solvency by
        # construction.
        equity_grants=list(cfg.get('equity_grants', [])),
        # Issue #936: the DECLARED deposit products (optimizer-swept,
        # read by scenario_discovery/simulate) and the SINGLE taken product
        # apply_overlay wrote onto this scenario's config (engine-facing).
        # Absence-safe -- .get with no default returns []/None (a household
        # with no such product, the golden path), never a fabricated entry
        # (DP#24/DP#32).
        deposit_products=list(cfg.get('deposit_products', [])),
        deposit_product=cfg.get('deposit_product'),
        # Issue #1036: capitalize_interest defaults True when absent
        # (property.capitalize_interest key absent) so every internal-
        # config test built directly stays byte-identical to the pre-#1036
        # capitalization path (DP#32: absence is the fallback, never a
        # coercion of a supplied value). The raw cfg['borrow_to_invest_
        # options'] key is read directly by optimize.run_borrow_to_invest_
        # exploration (the optimizer, not the simulator, DP#22); it is NOT
        # lifted onto a SimulationConfig field (a dead surface -- D7).
        capitalize_interest=prop.get('capitalize_interest', True),
        # Issue #1040: hold_draw defaults False when absent (the pre-#1040
        # RRSP-refund paydown sweep) so every internal-config test built
        # directly stays byte-identical (DP#32: absence is the fallback,
        # never a coercion of a supplied value).
        hold_borrow_to_invest_draw=prop.get('borrow_to_invest_hold_draw', False),
        account_return_overrides=accounts.get('return_overrides', {}) if isinstance(accounts, dict) else {},
        account_locked=accounts.get('locked', {}) if isinstance(accounts, dict) else {},
        account_mer_drag=accounts.get('mer_drag', {}) if isinstance(accounts, dict) else {},
    )


def config_to_dict(config: 'SimulationConfig') -> Dict:
    """Export ``config`` as a dict matching the input.json schema.

    The write half of DP#24's round trip, and the body of
    ``SimulationConfig.to_dict``.
    """
    return {
        'assumptions': {
            # DP#24: re-emit the horizon rule (when set) alongside the span it
            # derived, so a saved config reloads to the same projection.
            **({'horizon_age': config.horizon_age} if config.horizon_age else {}),
            'projection_years': config.projection_years,
            'investment_return': config.investment_return,
            'salary_growth': config.salary_growth,
            'inflation': config.inflation,
            'tfsa_growth': config.tfsa_growth,
            'capital_gains_inclusion': config.capital_gains_inclusion,
            'resp_eap_taxable_portion': config.resp_eap_taxable_portion,
            'resp_eap_tax_rate': config.resp_eap_tax_rate,
            'start_year': config.start_year,
            'non_reg_yield_rate': config.non_reg_yield_rate,
            'frozen_brackets': config.frozen_brackets,
            'time_step': config.time_step,
            # Issue #688: only re-emitted when the household actually
            # declared a reserve policy. None round-trips to "absent"
            # (no reserve, stated), never to a fabricated block (DP#32) --
            # and re-emitting it is what makes the target sweepable
            # through apply_overlay, which reads it back off here.
            **({'emergency_reserve': {
                'target_months': config.emergency_reserve_target_months,
                'rate': config.emergency_reserve_rate,
                'held_in': config.emergency_reserve_held_in,
                'instrument': config.emergency_reserve_instrument,
            }} if config.emergency_reserve_target_months is not None else {}),
        },
        'cash_flows': config.cash_flows,
        'savings': {
            'rate': config.savings_rate,
        },
        'property': {
            'house_value': config.house_value,
            # Issue #963 (epic #956 bite F): only re-emit when declared --
            # None means "no appreciation, static value" (the golden path),
            # not a value to round-trip as an explicit null that a naive
            # from_dict() re-read would treat as "declared but empty" (DP#32).
            **({'appreciation_rate': config.appreciation_rate}
               if config.appreciation_rate is not None else {}),
            'mortgage_balance': config.mortgage_balance,
            'mortgage_rate': config.mortgage_rate,
            # Issue #1075 (DP#24/DP#32): only re-emit when declared --
            # 0.0 round-trips to 'absent' (no deductible tranche), the
            # same absence-safe convention property.cash_out uses, so a
            # load->modify->save cycle never fabricates a deductible
            # balance for a household that has none.
            **({'deductible_mortgage_balance': config.deductible_mortgage_balance}
               if config.deductible_mortgage_balance else {}),
            # Issue #1075 (DP#24/DP#32): same absence-safe convention as
            # deductible_mortgage_balance -- the exact deductible
            # interest (sum of each flagged tranche's balance * its own
            # rate) round-trips only when a deductible tranche exists.
            **({'deductible_mortgage_interest': config.deductible_mortgage_interest}
               if config.deductible_mortgage_interest else {}),
            'ltv_max': config.ltv_max,
            'amortization_years': config.amortization_years,
            'margin_available': config.margin_available,
            # Issue #730 (DP#24/DP#18): re-emit a booked refinance
            # cash_out so a load->modify->save cycle does not silently
            # drop the invested-capital source it recorded. Emitted only
            # when non-zero -- 0.0 round-trips to 'absent' (no refinance
            # booked), the same absence-safe convention ScenarioOverlay
            # .to_dict() uses for its own cash_out (#257). Before this,
            # from_dict() ingested property.cash_out but to_dict() never
            # re-emitted it, so any saved config lost the cash-out leg of
            # its refinance on the next load.
            **({'cash_out': config.cash_out} if config.cash_out else {}),
            # Issue #1039 (DP#24): re-emit a declared opening drawn HELOC
            # position so a load->modify->save cycle does not silently
            # drop it. Emitted only when non-zero -- 0.0 round-trips to
            # 'absent' (undrawn, #577), the same absence-safe convention
            # cash_out uses above.
            **({'heloc_opening_balance': config.heloc_opening_balance}
               if config.heloc_opening_balance else {}),
            **({'heloc_opening_investment_portion':
                config.heloc_opening_investment_portion}
               if config.heloc_opening_investment_portion else {}),
            'heloc_readvance': config.heloc_readvance,
            'charge_ltv_limit': config.charge_ltv_limit,
            'heloc_ltv_limit': config.heloc_ltv_limit,
            # DP#24: only re-emit when declared, same pattern as
            # horizon_age above -- None must round-trip to "absent", not
            # a literal null a naive from_dict() re-read would treat as
            # "declared but empty" (DP#32).
            **({'refinance_amortization_years': config.refinance_amortization_years}
               if config.refinance_amortization_years is not None else {}),
            # Issue #792 (DP#24): only re-emit when declared -- None means
            # "no declared split" (today's internal optimization), not a
            # value to round-trip as an explicit null (DP#32). A declared
            # 0 IS re-emitted (it is a real choice, distinct from absence).
            **({'refinance_advance_deductible_non_reg':
                    config.refinance_advance_deductible_non_reg}
               if config.refinance_advance_deductible_non_reg is not None else {}),
            # Issue #137 (DP#24): only re-emit the deployment lag when declared
            # -- 0 round-trips to 'absent' (no lag, byte-identical to the
            # pre-feature path), the same absence-safe convention
            # refinance_amortization_years (None -> absent) uses above. A
            # declared lag > 0 must survive a load->modify->save cycle.
            **({'deployment_lag_months': config.deployment_lag_months}
               if config.deployment_lag_months else {}),
            # Issue #137 (DP#24): only re-emit a non-default parking_rate -- 0.0
            # round-trips to 'absent' (idle cash earning nothing, the ordinary
            # case), so a no-parking-rate household round-trips byte-identical.
            **({'deployment_lag_parking_rate': config.deployment_lag_parking_rate}
               if config.deployment_lag_parking_rate else {}),
            # Issue #139 (DP#24): only re-emit the net year-0 transaction
            # cost when non-zero -- 0.0 round-trips to 'absent' (no net year-0
            # refinance origination cost, the ordinary case), so a household
            # that declares no transaction_costs round-trips byte-identical.
            **({'transaction_cost_year0': config.transaction_cost_year0}
               if config.transaction_cost_year0 else {}),
            # issue #654: only re-emitted when actually declared --
            # None means "never declared" (DP#32), not a value to
            # round-trip as an explicit null.
            **({'heloc_rate': config.heloc_rate} if config.heloc_rate is not None else {}),
            **({'heloc_rate_type': config.heloc_rate_type} if config.heloc_rate_type is not None else {}),
            # Issue #1036 (DP#24): only re-emit capitalize_interest when
            # it is NOT the default (True) -- True round-trips to 'absent'
            # (the pre-#1036 capitalization path, byte-identical), False is
            # a real declared 'service in cash' that must survive a
            # load->modify->save cycle (DP#32).
            **({'capitalize_interest': config.capitalize_interest}
               if config.capitalize_interest is not True else {}),
            # Issue #1040 (DP#24): only re-emit when declared True --
            # False round-trips to 'absent' (the pre-#1040 paydown sweep,
            # byte-identical), True is a real declared 'hold the draw
            # flat' that must survive a load->modify->save cycle (DP#32).
            **({'borrow_to_invest_hold_draw': config.hold_borrow_to_invest_draw}
               if config.hold_borrow_to_invest_draw else {}),
            # issue #689: only re-emitted when actually declared -- None
            # means "never declared" (DP#32), same convention as
            # heloc_rate above.
            **({'credit_facility_limit': config.credit_facility_limit,
                'credit_facility_secured': config.credit_facility_secured}
               if config.credit_facility_limit > 0 else {}),
            **({'credit_facility_rate': config.credit_facility_rate}
               if config.credit_facility_rate is not None else {}),
            **({'credit_facility_rate_type': config.credit_facility_rate_type}
               if config.credit_facility_rate_type is not None else {}),
            # Issue #956 bite E (DP#24): only re-emit when declared --
            # None means "no principal sale" (the hold case, the golden
            # path), not a value to round-trip as an explicit null that a
            # naive from_dict() re-read would treat as "declared but
            # empty" (DP#32).
            **({'principal_sale': config.principal_sale}
               if config.principal_sale is not None else {}),
        },
        'family': {
            'members': config.family_members,
            'children': config.children,
            'private_loans': config.private_loans,
            'gifts': config.gifts,
            'first_home_purchases': config.first_home_purchases,
        },
        'zev_purchases': config.zev_purchases,
        'accounts': {
            'rrsp_annual_percent': config.rrsp_annual_percent,
            'rrsp_annual_max': config.rrsp_annual_max,
            'tfsa_annual_room_per_person': config.tfsa_annual_room_per_person,
            'resp_current_balance': config.resp_current_balance,
            'resp_study_start_age': config.resp_study_start_age,
            'resp_study_duration_years': config.resp_study_duration_years,
            'resp_used_for_education': config.resp_used_for_education,
            'resp_composition': config.resp_composition,
            'deduct_later_bracket_target': config.deduct_later_bracket_target,
            # Issue #823 (DP#24): round-trip the per-account override /
            # illiquidity maps so a load->modify->save cycle does not
            # silently drop them. Empty dict round-trips to 'absent'
            # (no override declared), the same absence-safe convention
            # used for lira / equity_grants above.
            **({'return_overrides': config.account_return_overrides}
               if config.account_return_overrides else {}),
            **({'locked': config.account_locked}
               if config.account_locked else {}),
            **({'mer_drag': config.account_mer_drag}
               if config.account_mer_drag else {}),
        },
        'tax': {
            'country': config.country,
            'province': config.province,
        },
        'portfolio': config.portfolio_data,
        'retirement': config.retirement_data,
        'estate': config.estate_data,
        'return_model': config.return_model_data,
        # Issue #729 (DP#24): re-emit the top-level CRI/LIRA block so a
        # load->modify->save cycle does not silently drop locked-in
        # pension balances. Emitted only when non-empty -- an empty dict
        # round-trips to 'absent' (no LIRA declared), the same
        # absence-safe convention used for consumer_loans/installments/
        # equity_grants above. Before this, from_dict() ingested
        # cfg['lira'] but to_dict() never re-emitted it, so any saved
        # config lost its locked-in accounts on the next load (#293).
        **({'lira': config.lira_data} if config.lira_data else {}),
        # Issue #679: only re-emitted when actually declared -- None
        # means "never supplied" (DP#32), not a value to round-trip as
        # a fabricated 0. Issue #761: the discretionary_fraction travels
        # alongside living_costs when the household declared a split.
        **({'household_budget': {
                'living_costs': config.living_costs,
                **({'discretionary_fraction': config.discretionary_fraction}
                   if config.discretionary_fraction is not None else {}),
                # Issue #760 (DP#24): the dated segments travel alongside
                # living_costs when declared -- an empty list round-trips to
                # "absent" (no dated segments), never a fabricated block.
                **({'expense_segments': config.expense_segments}
                   if config.expense_segments else {})}}
           if config.living_costs is not None else {}),
        # Issue #763: only re-emitted when the household actually declared
        # consumer loans -- an empty list round-trips to "absent" (no
        # consumer debt), never to a fabricated block (DP#24/DP#32).
        **({'consumer_loans': config.consumer_loans}
           if config.consumer_loans else {}),
        # Issue #759: only re-emitted when the household actually declared
        # installment plans -- an empty list round-trips to "absent" (no
        # payment plan), never to a fabricated block (DP#24/DP#32).
        **({'installments': config.installments}
           if config.installments else {}),
        # Issue #768: only re-emitted when the household actually declared
        # equity grants -- an empty list round-trips to 'absent' (no
        # grants), never to a fabricated block (DP#24/DP#32).
        **({'equity_grants': config.equity_grants}
           if config.equity_grants else {}),
        # Issue #692: only re-emitted when the household actually declared a
        # couple-owned non-principal property -- an empty list round-trips
        # to 'absent' (no such property), never a fabricated block
        # (DP#24/DP#32).
        **({'properties': config.properties}
           if config.properties else {}),
        # Issue #936: the declared deposit products and the single taken
        # product are only re-emitted when actually present -- an empty list
        # / None round-trips to 'absent' (no product), never a fabricated block
        # (DP#24/DP#32).
        **({'deposit_products': config.deposit_products}
           if config.deposit_products else {}),
        **({'deposit_product': config.deposit_product}
           if config.deposit_product is not None else {}),
    }
