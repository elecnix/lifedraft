#!/usr/bin/env python3
"""
RESP Module — All government rules encoded

Implements:
- CESG (Canada Education Savings Grant): 20% basic + additional for low income
- QESI (Quebec Education Savings Incentive): 10% basic + supplementary
- Age-based eligibility (ends at calendar year turning 17)
- 16-17 contribution requirements
- Carry-forward of unused CESG room
- Lifetime limits ($7,200 CESG, $3,600 QESI, $50,000 contributions per child)
- Income-tested additional CESG and QESI rates
- Canada Learning Bond (CLB) eligibility

DP#20: All income thresholds and annual room amounts are year-versioned data.
DP#12: QESI has its own thresholds separate from CESG.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — RESP, CESG, CLB, QESI entries
    ITA s.146.1 (RESP), CESG Act
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-education-savings-grant-cesg.html
    https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/rc4092/registered-education-savings-plans-resps.html
    https://www.retraitequebec.gouv.qc.ca/en/epargne-etude/reee/qei/Pages/qei.aspx
"""
import logging

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, FrozenSet, List, Optional, Tuple


# ── Year-versioned CESG income thresholds (DP#20, DP#12) ─────────────────
# CESG additional rates apply based on family income against these thresholds.
# Indexed annually by CRA.
# Source 2024-2025: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-education-savings-grant-cesg.html
# Source 2026: projected from CRA indexing factor.
# Also available via TaxDataProvider (cesg_first_threshold, cesg_second_threshold).
CESG_THRESHOLDS: Dict[int, Dict[str, float]] = {
    2024: {'first_threshold': 55867, 'second_threshold': 111733},
    2025: {'first_threshold': 57375, 'second_threshold': 114750},
    2026: {'first_threshold': 58523, 'second_threshold': 117045},
}

# ── Year-versioned QESI income thresholds (DP#12) ───────────────────────
# QESI thresholds are set by Retraite Québec and differ from CESG thresholds.
# Source 2024-2025: https://www.retraitequebec.gouv.qc.ca/en/epargne-etude/reee/qei/Pages/qei.aspx
# Source 2025-2026: Retraite Québec annual indexing announcement.
# Also available via TaxDataProvider (qesi_first_threshold, qesi_second_threshold).
QESI_THRESHOLDS: Dict[int, Dict[str, float]] = {
    2024: {'first_threshold': 51780, 'second_threshold': 103545},
    2025: {'first_threshold': 53255, 'second_threshold': 106495},
    2026: {'first_threshold': 54345, 'second_threshold': 108680},
}

# ── Year-versioned CLB income thresholds (DP#20) ────────────────────────
# Key = number of children in household.
# 1 = 1–3 children, 4 = 4 children, 5 = 5+ children.
# Indexed annually. Source: Employment and Social Development Canada.
# Also available via TaxDataProvider (clb_threshold_1_3_children, etc.).
CLB_THRESHOLDS: Dict[int, Dict[int, float]] = {
    2024: {1: 55867, 4: 63036, 5: 70234},
    2025: {1: 57375, 4: 64733, 5: 72123},
    2026: {1: 58523, 4: 66078, 5: 73633},
}

# ── CESG annual room and contribution max (DP#20) ────────────────────────
# The contribution max changed in 2005: $2,000 → $2,500 (CESG Act amendment).
# The annual CESG room changed in 2007: $400/year → $500/year.
# These are separate change years because the 2005 amendment increased
# the matching contribution max but the annual grant room caught up in 2007.
# Children born before 2007 accumulate room at the rate applicable in each year.
CESG_ANNUAL_ROOM_CHANGE_YEAR = 2007
CESG_CONTRIBUTION_MAX_CHANGE_YEAR = 2005
# Issue #295 (DP#12/#20): CESG grant room accrues from 1998, the year the
# Canada Education Savings Grant began (ESDC, "Canada Education Savings
# Grant (CESG)"; InfoCapsule 12, grant room and carry-forward). A beneficiary
# born earlier accrues room only from 1998. Room is derived in code from the
# birth year, never read from input.
CESG_PROGRAM_START_YEAR = 1998
# The last age at which a beneficiary accrues CESG room and can be paid CESG:
# room accrues through the end of the calendar year the beneficiary turns 17.
CESG_LAST_ELIGIBLE_AGE = 17


def _nearest_year(data: dict, year: int) -> int:
    """Find the nearest available year in a year-keyed dict."""
    available = sorted(data.keys())
    if not available:
        raise ValueError("No year data available")
    nearest = min(available, key=lambda y: abs(y - year))
    if abs(nearest - year) > 1:
        logging.warning(
            f"Year {year} is {abs(nearest - year)} years from nearest available data "
            f"(year {nearest}); thresholds may be inaccurate"
        )
    return nearest


def get_cesg_thresholds(year: int, provider: 'TaxDataProvider' = None) -> Dict[str, float]:
    """Get CESG income thresholds for a given year (DP#20).

    Uses TaxDataProvider when available (DP#12), falls back to hardcoded dict.
    Returns a copy so callers cannot mutate the source data.
    """
    if provider is not None:
        data = provider.get_year_data(year, 'canada', 'quebec')
        if data and data.cesg_first_threshold > 0:
            return {'first_threshold': data.cesg_first_threshold,
                    'second_threshold': data.cesg_second_threshold}
    if year in CESG_THRESHOLDS:
        return dict(CESG_THRESHOLDS[year])
    nearest = _nearest_year(CESG_THRESHOLDS, year)
    return dict(CESG_THRESHOLDS[nearest])


def get_qesi_thresholds(year: int, provider: 'TaxDataProvider' = None) -> Dict[str, float]:
    """Get QESI income thresholds for a given year (DP#12).

    QESI thresholds are set by Retraite Québec and differ from CESG.
    Uses TaxDataProvider when available (DP#12), falls back to hardcoded dict.
    Returns a copy so callers cannot mutate the source data.
    """
    if provider is not None:
        data = provider.get_year_data(year, 'canada', 'quebec')
        if data and data.qesi_first_threshold > 0:
            return {'first_threshold': data.qesi_first_threshold,
                    'second_threshold': data.qesi_second_threshold}
    if year in QESI_THRESHOLDS:
        return dict(QESI_THRESHOLDS[year])
    nearest = _nearest_year(QESI_THRESHOLDS, year)
    return dict(QESI_THRESHOLDS[nearest])


def get_clb_thresholds(year: int, provider: 'TaxDataProvider' = None) -> Dict[int, float]:
    """Get CLB income thresholds for a given year (DP#20).

    Key = number of children. Uses TaxDataProvider when available (DP#12),
    falls back to hardcoded dict.
    Returns a copy so callers cannot mutate the source data.
    """
    if provider is not None:
        data = provider.get_year_data(year, 'canada', 'quebec')
        if data and data.clb_threshold_1_3_children > 0:
            return {1: data.clb_threshold_1_3_children,
                    4: data.clb_threshold_4_children,
                    5: data.clb_threshold_5plus_children}
    if year in CLB_THRESHOLDS:
        return dict(CLB_THRESHOLDS[year])
    nearest = _nearest_year(CLB_THRESHOLDS, year)
    return dict(CLB_THRESHOLDS[nearest])


def get_cesg_annual_room(year: int) -> float:
    """Get CESG annual room for a given year (DP#20).

    Returns $400 for years before 2007, $500 from 2007 onward.
    """
    return 400 if year < CESG_ANNUAL_ROOM_CHANGE_YEAR else 500


def get_cesg_contribution_max(year: int) -> float:
    """Get CESG contribution max for a given year (DP#20).

    Maximum contribution on which basic CESG (20%) is calculated.
    Returns $2,000 for years before 2005, $2,500 from 2005 onward.
    """
    return 2000 if year < CESG_CONTRIBUTION_MAX_CHANGE_YEAR else 2500


def cesg_basic_room_accrued(birth_year: int, through_year: int) -> float:
    """Basic CESG grant room accrued by a beneficiary from birth through the
    end of ``through_year`` (issue #295, DP#12/#20: a code table, not input).

    ESDC accrues the annual basic room (``get_cesg_annual_room``: $400 a year
    before 2007, $500 from 2007) for every calendar year from the later of the
    birth year and 1998 (the program start) through the year the beneficiary
    turns 17. Residence in Canada since birth is assumed (the contract carries
    no residency history before ``as_of``).
    """
    first = max(birth_year, CESG_PROGRAM_START_YEAR)
    last = min(through_year, birth_year + CESG_LAST_ELIGIBLE_AGE)
    return float(sum(get_cesg_annual_room(y) for y in range(first, last + 1)))


