"""Tests for issue #584 (DP#10/DP#26, epic #603): rules as a registry.

## What this file proves

`simulate_year_pure` used to be one ~790-line function with every
government-program rule inlined as an anonymous block. Nothing enumerated
the rule space, so a rule that was silently missing (RRIF minimums, #574;
the entire retirement transition on the optimizer path, #627) produced no
error -- just a confident, plausible, wrong number.

`simulation_rules.py` decomposes that function into 18 named, registered
rules (`RULES`) run in an explicitly declared order (`RULE_ORDER`). This
file is the enforcement the refactor exists to make possible:

1. **A rule either registers or it doesn't (DP#32).** `EXPECTED_RULE_NAMES`
   is declared here, independently of `simulation_rules.py` -- if a rule is
   ever added to the registry without being added here, or removed from the
   registry without being removed here, `test_every_expected_rule_is_registered`
   fails. That is what makes an unregistered-but-expected rule a *visible*
   absence rather than a silent gap: the set comparison, not a human
   remembering to check.
2. **Ordering is declared, not emergent.** `test_rule_order_is_pinned` pins
   the exact sequence; a reorder is a diff someone has to intend and
   justify, not something that falls out of dict insertion order.
3. **An unregistered-but-declared rule fails LOUDLY at call time, not
   silently.** `test_run_rules_raises_if_a_declared_rule_has_no_implementation`
   proves `run_rules` raises rather than skipping a name in `RULE_ORDER`
   that has no `@rule(...)` behind it.
4. **A registered rule that never fires is the SAME defect as a rule that
   was never written (#627's actual shape: nominally present, silently
   inert).** `test_every_rule_fires_somewhere_in_representative_households`
   sweeps a handful of representative households/scenarios and asserts
   every rule in `RULE_ORDER` has an observable effect in at least one
   year of at least one scenario -- and reports by name any that don't.
"""

import pytest

import simulation_rules
from simulation_rules import RuleContext, YearWorkingState, RULE_ORDER, RULES, run_rules, trace_firing
from simulation_state import SimState, simulate_year_pure, _default_canada_state
from simulation_config import SimulationConfig

from test_golden_trajectory_581 import golden_household_config, sm_only_config, _run as _run_golden


def _default_tax_provider_combined_brackets():
    """Issue #956 bite B: the combined federal+provincial bracket list the
    default tax provider resolves for the year -- the SAME list the prologue
    passes to RuleContext.year_brackets for the marginal rates, so the
    property_disposition rule's gain bands against the owner's taxable income
    at the real marginal rate. Used by the sale scenario in the coverage sweep
    (a direct simulate_year_pure call that must pass real brackets)."""
    from tax_data import default_tax_provider
    return default_tax_provider().get_combined_brackets()


# ============================================================================
# 1. The expected rule set is declared HERE, independently of the registry.
#    A rule silently added to (or dropped from) simulation_rules.py without
#    a matching change here fails the build -- that IS the "visible
#    absence" property DP#32 asks for.
# ============================================================================

EXPECTED_RULE_NAMES = frozenset({
    'retirement_income',           # epic #795 bite 1: CPP/OAS/pension onset + income stop + drawdown sizing (was inline in the fold's prologue)
    'contributions',              # clamp + book this year's RRSP/TFSA/non-reg allocations
    'rrsp_ledger',                 # per-contribution deduction ledger (DP#19)
    'rrsp_deduction',              # deduct-now vs deduct-later (issue #546)
    'heloc_tracing',               # ITA §20(1)(c) purpose-tracing buckets
    'borrowing_purpose',           # issue #850: trace the year-0 lump's advance + drawn-margin borrowings to purpose
    'registered_growth',           # RRSP/TFSA grow at the gross tax-sheltered rate
    'non_reg_growth',              # DP#27: non-reg grows at income-type after-tax rate
    'emergency_reserve_growth',    # issue #688: reserve compounds at its OWN cash rate, not the portfolio's
    'deposit_product_growth',        # issue #936: a taken deposit product grows at its own rate_schedule, net of interest tax
    'lira_lif',                    # CRI/LIRA growth, conversion to LIF at 71, LIF withdrawals
    'resp',                        # RESP contribute/grow/wind-down (issue #578)
    'mortgage',                    # extract this year's amortization data
    'consumer_loans',              # issue #763: amortize closed-end consumer loans (car/student/personal) into debt service
    'installments',                # issue #759: service fixed-term 0%-interest installment plans (date-scheduled payment) into debt service
    'second_property_mortgage',   # issue #967: service mid-horizon mortgages originated by properties' purchase.financing (originate + amortize to payoff, interest deductible for a rental) into debt service
    'sm_readvance',                # Smith Manoeuvre: readvance principal paydown, BOUNDED by the charge (#681)
    'sm_interest',                 # SM HELOC interest + QC-carry-forward-limited deduction
    'sm_investment_growth',        # SM investment grows at after-tax rate (issue #576)
    'margin_heloc_interest',       # interest on the drawn HELOC, capitalized only as far as the charge allows (#577/#681)
    'heloc_interest_servicing',    # issue #681: interest the charge can't absorb is PAID IN CASH from non-reg/SM
    'principal_disposition',        # issue #956 bite E: a declared mid-horizon SALE of the PRINCIPAL residence settles in its sale year (net proceeds invested post-growth, gain taxed + PRE-apportioned ≈ 0 for a fully-designated home, secured debt discharged, conservation identity on net_assets Δnet_assets = V - selling_costs - T)
    'rrsp_refund_heloc_paydown',   # apply RRSP refund to HELOC paydown
    'fhsa',                        # FHSA contribution + growth + annual room (issue #124)
    'contribution_room',           # DP#20: add this year's year-versioned RRSP/TFSA room
    'retirement_drawdown',         # issue #294/#363/#579: net-target retirement drawdown
    'rrif_minimum',                # issue #574: mandatory RRIF minimum withdrawal from 71
    'sm_unwind',                   # issue #1017: under liquidate_to_target, unwind the SM sleeve to fund the spending shortfall (sell SM, repay HELOC, pay cap-gains tax, deliver net)
    'property_disposition',        # issue #956 bite B: a declared mid-horizon property SALE settles in its sale year (net proceeds invested post-growth, gain taxed + PRE-apportioned, conservation identity Δtotal_assets = -(selling_costs + T))
    'tuition_credit',              # epic #795 bite 3: federal (+ QC) tuition tax credit (own credit + #784 carry-forward + #785 transfers) -- was inline in the fold's prologue
    'solvency',                    # issue #679: cash-flow identity + forced-liquidation waterfall
    'amt',                         # issue #710: year-end Alternative Minimum Tax assessment (max(regular, AMT)) over the year's realized capital gains
})

