"""Issue #363: the MIXED-USE principal residence (static share).

## The gap

The engine treated each property as wholly one use for the whole horizon. The
schema made it structural: `property.rental` was forced to null unless
`kind=rental`, so an owner-occupied home could not declare a rented basement, a
duplex unit, or a home business at all. A household that rented part of its home
had to split the building into two synthetic records by hand -- duplicating the
purchase, sale and financing data, with nothing keeping the halves consistent.

Meanwhile `pre_designation.taxable_gain_fraction` apportioned the exemption by
*designated years* only, so a fully designated home was **fully sheltered even if
part of it earned rent or business income**. That is the dangerous direction:
the engine would shelter a gain the ITA does not.

## What this layer adds

A `rental.share` on a principal residence, strictly between 0 and 1:

- rent and expenses are taxed in that proportion;
- only that proportion of mortgage interest is deductible (s.20(1)(c));
- on disposition the exemption shelters only the owner-occupied remainder, via
  `share + (1 - share) x taxable_fraction` -- so a fully designated duplex at a
  0.4 share is **40% taxable, not exempt**.

Absent share = the whole property, which is what `kind=rental` already means, so
every existing rental and every pure principal residence is byte-identical
(DP#32).

## What this layer does NOT do

The **dated change-of-use event** (acceptance test A) -- s.45(1)(c) deemed
disposition of a portion at FMV mid-horizon, the s.45(2)/(3) elections, the
ancillary-use exception, and the s.13(7)(b) UCC reset. This layer is the STATIC
share only. Acceptance A needs a property whose mix CHANGES during the
projection, which is a separate event model; it is not quietly approximated here.
"""
import json
import tempfile
import os

import pytest

import input_contract as ic
from rules_disposition import _disposition_gain_tax

_BRACKETS = [
    {'min': 0, 'max': 51_780, 'rate': 0.2653, 'label': ''},
    {'min': 51_780, 'max': 103_545, 'rate': 0.3612, 'label': ''},
    {'min': 103_545, 'max': 10**9, 'rate': 0.45, 'label': ''},
]


def _sale(**overrides):
    s = {
        'date': '2029-06-01', 'year': 2029,
        'owner_roles': {'primary': 1.0},
        'designated_principal_residence_years': [
            {'from': '2024-01-01', 'to': '2029-12-31'}],
    }
    s.update(overrides)
    return s


class TestTheExemptionIsApportionedByTheShare:
    """Acceptance B: a fully designated duplex at a 0.4 rental share is 40%
    taxable, not exempt. Before this change it was fully sheltered."""

    def test_a_pure_principal_residence_is_still_fully_sheltered(self):
        """No share -> the whole property is the principal residence."""
        assert _disposition_gain_tax(250_000.0, _sale(), 2029, _BRACKETS,
                                     60_000.0, 0.0) == 0.0

    def test_a_duplex_shelters_only_the_owner_occupied_share(self):
        mixed = _sale(mixed_use_share=0.4)
        pure = _sale()
        tax = _disposition_gain_tax(250_000.0, mixed, 2029, _BRACKETS,
                                    60_000.0, 0.0)
        assert tax > 0.0
        assert tax > _disposition_gain_tax(250_000.0, pure, 2029, _BRACKETS,
                                            60_000.0, 0.0)

    def test_the_taxable_gain_is_the_share_of_the_gain(self):
        """The issue's figure: 0.4 x 250,000 = 100,000 exposed, 50,000 taxable
        at the 50% inclusion. Pricing the whole gain at the 0.4 fraction must
        equal pricing 40% of it undesignated."""
        mixed = _sale(mixed_use_share=0.4)
        whole_gain_at_share = _disposition_gain_tax(
            250_000.0, mixed, 2029, _BRACKETS, 60_000.0, 0.0)
        share_of_gain_undesignated = _disposition_gain_tax(
            100_000.0, _sale(designated_principal_residence_years=[]), 2029,
            _BRACKETS, 60_000.0, 0.0)
        assert whole_gain_at_share == pytest.approx(share_of_gain_undesignated)

    def test_a_larger_share_exposes_more(self):
        low = _disposition_gain_tax(250_000.0, _sale(mixed_use_share=0.2),
                                    2029, _BRACKETS, 60_000.0, 0.0)
        high = _disposition_gain_tax(250_000.0, _sale(mixed_use_share=0.6),
                                     2029, _BRACKETS, 60_000.0, 0.0)
        assert high > low > 0.0

    def test_an_undesignated_mixed_home_is_fully_taxable(self):
        """Both halves exposed: the share AND the undesignated remainder."""
        mixed = _sale(mixed_use_share=0.4,
                      designated_principal_residence_years=[])
        all_taxable = _sale(designated_principal_residence_years=[])
        assert _disposition_gain_tax(250_000.0, mixed, 2029, _BRACKETS,
                                     60_000.0, 0.0) == pytest.approx(
            _disposition_gain_tax(250_000.0, all_taxable, 2029, _BRACKETS,
                                  60_000.0, 0.0))


