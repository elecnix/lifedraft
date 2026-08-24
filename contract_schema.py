"""Schema composition, validation, and reading a contract document off disk.

The universal schema (``schema/input_schema.json``) and the Canada overlay
(``schema/countries/canada/input_schema.json``) are merged into one
draft-2020-12 document by ``compose_schema()``; ``validate_contract()``
validates a candidate document against it and reports EVERY violation, not
just the first (a machine author gets a complete error report, not
one-at-a-time whack-a-mole).

The two-file split is a deliberate decision (see ``input_contract``'s module
docstring): ~60 open jurisdiction issues and 3 in-flight country PRs depend
on the universal/overlay seam existing. The merge rule is total and
declarative -- never an ``or``-fallback over instance DATA (that is exactly
the DP#32 failure class this epic exists to eliminate); it is a one-time,
deterministic merge of two SCHEMA documents at first use.

Nothing here maps anything: this module knows the document's SHAPE, never its
meaning. The mapping lives in the ``contract_*`` modules the
``input_contract`` orchestrator drives.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import jsonschema

from contract_errors import ContractValidationError


REPO_ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = REPO_ROOT / "schema"
UNIVERSAL_SCHEMA_PATH = SCHEMA_DIR / "input_schema.json"
#: Key in ``UNIVERSAL_SCHEMA_PATH`` listing the ``$defs`` fragment files that
#: complete the universal schema (paths relative to ``SCHEMA_DIR``). The list is
#: DATA in the schema file, not a constant in this module (DP#2/DP#14), and it
#: is REQUIRED: a root file that does not declare it raises ``KeyError`` at
#: import rather than composing a silently-truncated schema (DP#32).
UNIVERSAL_PARTS_KEY = "x-schema-parts"
CANADA_OVERLAY_SCHEMA_PATH = SCHEMA_DIR / "countries" / "canada" / "input_schema.json"
EXAMPLE_PATH = SCHEMA_DIR / "example.json"


def _merge_fragment(base: Dict, overlay: Dict) -> Dict:
    """Merge one overlay object-schema FRAGMENT into one base object-schema
    fragment (both already dicts, e.g. two ``$defs`` entries of the same
    name, or the root schema's top-level fragment). Total and deterministic:

    - ``properties``: shallow dict union; a key present in both is REPLACED
      by the overlay's definition (the overlay is refining/replacing a
      jurisdiction-agnostic placeholder with the real jurisdiction shape --
      #602's hard decision, not a fallback over instance data).
    - ``required``: base's list followed by overlay's additions, deduplicated,
      base order preserved (a union of what's required, never a subtraction).
    - ``allOf``: base's list followed by overlay's list, concatenated. allOf
      is already an AND of its members, so concatenation is the union of
      constraints -- not a choice between them.
    - every other key the overlay defines (``enum``, ``const``, ``type``,
      ``minItems``, ``description``, ``additionalProperties``, ...) REPLACES
      the base's value for that key, or is added if base didn't have it.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if key == "properties":
            merged["properties"] = {**merged.get("properties", {}), **value}
        elif key == "required":
            existing = list(merged.get("required", []))
            for item in value:
                if item not in existing:
                    existing.append(item)
            merged["required"] = existing
        elif key == "allOf":
            merged["allOf"] = list(merged.get("allOf", [])) + list(value)
        else:
            merged[key] = value
    return merged


def load_universal_schema() -> Dict:
    """Assemble the universal (jurisdiction-agnostic) schema from its files.

    ``schema/input_schema.json`` holds the document spine -- metadata, the root
    ``required``/``allOf`` and the top-level ``properties`` -- and names its
    ``$defs`` fragments in ``x-schema-parts``. Each fragment is folded in with
    ``compose_schema`` itself, i.e. the SAME total, declarative merge the Canada
    overlay already goes through (DP#8/DP#9: one composition mechanism, not
    two). A fragment carries only ``$defs`` today, so the fold is a ``$defs``
    union; the root-property refinement rule applies unchanged, which means a
    fragment can never invent a root key.

    The split is presentation, not semantics: the object returned here is the
    object the single 1,368-line file used to be.
    """
    root = json.loads(UNIVERSAL_SCHEMA_PATH.read_text())
    part_paths = root.pop(UNIVERSAL_PARTS_KEY)
    universal = root
    for rel in part_paths:
        universal = compose_schema(universal, json.loads((SCHEMA_DIR / rel).read_text()))
    return universal


def compose_schema(universal: Optional[Dict] = None, overlay: Optional[Dict] = None) -> Dict:
    """Merge the Canada overlay into the universal target schema.

    Root-level: every key in ``overlay['properties']`` must refine a key
    already present in ``universal['properties']`` (the overlay REFINES an
    existing universal field; it may never invent a new root key -- new root
    keys belong in the universal file, by construction of DP#14's
    universal/jurisdiction split). ``$defs``: overlay entries merge into a
    same-named base entry via ``_merge_fragment``, or are added verbatim if
    the base has no entry of that name (a genuinely jurisdiction-only shape,
    e.g. ``lira``/``lsif``/``fhsa``/``resp``/``person_room``).
    """
    if universal is None:
        universal = load_universal_schema()
    if overlay is None:
        overlay = json.loads(CANADA_OVERLAY_SCHEMA_PATH.read_text())

    composed = dict(universal)

    overlay_root_props = overlay.get("properties", {})
    unknown_root_keys = set(overlay_root_props) - set(universal.get("properties", {}))
    if unknown_root_keys:
        raise ContractValidationError(
            f"Canada overlay declares root propert{'y' if len(unknown_root_keys) == 1 else 'ies'} "
            f"not present in the universal schema (overlay may only refine an "
            f"existing key): {sorted(unknown_root_keys)}"
        )
    merged_props = dict(universal.get("properties", {}))
    for key, frag in overlay_root_props.items():
        merged_props[key] = _merge_fragment(merged_props[key], frag)
    composed["properties"] = merged_props

    merged_defs = dict(universal.get("$defs", {}))
    for name, frag in overlay.get("$defs", {}).items():
        if name in merged_defs:
            merged_defs[name] = _merge_fragment(merged_defs[name], frag)
        else:
            merged_defs[name] = frag
    composed["$defs"] = merged_defs

    # The overlay is a jurisdiction extension, not an independent schema --
    # its own $schema/$id/title are metadata about the fragment, not the
    # composed whole. Drop anything besides properties/$defs it might carry.
    composed.pop("$comment", None)
    return composed


_COMPOSED_SCHEMA: Optional[Dict] = None
_VALIDATOR: Optional["jsonschema.protocols.Validator"] = None


def get_validator():
    """Lazily build (and cache) the composed-schema Draft202012Validator."""
    global _COMPOSED_SCHEMA, _VALIDATOR
    if _VALIDATOR is None:
        _COMPOSED_SCHEMA = compose_schema()
        jsonschema.Draft202012Validator.check_schema(_COMPOSED_SCHEMA)
        _VALIDATOR = jsonschema.Draft202012Validator(_COMPOSED_SCHEMA)
    return _VALIDATOR


def validate_contract(document: Dict) -> None:
    """Validate ``document`` against the composed target schema.

    Raises ``ContractValidationError`` listing every violation (not just the
    first) when invalid. Returns None (no exception) when valid.
    """
    validator = get_validator()
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    if errors:
        formatted = "\n".join(
            f"  - {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in errors
        )
        raise ContractValidationError(
            f"Input contract failed validation ({len(errors)} error"
            f"{'s' if len(errors) != 1 else ''}):\n{formatted}"
        )


def load_contract_json(path: str) -> Dict:
    with open(path) as f:
        return json.load(f)


def _default_example() -> Dict:
    return load_contract_json(str(EXAMPLE_PATH))