def cesg_carry_forward_room(child: 'RESPChild', year: int) -> float:
    """Unused basic CESG room carried into ``year`` (issue #295): the room
    accrued through the end of ``year - 1`` minus every basic CESG paid so far
    (declared history plus the grants the fold has paid), floored at 0.

    A child whose grant history was never declared (``grant_history is None``,
    the in-memory legacy path) has exactly 0 carry-forward: room cannot be
    derived from grants that were never stated, and it is disclosed through
    model_fidelity's ``resp_grant_history_not_declared`` entry.
    """
    if child.grant_history is None:
        return 0.0
    accrued = cesg_basic_room_accrued(child.birth_year, year - 1)
    return max(0.0, accrued - child.total_basic_cesg_received)


def _cesg_normal_annual_max(year: int, family_income: float) -> float:
    """Compute the normal (non-catchup) annual CESG maximum for a given year and income."""
    annual_room = get_cesg_annual_room(year)
    thresholds = get_cesg_thresholds(year)
    first_threshold = thresholds['first_threshold']
    second_threshold = thresholds['second_threshold']
    low_max = annual_room + 100
    mid_max = annual_room + 50
    if family_income <= first_threshold:
        return low_max
    elif family_income <= second_threshold:
        return mid_max
    return annual_room


@dataclass(frozen=True)
class RESPGrantHistory:
    """A beneficiary's declared RESP history as at the projection start
    (issue #295), from the ESDC or promoter beneficiary statement.

    Lifetime amounts are summed over every RESP naming the beneficiary.
    ``years_with_100_before_age_15`` counts the completed calendar years
    before the ``as_of`` year, up to the year the beneficiary turned 15, with
    at least $100 contributed -- the second prong of the CESG 16-17 test.
    Every field is required: there is no default history (DP#13/DP#32).
    """
    contributions_total: float
    contributions_before_age_15: float
    years_with_100_before_age_15: int
    cesg_basic_received: float
    cesg_additional_received: float
    qesi_received: float
    clb_received: float


@dataclass
class RESPChild:
    """A child beneficiary in an RESP.
    
    DP#28: Eligibility (province-based) is computed from province per year,
    not stored as a static boolean. The `is_quebec_resident` field is
    deprecated — use `is_quebec_resident_in(year)` instead.
    
    DP#16: Province is data from the config. When province='quebec' is present,
    QESI eligibility is automatically determined.
    """
    name: str
    birth_year: int  # Calendar year of birth (DP#1: store dates, not derived values)
    # Issue #295: the declared history this child was seeded from, or None
    # when it was never declared (the in-memory legacy path). REQUIRED -- no
    # default -- so no constructor can silently mean "undeclared" (DP#32).
    # Build children through resp_child_from_config, the one shared
    # constructor (DP#9).
    grant_history: Optional[RESPGrantHistory]
    province: str = 'quebec'  # DP#16: derived from config, not a boolean flag
    is_quebec_resident: bool = True  # Computed from province; prefer province parameter

    # Running lifetime counters: seeded from grant_history, then advanced by
    # the fold each year (#1046). total_cesg_received is the lifetime CESG
    # counter the $7,200 cap reads (basic + additional, CLB excluded);
    # total_additional_cesg_received is its additional-CESG part, so the basic
    # part (which consumes carry-forward room) is their difference.
    total_contributions: float = 0.0
    total_cesg_received: float = 0.0
    total_additional_cesg_received: float = 0.0
    total_qesi_received: float = 0.0
    total_clb_received: float = 0.0

    contribution_years: list = field(default_factory=list)
    total_before_age_15: float = 0.0
    # Completed calendar years before the projection start in which at least
    # $100 was contributed by the end of the year the child turned 15 (the
    # declared half of the 16-17 test's second prong; projection years are
    # counted from contribution_years).
    prior_years_with_100_before_age_15: int = 0

    @property
    def total_basic_cesg_received(self) -> float:
        """Lifetime basic CESG: the part of total_cesg_received that is not
        additional CESG. Basic CESG is what consumes grant room."""
        return self.total_cesg_received - self.total_additional_cesg_received

    def is_quebec_resident_in(self, year: int = None) -> bool:
        """DP#28: Quebec residency is computed from province, not a static boolean.
        
        Per DP#16: if province='quebec' is present, QESI eligibility
        is automatically determined. This allows province to change across
        simulation years (e.g., moving from Quebec to Ontario).
        
        Backward compat: if is_quebec_resident is explicitly set to False
        (not the default True), and province is still the default 'quebec',
        honor the explicit boolean. This handles code that constructs
        RESPChild(is_quebec_resident=False) without setting province.
        
        Args:
            year: Simulation year (reserved for future per-year province lookup)
        
        Returns:
            True if the child is a Quebec resident for the given year
        """
        # Backward compat: explicit is_quebec_resident=False overrides province default
        if not self.is_quebec_resident and self.province.lower() in ('quebec', 'qc'):
            # is_quebec_resident was explicitly set to False but province
            # defaulted to quebec — the caller intended non-Quebec
            return False
        # DP#16: auto-include when trigger data matches
        return self.province.lower() in ('quebec', 'qc')

    def age_in_year(self, year: int) -> int:
        """Calculate age in a given calendar year (DP#1: computed from birth_year)."""
        return year - self.birth_year

    def cesg_eligible(self, year: int) -> bool:
        """CESG is available until the end of the calendar year the beneficiary turns 17."""
        age = year - self.birth_year
        return age <= 17

    def cesg_16_17_eligible(self, year: int) -> bool:
        """For ages 16 and 17, must meet one of two conditions:
        1. Total contributions ≥ $2,000 before end of year turning 15
        2. At least $100/year contributions in 4+ years before end of year turning 15
        """
        age = year - self.birth_year
        if age < 16 or age > 17:
            return True

        if self.total_before_age_15 >= 2000:
            return True

        # Issue #295: the declared count covers completed calendar years
        # before the projection start, and contribution_years holds only
        # projection years, so no year is counted twice.
        years_with_100 = self.prior_years_with_100_before_age_15 + sum(
            1 for yr, amt in self.contribution_years
            if yr <= self.birth_year + 15 and amt >= 100)
        return years_with_100 >= 4


def resp_child_from_config(child_cfg: Dict, start_year: int,
                           default_province: str) -> RESPChild:
    """The ONE constructor of an ``RESPChild`` from a config child entry
    (issue #295, DP#9) -- used by FamilySimulation, the optimizer and
    ``analyze_resp_for_family`` alike, so the three cannot seed a child
    differently.

    Birth year: ``child_cfg['birth_year']`` when present, otherwise
    ``start_year - child_cfg['age']`` (age 0 is a newborn born in
    ``start_year``). A child with neither is refused: a birth year of 0 would
    make the child silently ineligible for every grant (DP#1/DP#28/DP#32).

    History: ``child_cfg['resp_history']`` -- the declared beneficiary
    history mapped from the contract -- seeds every lifetime counter. Every
    key is indexed, so a history missing a figure raises ``KeyError`` instead
    of defaulting to 0 (DP#13/DP#32). An absent or None history is the
    in-memory legacy path: zero counters, ``grant_history=None`` (no
    carry-forward room), disclosed by model_fidelity's
    ``resp_grant_history_not_declared``.
    """
    name = child_cfg.get('name', 'Child')
    if 'birth_year' in child_cfg:
        birth_year = child_cfg['birth_year']
    elif 'age' in child_cfg:
        birth_year = start_year - child_cfg['age']
    else:
        raise ValueError(
            f"RESP beneficiary {name!r} has neither 'birth_year' nor 'age': "
            f"its CESG/QESI eligibility cannot be dated (DP#1). Declare the "
            f"child's birth date.")
    province = child_cfg['province'] if 'province' in child_cfg else default_province
    history = child_cfg['resp_history'] if 'resp_history' in child_cfg else None
    child = RESPChild(
        name=name,
        birth_year=birth_year,
        grant_history=None,
        province=province,
        is_quebec_resident=(province.lower() in ('quebec', 'qc')),
    )
    if history is None:
        return child
    grant_history = RESPGrantHistory(
        contributions_total=history['contributions_total'],
        contributions_before_age_15=history['contributions_before_age_15'],
        years_with_100_before_age_15=history['years_with_100_before_age_15'],
        cesg_basic_received=history['cesg_basic_received'],
        cesg_additional_received=history['cesg_additional_received'],
        qesi_received=history['qesi_received'],
        clb_received=history['clb_received'],
    )
    child.grant_history = grant_history
    child.total_contributions = grant_history.contributions_total
    child.total_cesg_received = (grant_history.cesg_basic_received
                                 + grant_history.cesg_additional_received)
    child.total_additional_cesg_received = grant_history.cesg_additional_received
    child.total_qesi_received = grant_history.qesi_received
    child.total_clb_received = grant_history.clb_received
    child.total_before_age_15 = grant_history.contributions_before_age_15
    child.prior_years_with_100_before_age_15 = grant_history.years_with_100_before_age_15
    return child


