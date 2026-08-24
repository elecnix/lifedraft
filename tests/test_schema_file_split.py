#!/usr/bin/env python3
"""The universal schema is split across files, and the split stays honest.

``schema/input_schema.json`` was a single 1,368-line, 140 KB document. That
size had a real cost: the repository's automated PR reviewer reads whole
files and could not read this one, so every PR that touched the schema came
back ``COULD_NOT_EVALUATE`` -- the schema was, in practice, the one file
nobody reviewed.

The fix is presentation-only: the spine (metadata, root ``required``,
``allOf`` and the top-level ``properties``) stays in ``input_schema.json``,
which now names its ``$defs`` fragments in ``x-schema-parts``; each fragment
is folded in by ``contract_schema.load_universal_schema()`` using
``compose_schema`` -- the SAME merge the Canada overlay already went through
(DP#8/DP#9: one composition mechanism, not two).

A split is only safe while nothing can fall out of it silently, so this
module is the guard (DP#32 -- a fragment that stops being composed must fail
loudly, not quietly shrink the contract):

1. the declared part list and the files on disk are exactly each other;
2. no ``$def`` name is declared twice (``_merge_fragment`` would silently
   merge two unrelated definitions of the same name);
3. the spine declares no ``$defs`` of its own, so there is one home per name;
4. every ``$ref`` in the composed document resolves;
5. no schema file exceeds the review budget again.
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The fragment machinery lives in contract_schema (the adapter was split
# per namespace); input_contract is now only the orchestrator.
import contract_schema as ic

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
PARTS_DIR = SCHEMA_DIR / "defs"

#: The reason this split exists: a file the automated reviewer cannot read is
#: a file that is never reviewed. 40 KiB is the budget every schema file --
#: spine, fragment, jurisdiction overlay and the worked example -- fits in.
MAX_SCHEMA_FILE_BYTES = 40 * 1024


def _root_spine():
    return json.loads(ic.UNIVERSAL_SCHEMA_PATH.read_text())


class PartRegistrationTest(unittest.TestCase):
    def test_declared_parts_and_files_on_disk_are_the_same_set(self):
        declared = set(_root_spine()[ic.UNIVERSAL_PARTS_KEY])
        on_disk = {str(p.relative_to(SCHEMA_DIR)) for p in sorted(PARTS_DIR.glob("*.json"))}
        self.assertEqual(
            declared, on_disk,
            "schema/defs/*.json and input_schema.json's x-schema-parts have drifted: "
            "a fragment on disk that nothing lists is silently NOT part of the "
            "contract, and a listed fragment that is missing would fail at import.")

    def test_every_declared_part_exists_and_is_a_defs_only_fragment(self):
        for rel in _root_spine()[ic.UNIVERSAL_PARTS_KEY]:
            with self.subTest(part=rel):
                path = SCHEMA_DIR / rel
                self.assertTrue(path.is_file(), f"{rel} is declared but missing")
                frag = json.loads(path.read_text())
                self.assertIn("$defs", frag, f"{rel} contributes no $defs")
                extra = set(frag) - {"$schema", "$id", "title", "$comment", "$defs"}
                self.assertEqual(
                    set(), extra,
                    f"{rel} carries {sorted(extra)}; a fragment supplies $defs and "
                    "metadata only -- root-level shape belongs in the spine (DP#14).")

    def test_spine_declares_no_defs_of_its_own(self):
        self.assertNotIn(
            "$defs", _root_spine(),
            "input_schema.json is the spine: every $def has exactly one home, in a "
            "schema/defs/ fragment.")


    def test_every_declared_part_is_tracked_by_git(self):
        """.gitignore blanket-ignores ``*.json``; a fragment that is not
        explicitly un-ignored exists on the author's disk, composes fine
        locally, and is simply absent from the clone CI (and everyone else)
        builds -- the loudest possible version of a silently-dropped contract
        block."""
        import subprocess
        repo_root = SCHEMA_DIR.parent
        try:
            tracked = subprocess.run(
                ["git", "-C", str(repo_root), "ls-files", "--", "schema"],
                capture_output=True, text=True, check=True).stdout.split()
        except (OSError, subprocess.CalledProcessError) as exc:
            self.skipTest(f"not a git checkout: {exc}")
        for rel in _root_spine()[ic.UNIVERSAL_PARTS_KEY]:
            with self.subTest(part=rel):
                self.assertIn(
                    f"schema/{rel}", tracked,
                    f"schema/{rel} is not tracked by git -- add a `!` rule for it "
                    "in .gitignore next to !schema/input_schema.json.")


    def test_every_declared_part_ships_in_the_installed_package(self):
        """A fragment that git tracks but setuptools does not copy is missing
        from every non-editable install -- the schema composes in the repo and
        raises FileNotFoundError for anyone who `pip install`ed it."""
        import tomllib
        pyproject = SCHEMA_DIR.parent / "pyproject.toml"
        package_data = tomllib.loads(pyproject.read_text())["tool"]["setuptools"][
            "package-data"]
        patterns = set(package_data.get("schema", [])) | set(package_data.get("*", []))
        import fnmatch
        for rel in _root_spine()[ic.UNIVERSAL_PARTS_KEY]:
            with self.subTest(part=rel):
                self.assertTrue(
                    any(fnmatch.fnmatch(rel, pat) for pat in patterns),
                    f"schema/{rel} matches no package-data pattern in "
                    f"pyproject.toml's [tool.setuptools.package-data] {sorted(patterns)}")


class NoDuplicateDefinitionTest(unittest.TestCase):
    def test_no_def_name_is_declared_by_two_fragments(self):
        home = {}
        for rel in _root_spine()[ic.UNIVERSAL_PARTS_KEY]:
            for name in json.loads((SCHEMA_DIR / rel).read_text())["$defs"]:
                self.assertNotIn(
                    name, home,
                    f"$defs/{name} is declared by both {home.get(name)} and {rel}; "
                    "compose_schema would merge them silently.")
                home[name] = rel

    def test_universal_defs_are_exactly_the_union_of_the_fragments(self):
        union = set()
        for rel in _root_spine()[ic.UNIVERSAL_PARTS_KEY]:
            union |= set(json.loads((SCHEMA_DIR / rel).read_text())["$defs"])
        self.assertEqual(union, set(ic.load_universal_schema()["$defs"]))


class ComposedDocumentTest(unittest.TestCase):
    def test_every_ref_in_the_composed_schema_resolves(self):
        composed = ic.compose_schema()
        names = set(composed["$defs"])
        missing = set()

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref" and isinstance(value, str):
                        self.assertTrue(value.startswith("#/$defs/"), value)
                        if value[len("#/$defs/"):] not in names:
                            missing.add(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(composed)
        self.assertEqual(set(), missing,
                         "a $ref lost its target -- a fragment is not being composed")

    def test_composed_schema_is_a_valid_draft_2020_12_schema(self):
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(ic.compose_schema())

    def test_the_shipped_example_still_validates(self):
        ic.validate_contract(json.loads(ic.EXAMPLE_PATH.read_text()))


class ReviewBudgetTest(unittest.TestCase):
    def test_no_schema_file_exceeds_the_review_budget(self):
        oversized = {
            str(p.relative_to(SCHEMA_DIR)): p.stat().st_size
            for p in sorted(SCHEMA_DIR.rglob("*.json"))
            if p.stat().st_size > MAX_SCHEMA_FILE_BYTES
        }
        self.assertEqual(
            {}, oversized,
            f"schema files over {MAX_SCHEMA_FILE_BYTES} bytes are back: {oversized}. "
            "Split the block along a top-level seam into a new schema/defs/ "
            "fragment and list it in input_schema.json's x-schema-parts.")


if __name__ == "__main__":
    unittest.main()
