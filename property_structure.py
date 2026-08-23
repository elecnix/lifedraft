#!/usr/bin/env python3
"""Mortgage-structure, sourcing, and property-funding overlays.

Split out of ``simulation_config.py``. All of it answers ONE question that the
generic ``ScenarioOverlay`` does not: given a property and a declared borrowing
STRUCTURE (how the charge is carved into house / investment / line tranches, and
which pocket each dollar of a purchase comes out of), what does the resulting
config look like -- and when must it be refused instead of simulated?

  - ``_validate_tranche_spec`` / ``_tranche_line_rate`` /
    ``_apply_tranched_structure``: the multi-tranche charge split;
  - ``apply_structure_overlay``: the single entry point that lands a structure
    on one property's config;
  - ``apply_sourcing_overlay``: how a purchase is sourced across cash and the
    tranches;
  - ``apply_property_funding_overlay``: the same, per property, across a whole
    config.

Every refusal here is an OSFI B-20 charge breach (``charge_limits``), raised
rather than silently simulated at >100% LTV.
"""

import logging
from copy import deepcopy
from typing import Dict

from charge_limits import (
    _CHARGE_TOLERANCE,
    ChargeLimitExceededError,
    OSFI_B20_CHARGE_LTV_MAX,
    OSFI_B20_REVOLVING_LTV_MAX,
    charge_limit,
    heloc_revolving_limit,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Issue #1075: the 3-tranche readvanceable structure (house / deductible
# investment / line) -- the tranche-aware half of ``apply_structure_overlay``
# / ``apply_sourcing_overlay``. ONE spelling of the spec validation and the
# amount application, shared by the contract-load check, the two overlays and
# the optimizer's sweep points (DP#9).
# ═══════════════════════════════════════════════════════════════════════════


# Issue #1075 (optimizer half): the DEFAULT floor for the house tranche's
# sweep -- 60% of the registered charge -- when the structure declares no
# ``min_house_floor``. DP#13: a fallback for ABSENT input, never an opinion
# that overrides a declared floor (``_tranche_house_floor`` prefers the
# declared value, and the sweep's floor is not the cash-back threshold).
_HOUSE_SWEEP_FLOOR_FRACTION = 0.6
_TRANCHE_KINDS = ('house', 'investment', 'line')


def _validate_tranche_spec(structure: Dict) -> Dict:
    """Validate a structure's ``tranches`` declaration, returning it keyed by
    kind. DP#32: every refusal is loud and names the structure and the tranche
    -- an invalid spec must never be silently half-applied.

    Raises:
        ValueError: a spec the sweep could not honour -- overlapping kinds,
            a ``min_amount`` on a non-house tranche, a ``deductible`` flag on
            a non-investment tranche, an unpriced revolving segment, or a
            missing/empty ``tranches`` array. ``input_contract`` wraps this
            as ``ContractAdaptationError`` at contract-load time.
    """
    label = _structure_label(structure)
    tranches = structure.get('tranches')
    if not tranches:
        raise ValueError(
            f"structure {label!r} declares tranches but the tranches array is "
            f"empty -- nothing to apply (issue #1075)."
        )
    by_kind: Dict[str, Dict] = {}
    for t in tranches:
        kind = t.get('kind')
        if kind not in _TRANCHE_KINDS:
            raise ValueError(
                f"structure {label!r} declares a tranche of unknown kind "
                f"{kind!r} -- must be one of {list(_TRANCHE_KINDS)} (issue #1075)."
            )
        if kind in by_kind:
            raise ValueError(
                f"structure {label!r} declares TWO tranches of kind {kind!r} -- "
                f"the 3-tranche split has at most one sub-account per kind; "
                f"overlapping kinds cannot be priced (issue #1075)."
            )
        if 'min_amount' in t and kind != 'house':
            raise ValueError(
                f"structure {label!r} tranche {kind!r} declares a min_amount -- "
                f"only the 'house' tranche carries a floor (its amount is the "
                f"household's house mortgage, which is what a lender's cash-back "
                f"programs price); the investment and line amounts are swept "
                f"from the charge (issue #1075)."
            )
        if 'min_house_floor' in t and kind != 'house':
            raise ValueError(
                f"structure {label!r} tranche {kind!r} declares a "
                f"min_house_floor -- only the 'house' tranche carries a sweep "
                f"floor (its amount is the household's house mortgage, which is "
                f"what a lender's cash-back programs price); the investment and "
                f"line amounts are swept from the charge (issue #1075)."
            )
        if t.get('deductible') and kind != 'investment':
            raise ValueError(
                f"structure {label!r} tranche {kind!r} declares deductible: true "
                f"-- only the 'investment' tranche's interest is deductible "
                f"under ITA s.20(1)(c) (borrowed to invest); the house tranche "
                f"and the line are never deductible by declaration (issue #1075)."
            )
        if 'rate_type' in t and 'rate' not in t:
            raise ValueError(
                f"structure {label!r} tranche {kind!r} declares a rate_type but "
                f"no rate -- a rate_type without its rate is meaningless (issue "
                f"#1075)."
            )
        by_kind[kind] = t
    return by_kind


def _tranche_house_floor(by_kind: Dict, charge: float) -> float:
    """Issue #1075 (optimizer half): the house tranche amount's SWEEP FLOOR.

    ``min_house_floor`` when the house tranche declares one, else 60% of the
    registered charge (``_HOUSE_SWEEP_FLOOR_FRACTION`` -- DP#13: a fallback
    for absent input, never an opinion). This is NOT the ``min_amount``:
    that is the CASH-BACK THRESHOLD (the point the sweep reports the
    incentive at), and the whole point of this sweep is that the house
    mortgage may go BELOW it, forgoing the cash-back. The sweep enumerates
    house amounts from this floor UP to the charge (``_tranche_sweep_points``
    in optimize.py); ``_apply_tranched_structure`` refuses a split below it
    loudly (DP#32: never clamp, never silently no-op).

    A structure that declares no house tranche at all has no house mortgage
    to floor -- the sweep is over the investment/line split alone, house
    pinned at 0, exactly as before this dimension existed.
    """
    house_tranche = by_kind.get('house')
    if house_tranche is None:
        return 0.0
    declared_floor = house_tranche.get('min_house_floor')
    # DP#32: explicit presence test -- a declared floor of exactly 0 is a
    # real value (sweep the whole charge's worth of house room), never
    # shadowed by the fallback.
    if declared_floor is not None:
        return declared_floor
    return charge * _HOUSE_SWEEP_FLOOR_FRACTION


def _tranche_line_rate(structure: Dict, by_kind: Dict) -> tuple:
    """The revolving segment's (rate, rate_type) for a tranches-declared
    structure: the 'line' tranche's own pair, else the structure-level
    ``revolving_rate``/``revolving_rate_type`` (the #687 spelling), else None.
    Raises ValueError when the structure can carry a line balance -- today
    (line amount > 0) or later (readvanceable) -- and no pair prices it
    (#654, DP#32: never derived from the mortgage's own rate)."""
    label = _structure_label(structure)
    line = by_kind.get('line')
    # DP#32: explicit None-testing -- a declared rate of exactly 0.0 is a real
    # value, never shadowed by the fallback (and `or` would make 0
    # unrepresentable, the exact #654 trap).
    rate = None
    if line is not None and line.get('rate') is not None:
        rate = line['rate']
    elif structure.get('revolving_rate') is not None:
        rate = structure['revolving_rate']
    rate_type = None
    if line is not None and line.get('rate_type') is not None:
        rate_type = line['rate_type']
    elif structure.get('revolving_rate_type') is not None:
        rate_type = structure['revolving_rate_type']
    if (rate is None) != (rate_type is None):
        raise ValueError(
            f"structure {label!r} prices its revolving line with a rate but no "
            f"rate_type (or vice versa) -- a line that can carry a balance must "
            f"declare both, and it is never derived from the mortgage's own "
            f"rate (#654/#1075)."
        )
    return rate, rate_type


def _apply_tranched_structure(property_cfg: dict, structure: Dict) -> dict:
    """Issue #1075: apply a 3-tranche structure (house / deductible investment
    / line) to a ``property`` dict, returning a derived copy.

    The tranche spec declares the KINDS and the FIXED facts (the house
    tranche's SWEEP FLOOR -- ``min_house_floor``, defaulting to 60% of the
    charge, NOT the cash-back threshold, which the sweep may go below -- the
    investment tranche's deductibility, each tranche's own rate); the
    AMOUNTS come from ``structure['tranche_amounts']`` -- a concrete
    ``{house, investment, line}`` split summing to the registered charge, as
    the optimizer's sweep enumerates. The amounts are the OPENING POSITION,
    not a re-split of a carried-through drawn balance (#851's invariant
    applies to the share form): the household's drawn mortgage IS house +
    investment (the investment tranche was borrowed to invest), the undrawn
    room IS the line, and the deductible investment tranche's EXACT interest
    (balance x its OWN rate -- never the blended-rate product, which drags
    the house tranche's cheaper rate in) is carried on
    ``deductible_mortgage_balance``/``deductible_mortgage_interest`` for the
    s.20(1)(c) pricing.

    Without ``tranche_amounts`` (the contract-load check's call), the
    MINIMUM point is applied instead -- house at its SWEEP FLOOR (the
    smallest amount the sweep will ever enumerate; ``min_amount`` is NOT
    consulted -- it is the cash-back threshold, and going below it is the
    point of the sweep), investment 0, the whole rest of the charge as the
    line. That is the binding point for both OSFI B-20 ceilings (the
    largest possible revolving segment; the combined cap cannot bind since
    the split sums to the charge by construction), so a structure that
    passes it passes every sweep point -- and one whose floor exceeds the
    charge fails loudly here, at contract load, rather than when a sweep
    happens to reach it (DP#32).

    Money is conserved by construction: the three amounts partition the
    charge exactly, and the engine books mortgage debt for house + investment
    and (when drawn) the line -- every borrowed dollar once (DP#18).

    Raises:
        ValueError: an invalid tranche spec (``_validate_tranche_spec``).
        ChargeLimitExceededError: the house floor exceeds the charge, or the
            split breaches either OSFI B-20 ceiling (the 80% combined cap or
            the 65% revolving-only cap), or the amounts do not sum to the
            charge -- refused rather than silently clamped (DP#32).
    """
    property_cfg = deepcopy(property_cfg)
    by_kind = _validate_tranche_spec(structure)
    label = _structure_label(structure)

    house_value = property_cfg.get('house_value', 0.0)
    orig_mortgage = property_cfg.get('mortgage_balance', 0.0)
    # DP#32: explicit absence-testing, never `x or 0` -- margin_available
    # absent means "no facility at all" (#663), a different state from a
    # declared facility with $0 room, but its contribution to the charge is
    # 0 either way, which is all the arithmetic below needs.
    orig_margin = property_cfg.get('margin_available')
    orig_margin = 0.0 if orig_margin is None else orig_margin
    charge = orig_mortgage + orig_margin

    house_tranche = by_kind.get('house')
    investment_tranche = by_kind.get('investment')
    house_floor = _tranche_house_floor(by_kind, charge)

    amounts = structure.get('tranche_amounts')
    if amounts is None:
        # Contract-load feasibility check: the minimum point (house at its
        # SWEEP FLOOR -- the smallest amount the sweep will ever enumerate;
        # ``min_amount`` is the cash-back threshold and is NOT a floor, see
        # ``_tranche_house_floor`` -- no investment, the whole remainder as
        # the line: the binding revolving-cap case).
        if house_floor > charge + _CHARGE_TOLERANCE:
            raise ChargeLimitExceededError(
                f"structure {label!r}: its house sweep floor of "
                f"${house_floor:,.0f} exceeds the ${charge:,.0f} registered charge "
                f"-- there is no house amount between the floor and the charge, "
                f"so no 3-tranche split exists to sweep (issue #1075)."
            )
        amounts = {'house': house_floor, 'investment': 0.0,
                   'line': max(0.0, charge - house_floor)}

    house = amounts['house']
    investment = amounts['investment']
    line = amounts['line']

    if house < house_floor - _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"structure {label!r}: its house tranche amount of ${house:,.0f} "
            f"is below the ${house_floor:,.0f} sweep floor (the declared "
            f"min_house_floor, defaulting to 60% of the registered charge) -- "
            f"the sweep varies the house mortgage from its floor up to the "
            f"charge, and a split below the floor is not a candidate (issue "
            f"#1075)."
        )
    if abs((house + investment + line) - charge) > _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"structure {label!r}: its tranche amounts ${house:,.0f} (house) + "
            f"${investment:,.0f} (investment) + ${line:,.0f} (line) = "
            f"${house + investment + line:,.0f} do not sum to the "
            f"${charge:,.0f} registered charge -- the 3-tranche split is a "
            f"partition of ONE charge, and an amount above (or below) it is a "
            f"borrowed dollar that exists twice (or not at all) (issue #1075)."
        )

    line_rate, line_rate_type = _tranche_line_rate(structure, by_kind)
    readvanceable = bool(structure.get('readvanceable', False))
    if (line > 0 or readvanceable) and (line_rate is None or line_rate_type is None):
        raise ValueError(
            f"structure {label!r} carries a revolving line (${line:,.0f} of "
            f"room{', readvanceable' if readvanceable else ''}) but prices it "
            f"nowhere -- neither the 'line' tranche nor the structure-level "
            f"revolving_rate/revolving_rate_type. A line that can draw -- "
            f"directly, or later via readvance -- must be priced (#654/#1075); "
            f"it is never derived from the mortgage's own rate."
        )

    baseline_rate = property_cfg.get('mortgage_rate', 0.05)
    house_rate = house_tranche.get('rate') if (house_tranche and house_tranche.get('rate') is not None) else baseline_rate
    investment_rate = (investment_tranche.get('rate')
                       if (investment_tranche and investment_tranche.get('rate') is not None)
                       else baseline_rate)

    new_mortgage = house + investment
    new_margin = line
    if new_mortgage > 0:
        # The single amortization schedule is exact on total interest at the
        # balance-weighted rate (task #1075 data model); the per-tranche
        # EXACT interest is carried separately for the s.20(1)(c) pricing.
        blended_rate = (house * house_rate + investment * investment_rate) / new_mortgage
    else:
        blended_rate = baseline_rate

    charge_ltv_limit = property_cfg.get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX)
    heloc_ltv_limit = property_cfg.get('heloc_ltv_limit', OSFI_B20_REVOLVING_LTV_MAX)
    combined_max = charge_limit(house_value, charge_ltv_limit)
    revolving_max = heloc_revolving_limit(house_value, heloc_ltv_limit)
    _refuse_structure_over_charge(
        structure, (line / charge) if charge > 0 else 0.0, house_value,
        charge, new_mortgage, new_margin, combined_max, revolving_max,
        charge_ltv_limit, heloc_ltv_limit)

    property_cfg['mortgage_balance'] = new_mortgage
    property_cfg['mortgage_rate'] = blended_rate
    if investment > 0 and investment_tranche is not None and investment_tranche.get('deductible'):
        property_cfg['deductible_mortgage_balance'] = investment
        property_cfg['deductible_mortgage_interest'] = investment * investment_rate
    else:
        # DP#32: a split with no deductible tranche carries no deductible
        # keys -- never a fabricated zero.
        property_cfg.pop('deductible_mortgage_balance', None)
        property_cfg.pop('deductible_mortgage_interest', None)

    facility = dict(structure)
    facility['revolving_rate'] = line_rate
    facility['revolving_rate_type'] = line_rate_type
    _write_structure_facility(property_cfg, facility, new_margin)
    return property_cfg