@dataclass
class RESPCalculator:
    """Calculates CESG, QESI, and CLB based on official government rules.

    DP#20: All income thresholds are looked up by year from data dicts,
    not hardcoded constants. Use get_cesg_thresholds(year),
    get_qesi_thresholds(year), get_clb_thresholds(year).
    """

    # CESG rates (unchanged across years — thresholds shift, rates stay)
    CESG_BASIC_RATE: float = 0.20
    CESG_ADDITIONAL_LOW_RATE: float = 0.20
    CESG_ADDITIONAL_MID_RATE: float = 0.10
    CESG_LIFETIME_MAX: float = 7200

    # QESI rates
    QESI_BASIC_RATE: float = 0.10
    QESI_ANNUAL_CONTRIBUTION_MAX: float = 5000
    QESI_LIFETIME_MAX: float = 3600
    QESI_SUPPLEMENTARY_LOW_RATE: float = 0.10
    QESI_SUPPLEMENTARY_MID_RATE: float = 0.05

    # CLB
    CLB_INITIAL_PAYMENT: float = 500
    CLB_ANNUAL_PAYMENT: float = 100
    CLB_LIFETIME_MAX: float = 2000
    CLB_BIRTH_YEAR_MIN: int = 2004
    CLB_INCIDENTAL_EXPENSE: float = 25  # DP#12: One-time $25 incidental expense when first CLB is paid (issue #304)

    # RESP contribution limits
    RESP_LIFETIME_CONTRIBUTION_LIMIT: float = 50000
    RESP_EXCESS_TAX_RATE: float = 0.01  # DP#10: 1%/month penalty on excess contributions (issue #304)

    # The EAP payment limits (ITA s.146.1(2)(g.1)) are year-versioned data in
    # the module-level EAP_LIMITS table, read by eap_payment_limit() (#276).
    AIP_PENALTY_RATE: float = 0.20             # 20% additional tax on AIP (accumulated income payment)

    def calculate_cesg(self, contribution: float, child: RESPChild,
                       year: int, family_income: float) -> Dict:
        """CESG paid on ``contribution`` to ``child`` in ``year`` -- the ONE
        CESG calculation (issue #295, DP#9: the former catch-up variant had no
        production caller and is deleted).

        Rules (ESDC CESG page; InfoCapsule 12, grant room and carry-forward):

        * Additional CESG is 20% (income at or below the first threshold) or
          10% (at or below the second) of the first $500 contributed in the
          year. It is never carried forward.
        * Basic CESG is 20% of the contribution, limited by the child's basic
          grant room: this year's annual room plus the unused room carried
          forward (``cesg_carry_forward_room``), and never more than twice the
          annual room ($1,000 a year since 2007).
        * The total is limited by the $7,200 lifetime CESG maximum.

        A child whose grant history was never declared (``grant_history is
        None``) keeps the pre-#295 calculation exactly: no carry-forward, the
        basic grant on at most ``get_cesg_contribution_max`` of contribution,
        and the normal annual maximum.

        Returns ``basic_cesg`` and ``additional_cesg`` AFTER the lifetime cap,
        so a caller can advance the basic and additional counters separately;
        they always sum to ``total_cesg``. When the lifetime cap binds, the cut
        is taken from the basic part first. The split at the cap does not
        affect later grants, because nothing is paid after the lifetime cap.

        DP#20: income thresholds, annual room and contribution max are looked
        up by year.
        """
        if not child.cesg_eligible(year):
            return {
                'basic_cesg': 0,
                'additional_cesg': 0,
                'total_cesg': 0,
                'remaining_lifetime_cesg': max(0, self.CESG_LIFETIME_MAX - child.total_cesg_received),
                'eligible': False,
                'reason': f'Child age {year - child.birth_year} > 17'
            }

        age = year - child.birth_year
        if age >= 16 and not child.cesg_16_17_eligible(year):
            return {
                'basic_cesg': 0,
                'additional_cesg': 0,
                'total_cesg': 0,
                'remaining_lifetime_cesg': max(0, self.CESG_LIFETIME_MAX - child.total_cesg_received),
                'eligible': False,
                'reason': f'16-17 eligibility not met (need $2,000 total or $100/yr x 4 before age 15)'
            }

        remaining = self.CESG_LIFETIME_MAX - child.total_cesg_received
        if remaining <= 0:
            return {
                'basic_cesg': 0,
                'additional_cesg': 0,
                'total_cesg': 0,
                'remaining_lifetime_cesg': 0,
                'eligible': True,
                'reason': 'Lifetime CESG limit reached'
            }

        # DP#20: Year-versioned thresholds
        thresholds = get_cesg_thresholds(year)
        first_threshold = thresholds['first_threshold']
        second_threshold = thresholds['second_threshold']

        additional_cesg = 0
        if family_income <= first_threshold:
            additional_amount = min(contribution, 500)
            additional_cesg = additional_amount * self.CESG_ADDITIONAL_LOW_RATE
        elif family_income <= second_threshold:
            additional_amount = min(contribution, 500)
            additional_cesg = additional_amount * self.CESG_ADDITIONAL_MID_RATE

        annual_room = get_cesg_annual_room(year)
        if child.grant_history is None:
            # Undeclared history: the pre-#295 calculation, unchanged.
            contribution_max = get_cesg_contribution_max(year)
            basic_cesg = min(contribution, contribution_max) * self.CESG_BASIC_RATE
            total_cesg = min(basic_cesg + additional_cesg, remaining)
            total_cesg = min(total_cesg, _cesg_normal_annual_max(year, family_income))
        else:
            # Declared history: the basic grant can use this year's room plus
            # the unused room carried forward, up to twice the annual room.
            # accrued(year) - received == annual room + carry-forward whenever
            # the carry-forward is positive, and never exceeds the true room
            # otherwise.
            room = max(0.0, cesg_basic_room_accrued(child.birth_year, year)
                       - child.total_basic_cesg_received)
            basic_cesg = min(contribution * self.CESG_BASIC_RATE, room, 2 * annual_room)
            total_cesg = min(basic_cesg + additional_cesg, remaining)

        total_paid = round(total_cesg, 2)
        additional_paid = min(round(additional_cesg, 2), total_paid)
        return {
            'basic_cesg': round(total_paid - additional_paid, 2),
            'additional_cesg': additional_paid,
            'total_cesg': total_paid,
            'remaining_lifetime_cesg': round(remaining - total_cesg, 2),
            'eligible': True,
            'reason': None
        }

    def calculate_qesi(self, contribution: float, child: RESPChild,
                        year: int, family_income: float) -> Dict:
        """Calculate QESI (Québec Education Savings Incentive).

        DP#12: QESI uses its own income thresholds, separate from CESG.
        DP#20: Thresholds looked up by year.
        """
        if not child.is_quebec_resident_in(year):
            return {
                'basic_qesi': 0,
                'supplementary_qesi': 0,
                'total_qesi': 0,
                'remaining_lifetime_qesi': 0,
                'eligible': False,
                'reason': 'Not a Quebec resident'
            }

        if not child.cesg_eligible(year):
            return {
                'basic_qesi': 0,
                'supplementary_qesi': 0,
                'total_qesi': 0,
                'remaining_lifetime_qesi': self.QESI_LIFETIME_MAX - child.total_qesi_received,
                'eligible': False,
                'reason': f'Child age {year - child.birth_year} > 17'
            }

        remaining = self.QESI_LIFETIME_MAX - child.total_qesi_received
        if remaining <= 0:
            return {
                'basic_qesi': 0,
                'supplementary_qesi': 0,
                'total_qesi': 0,
                'remaining_lifetime_qesi': 0,
                'eligible': True,
                'reason': 'Lifetime QESI limit reached'
            }

        basic_qesi = min(contribution, self.QESI_ANNUAL_CONTRIBUTION_MAX) * self.QESI_BASIC_RATE
        basic_qesi = min(basic_qesi, 250)

        # DP#12: QESI uses its own thresholds, not CESG thresholds
        qesi_t = get_qesi_thresholds(year)
        qesi_first = qesi_t['first_threshold']
        qesi_second = qesi_t['second_threshold']

        supplementary_qesi = 0
        if family_income <= qesi_first:
            supplementary_qesi = min(contribution, 500) * self.QESI_SUPPLEMENTARY_LOW_RATE
            supplementary_qesi = min(supplementary_qesi, 50)
        elif family_income <= qesi_second:
            supplementary_qesi = min(contribution, 500) * self.QESI_SUPPLEMENTARY_MID_RATE
            supplementary_qesi = min(supplementary_qesi, 25)

        total_qesi = min(basic_qesi + supplementary_qesi, remaining)

        return {
            'basic_qesi': round(basic_qesi, 2),
            'supplementary_qesi': round(supplementary_qesi, 2),
            'total_qesi': round(total_qesi, 2),
            'remaining_lifetime_qesi': round(remaining - total_qesi, 2),
            'eligible': True,
            'reason': None
        }

    def calculate_clb(self, child: RESPChild, year: int,
                       family_income: float, num_children: int = 1) -> Dict:
        """Calculate Canada Learning Bond eligibility.

        DP#20: Income thresholds looked up by year with per-child-count granularity.
        """
        if child.birth_year < 2004:
            return {'clb_amount': 0, 'eligible': False, 'reason': 'Born before 2004'}

        age = year - child.birth_year
        if age >= 16:
            return {'clb_amount': 0, 'eligible': False, 'reason': 'Age 16+'}

        # DP#20: Year-versioned CLB thresholds with per-child-count granularity
        year_thresholds = get_clb_thresholds(year)

        # Map num_children to threshold key: 1-3 → key 1, 4 → key 4, 5+ → key 5
        if num_children <= 3:
            lookup_key = 1
        else:
            lookup_key = min(num_children, 5)
        if lookup_key not in year_thresholds:
            lookup_key = 1  # fallback to 1-child threshold
        income_threshold = year_thresholds[lookup_key]

        if family_income <= income_threshold:
            clb_this_year = 100
            incidental = 0.0
            if child.total_clb_received == 0:
                clb_this_year = 500
                incidental = self.CLB_INCIDENTAL_EXPENSE  # DP#12: One-time $25 for opening the RESP (issue #304)
            elif child.total_clb_received < self.CLB_LIFETIME_MAX:
                clb_this_year = min(100, self.CLB_LIFETIME_MAX - child.total_clb_received)
            else:
                clb_this_year = 0

            return {
                'clb_amount': clb_this_year,
                'incidental_expense': incidental,
                'total_clb_deposit': clb_this_year + incidental,
                'eligible': True,
                'income_eligible': True,
                'remaining_lifetime_clb': self.CLB_LIFETIME_MAX - child.total_clb_received - clb_this_year
            }
        else:
            return {'clb_amount': 0, 'eligible': False, 'reason': 'Income above CLB threshold'}

    def resp_contribution_check(self, contribution: float, child: RESPChild) -> Dict:
        """Check a contribution against the $50,000 per-beneficiary lifetime
        contribution limit (ITA s.146.1(1) "excess amount").

        ``contribution_allowed`` is the part that fits in the remaining
        lifetime room (never negative: a child already at or over the limit
        is allowed $0), and ``excess`` is the rest of THIS contribution. The
        fold contributes only the allowed part and redirects the excess
        (issue #295) -- it never silently drops it.
        """
        room = max(0.0, self.RESP_LIFETIME_CONTRIBUTION_LIMIT - child.total_contributions)

        if contribution > room:
            excess = contribution - room
            excess_tax = excess * self.RESP_EXCESS_TAX_RATE
            return {
                'contribution_allowed': room,
                'excess': excess,
                'excess_tax_per_month': excess_tax,
                'within_limits': False,
                'remaining_lifetime': 0
            }

        return {
            'contribution_allowed': contribution,
            'excess': 0,
            'excess_tax_per_month': 0,
            'within_limits': True,
            'remaining_lifetime': room - contribution
        }

    def grant_maximising_contribution(self, child: RESPChild, year: int) -> float:
        """The contribution to ``child`` in ``year`` that attracts the most
        basic CESG (issue #295): the year's CESG contribution max ($2,500),
        plus up to one more contribution max to use unused room carried
        forward -- counted only when the child can receive CESG this year
        (the 16-17 test) -- and never more than the child's remaining
        $50,000 lifetime contribution room.

        For a child with no declared history the carry-forward is 0, so this
        is exactly the contribution max, as before #295.
        """
        contribution_max = get_cesg_contribution_max(year)
        cap = contribution_max
        if child.cesg_16_17_eligible(year):
            carry_forward = cesg_carry_forward_room(child, year)
            if carry_forward > 0:
                cap = contribution_max + min(contribution_max,
                                             carry_forward / self.CESG_BASIC_RATE)
        # The cap keeps the table's own numeric type when nothing binds, so a
        # child with no declared history reproduces the pre-#295 allocation
        # exactly (the golden trajectory is compared value for value).
        lifetime_room = self.RESP_LIFETIME_CONTRIBUTION_LIMIT - child.total_contributions
        if lifetime_room < cap:
            return max(0.0, lifetime_room)
        return cap

    def household_grant_matched_cap(self, children: List[RESPChild], year: int) -> float:
        """The household's RESP allocation cap for ``year``: the sum of each
        CESG-eligible child's ``grant_maximising_contribution`` (issue #295).
        Read by StrategyEngine.allocate as ``FamilyState.resp_grant_matched_cap``.
        """
        return sum(self.grant_maximising_contribution(ch, year)
                   for ch in children if ch.cesg_eligible(year))

    def resp_collapse_proceeds(self, base_cfg: dict, n_mtr: float) -> dict:
        """Compute net proceeds from collapsing an RESP (no student enrolled)."""
        resp_balance, contributions, cesg, qesi, earnings = _resp_balance_and_composition(base_cfg)
        if resp_balance <= 0:
            return {'net_proceeds': 0.0, 'tax_cost': 0.0, 'grant_clawback': 0.0}

        grant_clawback = cesg + qesi

        # Issue #304: AIP (Accumulated Income Payment) — 20% additional tax
        # on earnings when RESP is collapsed without a student (ITA s.146.1).
        # Earnings are taxed at the beneficiary's MTR + 20% penalty rate.
        penalty_rate = self.AIP_PENALTY_RATE
        total_aip_rate = n_mtr + penalty_rate
        aip_tax = earnings * total_aip_rate

        net_proceeds = contributions + earnings - aip_tax

        return {
            'net_proceeds': round(net_proceeds, 2),
            'tax_cost': round(aip_tax, 2),
            'grant_clawback': round(grant_clawback, 2),
            'contributions_returned': round(contributions, 2),
            'earnings_taxed': round(earnings, 2),
            'effective_tax_rate': total_aip_rate,
        }

    def resp_eap_proceeds(self, base_cfg: dict) -> dict:
        """Compute net proceeds from normal EAP RESP withdrawals (student enrolled)."""
        resp_balance, contributions, cesg, qesi, earnings = _resp_balance_and_composition(base_cfg)
        if resp_balance <= 0:
            return {'net_proceeds': 0.0, 'tax_cost': 0.0}

        eap_portion = cesg + qesi + earnings
        student_mtr = base_cfg.get('assumptions', {}).get('resp_eap_tax_rate', 0.15)
        eap_tax = eap_portion * student_mtr

        net_proceeds = contributions + eap_portion - eap_tax

        return {
            'net_proceeds': round(net_proceeds, 2),
            'tax_cost': round(eap_tax, 2),
            'grant_clawback': 0.0,
            'contributions_returned': round(contributions, 2),
            'earnings_taxed': round(eap_portion, 2),
            'effective_tax_rate': student_mtr,
        }