# Explicit, declared order (issue #584: "order matters and must be
# explicit"). Contributions/deductions/tracing first (they price this
# year's cash movements); every account then grows; LIRA/LIF and RESP are
# independent side-lines; mortgage/SM/HELOC-margin/FHSA/room-addition round
# out the year; retirement drawdown and the RRIF minimum run LAST because
# they price against POST-growth balances (drawdown) and the OPENING Jan-1
# balance (RRIF minimum) -- see simulation_rules.py's RULE_ORDER comment
# and each rule's docstring for the specific dependency.
#
# Issue #763 added 'consumer_loans' (closed-end car/student/personal loans)
# right after 'mortgage': it amortizes those loans' declared payment/balance
# so 'solvency' can fold the payment into the debt-service term. It runs every
# year regardless of solvency (it does not depend on living_costs), so it
# cannot live inside the 'solvency' rule (which no-ops when living_costs <= 0).
#
# Issue #681 made two DELIBERATE changes to this order, and this pin is the
# mechanism that forced them to be declared rather than slipped in:
#
#   1. 'margin_heloc_interest' moved from AFTER 'sm_investment_growth' to
#      BEFORE 'sm_readvance'. It is a pure function of opening_heloc_balance
#      and the HELOC rate, so the move changes no number by itself -- but
#      'sm_readvance' now has to size the readvance against the room left
#      under the registered charge, and the drawn revolving balance (which
#      consumes that room) is the SM line PLUS this personal-draw margin.
#   2. 'heloc_interest_servicing' is NEW, and runs after 'sm_investment_growth'
#      because it is paid out of the non-reg and SM balances, which must be
#      final before it draws on them.
EXPECTED_RULE_ORDER = (
    # epic #795 bite 1: the retirement transition runs FIRST -- it depends
    # only on member data + pre-retirement income + year-brackets (not on
    # any account balance), and retirement_drawdown / rrif_minimum / solvency
    # read its outputs off YearWorkingState later in the order.
    'retirement_income',
    'contributions',
    'rrsp_ledger',
    'rrsp_deduction',
    'heloc_tracing',
    'borrowing_purpose',
    'registered_growth',
    'non_reg_growth',
    'emergency_reserve_growth',
    # issue #936: grow a taken deposit-product balance at its own rate_schedule
    # (a carved-out balance at its OWN rate, like the reserve above); sits with
    # the growth rules, a strict no-op when no product is taken.
    'deposit_product_growth',
    'lira_lif',
    'resp',
    'mortgage',
    # issue #763: amortize closed-end consumer loans BEFORE solvency reads
    # their payment as debt service (after 'mortgage', alongside the other
    # debt-servicing rules).
    'consumer_loans',
    # issue #759: apply fixed-term installment obligations' date-scheduled
    # payment BEFORE solvency reads it as debt service -- same position as
    # 'consumer_loans' (both publish a payment apply_solvency folds into its
    # debt-service term + reserve sizing). A 0%-interest committed payment
    # schedule, not a callable debt; not among the tax rules.
    'installments',
    # issue #967: service mid-horizon mortgages originated by properties'
    # `purchase.financing` BEFORE solvency reads their payment as debt
    # service -- same position as 'consumer_loans' / 'installments' (all
    # three publish a payment apply_solvency folds into its debt-service term
    # + reserve sizing). Originates the balance from the precomputed schedule
    # in the purchase year and amortizes it to payoff; a strict no-op for a
    # household with no financed property (the golden path) (DP#32).
    'second_property_mortgage',
    'margin_heloc_interest',
    'sm_readvance',
    'sm_interest',
    'sm_investment_growth',
    'heloc_interest_servicing',
    # issue #956 bite E: a declared mid-horizon SALE of the PRINCIPAL residence
    # settles in its sale year -- the home + its mortgage + any HELOC/SM
    # secured against it leave the balance sheet, net proceeds invested into
    # non-reg POST-GROWTH (conservation identity on net_assets holds exactly),
    # gain taxed + PRE-apportioned (≈ 0 for a fully-designated home). Runs
    # after the mortgage/HELOC/SM rules (to read the amortized balances and
    # let the SM investment grow -- a real asset that STAYS) and before
    # 'rrsp_refund_heloc_paydown' (no HELOC to pay down post-sale).
    'principal_disposition',
    'rrsp_refund_heloc_paydown',
    'fhsa',
    'contribution_room',
    'retirement_drawdown',
    'rrif_minimum',
    # Issue #1017: under liquidate_to_target, unwind the Smith-Manoeuvre sleeve
    # to fund the spending shortfall the ordinary financial drawdown + forced
    # RRIF minimum could not cover. Runs after rrif_minimum (so the shortfall
    # is the true post-RRIF gap) and before property_disposition / solvency (so
    # the net it delivers counts in the cash-flow identity). Gated on
    # liquidate_to_target + an SM sleeve -> a no-op for the golden household.
    'sm_unwind',
    # issue #956 bite B (sale-core): a declared mid-horizon property SALE
    # settles in its sale year -- net proceeds invested into non-reg
    # POST-GROWTH (conservation identity holds exactly), gain taxed +
    # PRE-apportioned, realized gain surfaced for the AMT base. Runs after
    # the growth/drawdown rules (gain bands against the owner's taxable
    # income; proceeds inject post-growth) and before 'solvency' (the
    # invested non-reg is on the balance sheet the waterfall reads).
    'property_disposition',
    # epic #795 bite 3: the tuition tax credit runs immediately before
    # 'solvency' (its sole consumer) -- it writes the per-member tax
    # reduction to YearWorkingState, which apply_solvency adds to
    # `available` so the cash-flow identity counts the POST-credit
    # after-tax income. Was inline in the fold's prologue (two spellings).
    'tuition_credit',
    'solvency',
    # issue #710: the AMT assessment runs DEAD LAST -- it is a year-end
    # assessment over ALL of the year's realized income, so it must run after
    # solvency has crystallized every forced-liquidation gain (the AMT base).
    'amt',
)


class TestRuleRegistryIsComplete:
    """DP#32: a rule either registers or it doesn't -- prove it here, not
    by trusting simulation_rules.py to have kept itself honest."""

    def test_every_expected_rule_is_registered(self):
        registered = set(RULES.keys())
        declared_order = set(RULE_ORDER)
        missing_from_registry = EXPECTED_RULE_NAMES - registered
        unexpected_in_registry = registered - EXPECTED_RULE_NAMES
        assert not missing_from_registry, (
            f"rule(s) {sorted(missing_from_registry)} are expected (per this "
            f"test's independently-declared EXPECTED_RULE_NAMES) but have no "
            f"@rule(...) registration in simulation_rules.py -- this is "
            f"exactly the #627 failure shape: a rule that should exist but "
            f"silently doesn't.")
        assert not unexpected_in_registry, (
            f"rule(s) {sorted(unexpected_in_registry)} are registered in "
            f"simulation_rules.py but not declared in this test's "
            f"EXPECTED_RULE_NAMES -- add them here (with the one-line "
            f"rationale every other entry has) so a future silent removal "
            f"is equally visible.")
        assert declared_order == EXPECTED_RULE_NAMES, (
            "RULE_ORDER and EXPECTED_RULE_NAMES must name exactly the same "
            "rules")

    def test_rule_order_is_pinned(self):
        assert RULE_ORDER == EXPECTED_RULE_ORDER, (
            "simulation_rules.RULE_ORDER has drifted from the order pinned "
            "here -- tax rules are order-dependent (issue #584); a reorder "
            "must be a deliberate, reviewed change to BOTH this pin and the "
            "rationale in simulation_rules.py's RULE_ORDER comment, not an "
            "incidental side effect of some other edit.")

    def test_run_rules_raises_if_a_declared_rule_has_no_implementation(self):
        """Behavioural proof of the DP#32 property: simulate a rule that is
        declared (in RULE_ORDER) but not implemented (missing from RULES) --
        exactly what "a rule that isn't there simply isn't there" (#584's
        motivating quote) looks like -- and confirm the fold refuses to
        silently skip it.
        """
        original_order = simulation_rules.RULE_ORDER
        fake_order = original_order + ('a_rule_nobody_registered',)
        simulation_rules.RULE_ORDER = fake_order
        try:
            ws = object()  # never reached -- run_rules must raise before calling any rule
            ctx = object()
            with pytest.raises(RuntimeError, match='a_rule_nobody_registered'):
                run_rules(ws, ctx)
        finally:
            simulation_rules.RULE_ORDER = original_order

    def test_registering_the_same_name_twice_to_different_functions_is_an_error(self):
        from simulation_rules import rule as rule_decorator

        def _fn_a(ws, ctx):
            return False

        def _fn_b(ws, ctx):
            return False

        rule_decorator('a_test_only_rule_name')(_fn_a)
        try:
            with pytest.raises(ValueError, match='already registered'):
                rule_decorator('a_test_only_rule_name')(_fn_b)
        finally:
            del RULES['a_test_only_rule_name']