def apply_structure_overlay(property_cfg: dict, structure: Dict) -> dict:
    """Issue #687: apply ONE candidate mortgage STRUCTURE
    (``decisions.mortgage.structure_options[]``) to a ``property`` config
    dict, returning a derived copy.

    A household facing a refinance/renewal decision may be choosing between
    genuinely different structures against the SAME registered charge --
    the whole charge as a plain amortizing mortgage, the same amount but
    flagged readvanceable (the line starts at $0 and grows only as
    principal is repaid), or a smaller mortgage plus an undrawn revolving
    line carved out today. ``structure['revolving_share']`` is the fraction
    of the CURRENT registered charge (``mortgage_balance + margin_available``)
    this structure sets aside as the revolving segment; the rest is available
    as amortizing mortgage. The charge total is UNCHANGED -- this overlay only
    changes how the same charge is SPLIT, never how much of it is drawn (that
    is ``apply_overlay``'s ``cash_out``, a separate, composable dimension).

    Issue #1075: a structure may instead declare ``tranches`` -- the
    3-tranche readvanceable form (house >= a declared minimum, deductible
    investment mortgage, readvanceable line) -- in which case the split is
    applied by ``_apply_tranched_structure`` below: the tranche AMOUNTS
    define the drawn/room position directly (mortgage = house + investment,
    undrawn room = line), and the deductible investment tranche's EXACT
    interest (balance x its OWN rate) is carried on
    ``deductible_mortgage_balance``/``deductible_mortgage_interest`` for the
    s.20(1)(c) pricing. The #687 share semantics above remain byte-for-byte
    for a structure that declares ``revolving_share`` (DP#13: the tranche
    spec is an additive opt-in).

    Issue #851: ``mortgage_balance`` is a DRAWN balance and ``margin_available``
    is UNDRAWN room -- two different things that must not be conflated. The
    drawn position (``mortgage_balance``) is carried through UNCHANGED; only the
    revolving segment (``margin_available`` = ``charge * revolving_share``, all
    undrawn on this no-cash-out path) is re-derived. So the household's total
    booked DEBT is the same for every ``revolving_share`` -- carving out a line
    that stays undrawn cannot invent or destroy a dollar of debt. This is the
    same drawn/room separation ``apply_sourcing_overlay`` (#845) applies at a
    positive ``cash_out``; this function is its ``cash_out``-of-0 case. (Before
    #851 this booked ``total_secured - new_margin`` as ``mortgage_balance``,
    which counted undrawn HELOC room as mortgage debt -- +$150,000 of phantom
    debt on #687's shipped line-free example.)

    ``structure.get('revolving_share')`` of ``None`` means "this structure
    dict carries no declared split" -- the identity/baseline case
    (``scenario_discovery``'s single-element fallback when
    ``decisions.mortgage.structure_options`` was never declared) -- and
    ``property_cfg`` is returned untouched, DP#13/DP#32: absence is not an
    opinion.

    Per DP#32/#663 (``has_readvanceable_facility``'s own docstring): "no
    revolving facility at all" and "a facility with $0 room" are NOT the
    same state. A structure with no revolving component AT ALL (revolving
    share 0.0 and not flagged readvanceable -- issue #687's structure A,
    "all_mortgage") clears ``margin_available``/``heloc_readvance``/
    ``heloc_rate``/``heloc_rate_type`` outright, rather than leaving a
    ``margin_available: 0`` key that would misreport a facility that
    structurally does not exist (and would spuriously trip
    ``resolve_heloc_rate``'s "declare a facility, declare its rate"
    warning for a structure that never asked for one). A structure that DOES
    carry a revolving component -- ``revolving_share > 0``, OR flagged
    ``readvanceable`` even at a $0 starting share (issue #687's structure B:
    the readvance mechanism, #664/#681, grows the line from $0 as principal
    is repaid) -- sets the facility fields for real, including
    ``heloc_rate``/``heloc_rate_type`` from the structure's OWN declared
    ``revolving_rate``/``revolving_rate_type`` (never derived from the
    mortgage's rate, #654).

    Raises:
        ChargeLimitExceededError: this structure's combined secured debt
            would exceed the registered charge (80% LTV, OSFI B-20), or its
            revolving segment ALONE would exceed the revolving-only ceiling
            (65% LTV, independent of the combined cap) -- refused rather
            than silently clamped (DP#32).
    """
    property_cfg = deepcopy(property_cfg)
    # Issue #1075: a structure that declares ``tranches`` (the 3-tranche
    # readvanceable form -- house / deductible investment / line) is applied
    # by the tranche machinery, NOT the share machinery: the tranches define
    # the split directly (and carry the deductible-tranche facts the share
    # form cannot express). See ``_apply_tranched_structure``.
    if structure.get('tranches') is not None:
        return _apply_tranched_structure(property_cfg, structure)

    revolving_share = structure.get('revolving_share')
    if revolving_share is None:
        return property_cfg

    house_value = property_cfg.get('house_value', 0.0)
    orig_mortgage = property_cfg.get('mortgage_balance', 0.0)
    orig_margin = property_cfg.get('margin_available', 0.0)
    total_secured = orig_mortgage + orig_margin

    charge_ltv_limit = property_cfg.get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX)
    heloc_ltv_limit = property_cfg.get('heloc_ltv_limit', OSFI_B20_REVOLVING_LTV_MAX)
    combined_max = charge_limit(house_value, charge_ltv_limit)
    revolving_max = heloc_revolving_limit(house_value, heloc_ltv_limit)

    # Issue #851: ``revolving_share`` allocates the CHARGE into a revolving
    # segment, but ``mortgage_balance`` is a DRAWN balance (debt owed,
    # amortized, interest accrued) while ``margin_available`` is UNDRAWN room.
    # ``new_mortgage`` must stay the drawn position (``orig_mortgage``), never
    # ``total_secured - new_margin`` -- summing the drawn mortgage and the
    # undrawn line into ``total_secured`` and re-splitting it booked the
    # household's undrawn HELOC room as mortgage debt (the phantom debt #577
    # exists to prevent). The error was ``orig_margin * (1 - share) -
    # orig_mortgage * share``: it INVENTS debt at low shares (+150,000 on the
    # #687 shipped example's line-free structure A) and DESTROYS it at high
    # ones, and is not equal across structures -- systematically favouring
    # carving out a line, exactly the conclusion the #687 structure table
    # exists to test. This is ``apply_sourcing_overlay``'s drawn/room
    # separation at cash_out=0 (the no-cash-out path this overlay owns): total
    # booked debt stays ``orig_mortgage`` for EVERY share, and any revolving
    # segment carved out is UNDRAWN room, costing nothing until drawn.
    new_margin = total_secured * revolving_share
    new_mortgage = orig_mortgage

    _refuse_structure_over_charge(
        structure, revolving_share, house_value, total_secured,
        new_mortgage, new_margin, combined_max, revolving_max,
        charge_ltv_limit, heloc_ltv_limit)

    property_cfg['mortgage_balance'] = new_mortgage
    _write_structure_facility(property_cfg, structure, new_margin)
    return property_cfg


