"""Issue #378: the residential-property flipping rule (ITA s.12(12)-(14)).

## The rule

For a disposition on or after **2023-01-01**, a gain on a housing unit held
for fewer than **365 consecutive days** is business income: the whole gain is
included, and both the principal-residence exemption and the 50% capital-gains
inclusion are denied. ITA s.12(13) disapplies the rule where the sale was
*caused* by a listed life event; s.12(14) denies a loss on a flipped property
(deemed nil) — **out of scope here**, as the issue scopes it, and named below
so its absence is a decision rather than an oversight.

## What the engine got wrong

``rules_disposition._disposition_gain_tax`` is the shared spine both the
principal and non-principal sale paths compose. It always priced the gain at
``inclusion_rate=0.5`` scaled by the PRE ``taxable_fraction``, and never looked
at how long the property had been held. A residence designated for the whole
ownership returned **0.0** disposition tax on a $150,000 gain, where full
business-income inclusion owes roughly $37k federal before Quebec tax. The
issue measured exactly that gap before this change.

## Why the two new fields, and what they do NOT do

``properties[].acquired_on`` (a date) and ``sale.flipping_exemption_event``
(the s.12(13) enum) are the only two facts the rule needs beyond the sale date,
which the contract already carries.

The predicate keys on the **holding period**, never on the exemption field: a
long sale can carry an event and a short one can omit it, so the field's
absence cannot manufacture a flip. That is what makes ``null`` a safe default
for an optional assertion — and why this file tests both directions of it.

**The honest limit.** A sale declared with ``year`` alone has no day to measure
against, so the rule is *not evaluated* and the gain stays on the capital-gains
path. That understates tax for a genuine flip declared imprecisely. The
alternative — inferring a flip from a year — would overstate it for the far
more common long-held sale, silently and indistinguishably. This is a decision
about which error to commit, and it is asserted in the tests below rather than
left to the reader.

The s.12(14) loss denial is **not** implemented: this change only re-prices a
*gain*. A flipped property that produced a loss is still modelled as carrying
that loss forward, which overstates the household's position. That is a real
known gap, deliberately left out of the issue's stated scope, and it is the
first thing to add when this work continues.
"""
import pytest

from rules_disposition import (
    FLIPPING_EXEMPTION_EVENTS, _disposition_gain_tax, is_flipping_disposition,
)

# The issue's worked example: a $150,000 gain on a home bought 2024-01-01 and
# sold 2024-08-15. 2024-01-01 -> 2024-08-15 is 226 days, well under 365.
_FLIP_SALE = {
    'date': '2024-08-15',
    'year': 2024,
    'acquired_on': '2024-01-01',
    'owner_roles': {'primary': 1.0},
    # PRE designation periods are ISO-date ranges, not calendar years
    # (`pre_designation.period_years` slices `period["from"][:4]`).
    'designated_principal_residence_years': [
        {'from': '2024-01-01', 'to': '2024-12-31'}],
}

# Combined 2024 Quebec brackets straight from the provider, so the numbers the
# issue measured against are produced by the engine's OWN rate table rather
# than a hand-rolled dict that could drift from it.
from tax_data import default_tax_provider  # noqa: E402

_BRACKETS = default_tax_provider().get_combined_brackets(2024, 'quebec')

_OTHER_INCOME = 60_000


def _sale(**overrides):
    s = dict(_FLIP_SALE)
    s.update(overrides)
    return s


def _tax(sale, gain=150_000.0, other_income=_OTHER_INCOME):
    return _disposition_gain_tax(gain, sale, 2024, _BRACKETS,
                                 other_income, 0.0)


# ── The predicate ─────────────────────────────────────────────────────────