# ============================================================================
# 2. No rule reads `self` (DP#26/#583's property, extended to the rules
#    the year step now delegates to).
# ============================================================================

class TestRulesDoNotReadSelf:
    def test_no_rule_function_references_the_name_self(self):
        import ast
        import inspect

        for name, fn in RULES.items():
            source = inspect.getsource(fn)
            tree = ast.parse(source)
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            assert 'self' not in names, (
                f"rule {name!r} references `self` -- every rule must take "
                f"its inputs explicitly via (ws, ctx), per DP#26/#583")


# ============================================================================
# 3. Coverage sweep: every registered rule must actually FIRE somewhere.
#
# A rule that is registered but whose wiring is broken (never reached with
# non-trivial inputs) is the SAME defect as a rule that was never written --
# both print a confident wrong number (#627's shape, generalized). This
# combines several representative scenarios because no single realistic
# household exercises every regime (the #581 golden household, by design,
# never draws its HELOC margin or turns on the Smith Manoeuvre -- see its
# own docstring -- so those rules need their own scenario).
# ============================================================================

def _make_config(**overrides):
    defaults = dict(
        projection_years=5,
        investment_return=0.06,
        mortgage_balance=0,
        mortgage_rate=0.05,
        margin_available=0,
        family_members=[
            {'role': 'primary', 'gross_income': 130000, 'birth_year': 1990,
             'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 20000},
            {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1992,
             'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 20000},
        ],
        children=[],
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def _state_with_lira(lira_balance, lira_birth_year, lif_balance=0, lif_birth_year=0):
    # #700/#643/#704: LIRA/LIF are per-adult stores (single primary slot).
    canada = _default_canada_state()
    canada['adult_lira'] = {'primary': {
        'balance': lira_balance, 'birth_year': lira_birth_year,
        'jurisdiction': 'federal', 'reference_rate': 0.06, 'conversion_year': 0}}
    canada['adult_lif'] = {'primary': {
        'balance': lif_balance, 'birth_year': lif_birth_year,
        'jurisdiction': 'federal', 'reference_rate': 0.06}}
    return SimState(jurisdiction_state={'canada': canada})


def _state_with_heloc_and_rrsp_room(heloc_balance, rrsp_room):
    canada = _default_canada_state()
    # #700: per-adult RRSP store — one synthetic primary adult with this room
    canada['adult_rrsp'] = {'primary': {'own': 0.0, 'own_room': rrsp_room, 'spousal_as_annuitant': 0.0}}
    return SimState(heloc_balance=heloc_balance, jurisdiction_state={'canada': canada})


def _state_with_fhsa_room(fhsa_room):
    # #700/#643/#704: FHSA is a per-adult store (single primary slot drives the compute).
    canada = _default_canada_state()
    canada['adult_fhsa'] = {'primary': {
        'balance': 0.0, 'room': fhsa_room,
        'lifetime_used': 0.0, 'lifetime_limit': 40000}}
    return SimState(jurisdiction_state={'canada': canada})


def test_every_rule_fires_somewhere_in_representative_households():
    fired_ever = {name: False for name in RULE_ORDER}

    def _merge(fired):
        for name, was_fired in fired.items():
            fired_ever[name] = fired_ever[name] or was_fired

    # ── Scenario A: the #581 golden household, 46 years -- accumulation,
    # RESP wind-down, retirement, RRIF conversion at 71, long decumulation.
    # By design (see test_golden_trajectory_581.py's docstring) it never
    # draws its HELOC margin and never turns on the Smith Manoeuvre.
    with trace_firing() as fired:
        _run_golden(golden_household_config())
    _merge(fired)

    # ── Scenario B: Smith Manoeuvre active (readvanceable mortgage).
    with trace_firing() as fired:
        cfg = SimulationConfig.from_dict(sm_only_config())
        from simulation import FamilySimulation
        FamilySimulation(cfg, use_readvanceable=True, deduct_later=False).run()
    _merge(fired)

    # ── Scenario B2: Issue #1017 -- liquidate_to_target + an SM sleeve + a
    # spending shortfall fires the sm_unwind rule (sell the SM sleeve, repay
    # the HELOC, deliver the net to the target). The golden household with a
    # large low-leverage SM sleeve injected at year 0 and a high spending
    # target, so the ordinary drawdown exhausts and the SM unwind funds the
    # target into deep retirement (mirrors tests/test_issue_1017_sm_unwind.py).
    with trace_firing() as fired:
        cfg = golden_household_config()
        cfg['family']['members'][0]['retirement_age'] = 65
        cfg['family']['members'][1]['retirement_age'] = 65
        cfg['assumptions']['horizon_age'] = 90
        cfg['retirement']['spending_target'] = 500_000
        cfg['retirement']['liquidate_to_target'] = True
        cfg['property']['mortgage_balance'] = 0
        cfg['property']['house_value'] = 2_000_000
        cfg['property']['heloc_rate'] = 0.05
        cfg['property']['heloc_readvance'] = True
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        sm_cfg = SimulationConfig.from_dict(cfg)
        sm_sim = FamilySimulation(sm_cfg, adapter=CanadaAdapter(sm_cfg),
                                  use_readvanceable=True, deduct_later=False)
        canada = sm_sim._state.jurisdiction_state['canada']
        canada['sm_investment_balance'] = 2_000_000
        canada['sm_investment_cost_basis'] = 1_000_000
        canada['readvance_heloc_balance'] = 200_000
        sm_sim.run()
    _merge(fired)

    # ── Scenario C: LIRA/LIF -- accumulation, conversion at 71, then a
    # post-conversion LIF-withdrawal year (direct simulate_year_pure calls,
    # mirroring tests/test_lira_wiring.py's pattern).
    config = _make_config()
    lira_state = _state_with_lira(lira_balance=100_000, lira_birth_year=1950)  # turns 71 in 2021
    with trace_firing() as fired:
        # Accumulation year (LIRA present, not yet 71).
        _, lira_state_2020 = simulate_year_pure(
            state=lira_state, year=2020,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config, investment_return=0.06, primary_marginal_rate=0.40)
        # Conversion year.
        _, converted_state = simulate_year_pure(
            state=lira_state_2020, year=2021,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config, investment_return=0.06, primary_marginal_rate=0.40)
        # Post-conversion: mandatory LIF withdrawal.
        simulate_year_pure(
            state=converted_state, year=2022,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config, investment_return=0.06, primary_marginal_rate=0.40)
    _merge(fired)

    # ── Scenario D: a drawn personal HELOC margin balance + a same-year
    # RRSP contribution/refund -- exercises margin_heloc_interest and
    # rrsp_refund_heloc_paydown, which the golden household's fixture
    # deliberately avoids (see test_golden_trajectory_581's cashout fixture
    # docstring: RRSP room is zeroed there specifically to NOT trigger this
    # rule, to isolate a different invariant).
    heloc_config = _make_config(rrsp_annual_max=40000)
    heloc_state = _state_with_heloc_and_rrsp_room(heloc_balance=50_000, rrsp_room=30_000)
    with trace_firing() as fired:
        simulate_year_pure(
            state=heloc_state, year=0,
            allocations={'primary_rrsp': 20_000, '_primary_income': 130000,
                         '_annual_savings': 20_000},
            config=heloc_config, investment_return=0.06, heloc_rate=0.06,
            primary_marginal_rate=0.40)
    _merge(fired)

    # ── Scenario E: FHSA contribution + growth. Deliberately separate from
    # every scenario above: none of the four predefined Canadian strategies
    # (Balanced, Max RRSP + Spousal, Readvance Priority, No Readvance) set
    # fhsa_pct > 0 for the ANNUAL allocate() path -- FHSA funding through
    # StrategyEngine.allocate() is opt-in strategy configuration; only the
    # year-0 lump-sum fill_room() waterfall funds it unconditionally (see
    # tests/test_issue_627_engine_collapse.py's item-5 comment). The golden
    # household (Scenario A) has fhsa_room_accumulated but never a lump sum,
    # so it does NOT exercise this rule either -- discovered by this exact
    # sweep, which is the coverage property #584 asks for working as
    # intended: this rule is genuinely correctly-implemented-but-unreached
    # by every OTHER scenario in this file, not evidence of a bug.
    fhsa_state = _state_with_fhsa_room(fhsa_room=8_000)
    with trace_firing() as fired:
        simulate_year_pure(
            state=fhsa_state, year=0,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=_make_config(), investment_return=0.06,
            primary_marginal_rate=0.40,
            fhsa_contribution=5_000, fhsa_annual_limit=8_000)
    _merge(fired)

    # ── Scenario F: a household with a declared emergency reserve whose
    # required outflows exceed its available inflows this year -- exercises
    # BOTH 'solvency' (issue #679: the cash-flow identity + the forced
    # liquidation) and 'emergency_reserve_growth' (issue #688: the reserve
    # compounds at its own declared cash rate). Deliberately separate from
    # every scenario above: none of them supply living_costs/after_tax_income
    # or declare a reserve, so both rules' DP#16 trigger-data gates keep them
    # no-ops (fired=False) everywhere else in this sweep.
    solvency_config = _make_config(
        emergency_reserve_target_months=6,
        emergency_reserve_rate=0.03,
        emergency_reserve_instrument='cash',
        # held_in=None: the reserve is held outside every declared account
        # (an ordinary savings balance), so there is no host account to carve
        # it out of -- the case that isolates these two rules from every
        # account-balance interaction in the fold.
        emergency_reserve_held_in=None,
        living_costs=80_000,
    )
    solvency_state = SimState(
        emergency_reserve_balance=5_000,
        jurisdiction_state={'canada': _default_canada_state()},
    )
    with trace_firing() as fired:
        simulate_year_pure(
            state=solvency_state, year=0,
            allocations={'_primary_income': 60_000, '_annual_savings': 0},
            config=solvency_config, investment_return=0.06,
            primary_marginal_rate=0.30,
            living_costs=80_000, after_tax_income=45_000,
        )
    _merge(fired)

    # ── Scenario G: a household carrying a closed-end consumer loan
    # (issue #763: car_loan/student_loan/personal_loan). Deliberately
    # separate: every other scenario builds SimulationConfig directly with
    # no consumer_loans list, so the 'consumer_loans' rule stays a no-op
    # (fired=False) everywhere else -- the rule's whole job is to amortize
    # a kind of debt none of the other representative households declare,
    # which is exactly the coverage property #584 asks for: a rule that is
    # correctly implemented but unreached by every OTHER scenario.
    consumer_config = _make_config(
        consumer_loans=[{
            'id': 'car_loan_g', 'kind': 'car_loan',
            'balance': 12_000, 'rate': 0.06,
            'payment_monthly': 300, 'amortization_years': 4,
        }],
    )
    consumer_state = SimState(
        consumer_loan_balances=[12_000],
        jurisdiction_state={'canada': _default_canada_state()},
    )
    with trace_firing() as fired:
        simulate_year_pure(
            state=consumer_state, year=0,
            allocations={'_primary_income': 130_000, '_annual_savings': 0},
            config=consumer_config, investment_return=0.06,
            primary_marginal_rate=0.40,
        )
    _merge(fired)

    # Scenario H: a household carrying a fixed-term installment plan
    # (issue #759: a medical/dental/education payment plan). Deliberately
    # separate for the same reason as Scenario G: every other scenario
    # builds SimulationConfig with no installments list, so the 'installments'
    # rule stays a no-op (fired=False) everywhere else -- the rule's whole
    # job is to service a kind of obligation none of the other representative
    # households declare. Built directly via SimulationConfig (not
    # input_contract), so the contract-boundary refusals (rate==0 /
    # non_discretionary / start_date>=as_of) do not apply here; the plan
    # carries the fields the rule reads.
    installment_config = _make_config(
        installments=[{
            'id': 'ortho_plan_g', 'owner': 'p1',
            'description': 'orthodontic payment plan',
            'start_date': '2026-01-01', 'monthly_amount': 500,
            'number_of_payments': 24, 'final_payment': 1000,
            'rate': 0, 'non_discretionary': True,
        }],
    )
    installment_state = SimState(
        installment_balances=[13_000],
        jurisdiction_state={'canada': _default_canada_state()},
    )
    with trace_firing() as fired:
        simulate_year_pure(
            state=installment_state, year=0,
            allocations={'_primary_income': 130_000, '_annual_savings': 0},
            config=installment_config, investment_return=0.06,
            primary_marginal_rate=0.40,
            # calendar_year is the ABSOLUTE year apply_installments compares
            # against the plan's start_date; simulate_year_pure falls back to
            # the year INDEX (0) when omitted, which would place year 0 in
            # calendar year 0 and the plan (start 2026) would never fire.
            calendar_year=2026,
        )
    _merge(fired)

    # Scenario I: a household that took a year-0 LEVERAGED LUMP SUM -- a
    # mortgage cash-out advance plus a draw on its revolving line, invested
    # (issue #850). Deliberately separate for the same reason as G and H: the
    # 'borrowing_purpose' rule traces those two borrowings to purpose under
    # s.20(1)(c), and NO other representative household here borrows to invest
    # -- every one of them passes a lump-free allocations dict, so the rule
    # correctly stays a no-op everywhere else. Without this scenario the rule
    # would be exactly the #627 shape the surrounding test exists to catch:
    # registered, ordered, and never once reached.
    borrowing_config = _make_config(mortgage_balance=400_000, margin_available=100_000)
    borrowing_state = SimState(
        mortgage_balance=400_000,
        heloc_balance=100_000,
        jurisdiction_state={'canada': _default_canada_state()},
    )
    with trace_firing() as fired:
        simulate_year_pure(
            state=borrowing_state, year=0,
            allocations={
                '_primary_income': 130_000, '_annual_savings': 0,
                # $200,000 borrowed at year 0, all of it into the
                # non-registered account -- an income-producing use, so both
                # borrowings trace as deductible.
                '_lump_sum': 200_000, '_lump_non_reg': 200_000,
                'non_reg': 200_000,
            },
            config=borrowing_config, investment_return=0.06,
            primary_marginal_rate=0.40,
            # Issue #1034: this scenario also exercises heloc_interest_servicing
            # (the drawn HELOC's interest is serviced from the non-reg pot,
            # which carries a gain after a year of growth) -- the rule prices
            # that gain and needs real brackets, like property_disposition does
            # in scenarios M/N. The prologue resolves these for the marginal
            # rates; pass the same combined brackets and the primary's taxable
            # income so the gain bands against it.
            year_brackets=_default_tax_provider_combined_brackets(),
            primary_taxable_income=130_000.0, spouse_taxable_income=0.0,
        )
    _merge(fired)

    # Scenario J: a retired household that realizes a LARGE capital gain in one
    # year -- a $1.2M net drawdown funded from a $3M non-registered pot with a
    # ~zero cost base, crystallizing a ~$1.6M gain (issue #710/#754). This is the
    # only add-back big enough to lift AMTI above regular taxable income far
    # enough to clear the AMT basic exemption ABOVE regular tax, so the 'amt'
    # rule fires (minimum > regular, a real surcharge) here and stays a strict
    # no-op in every other scenario -- none of which realize a capital gain (the
    # golden household realizes 0 in all 46 years). Built as a direct
    # simulate_year_pure call in an ABSOLUTE tax year (calendar_year=2026): AMT
    # is a year-versioned assessment, so the rule reads the calendar year, not
    # the projection index.
    amt_config = _make_config(
        family_members=[
            {'role': 'primary', 'gross_income': 0, 'birth_year': 1955},
            {'role': 'spouse', 'gross_income': 0, 'birth_year': 1957},
        ],
    )
    amt_state = SimState(
        non_reg_balance=3_000_000, non_reg_acb=0.0,
        jurisdiction_state={'canada': _default_canada_state()},
    )
    with trace_firing() as fired:
        simulate_year_pure(
            state=amt_state, year=0,
            allocations={'_primary_income': 0, '_annual_savings': 0},
            config=amt_config, investment_return=0.05,
            primary_marginal_rate=0.53, retiree_marginal_rate=0.53,
            calendar_year=2026,
            drawdown_net_target=1_200_000, drawdown_order=['non_reg'],
            any_retired=True, retirement_spending_target=1_200_000,
        )
    _merge(fired)

    # Scenario K: a household that TOOK a deposit product (issue #936: a HISA,
    # a term/GIC, a promotional teaser -- one generic mechanism). Deliberately
    # separate for the same reason as G/H: the 'deposit_product_growth' rule
    # grows the parked balance at the rate its rate_schedule prescribes, and NO
    # other representative household declares a deposit_product -- every other
    # scenario leaves config.deposit_product None, so the rule correctly stays a
    # strict no-op there. Without this scenario it would be exactly the #627
    # shape (registered, ordered, never reached). The taken product is set
    # directly on the config; SimState carries the funded balance the rule grows.
    deposit_config = _make_config(
        deposit_product={
            'id': 'promo_hisa', 'label': 'Promo HISA 3% for 730d then 1.5%',
            'account_kind': 'non_reg', 'fund_amount': 50_000,
            'funding_source': 'non_reg',
            'rate_schedule': [{'rate': 0.03, 'duration_days': 730},
                              {'rate': 0.015}],
            'rate_eligible_cap': 500_000, 'tax_character': 'interest',
        },
    )
    deposit_state = SimState(
        deposit_product_balance=50_000,
        jurisdiction_state={'canada': _default_canada_state()},
    )
    with trace_firing() as fired:
        simulate_year_pure(
            state=deposit_state, year=0,
            allocations={'_primary_income': 130_000, '_annual_savings': 0},
            config=deposit_config, investment_return=0.06,
            primary_marginal_rate=0.40,
        )
    _merge(fired)

    # Scenario L: a household that DECLARES tuition (epic #795 bite 3: the
    # federal + QC provincial tuition tax credit, #764/#783/#784/#785).
    # Deliberately separate: every other scenario builds SimulationConfig
    # with no ``tuition_by_year`` on any member, so the ``tuition_credit`` rule
    # correctly stays a no-op (fired=False) everywhere else -- the rule's whole
    # job is to price a credit none of the other representative households
    # declare, which is exactly the coverage property #584 asks for: a rule that
    # is correctly implemented but unreached by every OTHER scenario. Without
    # this scenario it would be exactly the #627 shape (registered, ordered,
    # never reached). A taxed couple both declaring tuition in ``sim_year``
    # (QC, so the provincial credit fires too) exercises the rule's full
    # own-credit + carry-forward path. Round numbers, role-based names (DP#4/#15).
    tuition_config = _make_config(
        family_members=[
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
             'rrsp_room_accumulated': 40_000, 'tfsa_room_accumulated': 20_000,
             'tuition_by_year': {2026: 12_000}},
            {'role': 'spouse', 'birth_year': 1982, 'gross_income': 45_000,
             'rrsp_room_accumulated': 20_000, 'tfsa_room_accumulated': 20_000,
             'tuition_by_year': {2026: 9_000}},
        ],
        projection_years=1,
    )
    with trace_firing() as fired:
        simulate_year_pure(
            state=SimState(jurisdiction_state={'canada': _default_canada_state()}),
            year=0,
            allocations={'_primary_income': 120_000, '_spouse_income': 45_000,
                         '_annual_savings': 0},
            config=tuition_config, investment_return=0.0,
            primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
            calendar_year=2026,
            # epic #795 bite 3: the prologue passes the pre-credit tax_before
            # + the tax provider; use the default provider (the rule falls
            # back to it when tax_provider is None) and round-number tax_before
            # figures large enough that the credit (fed 14% + QC 8% = 22% of
            # tuition) is less than tax, so it APPLIES (fired=True) rather than
            # only carrying forward.
            tax_provider=None,
            primary_tax_before=20_000.0, spouse_tax_before=8_000.0,
        )
    _merge(fired)

    # ── Scenario M: a household that DECLARES a mid-horizon property SALE
    # (issue #956 bite B: the property_disposition rule -- net proceeds
    # invested, gain taxed + PRE-apportioned). Every other scenario's
    # properties (incl. the golden fixture's principal residence) have no
    # `sale`, so the rule correctly stays a no-op (fired=False) everywhere
    # else -- the rule's whole job is to price a disposition none of the other
    # representative households declare, which is exactly the coverage property
    # #584 asks for: a rule that is correctly implemented but unreached by
    # every OTHER scenario. Without this scenario it would be exactly the
    # #627 shape (registered, ordered, never reached). A couple-owned cottage
    # sold in the scenario's calendar year (2026), with an accrued gain
    # (value_share > acb_share) so the capital-gains tax is non-zero. Round
    # numbers, role-based names (DP#4/#15).
    sale_property = {
        'id': 'cottage',
        'kind': 'recreational',
        'net_equity': 200_000.0,        # value_share - secured_share
        'value_share': 500_000.0,       # couple's share of gross value
        'secured_share': 300_000.0,     # couple's share of the mortgage
        'acb_share': 400_000.0,         # couple's share of ACB (a 100k gain)
        'sale': {
            'year': 2026,               # sold in this scenario's calendar year
            'selling_costs': 25_000.0,
            'owner_roles': {'primary': 0.5, 'spouse': 0.5},
            'designated_principal_residence_years': [],  # fully taxable gain
        },
    }
    sale_config = _make_config(
        projection_years=1,
        properties=[sale_property],
    )
    sale_state = SimState(jurisdiction_state={'canada': _default_canada_state()})
    with trace_firing() as fired:
        simulate_year_pure(
            state=sale_state, year=0,
            calendar_year=2026,
            allocations={'_primary_income': 130_000, '_spouse_income': 50_000,
                         '_annual_savings': 0},
            config=sale_config, investment_return=0.0,
            primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
            # The prologue resolves year_brackets for the marginal rates; pass
            # the same combined brackets the tax_calculator uses so the gain
            # bands against the owner's taxable income. The property_disposition
            # rule needs these to band the gain (a real bracket list, not None).
            year_brackets=_default_tax_provider_combined_brackets(),
            # Issue #956 bite B: the taxable income base the gain bands against.
            primary_taxable_income=130_000.0, spouse_taxable_income=50_000.0,
        )
    _merge(fired)

    # ── Scenario N: a household that DECLARES a mid-horizon SALE of the
    # PRINCIPAL residence (issue #956 bite E: the principal_disposition rule
    # -- home + mortgage + HELOC/SM leave the balance sheet, net proceeds
    # invested, gain taxed + PRE-apportioned ≈ 0 for a fully-designated home).
    # Every other scenario's principal (incl. the golden fixture, which
    # builds SimulationConfig.from_dict straight from a legacy dict with no
    # `principal_sale`) carries no sale, so the rule correctly stays a no-op
    # (fired=False) everywhere else -- the rule's whole job is to price a
    # principal disposition none of the other representative households
    # declare, which is exactly the coverage property #584 asks for. Without
    # this scenario it would be the #627 shape (registered, ordered, never
    # reached). A couple-owned principal sold in the scenario's calendar
    # year (2026), fully PRE-designated (tax ≈ 0), with a mortgage and a drawn
    # HELOC balance so the debt-discharge leg fires too. Round numbers,
    # role-based names (DP#4/#15). The principal carries value_share (full
    # value at the couple's 100% share here), acb_share = value_share (null
    # acb -> bought at value -> no accrued gain), and the PRE periods that
    # make the gain fully exempt.
    principal_sale_config = _make_config(
        projection_years=1,
        house_value=800_000,
        mortgage_balance=300_000,
        margin_available=100_000,
        principal_sale={
            'year': 2026,
            'selling_costs': 40_000.0,
            'owner_roles': {'primary': 0.5, 'spouse': 0.5},
            # Designate the whole ownership (2023 onward) -> fully PRE-exempt
            # -> taxable_fraction ≈ 0 -> disposition tax ≈ 0 (ITA s.40(2)(b)).
            'designated_principal_residence_years': [
                {'from': '2023-01-01', 'to': None}],
            'value_share': 800_000.0,   # couple's 100% share of gross value
            'acb_share': 800_000.0,     # null acb -> value_share -> no gain
        },
    )
    principal_sale_state = SimState(
        mortgage_balance=300_000,
        heloc_balance=50_000,           # a drawn HELOC margin to discharge
        jurisdiction_state={'canada': _default_canada_state()},
    )
    with trace_firing() as fired:
        simulate_year_pure(
            state=principal_sale_state, year=0,
            calendar_year=2026,
            allocations={'_primary_income': 130_000, '_spouse_income': 50_000,
                         '_annual_savings': 0},
            config=principal_sale_config, investment_return=0.0,
            primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
            year_brackets=_default_tax_provider_combined_brackets(),
            primary_taxable_income=130_000.0, spouse_taxable_income=50_000.0,
        )
    _merge(fired)

    # ── Scenario O: a household that DECLARES a mid-horizon MORTGAGE on a
    # second property (issue #967: the second_property_mortgage rule --
    # originate + service principal/interest from the purchase year to
    # payoff). Every other scenario's properties (incl. the golden
    # fixture) have no `purchase.financing`, so the rule correctly stays a
    # no-op (fired=False) everywhere else -- the rule's whole job is to
    # service a financed purchase none of the other representative
    # households declare, which is exactly the coverage property #584 asks
    # for: a rule that is correctly implemented but unreached by every OTHER
    # scenario. Without this scenario it would be the #627 shape
    # (registered, ordered, never reached). A couple-owned rental bought in
    # the scenario's calendar year (2026) with a financed mortgage; the
    # precomputed annual amortization schedule is built by the same pure
    # helper input_contract uses, so the servicing reads one spelling. Round
    # numbers, role-based names (DP#4/#15).
    from contract_property import _annual_amortization_schedule
    financed_schedule = _annual_amortization_schedule(
        principal=300_000.0, annual_rate=0.05,
        amortization_years=25, origination_year=2026,
        projection_years=5)
    financed_property = {
        'id': 'rental_fin',
        'kind': 'rental',
        'net_equity': 100_000.0,      # the down payment (value - mortgage)
        'value_share': 400_000.0,
        'secured_share': 300_000.0,
        'purchase': {
            'year': 2026,
            'closing_costs': 10_000.0,
            'financing': {
                'mortgage_amount': 300_000.0,
                'rate': 0.05,
                'rate_type': 'fixed',
                'amortization_years': 25,
                'origination_year': 2026,
                'deductible': True,
                'owner_roles': {'primary': 0.5, 'spouse': 0.5},
                'schedule': financed_schedule,
            },
        },
    }
    financed_config = _make_config(
        projection_years=1,
        properties=[financed_property],
    )
    # Seed the parallel mortgage-balance list (SimState.initial would do this;
    # this unit-test path builds SimState directly, so it seeds the parallel
    # list itself -- the same way the consumer-loan scenarios above seed
    # consumer_loan_balances). 0.0 = the mortgage has not yet originated (it
    # originates in the purchase year from the schedule).
    financed_state = SimState(
        second_property_mortgage_balances=[0.0],
        jurisdiction_state={'canada': _default_canada_state()},
    )
    with trace_firing() as fired:
        simulate_year_pure(
            state=financed_state, year=0,
            calendar_year=2026,
            allocations={'_primary_income': 130_000, '_spouse_income': 50_000,
                         '_annual_savings': 0},
            config=financed_config, investment_return=0.0,
            primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
        )
    _merge(fired)

    never_fired = sorted(name for name, fired in fired_ever.items() if not fired)
    assert not never_fired, (
        f"rule(s) {never_fired} are registered (present in RULE_ORDER) but "
        f"never had an observable effect in ANY of the representative "
        f"scenarios above -- this is the #627 failure shape: a rule that is "
        f"nominally present but effectively dead. Either the rule's wiring "
        f"is broken, or a scenario needs to be added/extended to actually "
        f"exercise it.")


def test_golden_household_alone_does_not_exercise_every_rule():
    """Sanity check on the coverage sweep's own premise: the golden
    household is NOT sufficient by itself (SM and LIRA/LIF are configured
    off), so combining it with the other scenarios above is doing real
    work, not being a redundant belt-and-suspenders check."""
    with trace_firing() as fired:
        _run_golden(golden_household_config())
    never_fired_in_golden_alone = {name for name, was_fired in fired.items() if not was_fired}
    assert never_fired_in_golden_alone, (
        "expected the golden household alone to leave at least one rule "
        "(SM/LIRA-LIF) unfired -- if this now fails, the golden household's "
        "fixture changed to configure SM or LIRA, and the extra scenarios "
        "in test_every_rule_fires_somewhere_in_representative_households "
        "may no longer be necessary (though keeping them is harmless)")
    # The SM rules specifically must be among them (golden never turns on
    # the Smith Manoeuvre — see golden_household_config()'s docstring).
    assert 'sm_readvance' in never_fired_in_golden_alone

# ============================================================================
# epic #795 bite 1 (DP#18/#710): the registered `retirement_income` rule is
# not dead -- a household that retires mid-projection must (a) actually fire
# it and (b) produce a DIFFERENT terminal result than the same household run
# with the rule surgically disabled. A rule that is registered but whose
# outputs no consumer reads, or whose firing changes no number, is the same
# defect as a rule that was never written (#627's shape, generalized).
# ============================================================================

def _retiring_household_config():
    """A fabricated household whose primary retires mid-projection.

    Primary born 1976, retirement_age 65 -> retires in 2041 (projection year
    16 with start_year 2026). Round numbers, role-based names (DP#4/DP#15):
    no real data. Carries RRSP/TFSA balances so the drawdown the rule sizes
    has something to draw against, exercising the rule's full output path
    (CPP/OAS/pension + drawdown_net_target + rrif_min_rate + any_retired).
    """
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1976, 'gross_income': 120_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 1_200,
                 'rrsp_room_accumulated': 40_000, 'tfsa_room_accumulated': 20_000,
                 'rrsp_balance': 300_000, 'tfsa_balance': 40_000},
                {'role': 'spouse', 'birth_year': 1978, 'gross_income': 80_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 1_000,
                 'rrsp_room_accumulated': 25_000, 'tfsa_room_accumulated': 20_000,
                 'rrsp_balance': 150_000, 'tfsa_balance': 30_000},
            ],
            'children': [],
        },
        'accounts': {'resp_current_balance': 0, 'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': 2026, 'projection_years': 25,
            'investment_return': 0.06, 'salary_growth': 0.02,
            'inflation': 0.02, 'frozen_brackets': True, 'time_step': 'yearly',
        },
        'portfolio': {
            'accounts': {
                'non_reg': {'balance': 0, 'cost_basis': 0,
                            'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                            'yield': {'eligible_dividends': 0.015, 'interest': 0.01}},
            },
        },
        'property': {
            'house_value': 800_000, 'mortgage_balance': 400_000,
            'mortgage_rate': 0.05, 'amortization_years': 20,
            'margin_available': 0, 'ltv_max': 0.80, 'heloc_readvance': False,
        },
        'savings': {'rate': 0.20},
        'retirement': {'spending_target': 70_000, 'rrif_conversion_age': 71},
        'tax': {'province': 'qc'},
    }


