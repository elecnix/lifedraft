"""The two ways the input contract refuses a document.

Both are ``ValueError`` subclasses, and both are raised at the ONE loading
boundary (``input_contract.load_and_map``): a document either fails the
composed JSON Schema (``ContractValidationError``, raised by
``contract_schema.validate_contract``) or it is schema-valid but says
something the engine cannot represent without silently dropping it
(``ContractAdaptationError``, raised by any of the ``contract_*`` mappers).

They live in their own module because every mapper module raises one and none
of them may import another just to get it (DP#25: dependencies point inward,
and this is the innermost node).
"""
from __future__ import annotations


class ContractValidationError(ValueError):
    """Raised by ``validate_contract`` -- carries every violation found."""


class ContractAdaptationError(ValueError):
    """Raised by ``to_internal_config`` when a validated document contains
    something this mapping honestly cannot represent yet (e.g. more than one
    couple's worth of generations -- #598, the engine's own structural limit,
    not a schema limit) -- loud refusal, never a silent partial mapping
    (DP#32)."""
