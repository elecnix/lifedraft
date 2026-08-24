#!/usr/bin/env python3
"""``ScenarioOverlay`` and the functions that apply one (DP#5/DP#18/DP#24).

Split out of ``simulation_config.py``. One concern: a scenario is a BASE config
plus a declared delta, and this module is the delta's type plus every way of
landing it on a base --

  - ``ScenarioOverlay``: the delta itself, and its own round trip
    (``to_dict``/``from_dict``/``from_overlay_diff``/``extract``);
  - ``apply_overlay`` / ``build_overlay_config``: the dict-level application
    (``simulate.py``'s path);
  - ``apply_ltv_overlay``: the ``SimulationConfig``-object application (the
    optimizers' path);
  - ``refinance_amortization_fallback`` and its placeholder constant: the ONE
    exploratory-script fallback for an amortization the household never
    declared.

DP#18 is the invariant the whole module exists to keep: an overlay must modify a
key the ENGINE reads, or it evaporates silently.
"""

import logging
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Dict, Optional

from charge_limits import (
    _CHARGE_TOLERANCE,
    ChargeLimitExceededError,
    MissingRefinanceAmortizationError,
    OSFI_B20_CHARGE_LTV_MAX,
    charge_limit,
)
from config_access import _dict_to_json, resolve_return_rate, set_return_rate

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # annotation-only: apply_ltv_overlay and from_overlay_diff
    # take a SimulationConfig but never import one -- they read attributes off
    # it, so there is no runtime dependency and no import cycle.
    from simulation_config import SimulationConfig