def test_retirement_income_rule_fires_for_a_retiring_household():
    """The rule must actually fire (mark itself fired) in a year where a
    member has reached retirement_age."""
    with trace_firing() as fired:
        _run_golden(_retiring_household_config())
    assert fired.get('retirement_income', False), (
        "retirement_income never fired for a household whose primary retires "
        "mid-projection -- a registered rule that does not fire is the #627 "
        "defect shape (nominally present, silently inert).")


def test_retirement_income_rule_changes_engine_output():
    """DP#18/#710: disabling the rule must change the terminal result.

    With the rule, the household's CPP/OAS/pension turn on at retirement and
    a drawdown is sized. Without it, those outputs stay at their seeded
    defaults (0.0/False/None) -- no CPP/OAS/pension, no drawdown target, no
    retirement spending figure -- so terminal total_assets must differ.
    A rule whose firing changes no number is dead weight.
    """
    from simulation_rules import RULES

    baseline_results = _run_golden(_retiring_household_config())
    baseline_terminal = baseline_results[-1].total_assets

    # Surgically stub the rule to a no-op (preserve RULE_ORDER so run_rules
    # still sees the name declared + implemented -- the DP#32 loud-failure
    # guard stays intact; only the rule's EFFECT is removed).
    original = RULES['retirement_income']
    RULES['retirement_income'] = lambda ws, ctx: False
    try:
        disabled_results = _run_golden(_retiring_household_config())
    finally:
        RULES['retirement_income'] = original

    disabled_terminal = disabled_results[-1].total_assets
    assert baseline_terminal != disabled_terminal, (
        f"terminal total_assets is identical ({baseline_terminal}) with vs "
        f"without the retirement_income rule -- the rule's outputs reach no "
        f"consumer, so it is a dead rule (DP#18/#710).")
    # Sanity: the retirement years' CPP is present with the rule and absent
    # without it (a stronger, more localized signal than the terminal scalar
    # -- guards against a coincidental terminal equality).
    with_rule_cpp = [r.cpp_income for r in baseline_results if r.any_retired]
    without_rule_cpp = [r.cpp_income for r in disabled_results if r.any_retired]
    assert any(c > 0 for c in with_rule_cpp), "expected nonzero CPP after retirement with the rule"
    assert all(c == 0 for c in without_rule_cpp), (
        "expected zero CPP after retirement with the rule disabled -- the "
        "rule is the sole source of CPP/OAS/pension in the fold")
