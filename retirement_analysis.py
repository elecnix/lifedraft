#!/usr/bin/env python3
"""Retirement-age, longevity & after-tax legacy analysis.

Answers, for the household in --input, four questions the default simulate.py
ranking cannot:

  1. Impact of retirement age (55/58/60/62/65) on lifetime cash flow.
  2. Will the money last to age ~95? (longevity / shortfall detection)
  3. How large is the legacy left to the kids?
  4. How tax-efficient is that legacy? (deemed-disposition tax at death, and how
     the drawdown strategy changes it)

Everything is in REAL (today's) dollars — the input uses a real return, 0% real
salary growth, and frozen brackets — so balances, spending, and the estate tax
are directly comparable and interpretable.

Usage:
    uv run python retirement_analysis.py --input <path-to-input-retirement.json>
"""
import argparse
import copy
import json

from countries.canada.adapter import CanadaAdapter
from objective import compute_after_tax_estate
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from member_config import find_member_by_role


def run_sim(base_dict, *, retire_age, drawdown_order, spending_target):
    """Run one full-horizon simulation; return the list of YearResult."""
    d = copy.deepcopy(base_dict)
    for m in d['family']['members']:
        m['retirement_age'] = retire_age
    d['retirement']['drawdown_order'] = drawdown_order
    d['retirement']['spending_target'] = spending_target
    cfg = SimulationConfig.from_dict(d)
    # Exclude the Smith Manoeuvre leverage decision from the retirement/legacy
    # question: perpetual HELOC borrowing-to-invest over a 50-year horizon is a
    # separate analysis and otherwise swamps the drawdown signal. No new
    # readvancing; RRSP deductions taken as normal.
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run(), cfg


def net_worth(r):
    return r.total_assets - r.total_debt


def liquid(r):
    """Drawable financial assets (excludes house)."""
    return (r.total_rrsp + r.total_tfsa + r.non_reg_balance
            + r.lif_balance + r.lira_balance)


def longevity(results, start_year, retire_age, birth_year):
    """Detect whether/when financial assets run out.

    Money 'runs out' the first retired year the drawable financial assets
    (RRSP/TFSA/non-reg/LIF/LIRA — the house is not spent) fall to ~zero. After
    that the couple lives on CPP+OAS alone. Returns (lasts_to_horizon: bool,
    age_ran_out or None, terminal_liquid).

    Per DP#1: age is date-computed from the primary member's ``birth_year``,
    never a hardcoded constant — a prior version baked in ``1979`` and so
    computed every age (including the ``age >= retire_age`` gate that decides
    whether a year counts as retired at all) wrong for any household not born
    in 1979, reporting a household that runs out of money as fine (#756).
    Per DP#32: ``birth_year`` is required. Absence must fail loudly, not
    default to a plausible-looking person.
    """
    if not birth_year:
        raise ValueError(
            f"longevity(): birth_year is required (DP#1/DP#32) — got "
            f"{birth_year!r}. The primary member's birth_year must be present "
            f"in the input contract; a missing birth date must not become a "
            f"confident wrong age."
        )
    terminal_liq = liquid(results[-1])
    ran_out_age = None
    for i, r in enumerate(results):
        age = (start_year + i) - birth_year
        if age >= retire_age and liquid(r) <= 1000.0:
            ran_out_age = age
            break
    return (ran_out_age is None), ran_out_age, terminal_liq


def estate_of(results, cfg):
    """After-tax estate from the terminal YearResult.

    Issue #580: delegates to objective.compute_after_tax_estate (the shared,
    pluggable-objective wiring) instead of calling compute_estate() directly.
    This also fixes a double-count this function used to make: it netted the
    mortgage out of house_equity (house_value - mortgage_balance) AND passed
    r.total_debt (which already includes the mortgage) as `debts` -- the
    mortgage was being subtracted twice from the gross estate. The shared
    helper nets it exactly once. Tax year is now derived per-scenario from
    cfg.start_year + the scenario's own horizon (DP#20: year-versioned,
    rather than one brackets year fixed across every retirement age/drawdown
    combination the caller compares).
    """
    obj_cfg = {
        'property': {'house_value': cfg.house_value},
        'tax': {'province': cfg.province, 'start_year': cfg.start_year},
        # epic #603 Track C Phase 2c (#600): the DECLARED estate elections --
        # the spousal-rollover election in particular, which this script's whole
        # RRSP-meltdown-vs-TFSA-first comparison is sensitive to.
        'estate': cfg.estate_data,
    }
    return compute_after_tax_estate(results, obj_cfg)