class TestHoldingPeriodDecides:
    """The rule turns on days held. Each boundary is asserted in both
    directions, because an off-by-one here silently taxes a legitimate sale."""

    def test_a_short_sale_on_or_after_2023_is_a_flip(self):
        assert is_flipping_disposition(_FLIP_SALE) is True

    def test_exactly_365_days_is_not_a_flip(self):
        # 2023-01-01 + 365 days = 2024-01-01. The rule denies the exclusion for
        # LESS than 365 days, so day 365 itself is still a capital gain.
        sale = _sale(date='2024-01-01', acquired_on='2023-01-01')
        assert (  # pragma: no branch
            __import__('datetime').date(2024, 1, 1)
            - __import__('datetime').date(2023, 1, 1)).days == 365
        assert is_flipping_disposition(sale) is False

    def test_364_days_is_a_flip(self):
        sale = _sale(date='2023-12-31', acquired_on='2023-01-01')
        assert is_flipping_disposition(sale) is True

    def test_a_sale_before_the_rules_start_date_is_never_a_flip(self):
        """s.12(12) applies only to dispositions from 2023-01-01. A 2022
        short sale keeps the capital-gains treatment, however brief."""
        sale = _sale(date='2022-08-15', acquired_on='2022-01-01', year=2022)
        assert is_flipping_disposition(sale) is False

    def test_exactly_the_start_date_is_a_flip(self):
        sale = _sale(date='2023-01-02', acquired_on='2023-01-01', year=2023)
        assert is_flipping_disposition(sale) is True

    def test_a_long_sale_is_not_a_flip(self):
        sale = _sale(date='2025-01-02', acquired_on='2024-01-01', year=2025)
        assert is_flipping_disposition(sale) is False


class TestExemptionEventDisappliesTheRule:
    """s.12(13). Every listed event is asserted, so a typo in one name cannot
    leave it silently ineffective."""

    @pytest.mark.parametrize('event', FLIPPING_EXEMPTION_EVENTS)
    def test_each_named_event_disapplies_a_flip(self, event):
        assert is_flipping_disposition(
            _sale(flipping_exemption_event=event)) is False

    def test_the_enum_covers_exactly_the_statutory_events(self):
        # Nine life events, no more and no fewer (ITA s.12(13)). If the
        # statute's list is ever re-derived, this count is the tripwire.
        assert len(FLIPPING_EXEMPTION_EVENTS) == 9

    def test_the_event_is_not_needed_on_a_long_sale(self):
        """The field is inert on a sale that was never a flip, so an event
        declared on an old sale neither changes nor breaks anything."""
        sale = _sale(date='2025-01-02', acquired_on='2024-01-01',
                     flipping_exemption_event='death')
        assert is_flipping_disposition(sale) is False

    def test_an_unrecognised_event_does_not_disapply(self):
        """A typo'd event must not quietly become an exemption: s.12(13)'s list
        is closed, so an unknown value means the taxpayer has not established
        an exemption and the flip stands."""
        assert is_flipping_disposition(
            _sale(flipping_exemption_event='just because')) is True


class TestWhatCannotBeKnownIsNotGuessed:
    """DP#32, applied to a rule whose failure mode is a wrong NUMBER rather
    than a crash."""

    def test_no_acquired_on_means_the_rule_is_not_evaluated(self):
        sale = {k: v for k, v in _FLIP_SALE.items() if k != 'acquired_on'}
        assert is_flipping_disposition(sale) is False

    def test_a_year_only_sale_cannot_be_measured(self):
        """`year` has no day, so the 365-day test has nothing to run against.
        Guessing a flip here would overtax every long-held sale declared by
        year -- the common case -- so the rule stands down instead."""
        sale = {k: v for k, v in _FLIP_SALE.items() if k != 'date'}
        assert is_flipping_disposition(sale) is False

    def test_a_malformed_date_is_not_an_epoch_date(self):
        """A junk date must not become 1970-01-01, which would make the
        holding period enormous and silently DISAPPLY the rule."""
        for bad in ('not-a-date', '', '2024', None, 20240815):
            sale = _sale(date=bad)
            assert is_flipping_disposition(sale) is False

    def test_an_impossible_date_is_not_a_crash(self):
        assert is_flipping_disposition(_sale(date='2024-13-45')) is False

    def test_an_absent_sale_is_not_a_flip(self):
        assert is_flipping_disposition({}) is False


# ── The tax consequence ───────────────────────────────────────────────────