def test_retirement_income_rule_defensive_no_birth_year_branch():
    """Cover the `_rrif_rate` `if not by: return 0.0` defensive guard (epic
    #795 bite 1). This branch is unreachable through the live fold --
    retirement status is date-derived from birth_year (countries.canada
    .retirement_transition.is_retired returns False when birth_year is 0),
    so a member the fold marks retired always has a birth_year -- but the
    rule defends against a direct caller passing contradictory input
    (primary_retired=True with a member carrying no birth_year). Exercised
    here directly so the coverage ratchet does not loosen on the moved code.
    """
    from simulation_rules import RuleContext, YearWorkingState, RULES
    from simulation_config import SimulationConfig
    from simulation import FamilySimulation

    base = {
        'family': {'members': [
            {'role': 'primary', 'birth_year': 0, 'retirement_age': 65,
             'gross_income': 0, 'cpp_monthly_estimated': 100,
             'oas_start_age': 65}], 'children': []},
        'accounts': {'rrsp_annual_max': 31000},
        'assumptions': {'projection_years': 1, 'investment_return': 0.05,
                        'salary_growth': 0.0, 'start_year': 2026,
                        'frozen_brackets': True, 'time_step': 'yearly'},
        'savings': {'rate': 0.1},
        'property': {'house_value': 600000, 'mortgage_balance': 0,
                     'mortgage_rate': 0.045, 'ltv_max': 0.8,
                     'margin_available': 0},
        'retirement': {}, 'cash_flows': [], 'tax': {'province': 'qc'},
    }
    cfg = SimulationConfig.from_dict(base)
    sim = FamilySimulation(cfg)
    ws = YearWorkingState(year=0)
    ctx = RuleContext(
        year=0, calendar_year=2026, allocations={}, config=cfg,
        investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=0.0, spouse_marginal_rate=0.0, resp_data=None,
        fhsa_contribution=0.0, rrsp_annual_limit=None, tfsa_annual_limit=None,
        fhsa_annual_limit=None, non_reg_after_tax_return=None,
        cpp_income=0.0, oas_income=0.0, pension_income=0.0, drawdown_order=None,
        rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0,
        retiree_marginal_rate=0.0, drawdown_bracket_target=None,
        drawdown_other_taxable_income=0.0,
        # Contradictory input: member marked retired but carrying no
        # birth_year. The rule must defend (rrif rate 0) rather than crash.
        primary_retired=True, spouse_retired=False,
        base_primary_income=0.0, base_spouse_income=0.0,
        year_brackets=sim.brackets, tax_indexation_rate=0.0,
    )
    RULES['retirement_income'](ws, ctx)
    assert ws.rrif_min_rate_primary == 0.0
    assert ws.any_retired