def money(x):
    return f"${x:>13,.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    args = ap.parse_args()

    # Epic #603 Track C Phase 2b: args.input is a contract document (the
    # sole wire format, validated). load_and_map maps it to the internal
    # shape the rest of this script's dict manipulation below operates on.
    import input_contract
    base = input_contract.load_and_map(args.input)

    prov = base['tax']['province']

    ages = base['retirement'].get('candidate_ages', [55, 58, 60, 62, 65])
    base_spend = base['retirement'].get('spending_target', 125000)
    # Drawdown orders (locked-in LIF/LIRA included so they fund spending too).
    # TFSA-first defers taxable RRSP (grows the terminal tax bomb); RRSP-meltdown
    # drains registered first in low-income years and preserves the tax-free TFSA
    # for heirs.
    strategies = {
        'TFSA-first (default)': ['tfsa', 'non_reg', 'rrsp', 'lif', 'lira'],
        'RRSP-meltdown':        ['rrsp', 'lif', 'lira', 'non_reg', 'tfsa'],
    }
    start_year = base['assumptions']['start_year']

    # DP#1/#756: longevity() dates the horizon against the primary member's
    # birth_year. A missing birth date must fail loudly here (DP#32), not
    # default to a plausible person -- the prior `1979` baked into
    # longevity() computed every age wrong for any other household.
    primary = find_member_by_role(base['family']['members'], 'primary')  # #699 seam
    if primary is None:
        raise ValueError(
            "No 'primary' member in family.members — longevity() cannot date "
            "the horizon without the primary's birth_year (DP#1/DP#32)."
        )
    primary_birth_year = primary.get('birth_year')
    if not primary_birth_year:
        raise ValueError(
            f"Primary member has no birth_year (got {primary_birth_year!r}) — "
            f"longevity() cannot compute age from a missing birth date "
            f"(DP#1/DP#32); add birth_year to the primary member in the input."
        )

    print("=" * 100)
    print("  RETIREMENT · LONGEVITY · AFTER-TAX LEGACY   (all figures in today's real dollars)")
    print(f"  Input: {args.input}")
    # DP#21/#591: resolve the representative return through return_model
    # (the single source of truth) rather than assuming the deprecated
    # assumptions.investment_return scalar is set -- the input contract's
    # return_model is the only return field it ever emits (Epic #603 Phase
    # 2b: this script now reads a contract document, not the legacy shape,
    # which had investment_return as a commonly-set alternate spelling).
    from simulation_config import resolve_return_rate
    real_return = resolve_return_rate(base, year=0)
    print(f"  Real return: {real_return:.0%} | "
          f"Base spending: {money(base_spend)}/yr net | Horizon: age ~95 | Province: {prov.upper()}")
    print("=" * 100)

    # ── 1 & 2: Longevity / adequacy across spending levels (default drawdown) ──
    print("\n  [1] WILL THE MONEY LAST?  (retire@60, TFSA-first drawdown)")
    print("  Net spend/yr →  outcome (real $ left at 95, or age money runs out)")
    print("  " + "-" * 90)
    for spend in [90000, 100000, 110000, 125000, 140000, 160000]:
        res, cfg = run_sim(base, retire_age=60,
                           drawdown_order=strategies['TFSA-first (default)'],
                           spending_target=spend)
        lasts, ran, term_liq = longevity(res, start_year, 60, primary_birth_year)
        est = estate_of(res, cfg)
        tag = "lasts to 95+" if lasts else f"RUNS OUT at age {ran}"
        mark = "  " if lasts else ">>"
        print(f"{mark}{money(spend)}/yr → {tag:<22} | financial assets left @95: {money(term_liq)} "
              f"| net estate (incl. house): {money(est.net_estate)}")

    # ── 3 & 4: Legacy trade-off — retirement age × drawdown strategy ──
    for label, order in strategies.items():
        print(f"\n  [3/4] LEGACY BY RETIREMENT AGE — drawdown: {label}  "
              f"(spend {money(base_spend)}/yr net)")
        print(f"  {'Retire@':>7} {'Assets?':>16} {'GrossEstate':>15} {'DeathTax':>13} "
              f"{'NetEstate':>13} {'TaxRate':>8}  Registered@95 (taxbomb)")
        print("  " + "-" * 96)
        for age in ages:
            res, cfg = run_sim(base, retire_age=age, drawdown_order=order,
                               spending_target=base_spend)
            lasts, ran, term_liq = longevity(res, start_year, age, primary_birth_year)
            est = estate_of(res, cfg)
            lasts_s = "to 95+" if lasts else f"OUT@{ran}"
            print(f"  {age:>7} {lasts_s:>16} {money(est.gross_estate)} "
                  f"{money(est.total_tax)} {money(est.net_estate)} "
                  f"{est.effective_tax_rate:>7.1%}  {money(est.registered_gross)}")

    # ── Detail: sustainable case decomposition (retire@65) ──
    print(f"\n  [DETAIL] Estate composition — retire@65 (sustainable), spend {money(base_spend)}/yr")
    for label, order in strategies.items():
        res, cfg = run_sim(base, retire_age=65, drawdown_order=order,
                           spending_target=base_spend)
        est = estate_of(res, cfg)
        print(f"\n   · {label}:")
        print(f"       TFSA (tax-free)          {money(est.tfsa)}")
        print(f"       Home equity (exempt)     {money(est.house_equity)}")
        print(f"       Registered (RRSP/RRIF)   {money(est.registered_gross)}  "
              f"− death tax {money(est.registered_tax)}")
        print(f"       Non-reg                  {money(est.non_reg_gross)}  "
              f"− cap-gains tax {money(est.non_reg_tax)}")
        print(f"       less debts               {money(-est.debts)}")
        print(f"       ── GROSS ESTATE          {money(est.gross_estate)}")
        print(f"       ── DEATH TAX             {money(-est.total_tax)}  ({est.effective_tax_rate:.1%})")
        print(f"       ══ NET TO HEIRS          {money(est.net_estate)}")

    print("\n" + "=" * 100)


if __name__ == '__main__':
    main()