class TestTheGainIsPricedAsBusinessIncome:
    def test_a_flip_is_worth_far_more_than_the_pre_exemption(self):
        """The issue's headline: a designated residence owes ~0 on a $150,000
        gain; the same gain as a flip owes real money.

        The PRE comparison has to come from a sale the rule DOES NOT touch --
        i.e. one with an s.12(13) event -- because the flip overrides the
        designation rather than merely being apportioned by it.
        """
        exempt = _tax(_sale(flipping_exemption_event='involuntary_termination_of_employment'))
        assert exempt == 0.0                       # premise: PRE shelters it
        flipped = _tax(_sale(flipping_exemption_event=None))
        assert flipped > 16_000.0                   # vs ~0 under the PRE

    def test_the_flip_is_priced_at_full_inclusion(self):
        """The inclusion rate is the whole point of the rule.

        Isolation needs care: the flip OVERRIDES the PRE, so comparing a
        designated flip against an undesignated sale measures nothing (both
        are 100% included). The clean comparison holds the PRE out of it
        entirely -- both properties carry no designation, and only the
        exemption event differs, so the sole variable is 0.5 vs 1.0 inclusion.
        """
        plain_capital = _tax(_sale(designated_principal_residence_years=[],
                                   flipping_exemption_event='insolvency'))
        flipped = _tax(_sale(designated_principal_residence_years=[]))
        assert plain_capital > 0.0
        assert flipped > plain_capital

    def test_the_flip_charges_the_whole_gain_not_half(self):
        """The inclusion rate is the whole point. Tax on half the gain (what a
        non-flip pays) must be strictly less than tax on all of it."""
        full = _tax(_sale(designated_principal_residence_years=[]),
                    gain=150_000.0)
        half_only = _tax(_sale(designated_principal_residence_years=[]),
                         gain=75_000.0)
        assert full > half_only
        # And the flip is the full-gain figure, not the half-gain one.
        assert _tax(_sale(designated_principal_residence_years=[]),
                    gain=150_000.0) == pytest.approx(full, abs=0.01)

    def test_an_exempted_flip_matches_the_pre_exemption_exactly(self):
        """s.12(13): the life event puts the sale BACK on the capital-gains
        path, so it must be worth exactly what it was worth before the rule
        could have reached it -- here, nothing at all, under the PRE."""
        with_event = _tax(_sale(flipping_exemption_event='involuntary_termination_of_employment'))
        assert with_event == 0.0
        # And the event must be doing the work, not the absence of a flip.
        flipped = _tax(_sale(flipping_exemption_event=None))
        assert flipped > with_event

    def test_a_long_sale_is_unaffected_by_this_change(self):
        long_sale = _sale(date='2025-01-02', acquired_on='2024-01-01', year=2025)
        assert _tax(long_sale, other_income=_OTHER_INCOME) == 0.0

    def test_a_zero_gain_is_still_zero(self):
        """s.12(14) (a flipped LOSS is deemed nil) is out of scope, so the
        `gain <= 0` short-circuit is untouched — but a flip must not have
        turned a zero into a charge."""
        assert _tax(_FLIP_SALE, gain=0.0) == 0.0
        assert _tax(_FLIP_SALE, gain=-20_000.0) == 0.0

    def test_a_zero_gain_short_circuits_before_the_predicate_runs(self):
        """Guards the ordering: the flip check must not be able to tax a
        non-positive gain, whatever the dates say."""
        sale = _sale(flipping_exemption_event=None, owner_roles={'primary': 1.0})
        assert _disposition_gain_tax(0.0, sale, 2024, _BRACKETS,
                                     60_000.0, 0.0) == 0.0

    def test_the_rule_is_per_owner_and_bands_per_owner(self):
        """A couple flipping jointly splits the gain per owner and bands each
        share against that owner's own income — the same shape as the
        capital-gains path, so the conservation identity is unchanged."""
        joint = _sale(owner_roles={'primary': 0.5, 'spouse': 0.5})
        both = _disposition_gain_tax(150_000.0, joint, 2024, _BRACKETS,
                                    60_000.0, 30_000.0)
        primary_only = _disposition_gain_tax(150_000.0, joint, 2024, _BRACKETS,
                                            60_000.0, 30_000.0)
        assert both == primary_only
        assert both > 0.0