@dataclass
class ScenarioOverlay:
    """DP#18: A scenario is a base config plus a set of overlay deltas.

    Per DP#18, overlays modify a base configuration - they don't replace it.
    Fields default to None (meaning "no change from base") rather than 0.0
    (which would silently zero out base income).

    DP#24: Supports round-trip serialization via from_dict()/to_dict()/to_json().
    """""
    label: str
    cash_out: float = 0.0
    resp_cash_out: float = 0.0
    primary_income: Optional[float] = None
    spouse_income: Optional[float] = None
    mortgage_rate: float = 0.05  # DP#13: round-number placeholder
    use_readvanceable: bool = False
    deduct_later: bool = False
    investment_return: Optional[float] = None
    ltv: Optional[float] = None
    # DP#14/DP#5: Sensitivity overlay fields
    inflation: Optional[float] = None      # Override inflation rate (affects real return)
    salary_growth: Optional[float] = None   # Override salary growth rate
    # Issue #303: retirement-age dimension. None means "no change from base".
    retirement_age: Optional[int] = None
    # Issue #655: the amortization a cash_out > 0 refinance is repaid over.
    # None means "use the base config's property.refinance_amortization_years";
    # if that is also absent, apply_overlay refuses (DP#32) rather than
    # silently inheriting property.amortization_years.
    refinance_amortization_years: Optional[int] = None
    # Issue #688: the emergency-reserve target, in months of essential
    # outflows. THE sweep dimension the reserve question needs (0/3/6/12/24):
    # sweeping it shows the household the real trade -- expected terminal
    # wealth against the probability and cost of a forced sale (#679's
    # waterfall). None means "no change from base" (DP#18), which is NOT the
    # same as 0 ("hold no reserve") -- a swept 0 is a real, declared decision.
    emergency_reserve_months: Optional[float] = None
    # Issue #735: what fraction of margin_available is drawn and invested at
    # year 0 (0.0-1.0). 0.0 (the default) is the correct value for a scenario
    # that never asks about this dimension, NOT `None` -- a declared facility
    # simply is not drawn unless something draws it (DP#32), so there is no
    # "no change from base" ambiguity to preserve the way there is for
    # emergency_reserve_months (0 reserve-months is a real declared choice
    # distinct from "never asked"; 0 draw-fraction is the ONLY value that
    # makes sense as an unstated default). Sweep it with several
    # ScenarioOverlay instances (e.g. [0.0, 0.25, 0.5, 1.0]) to see the
    # trade-off the Smith-Manoeuvre question actually is.
    draw_fraction: float = 0.0
    # Issues #711/#712: the retirement income-splitting elections the optimizer
    # sweeps (DP#22/#30). ``cpp_share`` is the fraction (0..1) of the way to a
    # fully equalized CPP/QPP split between two retired spouses; ``pension_split_pct``
    # is the fraction (0..0.5) of the higher-bracket spouse's eligible pension
    # allocated to the lower-bracket spouse on a T1032 election. None means "no
    # change from base" (DP#18) — distinct from an explicit 0 ("elect not to
    # split"), both real declarable values; the engine default when neither the
    # overlay nor the base retirement block sets one is no split, preserving
    # current behaviour (DP#13/#32).
    cpp_share: Optional[float] = None
    pension_split_pct: Optional[float] = None
    # Issue #936: the deposit product this scenario TAKES (the single declared
    # product dict), or None for the implicit "leave it" baseline. When set,
    # apply_overlay carves the product's fund_amount out of its funding_source
    # (money-conserving) and lands it on cfg['deposit_product'] -- the key
    # SimulationConfig.from_dict reads and the engine acts on (DP#18). None
    # means "this scenario takes no product" -- distinct from a $0 product, and
    # the value every non-deposit sweep dimension carries, so the golden
    # invariant is untouched (DP#32). Each declared product + this None baseline
    # is a ranked take/leave candidate the optimizer scores head-to-head (#936
    # capability #4).
    deposit_product: Optional[Dict] = None

    def __init__(self, label: str, cash_out: float = 0.0, resp_cash_out: float = 0.0,
                 primary_income: Optional[float] = None, spouse_income: Optional[float] = None,
                 mortgage_rate: float = 0.05,
                 use_readvanceable: bool = False, deduct_later: bool = False,
                 investment_return: Optional[float] = None,
                 ltv: Optional[float] = None,
                 inflation: Optional[float] = None,
                 salary_growth: Optional[float] = None,
                 retirement_age: Optional[int] = None,
                 refinance_amortization_years: Optional[int] = None,
                 emergency_reserve_months: Optional[float] = None,
                 draw_fraction: float = 0.0,
                 cpp_share: Optional[float] = None,
                 pension_split_pct: Optional[float] = None,
                 deposit_product: Optional[Dict] = None):
        """Create a ScenarioOverlay."""
        self.label = label
        self.cash_out = cash_out
        self.resp_cash_out = resp_cash_out
        self.primary_income = primary_income
        self.spouse_income = spouse_income
        self.mortgage_rate = mortgage_rate
        self.use_readvanceable = use_readvanceable
        self.deduct_later = deduct_later
        self.investment_return = investment_return
        self.ltv = ltv
        self.inflation = inflation
        self.salary_growth = salary_growth
        self.retirement_age = retirement_age
        self.refinance_amortization_years = refinance_amortization_years
        self.emergency_reserve_months = emergency_reserve_months
        self.draw_fraction = draw_fraction
        self.cpp_share = cpp_share
        self.pension_split_pct = pension_split_pct
        self.deposit_product = deposit_product



    def to_dict(self) -> Dict:
        """DP#24: Serialize overlay to a plain dict.

        Only includes non-default values for optional fields to keep
        the output compact. The label is always included.

        Returns:
            Dict suitable for JSON serialization or from_dict() round-trip.
        """
        result = {'label': self.label}

        # Always include numeric fields (they have meaningful defaults)
        if self.cash_out != 0.0:
            result['cash_out'] = self.cash_out
        if self.resp_cash_out != 0.0:
            result['resp_cash_out'] = self.resp_cash_out
        if self.primary_income is not None:
            result['primary_income'] = self.primary_income
        if self.spouse_income is not None:
            result['spouse_income'] = self.spouse_income

        # mortgage_rate is always included (has a meaningful default)
        result['mortgage_rate'] = self.mortgage_rate

        # Boolean flags
        if self.use_readvanceable:
            result['use_readvanceable'] = True
        if self.deduct_later:
            result['deduct_later'] = True

        # Optional fields
        if self.investment_return is not None:
            result['investment_return'] = self.investment_return
        if self.ltv is not None:
            result['ltv'] = self.ltv

        # DP#14/DP#5: Sensitivity overlay fields
        if self.inflation is not None:
            result['inflation'] = self.inflation
        if self.salary_growth is not None:
            result['salary_growth'] = self.salary_growth

        # Issue #303: retirement-age dimension
        if self.retirement_age is not None:
            result['retirement_age'] = self.retirement_age

        # Issue #655: refinance amortization
        if self.refinance_amortization_years is not None:
            result['refinance_amortization_years'] = self.refinance_amortization_years

        # Issue #688: emergency-reserve target sweep dimension
        if self.emergency_reserve_months is not None:
            result['emergency_reserve_months'] = self.emergency_reserve_months

        # Issue #735: draw-fraction sweep dimension
        if self.draw_fraction != 0.0:
            result['draw_fraction'] = self.draw_fraction

        # Issues #711/#712: retirement income-splitting sweep dimensions
        if self.cpp_share is not None:
            result['cpp_share'] = self.cpp_share
        if self.pension_split_pct is not None:
            result['pension_split_pct'] = self.pension_split_pct

        # Issue #936: the taken deposit product (None = "leave it" baseline)
        if self.deposit_product is not None:
            result['deposit_product'] = self.deposit_product

        return result

    @classmethod
    def from_dict(cls, data: Dict) -> 'ScenarioOverlay':
        """DP#24: Deserialize overlay from a plain dict.

        Round-trips with to_dict():
            overlay = ScenarioOverlay(label="test", cash_out=100000)
            assert overlay == ScenarioOverlay.from_dict(overlay.to_dict())

        Args:
            data: Dict with overlay fields (as produced by to_dict()).

        Returns:
            New ScenarioOverlay instance.
        """
        return cls(
            label=data['label'],
            cash_out=data.get('cash_out', 0.0),
            resp_cash_out=data.get('resp_cash_out', 0.0),
            primary_income=data.get("primary_income"),
            spouse_income=data.get("spouse_income"),
            mortgage_rate=data.get('mortgage_rate', 0.05),
            use_readvanceable=data.get('use_readvanceable', False),
            deduct_later=data.get('deduct_later', False),
            investment_return=data.get('investment_return'),
            ltv=data.get('ltv'),
            inflation=data.get('inflation'),
            salary_growth=data.get('salary_growth'),
            retirement_age=data.get('retirement_age'),
            refinance_amortization_years=data.get('refinance_amortization_years'),
            emergency_reserve_months=data.get('emergency_reserve_months'),
            draw_fraction=data.get('draw_fraction', 0.0),
            cpp_share=data.get('cpp_share'),
            pension_split_pct=data.get('pension_split_pct'),
            deposit_product=data.get('deposit_product'),
        )

    def to_json(self, path: str = None, indent: int = 2) -> str:
        """DP#24: Serialize overlay to a JSON string."""
        return _dict_to_json(self.to_dict(), path=path, indent=indent)

    @classmethod
    def from_overlay_diff(cls, diff: Dict, base: 'SimulationConfig') -> 'ScenarioOverlay':
        """DP#24: Create a ScenarioOverlay from an overlay_diff result.

        Maps dot-path keys in the diff to ScenarioOverlay fields.
        Not all diff entries map to overlay fields; unmapped entries are
        derived effects (e.g. margin_available changes from cash_out).
        """
        overlays = diff.get('overlays', {})
        kwargs = {'label': 'from-diff'}

        # Direct mappings: diff keys → overlay field names
        _key_map = {
            'property.mortgage_rate': 'mortgage_rate',
            'assumptions.investment_return': 'investment_return',
            'assumptions.inflation': 'inflation',
            'assumptions.salary_growth': 'salary_growth',
            'property.ltv_max': 'ltv',
        }
        for diff_key, field_name in _key_map.items():
            if diff_key in overlays:
                kwargs[field_name] = overlays[diff_key]['to']

        # income: extract from family.members diffs
        for key, change in overlays.items():
            if key.startswith('family.members') and 'gross_income' in key:
                val = change['to']
                if '.0.' in key or key.endswith('.0.gross_income'):
                    kwargs['primary_income'] = val
                elif '.1.' in key or key.endswith('.1.gross_income'):
                    kwargs['spouse_income'] = val

        # cash_out: derived from mortgage_balance change
        if 'property.mortgage_balance' in overlays:
            kwargs['cash_out'] = overlays['property.mortgage_balance']['to'] - base.mortgage_balance

        # resp_cash_out: derived from resp_current_balance going to 0
        if 'accounts.resp_current_balance' in overlays:
            new_resp = overlays['accounts.resp_current_balance']['to']
            if new_resp == 0 and base.resp_current_balance > 0:
                kwargs['resp_cash_out'] = base.resp_current_balance

        return cls(**kwargs)

    @classmethod
    def extract(cls, base_cfg: dict, derived_cfg: dict) -> 'ScenarioOverlay':
        """DP#24: Extract a ScenarioOverlay from a base and derived config dict pair.

        Compares the two dicts to determine what overlay was applied.
        """
        kwargs = {'label': 'extracted'}

        base_prop = base_cfg.get('property', {})
        deriv_prop = derived_cfg.get('property', {})
        base_acct = base_cfg.get('accounts', {})
        deriv_acct = derived_cfg.get('accounts', {})
        base_fam = base_cfg.get('family', {})
        deriv_fam = derived_cfg.get('family', {})
        base_asmp = base_cfg.get('assumptions', {})
        deriv_asmp = derived_cfg.get('assumptions', {})

        # Income
        base_members = base_fam.get('members', [])
        deriv_members = deriv_fam.get('members', [])
        for m in deriv_members:
            if m.get('role') == 'primary':
                base_income = next(
                    (bm['gross_income'] for bm in base_members if bm.get('role') == 'primary'),
                    m['gross_income']
                )
                if m['gross_income'] != base_income:
                    kwargs['primary_income'] = m['gross_income']
            elif m.get('role') == 'spouse':
                base_income = next(
                    (bm['gross_income'] for bm in base_members if bm.get('role') == 'spouse'),
                    m['gross_income']
                )
                if m['gross_income'] != base_income:
                    kwargs['spouse_income'] = m['gross_income']

        # Mortgage rate
        if deriv_prop.get('mortgage_rate') != base_prop.get('mortgage_rate'):
            kwargs['mortgage_rate'] = deriv_prop['mortgage_rate']

        # Cash out
        cash_out = deriv_prop.get('mortgage_balance', 0) - base_prop.get('mortgage_balance', 0)
        if cash_out != 0:
            kwargs['cash_out'] = cash_out

        # LTV
        if deriv_prop.get('ltv_max') != base_prop.get('ltv_max'):
            kwargs['ltv'] = deriv_prop['ltv_max']

        # RESP cash out
        if deriv_acct.get('resp_current_balance', -1) == 0 and base_acct.get('resp_current_balance', 0) > 0:
            kwargs['resp_cash_out'] = base_acct['resp_current_balance']

        # Investment return — DP#21/#591 (#990): the engine reads return_model,
        # not the deprecated assumptions.investment_return scalar. apply_overlay
        # writes the swept rate via set_return_rate into return_model, so the
        # scalar is stale/absent on the derived config and the old comparison
        # here read a dead key — a return-rate overlay round-tripped through
        # extract as "no change" (DP#24 broken). Resolve the representative
        # year-0 rate from the source of truth on both sides; a fixed overlay
        # always lands a fixed return_model (set_return_rate), so the derived
        # resolved rate is the swept value and round-trips back into the overlay.
        base_rate = resolve_return_rate(base_cfg)
        deriv_rate = resolve_return_rate(derived_cfg)
        if deriv_rate != base_rate:
            kwargs['investment_return'] = deriv_rate

        # DP#14/DP#5: Sensitivity overlay fields
        if deriv_asmp.get('inflation') != base_asmp.get('inflation'):
            kwargs['inflation'] = deriv_asmp.get('inflation')
        if deriv_asmp.get('salary_growth') != base_asmp.get('salary_growth'):
            kwargs['salary_growth'] = deriv_asmp.get('salary_growth')

        return cls(**kwargs)


