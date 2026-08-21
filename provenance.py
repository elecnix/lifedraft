#!/usr/bin/env python3
"""The provenance sidecar (issue #660, epic #659 Track 1).

The contract (``input_contract.py`` / ``schema/input_schema.json``) records
*what* a value is. It has no way to record *how we know it*. ``as_of`` dates
a balance, but says nothing about whether that balance was read off a
custodial statement or invented by whoever authored the file. Epic #603's
DP#32 made **absence** fail loudly; it did nothing for the far larger
category of values that are present, plausible, and guessed -- which the
engine consumes with total confidence.

This module owns the OPTIONAL top-level ``provenance`` object described in
``schema/input_schema.json``: a sidecar keyed by RFC 6901 JSON Pointer into
the document, so the contract itself stays readable and diffable (wrapping
every scalar in ``{value, source}`` would make it unusable). It has three
jobs:

1. Pure JSON-Pointer get/set (``resolve_pointer`` / ``set_pointer``, and the
   ``Provenance.resolve`` / ``Provenance.with_value`` wrappers) -- DP#3: no
   hidden state, ``with_value`` returns a NEW document rather than mutating
   its argument (DP#24's load-modify-save round trip). This is the ONLY
   place in the repo that speaks RFC 6901.

2. ``load_provenance()`` -- validates the sidecar against the document it
   annotates and returns a frozen ``Provenance`` wrapper. Everything
   ``schema/input_schema.json``'s ``provenance_entry`` $def cannot express
   (a pointer must actually resolve; a ``plausible_range``/``domain`` must
   bracket/contain the leaf's live value) is enforced here, in Python,
   against the actual document -- not by jsonschema, which has no way to
   compare one part of a document against another part keyed by a runtime
   string. Raises ``ProvenanceValidationError`` (DP#32: reject loudly,
   never coerce a malformed entry into something plausible) listing every
   violation found, not just the first.

3. Two read APIs epic #659 Track 2 ("value of information") depends on:

   - ``Provenance.confidence(pointer)`` -- the declared level, or
     ``"assumed"`` when the pointer has no entry. THIS is property #1 from
     the issue: a leaf with no entry is ``assumed`` *by definition*, so
     "everything not backed by evidence" fall out for free and cannot be
     dodged by simply omitting an entry.
   - ``Provenance.uncertain_leaves(doc)`` -- every leaf in ``doc`` (i.e.
     every scalar reachable by walking the document tree, excluding the
     ``provenance`` sidecar itself) whose confidence is ``assumed`` or
     ``stated``, INCLUDING leaves with no entry at all (returned with
     ``plausible_range=None`` and ``domain=None`` -- Track 2 reports these
     as "unranked: no range declared," a finding, not a crash).

Track 2 ("Oracle") depends on this module's API being stable; this file is
the only thing that changes it. It does not touch ``optimize.py`` --
another agent owns that file's CLI.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent

CONFIDENCE_LEVELS = ("measured", "stated", "derived", "assumed", "unknown")

# Confidence levels for which a leaf, if it has an explicit sidecar entry,
# MUST declare a plausible_range or domain (schema/input_schema.json's
# provenance_entry $def enforces the same rule structurally; this tuple is
# also used by the Python-side cross-document checks below).
_RANGE_OR_DOMAIN_REQUIRED = ("assumed", "stated")

# Worst-first ordering for the --provenance report, per #660: a leaf with NO
# entry at all is worse than an explicit `assumed` entry that at least
# bothered to declare a range -- an author cannot dodge scrutiny by simply
# omitting an entry, and the report must show that starkly.
_RANK = {
    "unknown": 0,
    "assumed_no_range": 1,  # no sidecar entry at all -- implicit `assumed`
    "assumed": 2,
    "stated": 3,
    "derived": 4,
    "measured": 5,
}


class ProvenanceValidationError(ValueError):
    """Raised by ``load_provenance`` -- carries every violation found."""


class PointerResolutionError(ValueError):
    """Raised by ``resolve_pointer``/``set_pointer`` when a JSON Pointer
    does not resolve against the document it is applied to."""


# ── 1. RFC 6901 JSON Pointer -- pure get/set, no hidden state (DP#3) ────────

_MISSING = object()


def _split_pointer(pointer: str) -> List[str]:
    """Split an RFC 6901 pointer into unescaped reference tokens. ``""``
    (the whole-document pointer) splits to ``[]``."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PointerResolutionError(
            f"invalid JSON Pointer (must be \"\" or start with '/'): {pointer!r}"
        )
    # RFC 6901 sec. 4: decode '~1' -> '/' BEFORE '~0' -> '~' (encoding does
    # the reverse: '~' -> '~0' first, then '/' -> '~1' -- decoding undoes
    # that in the opposite order).
    return [tok.replace("~1", "/").replace("~0", "~") for tok in pointer.split("/")[1:]]