# ============================================================================
# epic #795 bite 3 (DP#18/#710): the registered `tuition_credit` rule is not
# dead -- a household that declares tuition must (a) actually fire it and
# (b) produce a DIFFERENT terminal result than the same household run with
# the rule surgically disabled. A rule that is registered but whose outputs
# no consumer reads, or whose firing changes no number, is the same defect as
# a rule that was never written (#627's shape, generalized -- the same
# provenance as the retirement_income block above).
# ============================================================================

def _tuition_household_config():
    """A fabricated couple where BOTH taxed members declare tuition in the
    first projection year (Quebec, so the QC provincial credit fires too).
    Round numbers, role-based names (DP#4/DP#15): no real data. Carries
    income high enough that the credit APPLIES (rather than only carrying
    forward), exercising the rule's full own-credit path (#764/#783).
    """
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
                 'retirement_age': 95, 'rrsp_balance': 0, 'tfsa_balance': 0,
                 'tuition_by_year': {2026: 12_000}},
                {'role': 'spouse', 'birth_year': 1982, 'gross_income': 45_000,
                 'retirement_age': 95, 'rrsp_balance': 0, 'tfsa_balance': 0,
                 'tuition_by_year': {2026: 9_000}},
            ],
            'children': [],
        },
        'accounts': {},
        'assumptions': {'start_year': 2026, 'projection_years': 3,
                        'investment_return': 0.0, 'salary_growth': 0.0,
                        'inflation': 0.0, 'frozen_brackets': True,
                        'time_step': 'yearly'},
        'portfolio': {'accounts': {}},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'mortgage_rate': 0.0, 'amortization_years': 25,
                     'margin_available': 0, 'ltv_max': 0.80, 'heloc_readvance': False},
        'savings': {'rate': 0.0},
        'retirement': {'spending_target': 0, 'rrif_conversion_age': 71},
        'tax': {'province': 'qc'},
        # living_costs so the solvency identity + after_tax_income observable fire.
        'household_budget': {'living_costs': 50_000},
    }


