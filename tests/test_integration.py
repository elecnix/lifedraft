#!/usr/bin/env python3
"""Integration test — verify all modules work together.

Uses fake data and round numbers only. No personal information.
"""

from tax_data import default_tax_provider
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tax_calculator import marginal_rate, effective_tax_rate, capital_gains_rate
from countries.canada.account_models import RRSPAccount, TSFAccount
from countries.canada.rate_model import ReadvanceableMortgage, build_rate_path, build_broker_scenarios, amortization_schedule

# Test cross-module integration
brackets = default_tax_provider().get_combined_brackets()
assert abs(marginal_rate(150000) - marginal_rate(150000, brackets)) < 0.0001, 'Tax functions mismatch!'

# Test account integration with round numbers
rrsp = RRSPAccount(contribution_room=200000)
actual, remaining = rrsp.contribute(50000)
assert actual == 50000
assert remaining == 150000

# Test readvanceable mortgage + rate model integration
rp = build_rate_path('3yr fixed', 0.04, 3, 'fixed', [0.05])
sched = amortization_schedule(100000, rp, 25, 60, readvance_smith=True)
rm = ReadvanceableMortgage()
for m in sched[:12]:
    rm.readvance(m['principal'])

print('All cross-module integrations work!')
print(f'  Tax: $150k marginal = {marginal_rate(150000)*100:.2f}%')
print(f'  RRSP: contributed ${actual:,}, room ${remaining:,}')
print(f'  RM: HELOC ${rm.heloc_balance:,.0f}, investment ${rm.investment_balance:,.0f}')
print(f'  Rate path: {rp.name} avg rate {rp.average_rate*100:.2f}%')