class TestTheSchemaRefusesDegenerateShares:
    """A principal may carry a rental block ONLY as genuine mixed use."""

    def _doc(self, share, kind='principal'):
        with open('schema/example.json') as fh:
            base = json.load(fh)
        # Reuse the example's OWN principal residence verbatim -- its owner
        # block is known-valid, and hand-shaping one invites a schema error that
        # has nothing to do with the share rule under test.
        home = json.loads(json.dumps(base['properties'][0]))
        r = {'gross_rent_annual': 12_000, 'expenses_annual': 3_000,
             'as_of': base['as_of']}
        if share is not None:
            r['share'] = share
        home['kind'] = kind
        home['rental'] = r
        base['properties'] = [home]
        return base

    def _share_error(self, doc):
        """The validation error about `share`, or None if the share rule is happy.

        Asserting on the WHOLE document round-trip would conflate the rule under
        test with unrelated flaws in the example (retired grandparents the
        admission gate refuses, #698; accounts owned by removed people). So this
        asks the one question that matters: **is the share rule what objectes?**
        """
        with tempfile.NamedTemporaryFile('w', suffix='.json',
                                         delete=False) as f:
            json.dump(doc, f)
            path = f.name
        try:
            ic.load_and_map(path)
            return None
        except Exception as exc:
            msg = str(exc)
            return msg if 'share' in msg else None
        finally:
            os.unlink(path)

    def _doc(self, share, kind='principal'):
        with open('schema/example.json') as fh:
            base = json.load(fh)
        home = json.loads(json.dumps(base['properties'][0]))
        r = {'gross_rent_annual': 12_000, 'expenses_annual': 3_000,
             'as_of': base['as_of']}
        if share is not None:
            r['share'] = share
        home['kind'] = kind
        home['rental'] = r
        base['properties'] = [home]
        return base

    def test_a_mixed_use_share_is_accepted(self):
        assert (self._share_error(self._doc(0.4)) is None) is True

    def test_a_share_of_one_is_refused(self):
        """A share of 1.0 says the whole home is a rental -- use kind=rental."""
        assert (self._share_error(self._doc(1.0)) is None) is False

    def test_a_share_of_zero_is_refused(self):
        """A share of 0.0 declares income-producing use on none of it: a dead read."""
        assert (self._share_error(self._doc(0.0)) is None) is False

    def test_a_principal_rental_without_a_share_is_refused(self):
        """`kind=principal` + a rental block and no share is ambiguous: is it the
        whole house or none of it? Neither, says the schema."""
        assert (self._share_error(self._doc(None)) is None) is False

    def test_a_whole_property_rental_needs_no_share(self):
        """kind=rental IS the whole property; byte-identical to #693, so the
        share rule must have nothing to say about it."""
        assert self._share_error(self._doc(None, kind='rental')) is None