# ── RESP wind-down (issue #578) ──────────────────────────────────────────
# An RESP does not compound forever: once the beneficiary reaches the study
# window, the plan is drawn down as EAPs (grants + earnings, taxable to the
# *student*) and PSE payments (contributions, tax-free to the *subscriber*).
# If the plan is never used for education, the grants are repaid and the
# earnings come out as an AIP taxed to the *subscriber* at their marginal
# rate plus a 20% penalty (ITA s.146.1). DP#28: the window is computed from
# the beneficiary's birth year plus a configurable start age/duration -- not
# a magic age hardcoded into the simulation fold.
# ── Declared approximations in the RESP wind-down (issue #585 / DP#32) ───
# An approximation that biases a headline figure must declare itself in the
# output, not hide in a docstring. #585's model-fidelity registry is not in
# the tree yet; when it lands, these two entries register there verbatim.
# Both are stated here, in structured form, so they are auditable *now*
# rather than being rediscovered later as "a confident, plausible, wrong
# number" (DP#32).
RESP_MODEL_APPROXIMATIONS = [
    {
        'id': 'resp_eap_student_tax_not_computed',
        'affects': 'RESP EAP tax cost',
        'direction': 'understates tax (assumes ~0%)',
        'description': (
            "EAPs are paid to the student and taxed in the student's hands. The "
            "engine models them as leaving the household untaxed, i.e. it assumes "
            "the basic personal amount plus tuition credits reduce the student's "
            "rate to ~0% -- which is the ordinary case, and the entire point of "
            "the vehicle. It is NOT computed from the student's bracket, so an "
            "unusually large single-year EAP, or a student with significant other "
            "income, is under-taxed by this model."
        ),
    },
    {
        'id': 'resp_pse_consumed_by_education',
        'affects': 'household terminal net worth',
        'direction': 'understates net worth (conservative)',
        'description': (
            "The tax-free return of contributions (PSE) is modelled as leaving "
            "household net worth along with the EAP -- i.e. the whole plan, "
            "including the subscriber's own returned capital, is assumed to be "
            "consumed by education. A family that instead pockets and reinvests "
            "the returned contributions would end with that amount MORE than this "
            "model reports. Surfaced per-year as YearResult.resp_pse_paid so the "
            "money is traceable, never silently vanished."
        ),
    },
    {
        'id': 'resp_eap_first_year_limit_is_calendar_year',
        'affects': 'RESP EAP timing; RESP AIP tax at collapse',
        'direction': 'understates the EAP payable in a capped year (conservative)',
        'description': (
            "ITA s.146.1(2)(g.1)(ii)(A) caps a full-time student's EAPs over the "
            "trailing 12 months only until they have been enrolled for 13 "
            "consecutive weeks in that period (issue #276). The engine is annual "
            "and study periods reach it as years, so the cap is applied to the "
            "WHOLE first calendar year of the study window, and to the whole "
            "first calendar year of any declared study period that starts in a "
            "calendar year after every earlier period ended (a restart in the "
            "very next calendar year is treated as a gap, too). The excess is "
            "deferred to later study years, or, when the window has closed, is "
            "priced through the AIP collapse. EAPs paid in the previous calendar "
            "year are not aggregated into the trailing 12 months. Both choices "
            "can only understate the EAP allowed in a year, never overstate it."
        ),
    },
    {
        'id': 'resp_eap_part_time_limit_not_modelled',
        'affects': 'RESP EAP timing for a part-time student',
        'direction': 'overstates the EAP allowed for a part-time student',
        'description': (
            "A student in a specified (part-time) educational program is capped "
            "at the specified-program amount per trailing 13-week period for as "
            "long as they study part-time (ITA s.146.1(2)(g.1)(ii)(B)). "
            "people[].study_periods[] carries no enrolment kind, so every "
            "declared period is treated as a qualifying (full-time) program, "
            "capped only in its first year. Follow-up: issue #281."
        ),
    },
]