# DP#13/DP#9: the round-number placeholder amortization used by the
# exploratory scripts (optimize.py's LTV/rate-anchor sweeps, output_plugins.py
# cash-flow reports) that sweep hypothetical LTV levels the household has not
# committed to -- as opposed to a *declared* refinance option, whose
# amortization is real data and always wins (DP#13's own distinction: a
# search/exploration parameter may default to a round, clearly-placeholder
# value; a declared fact may not). 25 years matches
# ``SimulationConfig.amortization_years``'s own DP#13 default for the
# incumbent mortgage. ``apply_ltv_overlay``/``apply_overlay`` themselves take
# NO such fallback (#655/DP#32): they require an explicit or config-declared
# amortization and refuse otherwise. This placeholder exists for ONE layer
# above them, so exploratory scripts keep working while still re-amortizing a
# refinance over a realistic new-loan term instead of the incumbent's
# remaining schedule.
REFINANCE_AMORTIZATION_PLACEHOLDER_YEARS = 25


def refinance_amortization_fallback(cfg: dict,
                                     placeholder: int = REFINANCE_AMORTIZATION_PLACEHOLDER_YEARS) -> int:
    """The refinance amortization an exploratory script should use: the
    config's own declared ``property.refinance_amortization_years`` if
    present, else the round DP#13 placeholder documented above. DP#9: the
    ONE place this fallback is computed, so every exploratory call site
    (optimize.py, output_plugins.py) agrees.
    """
    declared = cfg.get('property', {}).get('refinance_amortization_years')
    return declared if declared is not None else placeholder