def _escape_token(token: str) -> str:
    """Encode one reference token per RFC 6901 sec. 3."""
    return token.replace("~", "~0").replace("/", "~1")


def _join_pointer(tokens: List[str]) -> str:
    return "".join("/" + _escape_token(t) for t in tokens)


def resolve_pointer(doc: Any, pointer: str, default: Any = _MISSING) -> Any:
    """Return the value at ``pointer`` within ``doc``. Raises
    ``PointerResolutionError`` if the pointer does not resolve, unless
    ``default`` is given, in which case that is returned instead (absence-as-
    sentinel, not absence-as-truthiness -- DP#32)."""
    node = doc
    for tok in _split_pointer(pointer):
        if isinstance(node, dict):
            if tok not in node:
                if default is _MISSING:
                    raise PointerResolutionError(f"pointer does not resolve (no key {tok!r}): {pointer!r}")
                return default
            node = node[tok]
        elif isinstance(node, list):
            if tok == "-" or not _is_array_index(tok):
                if default is _MISSING:
                    raise PointerResolutionError(f"pointer does not resolve (not an array index {tok!r}): {pointer!r}")
                return default
            idx = int(tok)
            if idx < 0 or idx >= len(node):
                if default is _MISSING:
                    raise PointerResolutionError(f"pointer does not resolve (index {idx} out of range): {pointer!r}")
                return default
            node = node[idx]
        else:
            if default is _MISSING:
                raise PointerResolutionError(f"pointer descends into a scalar: {pointer!r}")
            return default
    return node


def _is_array_index(token: str) -> bool:
    # RFC 6901: an array index is "0" or a digit string with no leading
    # zero. Reject "-1", "01", "", "abc".
    return token.isdigit() and (token == "0" or not token.startswith("0"))


def set_pointer(doc: Any, pointer: str, value: Any) -> Any:
    """Return a NEW document equal to ``doc`` except that ``pointer`` now
    holds ``value`` (DP#3: pure, no mutation of the argument; DP#24: the
    load-modify-save round trip). ``"-"`` as the final array token appends.
    Raises ``PointerResolutionError`` if an intermediate segment of the
    pointer does not resolve to a container."""
    tokens = _split_pointer(pointer)
    if not tokens:
        return copy.deepcopy(value)
    new_doc = copy.deepcopy(doc)
    node = new_doc
    for tok in tokens[:-1]:
        if isinstance(node, dict):
            if tok not in node:
                raise PointerResolutionError(f"pointer does not resolve (no key {tok!r}): {pointer!r}")
            node = node[tok]
        elif isinstance(node, list):
            if not _is_array_index(tok) or int(tok) >= len(node):
                raise PointerResolutionError(f"pointer does not resolve (index {tok!r} out of range): {pointer!r}")
            node = node[int(tok)]
        else:
            raise PointerResolutionError(f"pointer descends into a scalar: {pointer!r}")
    last = tokens[-1]
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list):
        if last == "-":
            node.append(value)
        elif _is_array_index(last) and int(last) < len(node):
            node[int(last)] = value
        else:
            raise PointerResolutionError(f"pointer does not resolve (index {last!r} out of range): {pointer!r}")
    else:
        raise PointerResolutionError(f"pointer descends into a scalar: {pointer!r}")
    return new_doc


def _walk_leaves(node: Any, prefix: List[str]):
    """Yield (pointer, value) for every scalar (str/int/float/bool/None)
    reachable from ``node``, depth-first."""
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _walk_leaves(child, prefix + [key])
    elif isinstance(node, list):
        for idx, child in enumerate(node):
            yield from _walk_leaves(child, prefix + [str(idx)])
    else:
        yield _join_pointer(prefix), node


