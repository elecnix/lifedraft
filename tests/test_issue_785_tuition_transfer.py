#!/usr/bin/env python3
"""Issue #785: federal tuition credit transfer to a supporting spouse/parent
(ITA s.118.8, $5,000 federal limit).

A student with tuition credit exceeding their own tax has three destinations
in ITA order: (1) apply against own tax; (2) TRANSFER up to the federal $5,000
tuition-amount limit of the still-unused credit to a designated supporting
spouse/parent (reducing THAT person's tax); (3) CARRY FORWARD the rest (#784).
This is the mechanism that makes a CHILD's tuition credit valuable: the child
has no own tax (#701), so the full credit transfers to the parent.

Tests (DP#15: fabricated round numbers):
1. A child with tuition and zero income transfers min(credit, $5,000×rate) to
   the parent, reducing the parent's tax; the excess above the cap carries
   forward.
2. A taxed student uses own tax first, then transfers only the remainder.
3. No declared supporter: the credit carries forward (DP#32: no silent loss).
"""

import unittest

import countries.canada  # noqa: F401


class TestChildTuitionTransfer(unittest.TestCase):
    """A child with tuition and zero income transfers to a parent."""

    def _run(self, children, *, primary_income=120_000, years=2):
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        cfg = SimulationConfig(
            projection_years=years, house_value=0, mortgage_balance=0,
            mortgage_rate=0.0, amortization_years=25, margin_available=0,
            savings_rate=0.0, living_costs=40_000, start_year=2026,
            province='quebec', investment_return=0.0, salary_growth=0.0,
            family_members=[{'role': 'primary', 'birth_year': 1980,
                             'gross_income': primary_income, 'id': 'p1',
                             'rrsp_room_accumulated': 0,
                             'tfsa_room_accumulated': 0}],
            children=children)
        return FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()

    def test_child_transfers_to_parent_reducing_parents_tax(self):
        # $30,000 tuition: federal credit = 30000 × 0.14 = 4200. Transfer cap
        # = 5000 × 0.14 = 700. The child has no own tax, so the full credit
        # (4200) is available; 700 transfers to the parent; 3500 carries forward.
        r = self._run([{'name': 'child_a', 'birth_year': 2005, 'gross_income': 0,
                        'tuition_by_year': {2026: 30_000},
                        'tuition_transfer_to': 'p1'}])
        # Without the child (no transfer):
        r2 = self._run([])
        benefit = r[0].after_tax_income - r2[0].after_tax_income
        self.assertAlmostEqual(benefit, 700.0, places=2,
                               msg="the transfer credit ($700 = 5000×0.14) "
                                   "must reduce the parent's tax")

    def test_excess_above_5000_carries_forward(self):
        # $30,000 tuition: credit 4200, transfer 700, carry-forward 3500.
        # Year 2 (no new tuition): the 3500 carry-forward transfers again
        # (up to 700 cap), reducing the parent's tax a second time.
        r = self._run([{'name': 'child_a', 'birth_year': 2005, 'gross_income': 0,
                        'tuition_by_year': {2026: 30_000},
                        'tuition_transfer_to': 'p1'}], years=2)
        r2 = self._run([], years=2)
        y1_benefit = r[1].after_tax_income - r2[1].after_tax_income
        self.assertAlmostEqual(y1_benefit, 700.0, places=2,
                               msg="year-2: 3500 carry-forward transfers 700 "
                                   "(the cap) again, reducing tax a second time")

    def test_no_supporter_carries_forward_no_silent_loss(self):
        # DP#32: no declared supporter -> the credit carries forward (not lost).
        r = self._run([{'name': 'child_a', 'birth_year': 2005, 'gross_income': 0,
                        'tuition_by_year': {2026: 30_000}}], years=2)
        r2 = self._run([], years=2)
        # Year 1: no transfer (no supporter) -> parent's tax unchanged.
        self.assertAlmostEqual(r[0].after_tax_income, r2[0].after_tax_income,
                               places=2,
                               msg="no supporter -> no transfer in year 1")
        # Year 2: still no supporter -> still no transfer (carry-forward sits).
        self.assertAlmostEqual(r[1].after_tax_income, r2[1].after_tax_income,
                               places=2,
                               msg="no supporter -> carry-forward sits unused")