def _structure_label(structure: Dict) -> str:
    """The name a refusal message calls this structure by (DP#32: a refusal
    must name the option it refused, never a bare index)."""
    label = structure.get('label')
    if label is None:
        label = structure.get('id', '?')
    return label


def _refuse_structure_over_charge(structure: Dict, revolving_share: float,
                                  house_value: float, total_secured: float,
                                  new_mortgage: float, new_margin: float,
                                  combined_max: float, revolving_max: float,
                                  charge_ltv_limit: float,
                                  heloc_ltv_limit: float) -> None:
    """Both OSFI B-20 refusals for ONE candidate structure (#664/#687).

    DP#9: extracted so ``apply_structure_overlay`` and
    ``apply_sourcing_overlay`` (#845/#849) enforce the SAME two ceilings
    from one implementation -- a second copy is exactly how #619's two
    private ``_apply_ltv_overlay``s came to agree with each other and
    disagree with the engine.
    """
    label = _structure_label(structure)
    if total_secured > combined_max + _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"structure {label!r}: ${new_mortgage:,.0f} mortgage + "
            f"${new_margin:,.0f} revolving = ${total_secured:,.0f} secured "
            f"debt exceeds the ${combined_max:,.0f} charge limit "
            f"({charge_ltv_limit:.0%} of ${house_value:,.0f} house value) "
            f"-- OSFI B-20's legal maximum LTV for an uninsured combined "
            f"loan plan. Refused rather than simulated (#664/#687)."
        )
    if new_margin > revolving_max + _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"structure {label!r}: ${new_margin:,.0f} revolving segment "
            f"({revolving_share:.0%} of ${total_secured:,.0f} secured debt) "
            f"exceeds the ${revolving_max:,.0f} revolving-only ceiling "
            f"({heloc_ltv_limit:.0%} of ${house_value:,.0f} house value) -- "
            f"OSFI B-20 caps the readvanceable/revolving segment of a "
            f"combined loan plan independent of the 80% combined cap; "
            f"lending above it must be amortizing and non-readvanceable. "
            f"Refused rather than simulated (#664/#687)."
        )