def test_tuition_credit_rule_fires_for_a_tuition_household():
    """The rule must actually fire (mark itself fired) in a year where a
    member declares tuition."""
    with trace_firing() as fired:
        _run_golden(_tuition_household_config())
    assert fired.get('tuition_credit', False), (
        "tuition_credit never fired for a household whose members declare "
        "tuition in the first projection year -- a registered rule that does "
        "not fire is the #627 defect shape (nominally present, silently inert).")


def test_tuition_credit_rule_changes_engine_output():
    """DP#18/#710: disabling the rule must change the terminal result.

    With the rule, each member's tuition credit reduces their tax, so
    after_tax_income is HIGHER than without it (the credit is non-refundable
    but the members' tax exceeds the credit, so it applies in full). Without
    the rule, the credit stays at its seeded 0.0 default -- no tax reduction
    -- so terminal total_assets must differ (the after-tax income funds the
    solvency identity's `available`, and here the household's living_costs
    make the identity bind, so a higher after-tax income changes the
    shortfall/liquidation the waterfall forces). A rule whose firing changes
    no number is dead weight.
    """
    from simulation_rules import RULES

    baseline_results = _run_golden(_tuition_household_config())
    baseline_after_tax = baseline_results[0].after_tax_income

    # Surgically stub the rule to a no-op (preserve RULE_ORDER so run_rules
    # still sees the name declared + implemented -- the DP#32 loud-failure
    # guard stays intact; only the rule's EFFECT is removed).
    original = RULES['tuition_credit']
    RULES['tuition_credit'] = lambda ws, ctx: False
    try:
        disabled_results = _run_golden(_tuition_household_config())
    finally:
        RULES['tuition_credit'] = original

    disabled_after_tax = disabled_results[0].after_tax_income
    assert baseline_after_tax > disabled_after_tax, (
        f"year-1 after_tax_income is not higher with the tuition_credit rule "
        f"({baseline_after_tax}) than without it ({disabled_after_tax}) -- the "
        f"rule's tax reduction reaches no consumer, so it is a dead rule "
        f"(DP#18/#710). The credit should reduce each member's tax, raising "
        f"after_tax_income.")
    # Sanity: the carry-forward the rule surfaces is present with the rule and
    # absent without it (a stronger, more localized signal than the after-tax
    # scalar -- guards against a coincidental equality).
    assert baseline_results[0].primary_tuition_carryforward >= 0.0
    assert disabled_results[0].primary_tuition_carryforward == 0.0, (
        "expected zero carry-forward with the rule disabled -- the rule is "
        "the sole source of the tuition carry-forward in the fold")