def apply_overlay(base_cfg: dict, overlay: ScenarioOverlay) -> dict:
    """DP#18/DP#24: Apply a ScenarioOverlay to a base config dict, returning a derived config.

    Per DP#18, overlays modify - they don't replace. Income fields are only
    overridden when the overlay provides an explicit value (not None).

    The derived config is a deep copy of base_cfg with overlay fields applied.
    The overlay can be recovered via overlay.to_dict() for round-trip serialization (DP#24).

    Args:
        base_cfg: Base configuration dict (from input.json or SimulationConfig.to_dict()).
        overlay: ScenarioOverlay with delta values.

    Returns:
        Derived config dict with overlay applied.

    Raises:
        MissingRefinanceAmortizationError: overlay.cash_out > 0 (a refinance
            is being booked) but no refinance amortization is declared
            (issue #655; see apply_ltv_overlay's docstring).
        ChargeLimitExceededError: the resulting mortgage + HELOC would exceed
            the property's registered charge (issue #664; see
            apply_ltv_overlay's docstring).
    """
    cfg = deepcopy(base_cfg)

    # Override income - only if overlay explicitly sets it (DP#18: null means no change)
    for member in cfg['family']['members']:
        if member['role'] == 'primary' and overlay.primary_income is not None:
            member['gross_income'] = overlay.primary_income
        elif member['role'] == 'spouse' and overlay.spouse_income is not None:
            member['gross_income'] = overlay.spouse_income

    # Override mortgage rate
    cfg['property']['mortgage_rate'] = overlay.mortgage_rate

    # Property values
    house_value = cfg['property']['house_value']
    # Issue #1036: a mortgage-free household (no kind=mortgage liability) has
    # no `mortgage_balance` key in its internal property block --
    # input_contract.py omits it rather than writing 0 (DP#32: a missing
    # mortgage is a first-class state, not a zero mortgage). This overlay path
    # used to index the key directly and crash with KeyError on every overlay
    # (the LTV/cash-out exploration), so a mortgage-free household could not
    # run through main() at all. Treat the absent key as a $0 incumbent mortgage
    # -- the explicit-absence-test the rest of this function already uses for
    # margin_available (#663), never a truthiness coercion (DP#32).
    orig_mortgage = cfg['property']['mortgage_balance'] if 'mortgage_balance' in cfg['property'] else 0
    cash_out = overlay.cash_out

    # Money-flow model (issue #257): a cash-out refinance is a MORTGAGE increase.
    # The borrowed cash_out is added to mortgage_balance and its proceeds are
    # invested directly (sourced into the lump sum via property['cash_out']).
    #
    # issue #664: mortgage and HELOC are NOT independent borrowing sources --
    # on a readvanceable/all-in-one product they share ONE registered charge
    # with ONE combined limit. Booking `cash_out` as a mortgage advance
    # consumes exactly that much of the shared charge, so margin_available
    # (pre-existing undrawn HELOC room) shrinks dollar-for-dollar (never
    # below 0) rather than staying untouched -- leaving it untouched would
    # record the same borrowed dollar as debt twice (once as HELOC, once as
    # mortgage; #619's original finding, only partially fixed -- #619 zeroed
    # out the *inflation*, but nothing yet bounded the two combined against
    # the charge that backs them both).
    #
    # issue #663/DP#32: when there is no readvanceable facility at all,
    # base_cfg['property'] has no margin_available key (input_contract.py
    # deliberately omits it -- writing 0 would be the fallback DP#32
    # forbids). An overlay must not invent that key either: a "no HELOC"
    # household stays a "no HELOC" household through every overlay -- there
    # is simply no revolving room to shrink.
    if cash_out > 0:
        # issue #655: a cash-out refinance is a NEW LOAN, re-amortized over
        # its own declared term -- not the incumbent mortgage's remaining
        # amortization (which roughly doubles the implied payment on a
        # near-payoff mortgage). DP#32: the overlay's explicit value wins;
        # falling back to the base config's declared
        # property.refinance_amortization_years (sourced from
        # decisions.mortgage.refinance_options[].amortization_years);
        # absence of BOTH is refused rather than silently inheriting
        # property.amortization_years.
        amort = overlay.refinance_amortization_years
        if amort is None:
            amort = cfg['property'].get('refinance_amortization_years')
        if amort is None:
            raise MissingRefinanceAmortizationError(
                f"apply_overlay({overlay.label!r}) books a ${cash_out:,.0f} "
                f"cash-out refinance (mortgage ${orig_mortgage:,.0f} -> "
                f"${orig_mortgage + cash_out:,.0f}) but no refinance "
                f"amortization is declared. A refinance is a new loan -- "
                f"silently repaying it over the incumbent's remaining "
                f"{cfg['property'].get('amortization_years')}-year amortization "
                f"overstates the payment on a near-payoff mortgage by roughly "
                f"2x (#655). Set ScenarioOverlay.refinance_amortization_years, "
                f"or property.refinance_amortization_years on the base config."
            )
        cfg['property']['amortization_years'] = amort

        orig_margin = cfg['property'].get('margin_available')
        if orig_margin is not None:
            cfg['property']['margin_available'] = max(0.0, orig_margin - cash_out)
        margin_for_charge_check = cfg['property'].get('margin_available', 0.0)

        charge_ltv_limit = cfg['property'].get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX)
        limit = charge_limit(house_value, charge_ltv_limit)
        new_mortgage_balance = orig_mortgage + cash_out
        total_secured = new_mortgage_balance + margin_for_charge_check
        if total_secured > limit + _CHARGE_TOLERANCE:
            raise ChargeLimitExceededError(
                f"apply_overlay({overlay.label!r}) books ${new_mortgage_balance:,.0f} "
                f"mortgage + ${margin_for_charge_check:,.0f} HELOC room = "
                f"${total_secured:,.0f} secured debt "
                f"({total_secured / house_value:.0%} LTV, if house_value > 0) "
                f"against a ${limit:,.0f} charge limit ({charge_ltv_limit:.0%} "
                f"of ${house_value:,.0f} house value). Refused rather than "
                f"simulated (#664)."
            )

    cfg['property']['mortgage_balance'] = orig_mortgage + cash_out
    cfg['property']['cash_out'] = cash_out
    if overlay.ltv is not None:
        cfg['property']['ltv_max'] = overlay.ltv
    elif house_value > 0:
        cfg['property']['ltv_max'] = (orig_mortgage + cash_out) / house_value

    # RESP cash-out
    if overlay.resp_cash_out > 0:
        cfg['accounts']['resp_current_balance'] = 0
        if 'resp_composition' in cfg['accounts']:
            del cfg['accounts']['resp_composition']
        cfg['property']['free_cash'] = overlay.resp_cash_out

    if overlay.investment_return is not None:
        # DP#21 (#260/#591): route the swept rate through the one helper that
        # targets return_model, the engine's single source of truth.
        set_return_rate(cfg, overlay.investment_return)

    # DP#14/DP#5: Sensitivity overlay fields
    if overlay.inflation is not None:
        cfg.setdefault('assumptions', {})['inflation'] = overlay.inflation
    if overlay.salary_growth is not None:
        cfg.setdefault('assumptions', {})['salary_growth'] = overlay.salary_growth

    # Issue #303: retirement-age dimension. By default the swept age applies to
    # the PRIMARY member only (keeps the comparison simple — one moving part).
    # Set retirement.apply_to = "both" to move both adults' retirement_age in
    # lockstep when the household retires together.
    if overlay.retirement_age is not None:
        apply_to = (cfg.get('retirement', {}) or {}).get('apply_to', 'primary')
        for member in cfg['family']['members']:
            if apply_to == 'both' or member.get('role') == 'primary':
                member['retirement_age'] = overlay.retirement_age

    # Issues #711/#712: the CPP-sharing / pension-split elections land on the
    # keys the ENGINE reads -- SimulationConfig.from_dict maps cfg['retirement']
    # into SimulationConfig.retirement_data, which apply_retirement_income reads
    # as ret.get('cpp_share') / ret.get('pension_split_pct'). DP#18: writing them
    # anywhere else would be a dead-key sweep (the #591 shape). None => untouched.
    if overlay.cpp_share is not None:
        cfg.setdefault('retirement', {})['cpp_share'] = overlay.cpp_share
    if overlay.pension_split_pct is not None:
        cfg.setdefault('retirement', {})['pension_split_pct'] = overlay.pension_split_pct

    # Issue #688: the emergency-reserve target sweep (0/3/6/12/24 months).
    # DP#18: this must land on the key the ENGINE reads -- SimulationConfig
    # .from_dict maps `emergency_reserve.target_months` into
    # `SimulationConfig.emergency_reserve_target_months`, which SimState
    # .initial() sizes the cash sleeve from and simulation_rules
    # .apply_solvency draws first. A test that only asserts the merged CONFIG
    # changed would not prove that (#591's dead-key sweep is exactly this
    # shape); tests/test_issue_679_solvency.py asserts the ENGINE's OUTPUT
    # moves -- terminal wealth falls and the ruin outcome flips -- as the
    # target is swept.
    #
    # Sweeping a target onto a household that declared no reserve block at
    # all is a legitimate "what if I started holding one?" question, so the
    # block is created if absent -- but every other field it needs
    # (held_in/instrument/rate) has no answer the overlay could invent, so
    # the sweep is REFUSED rather than run against fabricated ones (DP#32).
    if overlay.emergency_reserve_months is not None:
        reserve_cfg = (cfg.get('assumptions', {}) or {}).get('emergency_reserve')
        if not reserve_cfg:
            raise ValueError(
                f"apply_overlay({overlay.label!r}) sweeps "
                f"emergency_reserve_months={overlay.emergency_reserve_months} "
                f"but the base config declares no assumptions.emergency_reserve "
                f"block -- so the reserve's instrument, its rate, and the "
                f"account it is held in are all unknown. A reserve swept "
                f"against an invented rate/location is not a sweep of the "
                f"household's real decision, it is a sweep of a fabricated "
                f"one (#688, DP#32). Declare the block (target_months may be "
                f"0) and sweep the target off it."
            )
        cfg['assumptions']['emergency_reserve'] = {
            **reserve_cfg, 'target_months': overlay.emergency_reserve_months}

    # Issue #936: the deposit product this scenario TAKES. DP#18: it must
    # land on the key the ENGINE reads -- SimulationConfig.from_dict maps
    # cfg['deposit_product'] into SimulationConfig.deposit_product, which
    # SimState.initial carves the fund_amount out of funding_source from
    # (money-conserving) and apply_deposit_product_growth grows at its
    # rate_schedule. None means the "leave it" baseline (the same money stays in
    # the funding source, growing at that account's rate) -- writing nothing
    # leaves the config in its no-product shape, byte-identical to today (DP#32). The
    # carve itself is done in SimState.initial (where the funding source's
    # opening balance is known), NOT here -- exactly as the emergency-reserve
    # sleeve (#688) carves in initial() rather than in the overlay.
    if overlay.deposit_product is not None:
        cfg['deposit_product'] = overlay.deposit_product

    # DP#24: Attach serializable overlay metadata for round-trip
    cfg['_overlay'] = overlay.to_dict()

    return cfg