def _write_structure_facility(property_cfg: dict, structure: Dict,
                              new_margin: float) -> None:
    """Write (or clear) the revolving-facility fields this structure implies.

    DP#9/DP#32: shared by ``apply_structure_overlay`` and
    ``apply_sourcing_overlay``. Mutates ``property_cfg`` in place -- both
    callers own a fresh deep copy already.
    """
    readvanceable = bool(structure.get('readvanceable', False))
    if new_margin > 0 or readvanceable:
        property_cfg['margin_available'] = new_margin
        property_cfg['heloc_readvance'] = readvanceable
        if structure.get('revolving_rate') is not None:
            property_cfg['heloc_rate'] = structure['revolving_rate']
        if structure.get('revolving_rate_type') is not None:
            property_cfg['heloc_rate_type'] = structure['revolving_rate_type']
    else:
        # Issue #687's structure A ("all_mortgage"): no revolving component
        # at all -- clear any facility this property dict inherited from
        # the base config, rather than leave stale heloc facts an
        # 'all_mortgage' household did not choose (DP#18).
        property_cfg.pop('margin_available', None)
        property_cfg.pop('heloc_readvance', None)
        property_cfg.pop('heloc_rate', None)
        property_cfg.pop('heloc_rate_type', None)


def apply_sourcing_overlay(property_cfg: dict, structure: Dict) -> dict:
    """Issues #845/#849: apply ONE candidate structure to a property that has
    ALREADY had a cash-out refinance booked on it by ``apply_overlay``.

    This is the composition ``apply_structure_overlay`` cannot do. That
    function re-splits ``mortgage_balance + margin_available`` and books the
    WHOLE split as ``mortgage_balance`` debt -- correct when the property's
    combined secured position is entirely drawn, wrong the moment
    ``apply_overlay`` has just advanced ``cash_out`` and left undrawn room
    beside it. Composing the two naively books the leftover room as mortgage
    debt (money from nowhere) and, at ``draw_fraction > 0``, invests the same
    borrowed dollar twice (``lump_sum = margin_available * draw_fraction +
    cash_out``, ``optimizer.py``). Measured on ``schema/example.json``'s own
    numbers, the naive composition mis-booked $100,000 of debt.

    The question this DOES answer -- the irreversible notary-day one #849
    opens with -- is **where the surplus comes from at a FIXED registered
    charge**:

      - ``revolving_share = 0.0``  -> the whole surplus is an amortizing
        MORTGAGE ADVANCE (cheaper rate; forced principal repayment erodes the
        deductible balance);
      - ``revolving_share >= cash_out / charge`` -> the whole surplus is a
        REVOLVING DRAW (dearer rate; interest-only, so the deductible balance
        does not amortize away);
      - in between -> the surplus is split, line first.

    ``cash_out`` can only say HOW MUCH; only ``revolving_share`` can say FROM
    WHERE (#849). So:

      charge          = mortgage_balance + margin_available  (post-refinance;
                        apply_overlay already booked the advance on the
                        mortgage and shrank the pre-existing room by it)
      drawn           = mortgage_balance  (every borrowed dollar, booked once)
      revolving       = charge * revolving_share
      line_draw       = min(cash_out, revolving)   <- the surplus, line first
      mortgage_balance = drawn - line_draw

    Money is conserved by construction, for EVERY share: the household still
    owes exactly ``drawn``, and ``optimizer.py`` still invests exactly
    ``cash_out`` (``margin_available * 0.0 + cash_out``) while booking
    ``margin_draw_for_lump_sum(cash_out, revolving) = line_draw`` of it as
    HELOC debt. That is why ``run_mortgage_structure_exploration`` pins the
    draw fraction to 0.0 on a cash-out basis: at a fixed charge the draw is
    IMPLIED by the sourcing split, and sweeping it again would invest
    borrowed money twice. Any residual room (``revolving - line_draw``, when
    the structure carves out a line larger than the surplus) stays undrawn --
    real standby liquidity, correctly costing nothing.

    ``revolving_share`` of ``None`` (the no-structure-declared identity case)
    returns ``property_cfg`` untouched, same as ``apply_structure_overlay``
    (DP#13/DP#32).

    Raises:
        ChargeLimitExceededError: same two OSFI B-20 ceilings
            ``apply_structure_overlay`` enforces, from the same helper (DP#9).
    """
    property_cfg = deepcopy(property_cfg)
    # Issue #1075: a tranches-declared structure is applied by the tranche
    # machinery, not the share machinery -- see ``apply_structure_overlay``'s
    # identical routing. For the tranched form the sweep point's AMOUNTS
    # already ARE the post-refinance drawn/room split (the investment
    # tranche is the advance; the line holds the rest as room), so the
    # sourcing line-draw re-split does not apply -- the year-0 lump-sum
    # machinery (``margin_draw_for_lump_sum``) still prices how much of the
    # surplus is drawn from the line when a cash-out is invested.
    if structure.get('tranches') is not None:
        return _apply_tranched_structure(property_cfg, structure)

    revolving_share = structure.get('revolving_share')
    if revolving_share is None:
        return property_cfg

    house_value = property_cfg.get('house_value', 0.0)
    drawn = property_cfg.get('mortgage_balance', 0.0)
    # DP#32: explicit absence-testing, never `x or 0`. `margin_available`
    # absent means "no facility at all" (#663) -- a DIFFERENT state from a
    # declared facility with $0 room -- but the charge it contributes is 0
    # either way, which is the only thing the arithmetic below needs. A
    # `cash_out` of 0 is likewise a real value ("this option advances
    # nothing"), not an unset one.
    room = property_cfg.get('margin_available')
    room = 0.0 if room is None else room
    charge = drawn + room
    cash_out = property_cfg.get('cash_out')
    cash_out = 0.0 if cash_out is None else cash_out

    charge_ltv_limit = property_cfg.get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX)
    heloc_ltv_limit = property_cfg.get('heloc_ltv_limit', OSFI_B20_REVOLVING_LTV_MAX)
    combined_max = charge_limit(house_value, charge_ltv_limit)
    revolving_max = heloc_revolving_limit(house_value, heloc_ltv_limit)

    new_margin = charge * revolving_share
    line_draw = min(cash_out, new_margin)
    new_mortgage = drawn - line_draw

    _refuse_structure_over_charge(
        structure, revolving_share, house_value, charge,
        new_mortgage, new_margin, combined_max, revolving_max,
        charge_ltv_limit, heloc_ltv_limit)

    property_cfg['mortgage_balance'] = new_mortgage
    _write_structure_facility(property_cfg, structure, new_margin)
    return property_cfg