RESP_DEFAULT_STUDY_START_AGE = 18
RESP_DEFAULT_STUDY_DURATION_YEARS = 4


def resp_study_window(birth_year: int,
                       study_start_age: int = RESP_DEFAULT_STUDY_START_AGE,
                       study_duration_years: int = RESP_DEFAULT_STUDY_DURATION_YEARS) -> tuple:
    """(first_year, last_year) of the beneficiary's RESP study window, inclusive.

    DP#1/DP#28: derived from birth_year -- never a bare hardcoded age check.
    """
    first_year = birth_year + study_start_age
    last_year = first_year + max(1, study_duration_years) - 1
    return first_year, last_year


def resp_study_window_for_child(child: dict, birth_year: int,
                                 study_start_age: int = RESP_DEFAULT_STUDY_START_AGE,
                                 study_duration_years: int = RESP_DEFAULT_STUDY_DURATION_YEARS) -> tuple:
    """(first_year, last_year) for ONE beneficiary, preferring the study window
    they actually DECLARED over the household-wide age assumption (issue #714).

    ``people[].study_periods[]`` -- the real dates a child starts and finishes
    school -- was parsed into ``child['study_periods']`` and then read by
    nobody: every beneficiary's RESP wound down on the GLOBAL
    ``assumptions.resp.study_start_age`` instead, so a child who starts at 19,
    or studies for six years, had their EAP/AIP schedule computed against a
    window they never told the engine to use. DP#1 (store dates, not derived
    values) and DP#28 (programs enter and exit on a schedule).

    The declared window spans the earliest start to the latest end, so a child
    with a CEGEP block and then a university block is in study for the whole
    span. Falls back to the age-derived window ONLY when nothing is declared --
    DP#13: a default is a fallback for absent input, never a way to overrule a
    value the household actually supplied.
    """
    resolved = _resolved_study_periods(child, study_duration_years)
    if not resolved:
        return resp_study_window(birth_year, study_start_age, study_duration_years)
    return min(start for start, _ in resolved), max(end for _, end in resolved)


def _resolved_study_periods(child: dict, study_duration_years: int) -> List[Tuple[int, int]]:
    """``(start_year, end_year)`` of every study period the child DECLARED,
    sorted by start year -- the ONE spelling of how a declared period's end is
    resolved, shared by the study window and the EAP-limit years (DP#9).

    Periods without a start year declare nothing and are skipped; an empty
    result means "nothing declared", and each caller falls back explicitly.

    An open-ended period (`end_date: null` -- "ongoing/unknown end", per the
    schema) must NOT read as "studies forever": that would keep the RESP in
    its tax-sheltered EAP window indefinitely and the AIP collapse -- grant
    repayment, subscriber tax, 20% penalty -- would never fire. That is a
    silent substitution in the FAVOURABLE direction, the exact defect class
    this codebase exists to prevent (DP#32). An unknown end is instead priced
    at the declared start plus the declared study duration: both are inputs,
    neither is an opinion baked into code (DP#2).
    """
    periods = child.get('study_periods') if child else None
    if not periods:
        return []
    return sorted(
        (p['start_year'],
         p['end_year'] if p.get('end_year') is not None
         else p['start_year'] + max(1, study_duration_years) - 1)
        for p in periods if p.get('start_year') is not None
    )


def default_resp_composition(resp_balance: float) -> Dict[str, float]:
    """Default contributions/CESG/QESI/earnings split for a balance whose
    real composition is unknown (issue #304's convention: a maxed-out Quebec
    RESP is roughly 50% contributions, 10% CESG, 5% QESI, 35% earnings).

    Used only as a fallback when ``accounts.resp_composition`` is absent from
    input; real composition data always wins (DP#13).
    """
    return {
        'total_contributions': resp_balance * 0.50,
        'total_cesg_received': resp_balance * 0.10,
        'total_qesi_received': resp_balance * 0.05,
        'investment_earnings': resp_balance * 0.35,
    }


def _resolve_resp_composition(composition: Dict, resp_balance: float) -> tuple:
    """Return the ``(contributions, cesg, qesi, earnings)`` tuple that prices a
    withdraw/collapse, from ONE spelling (DP#9).

    ``default_resp_composition`` is the single documented fallback for a
    balance whose composition is ABSENT (an estimation convention, DP#13 --
    real-tracked data always wins). Issue #95 (DP#19): the EAP path used to
    fall back to a DIFFERENT flat 50/50 split, inventing a cost basis that
    disagreed with the collapse path's own 50/10/5/35 convention -- two
    spellings of one rule. Routing both through this helper and
    ``default_resp_composition`` means a balance with no composition is priced
    identically everywhere (DP#9), and a positive composition that reaches the
    fold is honoured as-is, never overwritten by a fabricated split (DP#19/
    DP#32: tracked cost basis wins; absence is the only fallback trigger).
    """
    contributions = composition.get('total_contributions', 0)
    cesg = composition.get('total_cesg_received', 0)
    qesi = composition.get('total_qesi_received', 0)
    earnings = composition.get('investment_earnings', 0)
    if contributions + cesg + qesi + earnings == 0:
        default = default_resp_composition(resp_balance)
        return (default['total_contributions'], default['total_cesg_received'],
                default['total_qesi_received'], default['investment_earnings'])
    return contributions, cesg, qesi, earnings


def _resp_balance_and_composition(base_cfg: Dict) -> tuple:
    """Resolve ``(resp_balance, contributions, cesg, qesi, earnings)`` from a
    config, the ONE preamble shared by the EAP and collapse pricing methods
    (DP#9: both read the balance and its tracked composition identically).

    Extracted so the two methods do not duplicate the accounts-read + zero-
    guard + composition-resolution opening (a clone-detection finding on the
    single-spelling refactor). The zero-balance EARLY RETURN stays in each
    method because its empty-payload dict differs (collapse returns
    grant_clawback; EAP returns a bare pair) -- only the shared read is
    unified here.
    """
    accounts = base_cfg.get('accounts', {})
    resp_balance = accounts.get('resp_current_balance', 0)
    contributions, cesg, qesi, earnings = _resolve_resp_composition(
        accounts.get('resp_composition', {}), resp_balance)
    return resp_balance, contributions, cesg, qesi, earnings


