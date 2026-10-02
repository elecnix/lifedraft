"""Issue #381, disclosure half: a fee-free SM sleeve must be DISCLOSED, not silent.

#381 asked for two things. The behavioural half landed on #398: the sleeve has
its own declarable fee (``liabilities[kind=heloc].investment_mer``), applied with
the #291 per-account convention. This module pins the other half — what happens
when the household declares **no** fee.

The engine then compounds the borrowed-to-invest sleeve with no fee at all. That
is not automatically wrong: an undeclared input is an ABSENCE, and DP#32 makes
absence a no-op rather than an opinion. But money borrowed to invest is held in
funds or ETFs that pay a MER like any other holding, so a fee-free sleeve
**overstates** terminal assets and the SM's ranked benefit by an amount that grows
with the horizon — the optimistic-bias class this registry exists to name. So it
is disclosed: a registered caveat that fires exactly when the sleeve is in play
and no fee was declared, and stays silent the moment one is.

Both directions are pinned, because a caveat that fires unconditionally is noise
and a caveat that never fires is worse than none.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model_fidelity
from model_fidelity import Direction, FidelityContext

CAVEAT_ID = 'sm_sleeve_fee_free_when_undeclared'


def _active(caveat_id: str, ctx: FidelityContext) -> bool:
    """Whether the named caveat fires for this context, via the registry's own
    accessor (the public surface is `all_approximations()` +
    `Approximation.is_active`, plus `active_approximations(cfg, objective)`)."""
    approx = next((a for a in model_fidelity.all_approximations() if a.id == caveat_id),
                  None)
    return False if approx is None else approx.is_active(ctx)


def _ctx(*, readvanceable: bool, declared_mer=None, objective='max_net_benefit') -> FidelityContext:
    """A FidelityContext shaped like the ones the surfaces build: the config
    carries the declared HELOC facts, and the objective the run ranks on."""
    heloc = {
        'readvanceable': readvanceable,
        'capitalize_interest': True,
        'limit': 150000.0,
        'rate': 0.0545,
        'rate_type': 'variable',
        'deductibility': {'investment_portion': 0.0, 'personal_portion': 1.0},
        'collateral': 'principal_residence',
    }
    if declared_mer is not None:
        heloc['investment_mer'] = declared_mer
    return FidelityContext(
        cfg={'property': {'heloc': heloc,
                          'heloc_rate': 0.0545,
                          'has_heloc': readvanceable}},
        objective_name=objective,
    )


class FeeFreeSleeveIsDisclosed(unittest.TestCase):
    """The caveat fires only for the combination that is actually an
    approximation: a readvanceable sleeve, with no declared fee."""

    def test_the_caveat_is_registered(self):
        ids = [a.id for a in model_fidelity.all_approximations()]
        self.assertIn(CAVEAT_ID, ids,
                      "issue #381: a fee-free SM sleeve must be a registered "
                      "caveat, not a silent modelling choice")

    def test_it_fires_for_a_readvanceable_sleeve_with_no_declared_fee(self):
        ctx = _ctx(readvanceable=True)
        self.assertTrue(_active(CAVEAT_ID, ctx),
                        "borrowed-to-invest money held fee-free overstates "
                        "terminal assets and the SM's benefit; disclose it")

    def test_it_stays_silent_when_a_fee_is_declared(self):
        """The other direction, and the reason this is a caveat and not a
        constant: once the household declares its MER, there is nothing left to
        disclose."""
        self.assertFalse(_active(
            CAVEAT_ID, _ctx(readvanceable=True, declared_mer=0.0055)))
        # ... including a DECLARED ZERO, which is a value, not an absence (DP#32).
        self.assertFalse(_active(
            CAVEAT_ID, _ctx(readvanceable=True, declared_mer=0.0)))

    def test_it_stays_silent_without_a_readvanceable_sleeve(self):
        """No sleeve, nothing to disclose — a non-readvanceable line never funds
        the SM pot, so the approximation cannot be biting."""
        self.assertFalse(_active(
            CAVEAT_ID, _ctx(readvanceable=False)))

    def test_it_stays_silent_when_there_is_no_property_at_all(self):
        """No property facts means no readvanceable line, therefore no sleeve,
        therefore nothing to disclose.

        My first version of this predicate fired OPEN on a context too thin to
        judge, and an existing test caught the consequence: a config of
        ``{'assumptions': ...}`` -- no property, no sleeve, no strategy -- started
        rendering "the SM sleeve compounds fee-free". The claim is unsupported
        there, and an output caveat nobody can act on trains readers to skip
        caveats (test_issue_286's all-clear case, which now passes again).

        Fail OPEN is still right where the predicate genuinely cannot EVALUATE a
        fact it otherwise knows -- a malformed heloc raises, and
        ``Approximation.is_active`` catches and reports. An absent fact is just
        absent."""
        self.assertFalse(_active(CAVEAT_ID, FidelityContext()))
        self.assertFalse(_active(CAVEAT_ID, FidelityContext(cfg={'assumptions': {}})))


class TheCaveatNamesItsDirection(unittest.TestCase):
    """A caveat that cannot say WHICH way it biases is a caveat nobody can act
    on."""

    def test_it_overstates_and_says_why(self):
        approx = next(a for a in model_fidelity.all_approximations()
                      if a.id == CAVEAT_ID)
        self.assertEqual(approx.direction, Direction.OVERSTATES)
        self.assertEqual(approx.issue, '#381')
        self.assertIn('MER', approx.summary)
        self.assertTrue(approx.biased_figure,
                        "name the figure the caveat biases")


if __name__ == '__main__':  # pragma: no cover
    unittest.main()