# ── 2. Loading + validation ─────────────────────────────────────────────────


def _entry_errors(doc: Dict, pointer: str, entry: Any) -> List[str]:
    errs: List[str] = []
    if not isinstance(entry, dict):
        return [f"{pointer}: provenance entry must be an object, got {type(entry).__name__}"]

    confidence = entry.get("confidence")
    if confidence not in CONFIDENCE_LEVELS:
        errs.append(f"{pointer}: confidence must be one of {CONFIDENCE_LEVELS}, got {confidence!r}")
        return errs  # nothing else is checkable without a valid level

    try:
        current_value = resolve_pointer(doc, pointer)
        resolves = True
    except PointerResolutionError as exc:
        errs.append(f"{pointer}: {exc}")
        resolves = False
        current_value = None

    if confidence == "measured":
        if not entry.get("source"):
            errs.append(f"{pointer}: confidence=measured requires a non-empty 'source'")
        if not entry.get("as_of"):
            errs.append(f"{pointer}: confidence=measured requires 'as_of'")

    if confidence in _RANGE_OR_DOMAIN_REQUIRED:
        has_range = "plausible_range" in entry
        has_domain = "domain" in entry
        if not has_range and not has_domain:
            errs.append(
                f"{pointer}: confidence={confidence} requires 'plausible_range' or 'domain'"
            )

    if "plausible_range" in entry:
        rng = entry["plausible_range"]
        if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
            errs.append(f"{pointer}: plausible_range must be a 2-element [lo, hi]")
        else:
            lo, hi = rng
            try:
                if lo > hi:
                    errs.append(f"{pointer}: plausible_range {rng} has lo > hi")
            except TypeError:
                errs.append(f"{pointer}: plausible_range {rng} bounds are not comparable")
            if resolves:
                try:
                    if not (lo <= current_value <= hi):
                        errs.append(
                            f"{pointer}: plausible_range {rng} does not bracket the leaf's "
                            f"current value {current_value!r}"
                        )
                except TypeError:
                    errs.append(
                        f"{pointer}: plausible_range {rng} is not comparable to the leaf's "
                        f"current value {current_value!r}"
                    )

    if "domain" in entry:
        dom = entry["domain"]
        if not isinstance(dom, (list, tuple)) or len(dom) == 0:
            errs.append(f"{pointer}: domain must be a non-empty array")
        elif resolves and current_value not in dom:
            errs.append(
                f"{pointer}: domain {dom!r} does not contain the leaf's current value {current_value!r}"
            )

    return errs


@dataclass(frozen=True)
class UncertainLeaf:
    """One leaf whose confidence is ``assumed`` or ``stated`` -- exactly the
    set epic #659 Track 2 sweeps to compute value-of-information."""

    pointer: str
    current_value: Any
    confidence: str
    plausible_range: Optional[Tuple[Any, Any]]
    domain: Optional[Tuple[Any, ...]]
    resolved_by: Optional[str]


@dataclass(frozen=True)
class Provenance:
    """Frozen wrapper around a document's validated ``provenance`` sidecar."""

    entries: Mapping[str, Mapping[str, Any]]

    def confidence(self, pointer: str) -> str:
        """The declared confidence level for ``pointer``, or ``"assumed"``
        when there is no entry -- a leaf with no entry is `assumed` BY
        DEFINITION (#660 property #1)."""
        entry = self.entries.get(pointer)
        if entry is None:
            return "assumed"
        return entry["confidence"]

    def uncertain_leaves(self, doc: Dict) -> List[UncertainLeaf]:
        """Every leaf in ``doc`` (excluding the ``provenance`` sidecar
        itself) whose confidence is ``assumed`` or ``stated`` -- including
        leaves with no sidecar entry at all, returned with
        ``plausible_range=None`` and ``domain=None`` (Track 2 reports these
        as "unranked: no range declared," a finding, not a crash)."""
        substantive = {k: v for k, v in doc.items() if k != "provenance"}
        out: List[UncertainLeaf] = []
        for pointer, value in _walk_leaves(substantive, []):
            entry = self.entries.get(pointer)
            confidence = entry["confidence"] if entry is not None else "assumed"
            if confidence not in ("assumed", "stated"):
                continue
            plausible_range = tuple(entry["plausible_range"]) if entry and "plausible_range" in entry else None
            domain = tuple(entry["domain"]) if entry and "domain" in entry else None
            resolved_by = entry.get("resolved_by") if entry else None
            out.append(
                UncertainLeaf(
                    pointer=pointer,
                    current_value=value,
                    confidence=confidence,
                    plausible_range=plausible_range,
                    domain=domain,
                    resolved_by=resolved_by,
                )
            )
        return out

    @staticmethod
    def resolve(doc: Dict, pointer: str) -> Any:
        """Pure JSON-Pointer get (DP#3)."""
        return resolve_pointer(doc, pointer)

    @staticmethod
    def with_value(doc: Dict, pointer: str, value: Any) -> Dict:
        """Pure JSON-Pointer set (DP#3): returns a NEW document, ``doc`` is
        untouched. Track 2's sweeps call this to materialize each endpoint
        of a ``plausible_range``/``domain`` without mutating the baseline."""
        return set_pointer(doc, pointer, value)