# ── EAP payment limit (issue #276, ITA s.146.1(2)(g.1)) ──────────────────
# The plan may not pay an educational assistance payment (EAP) unless either
#   (ii)(A) the student is enrolled in a QUALIFYING educational program and
#       (I) has been so enrolled throughout at least 13 consecutive weeks in
#           the 12-month period ending at the time of the payment, or
#       (II) the payment plus every other EAP made to them in that 12-month
#           period does not exceed the qualifying amount below; or
#   (ii)(B) the student is 16 or older, enrolled in a SPECIFIED (part-time)
#       educational program, and the payment plus every other EAP made to them
#       in the 13-week period ending at the time of the payment does not
#       exceed the specified amount below.
# In both cases the Minister designated under the Canada Education Savings
# Act (ESDC) may approve a greater amount in writing. That approval is NOT
# modelled: the engine enforces the statutory amounts.
#
# So the full-time cap is a FIRST-13-WEEKS rule, not an annual one, and it is
# not pro-rated by the length of a term: the pro-rating that
# RESPCalculator.eap_payment_limit() applied (#304) is in no version of the
# statute and is deleted (DP#9).
#
# DP#12/DP#20: the amounts are year-versioned data keyed by the date the
# statute put them in force, each row citing its primary source. The
# figures are not indexed; a row stays in force until the next one.
EAP_LIMITS: List[Dict] = [
    {
        'effective_date': date(2007, 1, 1),
        'qualifying_12mo_before_13wk': 5000.0,
        'specified_per_13wk': 2500.0,
        'source': (
            "ITA s.146.1(2)(g.1)(ii)(A)(II) and (ii)(B) as replaced by S.C. 2007, "
            "c. 29 (Budget Implementation Act, 2007), s. 18(4); s. 18(8): applies "
            "to the 2007 and subsequent taxation years. "
            "https://laws-lois.justice.gc.ca/eng/AnnualStatutes/2007_29/FullText.html ; "
            "point-in-time text "
            "https://laws-lois.justice.gc.ca/eng/acts/I-3.3/section-146.1-20171214.html "
            "(retrieved 2026-09-23)"
        ),
    },
    {
        'effective_date': date(2023, 3, 28),
        'qualifying_12mo_before_13wk': 8000.0,
        'specified_per_13wk': 4000.0,
        'source': (
            "ITA s.146.1(2)(g.1)(ii)(A)(II) and (ii)(B) as replaced by S.C. 2023, "
            "c. 26 (Budget Implementation Act, 2023, No. 1), s. 39(2)-(3); "
            "s. 39(4): deemed in force March 28, 2023. "
            "https://laws-lois.justice.gc.ca/eng/AnnualStatutes/2023_26/FullText.html ; "
            "current text https://laws-lois.justice.gc.ca/eng/acts/I-3.3/section-146.1.html "
            "(retrieved 2026-09-23)"
        ),
    },
]

_EAP_PROGRAM_KEYS = {
    'qualifying': 'qualifying_12mo_before_13wk',
    'specified': 'specified_per_13wk',
}


def eap_payment_limit(year: int, program: str) -> float:
    """Statutory EAP limit for a calendar year (ITA s.146.1(2)(g.1)(ii)).

    ``program`` is ``'qualifying'`` -- the amount a full-time student may be
    paid over the trailing 12 months before they have been enrolled for 13
    consecutive weeks, clause (ii)(A)(II) -- or ``'specified'`` -- the amount a
    part-time student may be paid per trailing 13-week period, clause (ii)(B).
    Any other value raises: there is no default program (DP#32).

    Returns the LOWEST amount in force on any day of ``year``, so the year an
    amendment takes effect gets the conservative figure, never the favourable
    one. A year that begins before the earliest sourced row raises, naming the
    year and the earliest covered date: an unsourced year is never silently
    given a neighbouring year's law (the nearest-year trap). A year after
    the latest row gets the latest row: the amounts are not indexed, and a
    statute stays in force until it is amended.

    The Minister's written approval of a greater amount is not modelled.
    """
    if not isinstance(program, str) or program not in _EAP_PROGRAM_KEYS:
        raise ValueError(
            f"EAP program must be one of {sorted(_EAP_PROGRAM_KEYS)} "
            f"(ITA s.146.1(2)(g.1)(ii)); got {program!r}")
    key = _EAP_PROGRAM_KEYS[program]
    year_start, year_end = date(year, 1, 1), date(year, 12, 31)
    earliest = EAP_LIMITS[0]['effective_date']
    if year_start < earliest:
        raise ValueError(
            f"No sourced EAP limit covers {year}: EAP_LIMITS begins "
            f"{earliest.isoformat()}. Add the statute in force for {year} "
            f"(ITA s.146.1(2)(g.1)) with its source; do not reuse a later year's law.")
    in_force = [
        row[key] for idx, row in enumerate(EAP_LIMITS)
        if row['effective_date'] <= year_end
        and (idx + 1 == len(EAP_LIMITS) or EAP_LIMITS[idx + 1]['effective_date'] > year_start)
    ]
    return min(in_force)


def eap_limited_years(child: dict, first_year: int, study_duration_years: int) -> FrozenSet[int]:
    """Calendar years in which a full-time student cannot be presumed to have
    been enrolled for 13 consecutive weeks in the trailing 12 months, so the
    qualifying-program EAP limit applies (ITA s.146.1(2)(g.1)(ii)(A)).

    Always ``first_year`` (the first year of the study window), plus the start
    year of every declared study period that begins in a calendar year after
    every earlier period has ended -- a restart after a gap. A period starting
    in the year an earlier one ends is contiguous and is not capped. Dates
    reach the engine as years, so a restart in the very next calendar year is
    treated as a gap: that can only understate the EAP allowed, never
    overstate it (declared as ``resp_eap_first_year_limit_is_calendar_year``).
    """
    limited = {first_year}
    prior_end = None
    for start, end in _resolved_study_periods(child, study_duration_years):
        if prior_end is not None and start > prior_end:
            limited.add(start)
        prior_end = end if prior_end is None else max(prior_end, end)
    return frozenset(limited)


class EAPLimitExceeded(ValueError):
    """A planned EAP exceeds the statutory limit of ITA s.146.1(2)(g.1)."""


def assert_eap_within_limit(eap: float, eap_limit: Optional[float], *,
                             child_index: int, calendar_year: int) -> None:
    """Refuse an EAP above the statutory limit -- never clamp it (DP#32).

    ``eap_limit`` None means no statutory cap applies this year. The payout
    path calls this on WHATEVER schedule produced the draw, so a schedule that
    ignores the limit crashes the run instead of silently overpaying.
    """
    if eap_limit is not None and eap > eap_limit + 0.005:
        raise EAPLimitExceeded(
            f"RESP child index {child_index}: the planned EAP of ${eap:,.2f} in "
            f"{calendar_year} exceeds the statutory limit of ${eap_limit:,.2f} "
            f"(ITA s.146.1(2)(g.1)(ii)). A greater amount is payable only with "
            f"the written approval of the Minister designated under the Canada "
            f"Education Savings Act (ESDC), which this engine does not model.")


def resp_annual_withdrawal(contributions: float, cesg: float, qesi: float,
                            earnings: float, years_remaining: int, *,
                            eap_limit: Optional[float]) -> Dict[str, float]:
    """Split this year's RESP withdrawal so the plan drains to (near) zero by
    the end of the study window, instead of compounding forever (#578).

    Evenly spreads each composition bucket over the years remaining
    (including this one), so a plan entered mid-way through its window still
    finishes on schedule. Returns tax-free PSE (contributions returned) and
    taxable EAP (CESG + QESI + earnings, taxed in the *student's* hands) as
    separate totals, plus the per-bucket amounts withdrawn (DP#19: cost
    basis, not a blended balance, drives what is taxed).

    ``eap_limit`` is REQUIRED (no default, so no caller can forget it): the
    statutory EAP limit for this year (``eap_payment_limit``), or None when
    no statutory cap applies. When the even-spread EAP exceeds it, the three
    EAP buckets are scaled down pro rata so the EAP equals the limit (#276);
    the unpaid EAP stays in the plan and is re-spread over the years that
    remain -- a lawful deferral, not a loss. PSE is never capped.
    """
    years_remaining = max(1, years_remaining)
    w_contrib = contributions / years_remaining
    w_cesg = cesg / years_remaining
    w_qesi = qesi / years_remaining
    w_earnings = earnings / years_remaining
    eap = w_cesg + w_qesi + w_earnings
    if eap_limit is not None and eap > eap_limit:
        scale = eap_limit / eap
        w_cesg *= scale
        w_qesi *= scale
        w_earnings *= scale
        eap = w_cesg + w_qesi + w_earnings
    return {
        'pse': w_contrib,
        'eap': eap,
        'contributions_withdrawn': w_contrib,
        'cesg_withdrawn': w_cesg,
        'qesi_withdrawn': w_qesi,
        'earnings_withdrawn': w_earnings,
    }