def apply_property_funding_overlay(cfg: dict, assignment: Dict[str, Dict]) -> dict:
    """Issue #1011: apply ONE candidate funding ASSIGNMENT to a config,
    returning a derived copy.

    ``assignment`` maps a property id to the funding OPTION that cell scores
    (one entry of ``scenario_discovery.discover_property_funding_cells``'s
    ``assignment``). For each named property, this REBUILDS the property's
    ``purchase.financing`` block (or removes it) and recomputes its
    ``net_equity``/``secured_share`` so the down payment the waterfall draws
    and the originated mortgage the fold services both reflect the chosen
    funding method -- then re-optimisation ranks each cell's result by the
    active objective (DP#22).

    ``all_cash``: no originated mortgage; ``net_equity`` becomes the full
    couple-share value (less any year-0 secured debt), so the whole value
    leaves the portfolio as the down payment -- byte-identical to #696's
    equity-financed behaviour.

    ``mortgage``: ``down_pct`` of value is the down payment (``net_equity``)
    drawn through the waterfall and a mortgage originates for the rest
    (``value * (1 - down_pct)``, couple share), serviced from the purchase
    year to payoff, interest deductible when the property is a rental. The
    financing block is built from the SAME ``_annual_amortization_schedule``
    the fixed-``financing`` mapper uses (DP#9: one schedule spelling), and
    the deductibility follows the property's ``kind`` -- exactly the rule
    ``input_contract._map_owned_properties`` applies to a fixed ``financing``.

    The recompute inputs (``value_share``, the non-financing ``secured_base``,
    ``owner_roles``, ``deductible``, ``projection_years``) are carried on the
    property's ``purchase.funding_recompute`` block by the mapper precisely so
    this overlay does NOT re-derive the owner structure or the horizon here
    (DP#9). A property the assignment does NOT name is left untouched -- a
    multi-property cross only changes the properties the cell chooses for.

    Absence-safe (DP#32): the base ``cfg`` a household with no
    ``funding_options`` loads carries no ``funding_recompute`` and is never
    passed through here -- the exploration is gated on a declaration, so the
    golden trajectory never reaches this function.
    """
    from input_contract import _annual_amortization_schedule

    cfg = deepcopy(cfg)
    props = cfg.get('properties', [])
    for prop in props:
        pid = prop.get('id')
        if pid not in assignment:
            continue
        option = assignment[pid]
        purchase = prop.get('purchase')
        if purchase is None or 'funding_recompute' not in purchase:
            # Not a fundable purchase (no declaration carried a recompute
            # block). Leave it -- the assignment named a property this cell
            # cannot refund, which is a caller bug, not a silent no-op, but
            # crashing mid-sweep would lose every other cell's result, so we
            # skip the named-but-not-fundable property and let the caller's
            # absence tests catch the wiring gap.
            continue
        rc = purchase['funding_recompute']
        value_share = prop['value_share']
        secured_base = rc['secured_base']
        pyear = purchase['year']
        method = option['method']
        if method == 'mortgage':
            financed_share = value_share * (1.0 - option['down_pct'])
            schedule = _annual_amortization_schedule(
                financed_share, option['rate'],
                option['amortization_years'], pyear,
                rc['projection_years'])
            purchase['financing'] = {
                'mortgage_amount': financed_share,
                'rate': option['rate'],
                'rate_type': option['rate_type'],
                'amortization_years': option['amortization_years'],
                'origination_year': pyear,
                'deductible': rc['deductible'],
                'owner_roles': rc['owner_roles'],
                'schedule': schedule,
            }
            new_secured = secured_base + financed_share
            # A rental's interest deduction reads the financing schedule off
            # the rental block (mirroring _map_owned_properties); keep the
            # reference in sync with the block we just materialized.
            rental = prop.get('rental')
            if rental is not None:
                rental['financing_schedule'] = schedule
        elif method == 'all_cash':
            purchase.pop('financing', None)
            new_secured = secured_base
            rental = prop.get('rental')
            if rental is not None:
                rental.pop('financing_schedule', None)
        else:
            # A method the schema's enum does not list cannot reach here from a
            # validated document, but a future schema addition that forgets an
            # overlay branch must NOT silently fall through to the unfunded
            # identity (the DP#32 trap: an unknown funding method defaulting to
            # the favourable "do nothing"). Fail loudly instead -- covered by
            # test_issue_1011's direct-overlay unknown-method test.
            raise ValueError(
                f"unknown funding method {method!r} for property {pid!r}")
        prop['secured_share'] = new_secured
        prop['net_equity'] = value_share - new_secured
    return cfg