def load_provenance(doc: Dict) -> Provenance:
    """Validate ``doc['provenance']`` (defaulting to ``{}`` if absent --
    the whole sidecar is optional) against ``doc`` and return a frozen
    ``Provenance``. Raises ``ProvenanceValidationError`` listing every
    violation found (not just the first) when invalid:

    - a pointer that does not resolve against ``doc``
    - ``confidence`` outside the five declared levels
    - ``measured`` without ``source`` and ``as_of``
    - ``assumed``/``stated`` without a ``plausible_range`` or ``domain``
    - a ``plausible_range`` that does not bracket the leaf's current value
    - a ``domain`` that does not contain the leaf's current value
    """
    raw = doc.get("provenance")
    if raw is None:  # key absent OR explicitly null -- both mean "no sidecar" (DP#32: explicit
        raw = {}     # absence test, not an `or`-fallback that could swallow a legitimate falsy value)
    if not isinstance(raw, dict):
        raise ProvenanceValidationError(
            f"'provenance' must be an object keyed by JSON Pointer, got {type(raw).__name__}"
        )

    errors: List[str] = []
    for pointer, entry in raw.items():
        errors.extend(_entry_errors(doc, pointer, entry))

    if errors:
        formatted = "\n".join(f"  - {e}" for e in errors)
        raise ProvenanceValidationError(
            f"Provenance failed validation ({len(errors)} error{'s' if len(errors) != 1 else ''}):\n{formatted}"
        )

    entries = MappingProxyType({k: MappingProxyType(dict(v)) for k, v in raw.items()})
    return Provenance(entries=entries)


# ── 3. --provenance report ──────────────────────────────────────────────────


def _rank(pointer: str, provenance: Provenance) -> int:
    entry = provenance.entries.get(pointer)
    if entry is None:
        return _RANK["assumed_no_range"]
    return _RANK[entry["confidence"]]


def build_report(doc: Dict, provenance: Provenance) -> str:
    """Every leaf in ``doc`` (excluding the sidecar), its confidence and its
    source, sorted worst-first: unknown -> assumed-with-no-range -> assumed
    -> stated -> derived -> measured."""
    substantive = {k: v for k, v in doc.items() if k != "provenance"}
    rows = []
    for pointer, value in _walk_leaves(substantive, []):
        entry = provenance.entries.get(pointer)
        if entry is None:
            confidence_label = "assumed[no-entry]"
            source = ""
        else:
            confidence_label = entry["confidence"]
            source = entry.get("source", "")
        rows.append((_rank(pointer, provenance), pointer, confidence_label, value, source))
    rows.sort(key=lambda r: (r[0], r[1]))

    lines = [f"{'confidence':<20} {'pointer':<50} {'value':<20} source"]
    for _, pointer, confidence_label, value, source in rows:
        lines.append(f"{confidence_label:<20} {pointer:<50} {str(value):<20} {source}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Provenance report (#660): every leaf, its confidence, its source, worst-first.")
    parser.add_argument("--input", default="input.json", help="Path to an input contract document.")
    args = parser.parse_args()

    with open(args.input) as f:
        loaded_doc = json.load(f)
    loaded_provenance = load_provenance(loaded_doc)
    print(build_report(loaded_doc, loaded_provenance))