def resp_collapse_aip(cesg: float, qesi: float, earnings: float,
                       subscriber_marginal_rate: float,
                       aip_penalty_rate: float = RESPCalculator.AIP_PENALTY_RATE) -> Dict[str, float]:
    """Collapse an RESP that was not used for education (#578).

    CESG/QESI grants are repaid to the government (never received by the
    family). Investment earnings come out as an Accumulated Income Payment
    (AIP), taxable to the *subscriber* at their marginal rate plus a 20%
    additional tax (ITA s.146.1). Contributions are returned tax-free
    separately by the caller -- they are not part of this computation.
    """
    grant_repayment = cesg + qesi
    aip_tax = earnings * (subscriber_marginal_rate + aip_penalty_rate)
    return {
        'grant_repayment': round(grant_repayment, 2),
        'aip_tax': round(aip_tax, 2),
        'net_aip': round(earnings - aip_tax, 2),
    }


def analyze_resp_for_family(cfg: Dict) -> Dict:
    """Analyze RESP situation for the family based on input.json data."""
    calc = RESPCalculator()

    children = cfg['family'].get('children', [])
    family_income = sum(m['gross_income'] for m in cfg['family']['members'])
    for ch in children:
        family_income += ch.get('gross_income', 0)

    resp_balance = cfg['accounts']['resp_current_balance']

    ref_year = cfg.get('assumptions', {}).get('start_year', 2026)
    cesg_t = get_cesg_thresholds(ref_year)
    qesi_t = get_qesi_thresholds(ref_year)
    clb_t = get_clb_thresholds(ref_year)
    contrib_max = get_cesg_contribution_max(ref_year)
    annual_room = get_cesg_annual_room(ref_year)

    # Issue #92 (DP#16): the household's province is DATA from the config
    # (cfg['tax']['province'], the internal-shape location load_and_map
    # writes), NEVER fabricated as 'quebec'. QESI eligibility turns on where
    # the child actually RESIDES -- a Quebec assumption forced onto an
    # Ontario household silently over-credits QESI. Absence stays the
    # historical default 'quebec' (DP#32: an absent key falls back to the
    # prior behaviour). A present-but-empty tax block and an absent one are
    # both read via dict.get's default -- never `x or DEFAULT` (which would
    # conflate "empty" with "absent" and is the fault line DP#32 exists to
    # police).
    tax_block = cfg.get('tax', {})
    province = tax_block.get('province')
    province = province if province else 'quebec'

    results = {}

    for i, ch in enumerate(children):
        name = ch.get('name', f'Child {i+1}')
        # Issue #295: the one shared constructor (DP#9) -- it reads the
        # child's birth_year (not only 'age') and its declared grant history,
        # so this report agrees with the fold on every lifetime counter.
        child = resp_child_from_config(ch, ref_year, province)
        birth_year = child.birth_year
        age = ref_year - birth_year

        eligible = child.cesg_eligible(ref_year)
        age_16_17_eligible = child.cesg_16_17_eligible(ref_year)

        cesg_result = calc.calculate_cesg(contrib_max, child, ref_year, family_income)
        qesi_result = calc.calculate_qesi(contrib_max, child, ref_year, family_income)

        clb_result = calc.calculate_clb(child, ref_year, family_income, len(children))

        total_matching = cesg_result['total_cesg'] + qesi_result['total_qesi']
        pct_on_max = total_matching / contrib_max * 100 if contrib_max > 0 else 0

        results[name] = {
            'age': age,
            'birth_year': birth_year,
            'province': province,  # issue #92: surface the household's province used
            'cesg_eligible': eligible,
            'cesg_16_17_eligible': age_16_17_eligible if age >= 16 else 'N/A (under 16)',
            'cesg_basic_rate': f"{calc.CESG_BASIC_RATE*100:.0f}%",
            'cesg_additional_eligible': cesg_result.get('additional_cesg', 0) > 0,
            'cesg_annual_max': f"${cesg_result.get('total_cesg', 0):.0f}/yr" if eligible else "N/A (over 17)",
            'cesg_lifetime_remaining': f"${max(0, calc.CESG_LIFETIME_MAX - child.total_cesg_received):,.0f}",
            'qesi_eligible': child.is_quebec_resident_in(ref_year) and eligible,
            'qesi_annual_max': f"${qesi_result.get('total_qesi', 0):.0f}/yr" if (child.is_quebec_resident_in(ref_year) and eligible) else "N/A",
            'qesi_lifetime_remaining': f"${max(0, calc.QESI_LIFETIME_MAX - child.total_qesi_received):,.0f}",
            'total_matching_on_max': f"{total_matching:.0f}/yr ({pct_on_max:.0f}% on first ${contrib_max:,.0f})" if eligible else "N/A",
            '_total_matching_num': total_matching if eligible else 0,
            'clb_eligible': clb_result.get('eligible', False),
            'clb_amount': f"${clb_result.get('clb_amount', 0):.0f}/yr" if clb_result.get('eligible') else "Not eligible",
            'lifetime_contribution_limit': f"${calc.RESP_LIFETIME_CONTRIBUTION_LIMIT:,.0f}",
            'remaining_contribution_room': f"${max(0.0, calc.RESP_LIFETIME_CONTRIBUTION_LIMIT - child.total_contributions):,.0f}",
            'notes': []
        }

        if not eligible:
            results[name]['notes'].append(f"⚠️ AGE {age}: No longer eligible for CESG/QESI (must be ≤ 17)")
            results[name]['notes'].append("Can still contribute up to $50,000 lifetime, but NO matching grants")
            results[name]['notes'].append("Existing RESP balance with grants can still grow tax-free")
            results[name]['notes'].append("EAP withdrawals taxed in student's hands at low rate")
        elif age >= 16:
            results[name]['notes'].append(f"⚠️ AGE {age}: Must meet 16-17 contribution requirements for CESG")
            results[name]['notes'].append(f"16-17 eligible: {age_16_17_eligible}")
            if not age_16_17_eligible:
                results[name]['notes'].append("Need $2,000 total contributions OR $100/yr for 4 years before age 15")
        else:
            results[name]['notes'].append(f"✅ Eligible for CESG + QESI through end of {birth_year + 17}")
            remaining_years = 18 - age
            results[name]['notes'].append(f"Years remaining for grants: ~{remaining_years}")
            total_grants_possible = min(cesg_result.get('total_cesg', 500) + qesi_result.get('total_qesi', 250), 750) * remaining_years
            results[name]['notes'].append(f"Potential grants over remaining years: ~${total_grants_possible:,.0f}")

        if family_income > cesg_t['second_threshold']:
            results[name]['notes'].append(f"💡 At family income ${family_income:,.0f}, you get basic CESG only (20%)")
            results[name]['notes'].append(f"   No additional CESG — that's for families earning ≤ ${cesg_t['second_threshold']:,.0f}")
            results[name]['notes'].append(f"   Quebec QESI adds 10% on first $5,000 for basic credit")
            results[name]['notes'].append(f"   Total: 30% match on first ${contrib_max:,.0f}/yr (20% federal + 10% Quebec)")

        if clb_result.get('eligible'):
            results[name]['notes'].append(f"💡 CLB eligible: ${clb_result.get('clb_amount', 0):.0f}/yr (no contributions needed!)")
        else:
            clb_1_threshold = clb_t[1]
            if family_income > clb_1_threshold:
                results[name]['notes'].append(f"💡 Not CLB-eligible (family income > ${clb_1_threshold:,.0f})")

    def _child_cesg_eligible(ch: Dict) -> bool:
        by = ch.get('birth_year', 0)
        if by > 0:
            return (ref_year - by) <= 17
        return ch.get('age', 0) <= 17

    eligible_children = [ch for ch in children if _child_cesg_eligible(ch)]
    ineligible_children = [ch for ch in children if not _child_cesg_eligible(ch)]

    max_annual_contribution = contrib_max * len(eligible_children)

    # Compute actual total matching per child from calculated results
    total_matching_total = sum(
        results[ch.get('name', f'Child {i+1}')]['_total_matching_num']
        for i, ch in enumerate(children)
        if ch.get('name', f'Child {i+1}') in results
        and '_total_matching_num' in results[ch.get('name', f'Child {i+1}')]
    )

    # Remove internal helper from per-child output (DP#3: no leaked internals)
    for name in results:
        if isinstance(results[name], dict):
            results[name].pop('_total_matching_num', None)

    # Compute matching percentage dynamically from actual grant amounts and contrib_max
    # (DP#3: no hardcoded percentages — derive from year-versioned data)
    cesg_basic_on_max = RESPCalculator.CESG_BASIC_RATE * contrib_max
    qesi_basic_on_max = min(contrib_max, RESPCalculator.QESI_ANNUAL_CONTRIBUTION_MAX) * RESPCalculator.QESI_BASIC_RATE
    qesi_basic_capped = min(qesi_basic_on_max, 250)  # QESI basic annual cap

    if family_income <= cesg_t['first_threshold']:
        cesg_additional = min(contrib_max, 500) * RESPCalculator.CESG_ADDITIONAL_LOW_RATE
        qesi_supp = min(contrib_max, 500) * RESPCalculator.QESI_SUPPLEMENTARY_LOW_RATE
        qesi_supp = min(qesi_supp, 50)
        cesg_rate_str = '20% basic + 20% additional (low income)'
        qesi_rate_str = '10% basic + 10% supplementary (low income)'
    elif family_income <= cesg_t['second_threshold']:
        cesg_additional = min(contrib_max, 500) * RESPCalculator.CESG_ADDITIONAL_MID_RATE
        qesi_supp = min(contrib_max, 500) * RESPCalculator.QESI_SUPPLEMENTARY_MID_RATE
        qesi_supp = min(qesi_supp, 25)
        cesg_rate_str = '20% basic + 10% additional (mid income)'
        qesi_rate_str = '10% basic + 5% supplementary (mid income)'
    else:
        cesg_additional = 0
        qesi_supp = 0
        cesg_rate_str = '20% basic only'
        qesi_rate_str = '10% basic'

    total_matching_on_max = cesg_basic_on_max + cesg_additional + qesi_basic_capped + qesi_supp
    matching_pct = round(total_matching_on_max / contrib_max * 100)

    # Build important_notes dynamically from actual children data
    important_notes = []

    for ch in ineligible_children:
        ch_name = ch.get('name', 'Child')
        ch_age = ch.get('age', 0)
        important_notes.append(
            f"⚠️ {ch_name} (age {ch_age}) is PAST CESG age limit — no more matching grants possible"
        )
        important_notes.append(
            f"💡 {ch_name}'s RESP can still grow tax-free and be withdrawn as EAP (taxed in student's hands)"
        )

    for i, ch in enumerate(eligible_children):
        ch_name = ch.get('name', f'Child {i+1}')
        ch_age = ch.get('age', 0)
        birth_yr = ref_year - ch_age
        remaining_years = 18 - ch_age
        if remaining_years > 0:
            important_notes.append(
                f"✅ {ch_name} (age {ch_age}) has ~{remaining_years} years of CESG/QESI eligibility remaining (through {birth_yr + 17})"
            )

    if family_income <= cesg_t['first_threshold']:
        important_notes.append(
            f"💡 At ${family_income:,.0f} family income, you qualify for basic CESG (20%) + additional CESG (20%) + QESI (10%+10%) = {matching_pct}% on first ${contrib_max:,.0f}"
        )
    elif family_income <= cesg_t['second_threshold']:
        important_notes.append(
            f"💡 At ${family_income:,.0f} family income, you qualify for basic CESG (20%) + additional CESG (10%) + QESI (10%+5%) = {matching_pct}% on first ${contrib_max:,.0f}"
        )
    else:
        important_notes.append(
            f"💡 At ${family_income:,.0f} family income, you qualify for basic CESG (20%) + QESI (10%) = {matching_pct}% on first ${contrib_max:,.0f}"
        )
        important_notes.append(
            f"💡 You do NOT qualify for Additional CESG (that's for income ≤ ${cesg_t['second_threshold']:,.0f})"
        )

    important_notes.append(
        f"💡 {matching_pct}% matching is ONLY on the first ${contrib_max:,.0f}/year per eligible child"
    )
    important_notes.append(
        f"💡 Contributions beyond ${contrib_max:,.0f}/year earn NO additional matching (but grow tax-free)"
    )

    for ch in eligible_children:
        ch_name = ch.get('name', 'Child')
        ch_age = ch.get('age', 0)
        remaining_years = 18 - ch_age
        if remaining_years > 0:
            important_notes.append(
                f"💡 {ch_name}: Contribute ${contrib_max:,.0f}/year for {remaining_years} more years = ${contrib_max * remaining_years:,.0f} in contributions"
            )
            important_notes.append(
                f"💡 {ch_name}: Catch-up available — can contribute up to ${contrib_max * 2:,.0f} and get up to double CESG per year"
            )

    important_notes.append("💡 RESP lifetime contribution limit: $50,000 per child (NOT per year)")
    important_notes.append("💡 Contributions are NOT tax-deductible (unlike RRSP)")
    important_notes.append("💡 EAP withdrawals taxed in student's hands at their (low) tax rate")
    important_notes.append("💡 60% of EAP is taxable (grants + earnings), 40% is tax-free (contributions returned)")

    results['family_summary'] = {
        'family_income': family_income,
        'income_tier': f"High (>${cesg_t['second_threshold']:,.0f})" if family_income > cesg_t['second_threshold']
                        else f"Middle (${cesg_t['first_threshold']:,.0f}-${cesg_t['second_threshold']:,.0f})" if family_income > cesg_t['first_threshold']
                        else f"Low (≤${cesg_t['first_threshold']:,.0f})",
        'cesg_rate': cesg_rate_str,
        'qesi_rate': qesi_rate_str,
        'total_matching_rate': f'~{matching_pct}% on first ${contrib_max:,.0f}/child/year',
        'max_annual_contribution_for_matching': f"${max_annual_contribution:,.0f}/yr",
        'max_annual_grants': f"${total_matching_total:,.0f}/yr",
        'resp_balance': resp_balance,
        'cesg_lifetime_per_child': f"${calc.CESG_LIFETIME_MAX:,.0f}",
        'qesi_lifetime_per_child': f"${calc.QESI_LIFETIME_MAX:,.0f}",
        'clb_lifetime_per_child': f"${calc.CLB_LIFETIME_MAX:,.0f}" if family_income <= clb_t[1] else "Not eligible",
        'contribution_lifetime_per_child': f"${calc.RESP_LIFETIME_CONTRIBUTION_LIMIT:,.0f}",
        'important_notes': important_notes,
    }

    return results