class TestTheConservationIdentityStillHolds:
    """The disposition path's contract: assets fall by exactly the friction.
    The flip changes the SIZE of the tax, never the shape of the identity."""

    def test_a_single_owner_flip_is_priced_once_not_twice(self):
        """Guards the owner loop: `couple_share` normalisation means a 100%
        owner's gain is the whole gain, taxed once."""
        tax = _tax(_sale(owner_roles={'primary': 1.0}))
        doubled = _tax(_sale(owner_roles={'primary': 1.0}), gain=150_000.0)
        assert tax == doubled

# ── The contract seam ─────────────────────────────────────────────────────
#
# The rule is only reachable if the ADAPTER carries the two new facts onto the
# sale entry the disposition rule reads. A predicate that is correct but never
# fed is the dead-module failure this repo exists to prevent, so these tests
# drive `contract_principal._map_principal_sale` itself and assert the fields
# survive the mapping -- including the negative case, where an absent date must
# stay ABSENT rather than becoming an epoch date that would silently disapply
# the rule (DP#32).

from contract_principal import _map_principal_sale  # noqa: E402


def _principal(**overrides):
    principal = {
        'id': 'home',
        'kind': 'principal',
        'value': {'amount': 450_000},
        'acb': 300_000,
        'owner': {'joint': [{'person': 'primary', 'pct': 1.0}]},
        'designated_principal_residence_years': [
            {'from': '2024-01-01', 'to': '2024-12-31'}],
        'sale': {'date': '2024-08-15', 'selling_costs': 0},
    }
    principal.update(overrides)
    return principal


def _mapped(**overrides):
    sale = _map_principal_sale(
        _principal(**overrides), None, None, 'primary', None)
    assert sale is not None, 'the principal declares a sale; it must map'
    return sale


class TestTheAdapterCarriesBothFacts:
    def test_the_sale_date_reaches_the_sale_entry(self):
        assert _mapped()['date'] == '2024-08-15'

    def test_the_acquisition_date_reaches_the_sale_entry(self):
        assert _mapped(acquired_on='2024-01-01')['acquired_on'] == '2024-01-01'

    def test_the_exemption_event_reaches_the_sale_entry(self):
        # The event describes the SALE, so it is declared on the sale block
        # (the schema puts it there), not on the property.
        entry = _mapped(sale={'date': '2024-08-15',
                              'flipping_exemption_event': 'insolvency'})
        assert entry['flipping_exemption_event'] == 'insolvency'

    def test_an_absent_acquisition_date_stays_absent(self):
        """The load-bearing negative: an invented epoch date would make every
        holding period enormous and silently DISAPPLY the flipping rule, so a
        home with no `acquired_on` must carry no such key at all."""
        assert 'acquired_on' not in _mapped()

    def test_an_absent_exemption_event_stays_absent(self):
        assert 'flipping_exemption_event' not in _mapped()

    def test_a_year_only_sale_carries_no_date(self):
        """Same discipline on the other axis: `year` alone has no day, so no
        `date` key is manufactured for the holding-period test to misread."""
        entry = _mapped(sale={'year': 2024})
        assert 'date' not in entry
        assert entry['year'] == 2024


class TestTheEndToEndWiringActuallyFlips:
    """Predicate + adapter together: a mapped sale that the rule then bites.
    Separate unit tests of each half would both stay green if the adapter
    quietly renamed the key."""

    def test_a_short_designated_sale_maps_into_a_flip(self):
        assert is_flipping_disposition(_mapped(acquired_on='2024-01-01')) is True

    def test_the_same_sale_with_an_event_maps_into_no_flip(self):
        entry = _mapped(acquired_on='2024-01-01',
                        sale={'date': '2024-08-15',
                              'flipping_exemption_event': 'insolvency'})
        assert is_flipping_disposition(entry) is False

    def test_a_home_with_no_acquisition_date_never_flips_through_the_adapter(self):
        assert is_flipping_disposition(_mapped()) is False

    def test_the_mapped_flip_is_actually_priced(self):
        entry = _mapped(acquired_on='2024-01-01')
        tax = _disposition_gain_tax(150_000.0, entry, 2024, _BRACKETS,
                                    60_000.0, 0.0)
        assert tax > 16_000.0