def build_overlay_config(base_cfg: dict, overlay: ScenarioOverlay) -> dict:
    """DP#18: Overlay a delta on the base config, producing a derived scenario.

    Delegates to apply_overlay.
    """
    return apply_overlay(base_cfg, overlay)


def apply_ltv_overlay(config: 'SimulationConfig', ltv: float,
                       refinance_amortization_years: Optional[int] = None) -> 'SimulationConfig':
    """Overlay a target LTV onto a base SimulationConfig (DP#18, issues
    #257/#619/#664/#655).

    This is the ONE implementation of the LTV/cash-out overlay for the
    SimulationConfig-object callers -- GridOptimizer and ScipyOptimizer both
    call this instead of each maintaining their own copy (DP#9). It applies
    the same money-flow rule as ``apply_overlay`` above (the dict/
    ScenarioOverlay path used by simulate.py):

      - The cash-out needed to reach ``ltv`` is a MORTGAGE increase, booked
        as debt exactly once, on ``mortgage_balance``.
      - issue #664: a readvanceable mortgage and its HELOC are carved out of
        ONE registered charge with ONE combined limit -- NOT independent
        borrowing sources. ``margin_available`` (pre-existing undrawn HELOC
        room) shrinks dollar-for-dollar by the cash-out booked here (floored
        at 0), because that room is drawn from the exact same charge the
        mortgage advance just consumed. The resulting total secured debt
        (new mortgage balance + remaining margin_available) is asserted to
        fit inside ``charge_limit(house_value, charge_ltv_limit)``
        (OSFI B-20: 80% LTV is the legal maximum for an uninsured combined
        loan plan) -- a target LTV (or a pre-existing declared facility) that
        would breach it raises ``ChargeLimitExceededError`` rather than being
        silently simulated at >100% LTV.
      - issue #655: a cash-out refinance is a NEW LOAN, re-amortized over its
        own term -- not the incumbent mortgage's remaining amortization
        (which would roughly double the implied payment on a near-payoff
        mortgage). The new amortization comes from
        ``refinance_amortization_years`` if supplied, else
        ``config.refinance_amortization_years`` (sourced from a contract's
        ``decisions.mortgage.refinance_options[].amortization_years``); if
        neither is available, raises ``MissingRefinanceAmortizationError``
        (DP#32) rather than silently inheriting ``config.amortization_years``.
      - ``cash_out`` is recorded on the returned config so callers can size
        the invested lump sum as ``margin_available * draw_fraction +
        cash_out`` (issue #735: the margin draw is a swept decision,
        defaulting to 0.0/undrawn -- see ``GridOptimizer.optimize()``'s
        ``draw_fraction_options`` -- while the refinance proceeds are
        always fully realized) instead of recomputing it against an
        already-inflated margin.

    Prior to #619, ``optimizer.py`` and ``scipy_optimizer.py`` each carried a
    private ``_apply_ltv_overlay`` that inflated ``margin_available`` by
    ``cash_out`` -- the pre-#257 behaviour. The two copies agreed with each
    other (a test pinned them together) but both disagreed with this
    function, so every LTV > 0 optimizer result invested a full extra
    ``cash_out`` of capital that was never borrowed. #619 fixed the
    inflation, but left ``margin_available`` untouched by the overlay
    entirely -- so a household could still draw its FULL pre-existing HELOC
    limit *in addition to* a mortgage refinanced up to the same charge,
    doubling apparent borrowing capacity (#664).

    At LTV <= 0, or when the target LTV implies no cash-out (current LTV
    already meets or exceeds the target), returns config unchanged.

    Raises:
        MissingRefinanceAmortizationError: a cash-out is being booked but no
            refinance amortization is declared or supplied (#655).
        ChargeLimitExceededError: the resulting mortgage + HELOC would
            exceed the property's registered charge (#664).
    """
    if ltv <= 0:
        return config
    cash_out = max(0, ltv * config.house_value - config.mortgage_balance)
    if cash_out <= 0:
        return config

    amort = refinance_amortization_years
    if amort is None:
        amort = config.refinance_amortization_years
    if amort is None:
        raise MissingRefinanceAmortizationError(
            f"apply_ltv_overlay(ltv={ltv:.2%}) books a ${cash_out:,.0f} "
            f"cash-out refinance (mortgage ${config.mortgage_balance:,.0f} -> "
            f"${config.mortgage_balance + cash_out:,.0f}) but no refinance "
            f"amortization is declared. A refinance is a new loan -- "
            f"silently repaying it over the incumbent's remaining "
            f"{config.amortization_years}-year amortization overstates the "
            f"payment on a near-payoff mortgage by roughly 2x (#655). Set "
            f"config.refinance_amortization_years (mapped from "
            f"decisions.mortgage.refinance_options[].amortization_years), or "
            f"pass refinance_amortization_years= explicitly."
        )

    new_mortgage_balance = config.mortgage_balance + cash_out
    new_margin_available = max(0.0, config.margin_available - cash_out)

    overlaid = replace(config,
                        mortgage_balance=new_mortgage_balance,
                        cash_out=cash_out,
                        margin_available=new_margin_available,
                        amortization_years=amort,
                        ltv_max=ltv)

    total_secured = overlaid.mortgage_balance + overlaid.margin_available
    limit = charge_limit(overlaid.house_value, overlaid.charge_ltv_limit)
    if total_secured > limit + _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"apply_ltv_overlay(ltv={ltv:.2%}) books ${overlaid.mortgage_balance:,.0f} "
            f"mortgage + ${overlaid.margin_available:,.0f} HELOC room = "
            f"${total_secured:,.0f} secured debt "
            f"({total_secured / overlaid.house_value:.0%} LTV) against a "
            f"${limit:,.0f} charge limit ({overlaid.charge_ltv_limit:.0%} of "
            f"${overlaid.house_value:,.0f} house value). Refused rather than "
            f"simulated (#664)."
        )
    return overlaid
