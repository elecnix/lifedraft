"""Issue #141: the input-contract layer of the superficial-loss slice A.

CONTRACT MAPPING: ``decisions.superficial_loss.substitute_pairs`` -- the
household's declaration that its window repurchases are of NON-identical
substitutes (e.g. XEQT vs VEQT) -- is mapped, refused loudly when
malformed (DP#32), and carried onto ``SimulationConfig`` by the adapter.
The rule that CONSUMES the declaration (the registered ``superficial_loss``
rule and the ``classify_window`` primitive) lands in the next slice of the
stack; this slice only declares, maps, and stores (the #203/#205 shape).
"""

import pytest

from contract_decisions import map_superficial_loss


def _two_generation_doc():
    """A validated two-generation contract document (the adapter's Phase 1
    sub-family), via test_input_contract's fixture builders."""
    import contract_schema
    from test_input_contract import _load_example, _two_generation_subset
    doc = _two_generation_subset(_load_example())
    contract_schema.validate_contract(doc)
    return doc


class TestSubstitutePairDeclarationMapping:
    """DP#32 on the INPUT side: a malformed `decisions.superficial_loss`
    declaration must FAIL LOUDLY at load time, never coerce to the
    conservative default -- a household that declares substitutes and is
    silently ignored loses money it was told it could keep."""

    @staticmethod
    def _doc(pairs):
        doc = _two_generation_doc()
        doc['decisions']['superficial_loss'] = {'substitute_pairs': pairs}
        return doc

    def test_absent_block_returns_empty(self):
        doc = _two_generation_doc()
        assert 'superficial_loss' not in doc['decisions']
        assert map_superficial_loss(doc) == []

    def test_empty_block_is_a_partial_declaration_and_raises(self):
        # Cite thread (PR #196, logic-inversion): a PRESENT-but-empty block
        # is a household that started a declaration and stopped -- it must
        # be refused like substitute_pairs-absent, never silently defaulted
        # to the conservative denial. Only true absence (None) returns [].
        doc = _two_generation_doc()
        doc['decisions']['superficial_loss'] = {}
        with pytest.raises(ValueError, match='substitute_pairs is'):
            map_superficial_loss(doc)
        assert map_superficial_loss(_two_generation_doc()) == []

    def test_declared_pairs_round_trip(self):
        doc = self._doc([['XEQT', 'VEQT'], ['XBB', 'VAB']])
        assert map_superficial_loss(doc) == [['XEQT', 'VEQT'], ['XBB', 'VAB']]

    def test_adapter_emits_pairs_only_when_declared(self):
        import input_contract as ic
        # Absent block: the key stays OUT of the internal shape entirely,
        # so a no-declaration household round-trips byte-identically.
        legacy_without = ic.to_internal_config(_two_generation_doc())
        assert 'superficial_loss_substitute_pairs' not in legacy_without
        # Declared block: the pairs reach the legacy config the engine reads.
        legacy = ic.to_internal_config(self._doc([['XEQT', 'VEQT']]))
        assert legacy['superficial_loss_substitute_pairs'] == [['XEQT', 'VEQT']]

    def test_non_object_block_raises(self):
        doc = _two_generation_doc()
        doc['decisions']['superficial_loss'] = ['XEQT', 'VEQT']
        with pytest.raises(ValueError, match='must be an object'):
            map_superficial_loss(doc)

    def test_block_without_substitute_pairs_raises(self):
        # A partial declaration is refused: `substitute_pairs` absent means
        # the household STARTED a declaration and stopped -- silence here
        # would apply the conservative default to a household that meant
        # to declare (DP#32).
        doc = _two_generation_doc()
        doc['decisions']['superficial_loss'] = {'declared_elsewhere': True}
        with pytest.raises(ValueError, match='substitute_pairs is'):
            map_superficial_loss(doc)

    def test_non_list_substitute_pairs_raises(self):
        doc = self._doc('XEQT')
        with pytest.raises(ValueError, match='must be a list'):
            map_superficial_loss(doc)

    @pytest.mark.parametrize('bad_pair', [
        'XEQT',            # not a list
        ['XEQT'],          # one element
        ['XEQT', 'VEQT', 'XBB'],  # three elements
        ['XEQT', ''],      # empty string
        ['XEQT', 42],      # non-string
    ])
    def test_malformed_pair_raises(self, bad_pair):
        doc = self._doc([bad_pair])
        with pytest.raises(ValueError, match='exactly two non-empty strings'):
            map_superficial_loss(doc)

    def test_self_pair_raises(self):
        # A security is trivially identical to itself: declaring it a
        # substitute of itself asserts a falsehood to dodge the window.
        doc = self._doc([['XEQT', 'XEQT']])
        with pytest.raises(ValueError, match='same security twice'):
            map_superficial_loss(doc)