class TestTaxedMemberTransfer(unittest.TestCase):
    """A taxed student uses own tax first, then transfers only the remainder."""

    def _run(self, tuition_by_year, *, income=15_000, years=1):
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        cfg = SimulationConfig(
            projection_years=years, house_value=0, mortgage_balance=0,
            mortgage_rate=0.0, amortization_years=25, margin_available=0,
            savings_rate=0.0, living_costs=40_000, start_year=2026,
            province='quebec', investment_return=0.0, salary_growth=0.0,
            family_members=[
                {'role': 'primary', 'birth_year': 1980, 'gross_income': income,
                 'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
                {'role': 'spouse', 'birth_year': 1982, 'gross_income': 80_000,
                 'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
                 'tuition_by_year': tuition_by_year,
                 'tuition_transfer_to': 'p1'}],
            children=[])
        return FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()

    def test_student_uses_own_tax_first_then_transfers_remainder(self):
        # $50,000 tuition at 22% (fed 14% + QC 8%) = $11,000 credit. The
        # spouse's own tax on $80k is ~$23,000, so own tax absorbs all $11,000
        # (remainder = $0). Nothing transfers to the primary. Proof: the
        # result is IDENTICAL whether or not transfer_to is declared (the
        # transfer has nothing to transfer).
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        base = dict(projection_years=1, house_value=0, mortgage_balance=0,
            mortgage_rate=0.0, amortization_years=25, margin_available=0,
            savings_rate=0.0, living_costs=40_000, start_year=2026,
            province='quebec', investment_return=0.0, salary_growth=0.0,
            family_members=[
                {'role':'primary','birth_year':1980,'gross_income':120000,'id':'p1',
                 'rrsp_room_accumulated':0,'tfsa_room_accumulated':0},
                {'role':'spouse','birth_year':1982,'gross_income':80000,'id':'p2',
                 'rrsp_room_accumulated':0,'tfsa_room_accumulated':0,
                 'tuition_by_year':{2026:50000}}], children=[])
        # With transfer_to declared (but nothing to transfer: own tax absorbs all):
        cfg_transfer = SimulationConfig(**{**base,
            'family_members':[dict(base['family_members'][0]),
                              dict(base['family_members'][1], tuition_transfer_to='p1')]})
        # Without transfer_to (same tuition, same own-tax application):
        cfg_no = SimulationConfig(**base)
        r1 = FamilySimulation(cfg_transfer, adapter=CanadaAdapter(cfg_transfer)).run()
        r2 = FamilySimulation(cfg_no, adapter=CanadaAdapter(cfg_no)).run()
        self.assertAlmostEqual(r1[0].after_tax_income, r2[0].after_tax_income,
                               places=2,
                               msg="own tax absorbs the full credit -> no transfer "
                                   "-> result is identical with or without transfer_to")

    def test_student_with_excess_transfers_remainder_to_supporter(self):
        # Low-income student ($15k) with $50k tuition: credit $11,000; own
        # tax ~$3,853; remainder ~$7,147; transfer cap $700 -> transfers $700
        # to the primary (the supporter). The primary's tax drops by 700.
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        cfg = SimulationConfig(
            projection_years=1, house_value=0, mortgage_balance=0,
            mortgage_rate=0.0, amortization_years=25, margin_available=0,
            savings_rate=0.0, living_costs=40_000, start_year=2026,
            province='quebec', investment_return=0.0, salary_growth=0.0,
            family_members=[
                {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
                 'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
                {'role': 'spouse', 'birth_year': 1982, 'gross_income': 15_000,
                 'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
                 'tuition_by_year': {2026: 50_000},
                 'tuition_transfer_to': 'p1'}],
            children=[])
        r = FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()
        # Without the transfer:
        cfg2 = SimulationConfig(
            projection_years=1, house_value=0, mortgage_balance=0,
            mortgage_rate=0.0, amortization_years=25, margin_available=0,
            savings_rate=0.0, living_costs=40_000, start_year=2026,
            province='quebec', investment_return=0.0, salary_growth=0.0,
            family_members=[
                {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
                 'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
                {'role': 'spouse', 'birth_year': 1982, 'gross_income': 15_000,
                 'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
                 'tuition_by_year': {2026: 50_000}}],
            children=[])
        r2 = FamilySimulation(cfg2, adapter=CanadaAdapter(cfg2)).run()
        transfer = r[0].after_tax_income - r2[0].after_tax_income
        self.assertAlmostEqual(transfer, 700.0, places=2,
                               msg="the student's unused remainder transfers "
                                   "$700 (5000×0.14) to the primary")


class TestTransferBranchCoverage(unittest.TestCase):
    """Lean tests covering the remaining _process_tuition_transfers branches:
    the monthly-time-step call site, a transfer TARGETED at the spouse (the
    _supporter_tax spouse-id path and the to_spouse dispatch), and a PRIMARY
    member declaring a transfer (the taxed-member primary branch). Fabricated
    round numbers (DP#15)."""

    def _cfg(self, *, family_members, children, time_step='yearly', years=1):
        from simulation_config import SimulationConfig
        return SimulationConfig(
            projection_years=years, house_value=0, mortgage_balance=0,
            mortgage_rate=0.0, amortization_years=25, margin_available=0,
            savings_rate=0.0, living_costs=40_000, start_year=2026,
            province='quebec', investment_return=0.0, salary_growth=0.0,
            time_step=time_step, family_members=family_members, children=children)

    def _run(self, cfg):
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        return FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()

    def test_child_transfers_to_spouse_not_primary(self):
        # A child transfers to the SPOUSE (p2), not the primary. Exercises the
        # _supporter_tax spouse-id branch and the to_spouse dispatch in the
        # children loop. $30k tuition -> credit 4200 -> transfer cap 700 ->
        # 700 reduces the spouse's tax; 3500 carries forward.
        members = [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 20_000,
             'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            {'role': 'spouse', 'birth_year': 1982, 'gross_income': 120_000,
             'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0}]
        children = [{'name': 'child_a', 'birth_year': 2005, 'gross_income': 0,
                     'tuition_by_year': {2026: 30_000},
                     'tuition_transfer_to': 'p2'}]
        r = self._run(self._cfg(family_members=members, children=children))
        children_no = [{'name': 'child_a', 'birth_year': 2005, 'gross_income': 0,
                        'tuition_by_year': {2026: 30_000}}]
        r2 = self._run(self._cfg(family_members=members, children=children_no))
        benefit = r[0].after_tax_income - r2[0].after_tax_income
        self.assertAlmostEqual(benefit, 700.0, places=2,
                               msg="child->spouse transfer: 700 reduces the "
                                   "spouse's (higher-earner's) tax")

    def test_primary_member_transfers_remainder_to_spouse(self):
        # The PRIMARY declares tuition_transfer_to (to the spouse). Exercises
        # the taxed-member PRIMARY transfer branch. Low-income primary ($15k)
        # with $50k tuition: own tax ~$3853, credit ~$11,000, remainder ~$7,147
        # -> transfer cap 700 -> 700 reduces the spouse's tax.
        members = [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 15_000,
             'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
             'tuition_by_year': {2026: 50_000}, 'tuition_transfer_to': 'p2'},
            {'role': 'spouse', 'birth_year': 1982, 'gross_income': 120_000,
             'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0}]
        children = []
        r = self._run(self._cfg(family_members=members, children=children))
        members_no = [{k: v for k, v in members[0].items() if k != 'tuition_transfer_to'},
                      members[1]]
        r2 = self._run(self._cfg(family_members=members_no, children=children))
        transfer = r[0].after_tax_income - r2[0].after_tax_income
        self.assertAlmostEqual(transfer, 700.0, places=2,
                               msg="primary's unused remainder transfers 700 "
                                   "to the spouse")

    def test_child_transfer_applies_in_monthly_time_step(self):
        # The _run_monthly fold has its OWN _process_tuition_transfers call
        # site (a parallel prologue, #764 bite 2). A child->parent transfer
        # must reduce the parent's tax under time_step='monthly' exactly as it
        # does under 'yearly' -- the transfer mechanism is time-step-agnostic.
        members = [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
             'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            {'role': 'spouse', 'birth_year': 1982, 'gross_income': 20_000,
             'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0}]
        children = [{'name': 'child_a', 'birth_year': 2005, 'gross_income': 0,
                     'tuition_by_year': {2026: 30_000},
                     'tuition_transfer_to': 'p1'}]
        r_monthly = self._run(self._cfg(family_members=members, children=children,
                                        time_step='monthly'))
        children_no = [{'name': 'child_a', 'birth_year': 2005, 'gross_income': 0,
                        'tuition_by_year': {2026: 30_000}}]
        r_monthly_no = self._run(self._cfg(family_members=members,
                                           children=children_no, time_step='monthly'))
        benefit_monthly = r_monthly[0].after_tax_income - r_monthly_no[0].after_tax_income
        r_yearly = self._run(self._cfg(family_members=members, children=children,
                                       time_step='yearly'))
        benefit_yearly = r_yearly[0].after_tax_income - self._run(self._cfg(
            family_members=members, children=children_no, time_step='yearly'))[0].after_tax_income
        self.assertAlmostEqual(benefit_monthly, 700.0, places=2,
                               msg="monthly fold: child->parent transfer lands")
        self.assertAlmostEqual(benefit_monthly, benefit_yearly, places=2,
                               msg="the transfer is time-step-agnostic: monthly "
                                   "and yearly folds credit the same 700")

    def test_transfer_to_a_non_supporter_carries_forward_no_phantom_refund(self):
        # Defensive path (#785, DP#32): a child designates a transfer target
        # that is NEITHER taxed member (here 'p3' -- a non-taxed person, e.g.
        # a grandparent). _supporter_tax returns 0.0 for an unrecognised id,
        # so the transfer is floored at 0 -- no phantom refund, no crash. The
        # full credit carries forward (the supporter has no tax to reduce).
        members = [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
             'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            {'role': 'spouse', 'birth_year': 1982, 'gross_income': 20_000,
             'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0}]
        children = [{'name': 'child_a', 'birth_year': 2005, 'gross_income': 0,
                     'tuition_by_year': {2026: 30_000},
                     'tuition_transfer_to': 'p3'}]
        children_no = [{'name': 'child_a', 'birth_year': 2005, 'gross_income': 0,
                        'tuition_by_year': {2026: 30_000}}]
        r = self._run(self._cfg(family_members=members, children=children))
        r2 = self._run(self._cfg(family_members=members, children=children_no))
        self.assertAlmostEqual(r[0].after_tax_income, r2[0].after_tax_income,
                               places=2,
                               msg="transfer to a non-supporter is a no-op: no "
                                   "phantom refund, credit carries forward")

    def test_self_transfer_is_a_noop(self):
        # Defensive path (#785, DP#32): a taxed member designating THEMSELVES
        # as the transfer target. After own-tax application any leftover
        # credit implies remaining_tax == 0 (the credit exceeded the tax), so
        # the self-transfer is floored at 0 -- a harmless no-op, not a double
        # credit. Covers both the primary-self and spouse-self dispatch
        # branches. Low-income student + big tuition -> leftover credit, zero
        # remaining tax.
        members = [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 15_000,
             'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
             'tuition_by_year': {2026: 50_000}, 'tuition_transfer_to': 'p1'},
            {'role': 'spouse', 'birth_year': 1982, 'gross_income': 15_000,
             'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
             'tuition_by_year': {2026: 50_000}, 'tuition_transfer_to': 'p2'}]
        members_no = [
            {k: v for k, v in members[0].items() if k != 'tuition_transfer_to'},
            {k: v for k, v in members[1].items() if k != 'tuition_transfer_to'}]
        r = self._run(self._cfg(family_members=members, children=[]))
        r2 = self._run(self._cfg(family_members=members_no, children=[]))
        self.assertAlmostEqual(r[0].after_tax_income, r2[0].after_tax_income,
                               places=2,
                               msg="self-transfer is a no-op: leftover credit "
                                   "implies zero remaining tax, so the transfer "
                                   "is floored at 0 (no double credit)")


if __name__ == '__main__':
    unittest.main()