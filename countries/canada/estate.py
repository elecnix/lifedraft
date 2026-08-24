#!/usr/bin/env python3
"""Estate / deemed-disposition after-tax legacy (Canada).

At death, the Income Tax Act (ITA s.70(5), s.146(8.8)) triggers a *deemed
disposition*:

  - RRSP/RRIF/LIF/LIRA: the entire fair market value is included as ordinary
    income on the deceased's final (terminal) return — unless it rolls to a
    surviving spouse (ITA s.146(8.1)), in which case tax is deferred until the
    second death, when BOTH pots land on ONE return.
  - Non-registered: capital property is deemed sold at FMV; accrued gains are
    realized and taxed on the final return — unless it rolls to the surviving
    spouse at cost (ITA s.70(6)), deferring the whole gain to the second death.
  - TFSA: passes tax-free (no deemed-disposition income) under either
    designation — see ``EstatePlan.tfsa_successor_holder``.
  - Principal residence: gains are exempt (ITA s.40(2)(b)) — but ONLY for a
    property actually *designated* as the principal residence for the years in
    question. A second property (cottage, rental) is ordinary capital property
    and its accrued gain IS taxed.
  - Life insurance: the death benefit is received tax-free (ITA s.148(1)).

## Epic #603 Track C Phase 2c: elections are DECLARED, not assumed

This module used to take eight loose scalars and *silently assume* five things
the input contract could not express — every one of them resolving in the
favourable direction (issue #600, measured by agent Inky in PR #616):

  1. A spousal rollover was assumed on the first death, and NOT rolling over
     was literally inexpressible.
  2. The TFSA was assumed to pass to a successor holder (shelter survives)
     rather than a beneficiary (shelter ends).
  3. Non-registered gains were split **50/50** across the two terminal
     returns — a hardcoded guess over what is now (post-#613) the single
     largest tax base in the estate.
  4. The single ``property`` block was assumed to be the principal residence,
     and therefore to attract the s.40(2)(b) exemption.
  5. House FMV silently fell back to $0 when absent (DP#32, again).

All five are now **inputs** (``EstatePlan``), sourced from the input contract's
``estate`` namespace and its ``accounts[]``/``properties[]`` designations.

### The rollover election, and why the old default was backwards

The old docstring claimed the split-across-two-returns base case *was* the
spousal-rollover case. It is the opposite, and this module's own test suite
already proved it (``test_splitting_registered_between_spouses_lowers_tax``:
two $300k terminal returns are taxed less than one $600k return, because the
brackets are progressive and each return runs them from $0):

  - **Rollover elected** (``spousal_rollover=True``): the first-to-die's
    registered plan rolls tax-free into the survivor's. At the survivor's death
    there is ONE terminal return carrying BOTH pots, taxed as a single lump
    through the progressive brackets. Bracket compression makes this cost
    **more** total tax, not less — the rollover buys *deferral*, not exemption.
  - **Rollover declined** (``spousal_rollover=False``): each spouse's
    registered balance is included on their OWN terminal return. Two separate
    progressive runs from $0. Less total tax — which is exactly why #600 says
    the election "is sometimes deliberately declined, to use up a low-income
    spouse's brackets."

So the pre-Phase-2c code was computing the *declined-rollover* arithmetic while
documenting it as the *elected-rollover* case. Both are now real, selectable
and correct, and the dollar difference between them is quantified in the Phase
2c PR body.

This module owns the *estate* arithmetic as small pure functions (DP#3) so the
jurisdiction-agnostic engine stays clean (DP#10). It does NOT model probate fees
(minimal in Quebec — no probate tax), trusts, or lifetime gifting.

References:
    ITA s.70(5) — deemed disposition of capital property at death
    ITA s.70(6) — spousal rollover of capital property at cost
    ITA s.146(8.1)/(8.8) — RRSP/RRIF refund-of-premiums rollover / income inclusion
    ITA s.40(2)(b) — principal residence exemption
    ITA s.148(1) — life insurance death benefit received tax-free
    CRA T4011 — Preparing Returns for Deceased Persons
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


class EstateInputError(ValueError):
    """Raised when the estate inputs are incoherent — never defaulted past
    (DP#32: absence fails loudly, it does not become a plausible zero)."""


@dataclass(frozen=True)
class EstatePlan:
    """The estate ELECTIONS and designations, declared rather than assumed.

    Every field here was, before epic #603 Track C Phase 2c, a silent
    assumption baked into ``compute_estate``'s arithmetic (issue #600). The
    three money-moving fields have **no defaults** on purpose: a caller that
    has not decided cannot accidentally inherit the favourable answer. The one
    place these are built from real config is ``objective.plan_from_config``,
    which sources them from the input contract's ``estate`` namespace.

    ## The one general model: elections decide WHICH RETURN each asset lands on

    A deemed disposition always produces (at most) two terminal returns — the
    primary's and the spouse's. What the rollover election actually does is move
    the **first-to-die's** assets from their own return onto the **survivor's**.
    So ``compute_estate`` does not branch on "rollover vs not"; it taxes two
    returns whose *contents* these fields decide. Both extremes, and every
    partial case in between, fall out of the same arithmetic:

      - full rollover      → ``rolled_fraction=1.0``: the first-to-die's return
        is empty, the survivor's carries everything → ONE combined progressive
        run → bracket compression → MORE tax.
      - rollover declined  → ``rolled_fraction=0.0``: each return carries its
        own owner's assets → two progressive runs from $0 → LESS tax.
      - partial (a per-account ``rollover_overrides`` entry disagreeing with
        the household default — the shipped example does exactly this on the
        spousal RRSP) → ``0 < rolled_fraction < 1``, which no single boolean
        could have expressed, and which the pre-Phase-2c model would have had
        to silently drop.

    Attributes:
        spousal_rollover: the HOUSEHOLD election
            (``estate.default_spousal_rollover``), carried for reporting — it is
            what an output surface states. The arithmetic reads the two
            ``*_rolled_fraction`` fields below, which already fold in any
            per-account ``rollover_overrides``.
        primary_dies_first: from ``assumptions.mortality`` — decides who the
            survivor is, and therefore whose terminal return the rolled assets
            land on. Previously not modelled at all (there was no way to say who
            dies when; #600).
        registered_rolled_fraction: fraction (0..1) of the FIRST-TO-DIE's
            registered pot that actually rolls to the survivor (ITA s.146(8.1)).
            Derived from the household election plus any per-account overrides,
            weighted by balance.
        non_reg_rolled_fraction: the same, for the first-to-die's share of the
            non-registered pot (ITA s.70(6) — capital property rolls at cost).
        tfsa_successor_holder: True = a spouse is named *successor holder* and
            the TFSA stays sheltered in their hands. False = the TFSA passes to
            a *beneficiary* and the shelter ends at death. Both are tax-free at
            the death-date VALUE (the ITA is explicit on this), so at a
            point-in-time terminal valuation the dollar figures are identical;
            what differs is what happens to growth AFTER the death date, which a
            point-in-time estate snapshot does not reach. Consumed and reported
            (see ``EstateResult.tfsa_shelter_ends``) rather than silently
            assumed — and the residual modelling gap is DECLARED in
            model_fidelity.py (``tfsa_beneficiary_post_death_growth``) rather
            than papered over with a fabricated number.
        non_reg_primary_share: fraction (0..1) of the non-registered FMV **and**
            ACB actually OWNED by the primary; the remainder is the spouse's.
            Derived from the real ``accounts[kind=non_reg].owner`` declarations
            (including joint ``pct`` splits), replacing the hardcoded 50/50
            guess over what is now the estate's largest tax base.
        life_insurance_death_benefit: total face amount of in-force policies on
            the deceased(s), received tax-free (ITA s.148(1)).
        taxable_property_fmv: FMV of real property that is NOT the principal
            residence (a cottage, a rental) — ordinary capital property. Its
            VALUE is added to the gross estate and its accrued GAIN is taxed.
        taxable_property_acb: ACB of that same non-principal property.
        principal_residence_designated: whether the principal residence is
            actually *designated* for the exemption (ITA s.40(2)(b)). True =
            its gain is exempt (the old silent assumption). False = it is
            ordinary capital property and its accrued gain IS taxed — the
            realistic case for a family whose designation years went to a
            cottage instead.
        principal_residence_fmv: GROSS FMV of the principal residence (not net
            of the mortgage). Used ONLY as the gain base when
            ``principal_residence_designated`` is False — the residence's
            *value* reaches the gross estate via ``compute_estate``'s
            ``house_equity`` argument, so counting it here too would
            double-count it.
        principal_residence_acb: ACB of the principal residence. Only
            meaningful when it is not designated.
    """
    spousal_rollover: bool
    tfsa_successor_holder: bool
    non_reg_primary_share: float
    primary_dies_first: bool = False
    registered_rolled_fraction: float = 0.0
    non_reg_rolled_fraction: float = 0.0
    life_insurance_death_benefit: float = 0.0
    taxable_property_fmv: float = 0.0
    taxable_property_acb: float = 0.0
    # Real property has its OWN ownership split -- a couple can own the house
    # 50/50 while one of them holds the whole non-registered account. Defaulting
    # this to the non-reg share would be exactly the kind of quiet
    # cross-contamination between two unrelated facts that #595 is about.
    property_primary_share: float = 0.5
    principal_residence_designated: bool = True
    principal_residence_fmv: float = 0.0
    principal_residence_acb: float = 0.0
    # Issue #963 (epic #956 bite F): the principal residence's REAL annual
    # appreciation rate, carried from the contract's principal property by
    # ``contract_estate._map_estate`` so this plan is self-describing (the
    # estate's property data carries its own appreciation, not a pointer to
    # the ``property`` block). The estate's deemed-disposition
    # (``objective._estate_call_args``) compounds ``principal_residence_fmv``
    # by ``(1 + rate) ** (terminal_cal_year - start_year)`` using this rate --
    # the terminal year is a simulation-result fact, so the compounding lives
    # in the objective layer, not the mapper. ``None`` (the default) = a
    # static value: the objective layer's absence-test returns
    # ``principal_residence_fmv`` unchanged and never reads a rate, so a
    # household that declares no appreciation (incl. the golden fixture) is
    # byte-identical to today (DP#32). A negative rate is honored (a falling
    # market is a real scenario a sell/keep sweep must be robust to), so this
    # field is NOT in the ``__post_init__`` non-negativity check below.
    principal_residence_appreciation_rate: Optional[float] = None
    # Issue #694 (epic #690 bite 3): CCA recapture on the deemed disposition of a
    # rental property at death (ITA s.13(1)). This is the previously-claimed CCA
    # clawed back as ORDINARY income (100% inclusion) -- computed by the caller
    # from the terminal UCC the fold tracked, at the COUPLE level (like
    # ``taxable_property_fmv``), and taxed here stacked on the terminal return's
    # ordinary income, apportioned by ``property_primary_share`` and rolled by the
    # property rollover fraction exactly like the property's capital gain. The
    # capital GAIN on the same property is NOT here -- it is already in the
    # ``taxable_property_*`` gain (DP#9: recapture and gain are distinct bases).
    # 0.0 for a household with no CCA election (inert -- byte-identical estate).
    cca_recapture: float = 0.0

    # Issue #695 (epic #690 bite 4): the per-property, per-year principal-residence
    # exemption. When None (the default, and every household that does NOT contest
    # the exemption across two properties), the gain tax uses the legacy
    # ``principal_residence_designated`` bool + aggregate ``taxable_property_*``
    # path below -- byte-identical. When present, it is the family's real-property
    # gains as a list of ``{fmv, acb, taxable_fraction, is_principal}`` dicts, each
    # gain already apportioned by the s.40(2)(b) exemption the family designated to
    # it (``taxable_fraction`` in [0, 1]); this list REPLACES the legacy property
    # handling (both the principal bool and the aggregate) -- one property gets the
    # exemption for a given year, and the year ranges finally move the tax (#601).
    property_gains: Optional[Tuple[Dict, ...]] = None

    def __post_init__(self):
        for name in ('non_reg_primary_share', 'property_primary_share',
                     'registered_rolled_fraction', 'non_reg_rolled_fraction'):
            value = getattr(self, name)
            if not (0.0 <= value <= 1.0):
                raise EstateInputError(
                    f"{name} must be a fraction in [0, 1], got {value!r}."
                )
        for name in ('taxable_property_fmv', 'taxable_property_acb',
                     'principal_residence_fmv', 'principal_residence_acb',
                     'life_insurance_death_benefit', 'cca_recapture'):
            if getattr(self, name) < 0:
                raise EstateInputError(f"{name} must be a non-negative magnitude.")
        if self.property_gains is not None:
            for gain in self.property_gains:
                frac = gain['taxable_fraction']
                if not (0.0 <= frac <= 1.0):
                    raise EstateInputError(
                        f"property_gains taxable_fraction must be in [0, 1], "
                        f"got {frac!r} for {gain.get('id')!r}.")
                if gain['fmv'] < 0 or gain['acb'] < 0:
                    raise EstateInputError(
                        "property_gains fmv/acb must be non-negative magnitudes.")


@dataclass
class EstateResult:
    """After-tax estate broken out by source (all $ at the projection horizon)."""
    tfsa: float = 0.0                    # passes tax-free (both designations)
    house_equity: float = 0.0            # DESIGNATED principal residence — tax-free
    registered_gross: float = 0.0        # RRSP/RRIF/LIF/LIRA (both spouses)
    registered_tax: float = 0.0          # deemed-disposition income tax
    non_reg_gross: float = 0.0           # non-registered FMV
    non_reg_tax: float = 0.0             # capital-gains tax on accrued gains
    # Issue #1031: the Smith-Manoeuvre investment SLEEVE -- a leveraged non-reg
    # portfolio financed by the readvanceable HELOC, tracked separately from
    # ``non_reg_balance``. At death it is ordinary capital property (ITA s.70(5))
    # whose accrued gain (FMV - ACB) is taxed at the capital-gains inclusion
    # rate, exactly like the non-reg pot above; the HELOC that financed it is
    # already in ``debts`` (subtracted from the gross estate). Pre-#1031 the
    # estate subtracted the HELOC debt but IGNORED this asset, understating a
    # leveraged household's estate by the whole SM portfolio net of its
    # deemed-disposition tax. 0.0 for a household with no SM sleeve (the golden
    # household) -- inert (DP#32).
    sm_investment_gross: float = 0.0     # SM portfolio FMV (separate sleeve)
    sm_investment_tax: float = 0.0       # capital-gains tax on its accrued gain
    debts: float = 0.0                   # non-mortgage debt outstanding

    # ── epic #603 Phase 2c: previously unrepresentable, now real ──
    life_insurance: float = 0.0          # tax-free death benefit (ITA s.148(1))
    taxable_property_gross: float = 0.0  # UNdesignated real property FMV
    taxable_property_tax: float = 0.0    # capital-gains tax on its accrued gain
    # Issue #694 (epic #690 bite 3): tax on CCA recaptured at the rental's deemed
    # disposition -- ordinary income (100% inclusion), stacked on the terminal
    # return. Distinct from taxable_property_tax (the 50%-inclusion capital gain
    # on the same property). 0.0 with no CCA election.
    cca_recapture_tax: float = 0.0

    # Issue #1035: the Quebec investment-expense carry-forward (TA s.336.0.1)
    # RELEASED at the deemed disposition -- the terminal return's investment
    # income (taxable gains on the non-reg + SM pots) absorbs what the annual
    # cap stranded, reducing the QUEBEC slice of the death tax. A tax RELIEF
    # (>= 0): subtracted in ``total_tax``. 0.0 for households with no
    # carry-forward -> byte-identical (DP#32).
    qc_carryforward_relief: float = 0.0

    # The elections that produced these numbers, carried on the result so an
    # output surface can STATE them rather than leave the reader to assume
    # (DP#32 / #585: an assumption that moves the headline must be surfaced).
    spousal_rollover: bool = False
    tfsa_shelter_ends: bool = False      # True when a BENEFICIARY, not a successor holder

    @property
    def gross_estate(self) -> float:
        """Estate before deemed-disposition tax, net of debt."""
        return (self.tfsa + self.house_equity + self.registered_gross
                + self.non_reg_gross + self.sm_investment_gross
                + self.life_insurance
                + self.taxable_property_gross - self.debts)

    @property
    def total_tax(self) -> float:
        return (self.registered_tax + self.non_reg_tax
                + self.sm_investment_tax
                + self.taxable_property_tax + self.cca_recapture_tax
                - self.qc_carryforward_relief)

    @property
    def net_estate(self) -> float:
        """After-tax estate actually passing to heirs.

        UNFLOORED at zero by design (issue #1065): a household that dies
        INSOLVENT -- financial assets spent to zero with a debt still
        outstanding -- has ``debts > assets`` here, so ``gross_estate`` and
        therefore ``net_estate`` go NEGATIVE. The floor is the OBJECTIVE's
        job, not the balance sheet's: clamping here would hide insolvency
        (making "dies owing $200k" indistinguishable from "dies clean")
        instead of pricing it. ``objective._neg_after_tax_estate`` reads
        ``insolvency`` below to price the negative without fabricating a
        bonus. The bug is LIVE on main (an unsecured ``personal_loan`` (#763)
        + a principal sale (#956/#964) + ``liquidate_to_target`` (#1009)
        terminates at ``net_estate = -50,000``), so the unfloored balance
        sheet is telling the truth, and the objective must not launder it.
        """
        return self.gross_estate - self.total_tax

    @property
    def drawable_after_tax(self) -> float:
        """After-tax value of every estate pot EXCEPT the designated principal
        residence -- the spend-down surface the die-with-zero objective ranks
        on (issue #1081).

        "Die with zero" means the household's SPENDABLE savings are consumed,
        not that the balance-sheet residual is minimised. Two consequences,
        both load-bearing for ``objective._neg_after_tax_estate``:

        - The residence is OUTSIDE the spend-down target: it is consumed by
          living in it, so keeping it is not failing to spend down. A home-
          sale strategy that converts the residence into portfolio cash (and
          then spends it) is modelled by #956/#964 -- a sold principal
          contributes neither value nor debt here, so it drops out of this
          quantity naturally, with no special-casing.
        - DEBT must never cancel ASSETS in the score. ``net_estate`` nets
          them dollar-for-dollar, which let a strategy that borrows and does
          not repay buy one point of score per borrowed dollar while total
          assets stayed flat (#1081's central gradient). This property ADDS
          the debt back onto the asset side (``net_estate + debts``), so the
          score can price debt SEPARATELY as a pure penalty; the mortgage is
          already netted inside ``house_equity`` and therefore outside the
          spend-down surface with the residence itself.

        Identity (one spelling -- derived, never recomputed by callers):

            drawable_after_tax == net_estate + debts - house_equity

        Every non-residence pot enters after its own deemed-disposition tax
        (registered as ordinary income, non-reg/SM/taxable property at the
        capital-gains inclusion, CCA recapture stacked); the residence is
        PRE-exempt so no residence tax term exists to subtract.
        """
        return self.net_estate + self.debts - self.house_equity

    @property
    def insolvent(self) -> bool:
        """True when debt outlives the assets at death -- a negative
        ``net_estate`` (issue #1065). The predicate ``insolvency`` (below) is
        built on, so the objective's penalty has ONE spelling of the fact."""
        return self.net_estate < 0.0

    @property
    def insolvency(self) -> float:
        """The dollar depth of terminal insolvency: ``|net_estate|`` when
        insolvent, ``0.0`` when solvent (issue #1065). Read by
        ``objective._neg_after_tax_estate`` to price the insolvency penalty
        from a named field rather than magic arithmetic. ``-2|net_estate|`` =
        ``-|net_estate|`` (distance from zero) ``- |net_estate|`` (this
        penalty); the 2:1 ratio MOVES (not eliminates) the tie that rejects
        ``-abs(net_estate)`` -- see the objective's docstring and
        ``TestInsolvencyCrossover``."""
        return -self.net_estate if self.insolvent else 0.0

    @property
    def effective_tax_rate(self) -> float:
        """Estate tax as a fraction of the pre-tax estate (0 if none)."""
        g = self.gross_estate
        return self.total_tax / g if g > 0 else 0.0


def tax_on_registered_at_death(registered_balance: float, brackets: list) -> float:
    """Income tax on a registered balance fully included on a terminal return.

    The whole RRSP/RRIF/LIF/LIRA FMV is ordinary income in the year of death, so
    it is taxed through the progressive brackets from $0 (the terminal return
    typically has little other income). Uses the same progressive
    ``tax_on_income`` as the rest of the engine (DP#10).

    Args:
        registered_balance: FMV of registered accounts on the final return.
        brackets: combined federal+provincial brackets (tax_calculator format).

    Returns:
        Income tax owed (>= 0).
    """
    if registered_balance <= 0:
        return 0.0
    from tax_calculator import tax_on_income
    return tax_on_income(registered_balance, brackets)


def tax_on_capital_gain_at_death(fmv: float, acb: float, brackets: list,
                                 other_income: float,
                                 inclusion_rate: float = 0.5,
                                 taxable_fraction: float = 1.0) -> float:
    """Capital-gains tax on accrued gains of capital property at death.

    Capital property (non-registered investments, undesignated real property)
    is deemed sold at FMV; the accrued gain (FMV − ACB) is included at
    ``inclusion_rate`` and stacks on top of ``other_income`` (e.g. the
    registered inclusion already on the return). The tax returned is the
    *incremental* tax of adding the taxable portion to that other income, so it
    is priced at the marginal band it actually lands in rather than from $0.

    Args:
        fmv: fair market value at death.
        acb: adjusted cost base.
        brackets: combined brackets.
        other_income: income already on the terminal return (registered lump +
            any gain already stacked), so this gain is taxed at the right band.
        inclusion_rate: capital-gains inclusion (default 0.5).
        taxable_fraction: the fraction of the accrued gain that is TAXABLE, i.e.
            not sheltered by the principal-residence exemption (issue #695,
            ITA s.40(2)(b)). 1.0 = fully taxable (the default, and every
            non-PRE call); 0.0 = fully exempt. A partial value is the ITA's
            per-year apportionment when the family's one exemption is shared
            across two properties.

    Returns:
        Incremental capital-gains tax (>= 0).
    """
    gain = max(0.0, fmv - acb) * taxable_fraction
    if gain <= 0:
        return 0.0
    from tax_calculator import tax_on_income
    taxable = gain * inclusion_rate
    return (tax_on_income(other_income + taxable, brackets)
            - tax_on_income(other_income, brackets))


# The non-registered case is the SAME arithmetic: capital property is capital
# property, whether it is an ETF or a cottage. Modelling it twice would be
# exactly the clone this repo's own dupdelta detector exists to catch (DP#9),
# so this is an alias, not a copy. Kept because callers and tests reference the
# older, narrower name.
tax_on_nonreg_gains_at_death = tax_on_capital_gain_at_death


def after_tax_networth_of_own_accounts(*, rrsp: float, tfsa: float, fhsa: float,
                                       non_reg_fmv: float, non_reg_acb: float,
                                       brackets: list,
                                       inclusion_rate: float = 0.5) -> float:
    """After-tax net worth of ONE member's OWN accounts under a deemed
    disposition (epic #841 bite 4 -- the family objective values every member,
    not just the two adults).

    A single member with no surviving spouse to roll to: their whole registered
    balance (RRSP/RRIF) is ordinary income on their own terminal return
    (ITA s.146(8.8)), the non-registered accrued gain (FMV - ACB) is taxed at
    ``inclusion_rate`` stacking on top of that registered income (ITA s.70(5)),
    and TFSA passes tax-free. This reuses the SAME two deemed-disposition
    primitives ``compute_estate`` uses (``tax_on_registered_at_death`` and
    ``tax_on_capital_gain_at_death``) -- it does NOT define a second spelling of
    the death-tax arithmetic (DP#9), so a child member is taxed on exactly the
    rules the adults' estate is.

    **FHSA is treated tax-free here and NO new tax is invented for it.** An
    FHSA withdrawn for a qualifying first home is tax-free on both ends (the
    one doubly-advantaged registered account), which is the life-cycle outcome
    for the young savers this bite adds; and ``compute_estate`` itself models
    no FHSA death-tax at all. Counting FHSA at face value is therefore the
    treatment CONSISTENT with the existing estate handling, not a new rule --
    the alternative (taxing an unused FHSA as income at death) would be the
    invention this bite is forbidden from making.

    Args:
        rrsp: the member's registered (RRSP/RRIF/FHSA-excluded) balance, taxed
            as ordinary income at death.
        tfsa: the member's TFSA balance (tax-free).
        fhsa: the member's FHSA balance (tax-free, see above).
        non_reg_fmv: the member's non-registered fair market value.
        non_reg_acb: the member's non-registered adjusted cost base.
        brackets: combined federal+provincial brackets (tax_calculator format).
        inclusion_rate: capital-gains inclusion rate (default 0.5).

    Returns:
        After-tax net worth (>= 0 for non-negative inputs): the sum of the
        balances less the registered income tax and the non-registered
        capital-gains tax.
    """
    registered_tax = tax_on_registered_at_death(rrsp, brackets)
    non_reg_tax = tax_on_capital_gain_at_death(
        non_reg_fmv, non_reg_acb, brackets,
        other_income=rrsp, inclusion_rate=inclusion_rate)
    return (rrsp + tfsa + fhsa + non_reg_fmv) - registered_tax - non_reg_tax


@dataclass(frozen=True)
class TerminalReturn:
    """One member's contribution to the estate, as a single terminal return.

    A deemed disposition produces one terminal return per member who dies with
    assets (ITA s.70(5), s.146(8.8)). ``compute_estate`` sums the deemed-
    disposition tax over a LIST of these, in DEATH ORDER (first-to-die first),
    so a household of any size -- a couple, or a three-generation chain -- is
    expressed as N terminal returns rather than a hardcoded two (#705).

    Attributes:
        registered: this member's RRSP/RRIF/LIF/LIRA FMV in ABSOLUTE dollars,
            included as ordinary income on their terminal return before any
            rollover reallocation (``compute_estate`` rolls the elected fraction
            forward onto the next return).
        non_reg_share: this member's SHARE (0..1) of the HOUSEHOLD non-registered
            pot. Carried as a share, not absolute dollars, because
            ``compute_estate`` multiplies it by the household non_reg total only
            at tax time -- which is what keeps the two-member case byte-identical
            to the closed-form first/second split it generalises: in IEEE-754,
            ``a*(s - s*r)`` is not bit-equal to ``(a*s) - (a*s)*r``.
        property_share: this member's SHARE (0..1) of the household real-property
            pot, carried the same way and following the same capital-property
            rollover election (ITA s.70(6)).
    """
    registered: float = 0.0
    non_reg_share: float = 0.0
    property_share: float = 0.0


def couple_terminal_returns(*, registered_primary: float,
                            registered_spouse: float,
                            plan: EstatePlan) -> List[TerminalReturn]:
    """Map a two-adult couple + its ``EstatePlan`` onto the death-ordered list
    of terminal returns ``compute_estate`` consumes.

    This is the ONE place the couple-specific plan fields are read:
    ``primary_dies_first`` (who the survivor is, hence whose return the rolled
    assets land on), and ``non_reg_primary_share`` / ``property_primary_share``
    (the real ownership splits over the two household pots). ``compute_estate``
    itself is member-count-agnostic -- it takes the ordered list and rolls each
    first-to-die's assets forward onto the next return.

    The ordering and share arithmetic here reproduce EXACTLY the closed-form
    first/second split this generalises (#705), so a two-adult estate is
    byte-identical to the pre-#705 result.
    """
    first_registered = registered_primary if plan.primary_dies_first else registered_spouse
    second_registered = registered_spouse if plan.primary_dies_first else registered_primary

    p_share = plan.non_reg_primary_share
    first_share = p_share if plan.primary_dies_first else (1.0 - p_share)

    pp_share = plan.property_primary_share
    prop_first_share = pp_share if plan.primary_dies_first else (1.0 - pp_share)

    return [
        TerminalReturn(registered=first_registered, non_reg_share=first_share,
                       property_share=prop_first_share),
        TerminalReturn(registered=second_registered,
                       non_reg_share=1.0 - first_share,
                       property_share=1.0 - prop_first_share),
    ]


def compute_estate(*, members: Sequence[TerminalReturn],
                   tfsa: float, non_reg_fmv: float, non_reg_acb: float,
                   house_equity: float, debts: float,
                   brackets: list, plan: EstatePlan,
                   inclusion_rate: float = 0.5,
                   sm_investment_fmv: float = 0.0,
                   sm_investment_acb: float = 0.0,
                   qc_carry_forward: float = 0.0,
                   qc_provincial_brackets: Optional[list] = None) -> EstateResult:
    """After-tax estate under a deemed disposition, per the DECLARED ``plan``.

    ``plan`` is MANDATORY (epic #603 Track C Phase 2c, issue #600): there is no
    default ``EstatePlan``, because every one of its fields used to be a silent
    assumption that resolved in the favourable direction. A caller that has not
    decided must say so explicitly rather than inherit a flattering answer.

    **N terminal returns, one model (#705).** A deemed disposition produces one
    terminal return per member who dies with assets. This function sums the
    deemed-disposition tax over ``members`` -- a per-member list in DEATH ORDER
    (first-to-die first) -- so a couple is two returns and a three-generation
    estate is three, with no hardcoded count. The rollover election does not
    change the arithmetic; it changes *which return each asset lands on*, by
    rolling each first-to-die's assets forward onto the NEXT member's return
    (ITA s.70(6), s.146(8.1)). Full rollover, declined rollover and every
    partial case in between fall out of the same chain -- see ``EstatePlan``'s
    docstring. Build ``members`` for a couple via ``couple_terminal_returns``.

    ``house_equity`` is the principal residence's equity and always reaches the
    gross estate; whether its accrued GAIN is exempt depends on
    ``plan.principal_residence_designated`` (ITA s.40(2)(b)). Non-principal real
    property (``plan.taxable_property_*``) adds its value to the gross estate and
    its gain to the tax. ``plan.life_insurance_death_benefit`` is tax-free
    (ITA s.148(1)).

    Args:
        members: the per-member terminal returns, in DEATH ORDER. Each carries
            an absolute ``registered`` balance and its SHARE of the household
            non-reg / real-property pots (see ``TerminalReturn``). Must be
            non-empty.
        tfsa: combined TFSA (tax-free under both designations).
        non_reg_fmv: non-registered FMV (household total; ``members`` carry the
            ownership split as shares).
        non_reg_acb: non-registered adjusted cost base (household total).
        house_equity: equity in the principal residence (value net of mortgage).
        debts: outstanding NON-mortgage debt (the mortgage is already netted out
            of ``house_equity`` by the caller -- see objective.py).
        brackets: combined federal+provincial brackets.
        plan: the declared elections/designations. Mandatory.
        inclusion_rate: capital-gains inclusion rate.
        qc_carry_forward: unused Quebec investment-expense carry-forward (TA
            s.336.0.1) at death -- the amount ``apply_sm_interest`` stranded in
            prior years because the annual investment income could not absorb
            it. The terminal deemed disposition IS an investment-income event
            (the taxable capital gains on the non-reg + SM pots), so the
            carry-forward is applied against it here, in death order, up to
            each return's investment income (#1035). 0.0 -> byte-identical.
        qc_provincial_brackets: the PROVINCIAL slice of ``brackets``
            (``TaxDataProvider.get_split_brackets``). Required when
            ``qc_carry_forward > 0`` -- the carry-forward is a QUEBEC-only
            deduction and must not be valued on the combined federal+
            provincial brackets.

    Returns:
        EstateResult with the gross/tax/net breakdown, carrying the elections
        that produced it.
    """
    if plan is None:
        raise EstateInputError(
            "compute_estate requires an explicit EstatePlan (epic #603 Track C "
            "Phase 2c / issue #600). Every field it carries used to be a silent "
            "assumption that resolved in the favourable direction, so there is "
            "deliberately no default. Build one from config via "
            "objective.plan_from_config(cfg)."
        )
    if not members:
        raise EstateInputError(
            "compute_estate needs at least one terminal return (member): a "
            "deemed disposition with no one to settle has no estate to tax. "
            "Build the list via couple_terminal_returns for a couple, or one "
            "TerminalReturn per member for a multi-generation estate (#705)."
        )
    # Issue #1035: a stranded QC investment-expense carry-forward is released
    # against the deemed disposition's investment income. A Quebec-only
    # deduction cannot be valued on the combined brackets -- demand the
    # provincial slice explicitly rather than guess (DP#32).
    if qc_carry_forward > 0 and qc_provincial_brackets is None:
        raise EstateInputError(
            "compute_estate with qc_carry_forward > 0 requires "
            "qc_provincial_brackets (TaxDataProvider.get_split_brackets' "
            "provincial slice): TA s.336.0.1 is a QUEBEC-ONLY deduction and "
            "must not be valued on combined federal+provincial brackets.")

    # Real property whose GAIN is taxed, beyond the non-registered pot, as a list
    # of (fmv, acb, taxable_fraction). The taxable_fraction is the share of the
    # accrued gain NOT sheltered by the principal-residence exemption; it is 1.0
    # (fully taxed) in every legacy case and only differs when the family
    # allocates its one s.40(2)(b) exemption across two properties (#695).
    #
    # Legacy path (plan.property_gains is None): non-principal property
    # (cottage/rental) is always fully taxed, plus the principal residence itself
    # when it is NOT designated for the exemption (the fourth silent assumption
    # from #600). The residence's *value* is NOT added here -- it reaches the
    # gross estate via `house_equity` already.
    #
    # PRE path (plan.property_gains present, issue #695): the family's real
    # property carries its own per-property taxable_fraction, computed from the
    # designated year ranges; this list REPLACES both the aggregate and the bool.
    # taxable_property_gross (the estate VALUE, not its gain tax) is the
    # non-principal FMV; the principal's FMV is in `house_equity`, so is excluded.
    if plan.property_gains is None:
        property_gain_bases = [
            (plan.taxable_property_fmv, plan.taxable_property_acb, 1.0)]
        if not plan.principal_residence_designated:
            property_gain_bases.append(
                (plan.principal_residence_fmv, plan.principal_residence_acb, 1.0))
        taxable_property_gross = plan.taxable_property_fmv
    else:
        property_gain_bases = [
            (g['fmv'], g['acb'], g['taxable_fraction']) for g in plan.property_gains]
        taxable_property_gross = sum(
            g['fmv'] for g in plan.property_gains if not g['is_principal'])

    def _return_tax(registered_on_return: float, nr_share: float,
                    prop_share: float) -> tuple:
        """Tax one terminal return: the registered lump runs the progressive
        brackets from $0, then each capital gain stacks on top in sequence so it
        is priced at the marginal band it actually lands in.

        Returns (registered_tax, non_reg_tax, sm_tax, property_tax,
        cca_recapture_tax, qc_carryforward_relief) for this return. The relief
        consumes the household's remaining QC carry-forward (single-element
        list so the closure can mutate it across returns).
        """
        from tax_calculator import tax_on_income
        reg_tax = tax_on_registered_at_death(registered_on_return, brackets)

        # Issue #694: CCA recapture on the rental's deemed disposition is ORDINARY
        # income (100% inclusion, ITA s.13(1)), so it stacks directly on the
        # registered lump -- taxed at the marginal band it lands in, and BEFORE
        # any capital gain (which then stacks on top of it). Apportioned by
        # prop_share (the same split as the property gain it arises with), and 0
        # when no CCA was elected -- leaving every figure below byte-identical.
        recapture_on_return = plan.cca_recapture * prop_share
        recapture_tax = (
            tax_on_income(registered_on_return + recapture_on_return, brackets)
            - tax_on_income(registered_on_return, brackets))
        ordinary_income = registered_on_return + recapture_on_return

        nr_tax = tax_on_capital_gain_at_death(
            non_reg_fmv * nr_share, non_reg_acb * nr_share, brackets,
            other_income=ordinary_income, inclusion_rate=inclusion_rate)

        running = ordinary_income
        running += max(0.0, (non_reg_fmv - non_reg_acb) * nr_share) * inclusion_rate

        # Issue #1031: the Smith-Manoeuvre investment SLEEVE is ordinary capital
        # property at death (ITA s.70(5)) -- a separate non-reg portfolio financed
        # by the readvanceable HELOC. Its accrued gain (FMV - ACB) is taxed at the
        # capital-gains inclusion rate, stacking on top of the non-reg gain so it
        # is priced at the marginal band it lands in. It MIRRORS the non-reg pot:
        # the same per-member ownership share (``nr_share``) and the same rollover
        # election (``non_reg_rolled_fraction`` -- the SM sleeve has no separate
        # ownership declaration, so the non-reg split is the one spelling of
        # "who owns the household's taxable investments", DP#9). 0.0 when no SM
        # sleeve was declared (``sm_investment_fmv`` defaults 0.0) -> the
        # ``tax_on_capital_gain_at_death`` gain is 0 -> byte-identical (DP#32).
        sm_tax = tax_on_capital_gain_at_death(
            sm_investment_fmv * nr_share, sm_investment_acb * nr_share, brackets,
            other_income=running, inclusion_rate=inclusion_rate)
        running += max(0.0, (sm_investment_fmv - sm_investment_acb) * nr_share) * inclusion_rate

        prop_tax = 0.0
        for fmv, acb, taxable_fraction in property_gain_bases:
            prop_tax += tax_on_capital_gain_at_death(
                fmv * prop_share, acb * prop_share, brackets,
                other_income=running, inclusion_rate=inclusion_rate,
                taxable_fraction=taxable_fraction)
            running += (max(0.0, (fmv - acb) * prop_share)
                        * taxable_fraction * inclusion_rate)

        # Issue #1035: release the QC investment-expense carry-forward against
        # THIS return's investment income -- the taxable capital gains on the
        # financial pots (non-reg + SM sleeve; the same pots the annual cap's
        # Schedule L base counts). Applied in death order, up to each return's
        # income; valued on the QUEBEC provincial brackets only (the federal
        # deduction was never capped), at the marginal band of the return's
        # full income stack. 0.0 when nothing is carried forward -> every
        # pre-existing figure byte-identical (DP#32).
        qc_relief = 0.0
        if qc_remaining[0] > 0:
            investment_income_this_return = (
                max(0.0, (non_reg_fmv - non_reg_acb) * nr_share) * inclusion_rate
                + max(0.0, (sm_investment_fmv - sm_investment_acb) * nr_share)
                * inclusion_rate)
            allowed = min(qc_remaining[0], investment_income_this_return)
            if allowed > 0:
                from tax_calculator import deduction_value as _dv
                qc_relief = _dv(running, allowed, qc_provincial_brackets)
                qc_remaining[0] -= allowed

        return reg_tax, nr_tax, sm_tax, prop_tax, recapture_tax, qc_relief

    # Roll each first-to-die's assets forward onto the NEXT member's return, in
    # death order (the two-member case reduces to the old first->second roll).
    # `registered` rolls in absolute dollars; the non-reg and property SHARES
    # roll in share-space and are multiplied by the household total only inside
    # `_return_tax` -- that ordering is what keeps N=2 byte-identical to the
    # closed-form split (#705). The last member has no survivor to roll to, so
    # everything carried lands on their own return.
    n = len(members)
    reg_carried = members[0].registered
    nr_share_carried = members[0].non_reg_share
    prop_share_carried = members[0].property_share

    registered_tax = 0.0
    non_reg_tax = 0.0
    sm_tax = 0.0
    property_tax = 0.0
    cca_recapture_tax = 0.0
    qc_carryforward_relief = 0.0
    qc_remaining = [qc_carry_forward]  # mutated across returns (death order)
    for i in range(n):
        if i < n - 1:
            reg_rolled = reg_carried * plan.registered_rolled_fraction
            reg_on_return = reg_carried - reg_rolled
            nr_rolled = nr_share_carried * plan.non_reg_rolled_fraction
            nr_share_on_return = nr_share_carried - nr_rolled
            prop_rolled = prop_share_carried * plan.non_reg_rolled_fraction
            prop_share_on_return = prop_share_carried - prop_rolled

            nxt = members[i + 1]
            reg_carried = nxt.registered + reg_rolled
            nr_share_carried = nxt.non_reg_share + nr_rolled
            prop_share_carried = nxt.property_share + prop_rolled
        else:
            reg_on_return = reg_carried
            nr_share_on_return = nr_share_carried
            prop_share_on_return = prop_share_carried

        r, nr, sm, p, rec, relief = _return_tax(
            reg_on_return, nr_share_on_return, prop_share_on_return)
        registered_tax += r
        non_reg_tax += nr
        sm_tax += sm
        property_tax += p
        cca_recapture_tax += rec
        qc_carryforward_relief += relief

    registered_gross = sum(m.registered for m in members)

    return EstateResult(
        tfsa=tfsa,
        house_equity=house_equity,
        registered_gross=registered_gross,
        registered_tax=registered_tax,
        non_reg_gross=non_reg_fmv,
        non_reg_tax=non_reg_tax,
        sm_investment_gross=sm_investment_fmv,
        sm_investment_tax=sm_tax,
        debts=debts,
        life_insurance=plan.life_insurance_death_benefit,
        taxable_property_gross=taxable_property_gross,
        taxable_property_tax=property_tax,
        cca_recapture_tax=cca_recapture_tax,
        qc_carryforward_relief=qc_carryforward_relief,
        spousal_rollover=plan.spousal_rollover,
        tfsa_shelter_ends=not plan.tfsa_successor_holder,
    )