def print_resp_report(results: Dict):
    """Print a formatted RESP analysis report."""
    print("\n" + "=" * 100)
    print("🏫 RESP ANALYSIS — GOVERNMENT GRANTS & ELIGIBILITY")
    print("=" * 100)

    summary = results.pop('family_summary')

    print(f"\n  👨‍👩‍👧‍👧 Family Income: ${summary['family_income']:,.0f} ({summary['income_tier']})")
    print(f"  📍 Quebec Resident: Yes (QESI eligible)")
    print(f"  📊 Matching Rate: {summary['total_matching_rate']}")
    print(f"  💰 Max annual contribution for full matching: {summary['max_annual_contribution_for_matching']}")
    print(f"  🎁 Max annual grants: {summary['max_annual_grants']}")

    for name, info in results.items():
        print(f"\n  {'─' * 80}")
        print(f"  👤 {name} (age {info['age']})")
        print(f"     CESG eligible: {'✅ Yes' if info['cesg_eligible'] else '❌ NO — past age 17'}")
        if info['cesg_16_17_eligible'] != 'N/A (under 16)':
            print(f"     16-17 eligible: {'✅ Yes' if info['cesg_16_17_eligible'] else '❌ No'}")
        print(f"     CESG rate: {info['cesg_basic_rate']} basic" +
              (f" + {info['cesg_additional_eligible']} additional" if info.get('cesg_additional_eligible') else "") +
              (f" → {info['cesg_annual_max']}" if info['cesg_eligible'] else ""))
        print(f"     QESI rate: 10% basic" +
              (f" → {info['qesi_annual_max']}" if info.get('qesi_eligible') else ""))
        print(f"     Total matching: {info['total_matching_on_max']}")
        print(f"     Lifetime limits: CESG {info['cesg_lifetime_remaining']}, QESI {info['qesi_lifetime_remaining']}")
        print(f"     Contribution limit: {info['lifetime_contribution_limit']} lifetime per child")
        print(f"     Remaining contribution room: {info['remaining_contribution_room']}")

        if info['notes']:
            print(f"\n     Notes:")
            for note in info['notes']:
                print(f"       {note}")

    print(f"\n  {'═' * 80}")
    print(f"  📋 FAMILY SUMMARY & ACTION ITEMS")
    print(f"  {'═' * 80}")
    for note in summary['important_notes']:
        print(f"     {note}")

    print()


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="RESP Grant Eligibility Calculator")
    parser.add_argument("--input", default="input.json")
    args = parser.parse_args()

    # Epic #603 Track C Phase 2b: args.input is a contract document (the
    # sole wire format, validated). load_and_map maps it to the internal
    # shape analyze_resp_for_family reads (cfg['family']/cfg['accounts']).
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    import input_contract
    cfg = input_contract.load_and_map(args.input)
    results = analyze_resp_for_family(cfg)
    print_resp_report(results)
