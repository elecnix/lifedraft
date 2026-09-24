"""Unit tests for tools/examples.py (issues #300, #319).

Nothing here runs the real optimize.py. Its run step is exercised with stub
scripts written to tmp_path, and the projections with fabricated full reports
(round numbers, role-based labels; DP#4, DP#15). The real optimizer is driven
by tests/test_examples_guard.py.

Each of #300's required sabotages has a unit-level twin here, so the detector
is seen to fire without waiting for a regeneration: hand-edited report number,
deleted README section, verdict changed only in meta.json, and a zero-match
examples tree.

The simulate mode (#319) is cheap enough (~1 s per real single run) that its
sabotage suite drives the REAL engine: ``test_simulate_sabotage_*`` copy the
committed simulate example into a git-initialised tmp tree and call the real
``regenerate`` (no stub, no monkeypatch), so each sabotage is seen to fail
against what the engine actually produces.
"""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "lifedraft_examples_tool", REPO_ROOT / "tools" / "examples.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lifedraft_examples_tool"] = mod
    spec.loader.exec_module(mod)
    return mod


ex = _load_tool()

SEED = REPO_ROOT / "examples" / "lifedraft" / "minimal-two-adult"
SIM = REPO_ROOT / "examples" / "lifedraft" / "single-run-two-adult"


# ------------------------------------------------------------------ fixtures
def _year_row(year: int, total_assets: int) -> dict:
    row = {column: 0 for column in ex.KEY_YEAR_COLUMNS}
    row.update(year=year, total_assets=total_assets, ruined=False)
    row["extra_column_not_projected"] = 1000
    return row


def _scenario(strategy: str, net_benefit: int, future_value: int) -> dict:
    return {
        "strategy": strategy,
        "net_benefit": net_benefit,
        "future_value": future_value,
        "ltv": 0.5,
        "exhausted": False,
        "deduct_later": None,
        "year_by_year": [_year_row(2030, 100_000), _year_row(2031, future_value)],
        "solvency": {"ok": True},
        "runway": [1, 2],
    }


def _full_report() -> dict:
    # Deliberately NOT sorted by any metric: the engine's order is the order,
    # and a projection that re-sorts must not pass by coincidence.
    scenarios = [
        _scenario("strategy_b", 200_000, 900_000),
        _scenario("strategy_a", 500_000, 3_000_000),
        _scenario("strategy_c", 100_000, 1_000_000),
    ]
    return {
        "title": "Strategy Optimizer Results",
        "situation": {"members": 2},
        "model_fidelity": {"dollars": "nominal"},
        "optimal_refi_level": None,
        "resp_cashout": {},
        "equity_grants": [],
        "runway": [],
        "runway_sweep": [],
        "asset_location": {},
        "total_scenarios": 3,
        "scenarios": scenarios,
        "category_bests": [{"label": "best", "strategy": "strategy_b", "rows": [1]}],
        "year_by_year": copy.deepcopy(scenarios[0]["year_by_year"]),
    }


def _bytes(full: dict) -> bytes:
    return json.dumps(full).encode("utf-8")


def _write_example(root: Path, source: str = "src", slug: str = "case-one",
                   source_example: Path = SEED) -> Path:
    """A statically-valid example directory copied from a committed example
    (the optimize-mode seed unless ``source_example`` says otherwise)."""
    example = root / source / slug
    example.mkdir(parents=True)
    for name in ex.REQUIRED_FILES:
        (example / name).write_bytes((source_example / name).read_bytes())
    meta = json.loads((example / "meta.json").read_text())
    meta["source"] = source
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return example


def _git_init(path: Path, ignore: str = "") -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if ignore:
        (path / ".gitignore").write_text(ignore)


def _set_verdict(example: Path, readme_verdict: str, meta_verdict: str,
                 linked=None, explanation: str = "Explanation line.") -> None:
    readme = (example / "README.md").read_text()
    head, _, _ = readme.partition("## Verdict\n")
    body = f"## Verdict\n\n{readme_verdict}\n"
    if explanation:
        body += f"\n{explanation}\n"
    (example / "README.md").write_text(head + body)
    meta = json.loads((example / "meta.json").read_text())
    meta["verdict"] = meta_verdict
    if linked is not None:
        meta["linked_issues"] = linked
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")


# ------------------------------------------------------------------ discovery
def test_discover_raises_on_missing_root(tmp_path):
    with pytest.raises(ex.ExamplesError, match="does not exist"):
        ex.discover_examples(tmp_path / "examples")


def test_discover_raises_on_zero_examples(tmp_path):
    root = tmp_path / "examples"
    root.mkdir()
    (root / "README.md").write_text("guide\n")
    with pytest.raises(ex.ExamplesError, match="zero examples"):
        ex.discover_examples(root)


def test_discover_raises_on_empty_source_dir(tmp_path):
    root = tmp_path / "examples"
    (root / "src").mkdir(parents=True)
    with pytest.raises(ex.ExamplesError, match="holds no example"):
        ex.discover_examples(root)


def test_discover_rejects_bad_slug(tmp_path):
    root = tmp_path / "examples"
    (root / "src" / "Bad_Slug").mkdir(parents=True)
    with pytest.raises(ex.ExamplesError, match="Bad_Slug"):
        ex.discover_examples(root)
    root2 = tmp_path / "examples2"
    (root2 / "Bad_Source" / "ok").mkdir(parents=True)
    with pytest.raises(ex.ExamplesError, match="Bad_Source"):
        ex.discover_examples(root2)


def test_discover_rejects_stray_file(tmp_path):
    root = tmp_path / "examples"
    (root / "src" / "case-one").mkdir(parents=True)
    (root / "notes.txt").write_text("x")
    with pytest.raises(ex.ExamplesError, match="stray file"):
        ex.discover_examples(root)
    (root / "notes.txt").unlink()
    (root / "src" / "loose.json").write_text("{}")
    with pytest.raises(ex.ExamplesError, match="stray file"):
        ex.discover_examples(root)


def test_discover_collects_incomplete_example_instead_of_dropping_it(tmp_path):
    root = tmp_path / "examples"
    (root / "src" / "case-one").mkdir(parents=True)
    (root / "src" / "case-two").mkdir(parents=True)
    (root / "src" / "case-two" / "README.md").write_text("only a readme\n")
    found = ex.discover_examples(root)
    assert [ex.example_id(p) for p in found] == ["src/case-one", "src/case-two"]


def test_discover_finds_the_committed_seed():
    assert SEED in ex.discover_examples()


# ------------------------------------------------------------------ check_files
def test_check_files_reports_missing_and_extra_files(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    assert ex.check_files(example) == []
    (example / "meta.json").unlink()
    (example / "full.json").write_text("{}")
    (example / "sub").mkdir()
    problems = "\n".join(ex.check_files(example))
    assert "missing required file(s) ['meta.json']" in problems
    assert "full.json" in problems and "sub" in problems


def test_check_files_reports_oversize_report(tmp_path, monkeypatch):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    monkeypatch.setitem(ex.SIZE_CAPS, "report.json", 10)
    problems = ex.check_files(example)
    assert any("report.json" in p and "over its 10 B cap" in p for p in problems)


def test_check_files_reports_gitignored_file(tmp_path):
    _git_init(tmp_path, ignore="*.json\n")
    example = _write_example(tmp_path / "examples")
    problems = "\n".join(ex.check_files(example))
    for name in ("input.json", "report.json", "meta.json"):
        assert f"{name} is gitignored" in problems
    assert "README.md is gitignored" not in problems
    # The real repository's negations keep the committed seed trackable.
    assert ex.check_files(SEED) == []


def test_check_files_raises_outside_a_git_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    example = _write_example(tmp_path / "examples")
    with pytest.raises(ex.ExamplesError, match="git check-ignore failed"):
        ex.check_files(example)


# ------------------------------------------------------------------ projection
def test_project_report_keeps_engine_order_scalars_and_hash():
    full = _full_report()
    raw = _bytes(full)
    report = ex.project_report(full, full_bytes=raw)
    assert [s["strategy"] for s in report["scenarios"]] == [
        "strategy_b", "strategy_a", "strategy_c"]
    assert [s["rank"] for s in report["scenarios"]] == [1, 2, 3]
    first = report["scenarios"][0]
    assert first == {
        "rank": 1, "strategy": "strategy_b", "net_benefit": 200_000,
        "future_value": 900_000, "ltv": 0.5, "exhausted": False, "deduct_later": None,
    }
    assert report["category_bests"] == [{"label": "best", "strategy": "strategy_b"}]
    assert report["winner_year_by_year"] == [
        {c: row[c] for c in ex.KEY_YEAR_COLUMNS}
        for row in full["scenarios"][0]["year_by_year"]
    ]
    assert report["winner_year_by_year"][-1]["total_assets"] == first["future_value"]
    assert report["projection"] == {
        "version": ex.PROJECTION_VERSION,
        "function": "tools/examples.py::project_report",
        "source": "optimize.py --json",
        "full_report_bytes": len(raw),
        "full_report_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "key_year_columns": list(ex.KEY_YEAR_COLUMNS),
    }
    for key in ex.TOP_VERBATIM:
        assert report[key] == full[key]
    assert set(report) == ex.TOP_VERBATIM | {
        "projection", "scenarios", "category_bests", "winner_year_by_year"}


def test_project_report_copies_floats_unrounded():
    full = _full_report()
    full["scenarios"][0]["net_benefit"] = 200_000.123456789
    report = ex.project_report(full, full_bytes=_bytes(full))
    assert report["scenarios"][0]["net_benefit"] == 200_000.123456789


def test_project_report_refuses_unknown_top_level_key():
    full = _full_report()
    full["new_section"] = {}
    with pytest.raises(ex.ExamplesError, match=r"unknown \['new_section'\].*PROJECTION_VERSION"):
        ex.project_report(full, full_bytes=_bytes(full))


def test_project_report_refuses_missing_top_level_key():
    full = _full_report()
    del full["model_fidelity"]
    with pytest.raises(ex.ExamplesError, match=r"missing \['model_fidelity'\]"):
        ex.project_report(full, full_bytes=_bytes(full))


def test_project_report_refuses_missing_year_column():
    full = _full_report()
    for row in full["scenarios"][0]["year_by_year"]:
        del row["oas_clawback"]
    full["year_by_year"] = copy.deepcopy(full["scenarios"][0]["year_by_year"])
    with pytest.raises(ex.ExamplesError, match="'oas_clawback'"):
        ex.project_report(full, full_bytes=_bytes(full))


def test_project_report_refuses_winner_mismatch():
    full = _full_report()
    full["year_by_year"] = copy.deepcopy(full["scenarios"][1]["year_by_year"])
    with pytest.raises(ex.ExamplesError, match="winner-identity"):
        ex.project_report(full, full_bytes=_bytes(full))


def test_project_report_refuses_empty_scenarios():
    full = _full_report()
    full["scenarios"] = []
    full["total_scenarios"] = 0
    with pytest.raises(ex.ExamplesError, match="empty 'scenarios'"):
        ex.project_report(full, full_bytes=_bytes(full))


def test_project_report_refuses_total_scenarios_mismatch():
    full = _full_report()
    full["total_scenarios"] = 4
    with pytest.raises(ex.ExamplesError, match="total_scenarios"):
        ex.project_report(full, full_bytes=_bytes(full))


def test_dump_report_json_is_deterministic_and_refuses_nan():
    full = _full_report()
    report = ex.project_report(full, full_bytes=_bytes(full))
    first = ex.dump_report_json(report)
    assert first == ex.dump_report_json(copy.deepcopy(report))
    assert first.endswith("}\n")
    report["scenarios"][0]["net_benefit"] = float("nan")
    with pytest.raises(ex.ExamplesError, match="non-finite"):
        ex.dump_report_json(report)


# ------------------------------------------------------------------ secret-scan shape
# PR #318's first CI run failed secret-scan: detect-secrets 1.5.0 flagged the
# bare 64-hex sha256 in the seed's report.json as a "Hex High Entropy String".
# The digest changes on every legitimate regen, so a .secrets.baseline entry
# would have to be re-added in every engine PR; the digest is typed instead.
def test_projection_digest_is_typed_not_bare_hex():
    full = _full_report()
    raw = _bytes(full)
    digest = ex.project_report(full, full_bytes=raw)["projection"]["full_report_digest"]
    assert digest == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert ex.bare_hex_strings({"digest": digest}) == []


def test_bare_hex_strings_finds_values_and_keys_at_any_depth():
    bare = "0123456789abcdef" * 4
    doc = {"a": [{"b": bare}], bare: 1, "short": "abcdef", "digest": "sha256:" + bare,
           "word": "strategy_a", "n": 1234567890123456}
    assert ex.bare_hex_strings(doc) == ["/a/0/b", f"/{bare} (key)"]
    assert ex.bare_hex_strings("DEADBEEF" * 2) == ["/"]
    assert ex.bare_hex_strings("DEADBEEF" * 2 + "g") == []
    assert ex.bare_hex_strings("f" * (ex.BARE_HEX_MIN_LEN - 1)) == []


def test_dump_report_json_refuses_bare_hex_string():
    full = _full_report()
    report = ex.project_report(full, full_bytes=_bytes(full))
    report["projection"]["full_report_digest"] = hashlib.sha256(b"x").hexdigest()
    with pytest.raises(ex.ExamplesError,
                       match=r"bare hex string\(s\) at \['/projection/full_report_digest'\]"):
        ex.dump_report_json(report)


def test_committed_bare_hex_fails_static_contract(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path)
    assert ex.check_no_bare_hex(example) == []
    report = json.loads((example / "report.json").read_text())
    report["projection"]["full_report_digest"] = hashlib.sha256(b"x").hexdigest()
    (example / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    problems = "\n".join(ex.static_problems(example))
    assert "report.json holds bare hex string(s) at ['/projection/full_report_digest']" in problems
    assert "secret scan" in problems
    (example / "report.json").write_text("{")
    assert "report.json is not readable JSON" in "\n".join(ex.check_no_bare_hex(example))
    meta = json.loads((example / "meta.json").read_text())
    meta["publication_id"] = "0123456789abcdef0123"
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    assert any("meta.json holds bare hex" in p for p in ex.check_no_bare_hex(example))


def test_seed_report_has_no_bare_hex_string():
    assert ex.bare_hex_strings(json.loads((SEED / "report.json").read_text())) == []


def test_dump_report_json_refuses_oversize(monkeypatch):
    full = _full_report()
    report = ex.project_report(full, full_bytes=_bytes(full))
    monkeypatch.setattr(ex, "REPORT_JSON_CAP", 100)
    with pytest.raises(ex.ExamplesError, match="over the 100 B cap"):
        ex.dump_report_json(report)


# ------------------------------------------------------------------ markdown
_MD = (
    "# Results\n\nintro\n\n"
    "## Section one\n\n" + "a" * 100 + "\n\n"
    "## Section two\n\n" + "b" * 100 + "\n\n"
    "## Section three\n\n" + "c" * 100 + "\n"
)


def test_project_markdown_verbatim_under_cap():
    assert ex.project_markdown(_MD) == _MD


def test_project_markdown_trims_at_section_boundary_with_marker(monkeypatch):
    assert len(_MD.encode()) == 373
    monkeypatch.setattr(ex, "REPORT_MD_CAP", 372)
    out = ex.project_markdown(_MD)
    cut = _MD.index("## Section three")
    assert out.startswith(_MD[:cut])
    assert out[len(_MD[:cut]):] == (
        f"\n<!-- trimmed by tools/examples.py project_markdown v1: kept {cut} of "
        f"{len(_MD)} bytes; full output via optimize.py --md -->\n"
    )
    assert "Section three" not in out
    assert len(out.encode()) <= 372
    assert ex.project_markdown(_MD) == out
    # A tighter cap cuts at the previous boundary, never mid-section.
    monkeypatch.setattr(ex, "REPORT_MD_CAP", 300)
    assert ex.project_markdown(_MD).startswith(_MD[:_MD.index("## Section two")] + "\n<!--")


def test_project_markdown_refuses_when_no_boundary_fits(monkeypatch):
    monkeypatch.setattr(ex, "REPORT_MD_CAP", 130)
    with pytest.raises(ex.ExamplesError, match="no '## ' section boundary"):
        ex.project_markdown(_MD)


# ------------------------------------------------------------------ compare
def _committed(tmp_path: Path, json_text: str, md_text: str) -> Path:
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    (example / "report.json").write_text(json_text)
    (example / "report.md").write_text(md_text)
    return example


def test_compare_reports_passes_on_identical_bytes(tmp_path):
    example = _committed(tmp_path, '{\n  "a": 1\n}\n', "# md\n")
    assert ex.compare_reports(example, '{\n  "a": 1\n}\n', "# md\n") == []


def test_compare_reports_detects_hand_edited_number(tmp_path):
    fresh = '{\n  "net_benefit": 8325994.9\n}\n'
    example = _committed(tmp_path, fresh.replace("8325994", "9325994"), "# md\n")
    problems = ex.compare_reports(example, fresh, "# md\n")
    assert len(problems) == 1
    message = problems[0]
    assert '-  "net_benefit": 9325994.9' in message
    assert '+  "net_benefit": 8325994.9' in message
    assert "tools/examples.py regen examples/src/case-one" in message
    assert "explain the move in the PR" in message


def test_compare_reports_detects_whitespace_only_edit(tmp_path):
    fresh = '{\n  "a": 1\n}\n'
    example = _committed(tmp_path, '{\n "a": 1\n}\n', "# md\n")
    assert len(ex.compare_reports(example, fresh, "# md\n")) == 1


def test_compare_reports_detects_edited_markdown(tmp_path):
    example = _committed(tmp_path, "{}\n", "# md\nline X\n")
    problems = ex.compare_reports(example, "{}\n", "# md\nline\n")
    assert len(problems) == 1 and "report.md" in problems[0]


def test_compare_reports_flags_missing_committed_report(tmp_path):
    example = _committed(tmp_path, "{}\n", "# md\n")
    (example / "report.md").unlink()
    problems = ex.compare_reports(example, "{}\n", "# md\n")
    assert len(problems) == 1 and "missing" in problems[0]


def test_compare_reports_truncates_long_diff(tmp_path):
    fresh = "".join(f"line {i}\n" for i in range(200))
    example = _committed(tmp_path, "{}\n", fresh.replace("line", "LINE"))
    problems = ex.compare_reports(example, "{}\n", fresh)
    assert "more lines" in problems[0]


def test_write_reports_writes_both_files(tmp_path):
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    ex.write_reports(example, "{}\n", "# md\n")
    assert (example / "report.json").read_text() == "{}\n"
    assert (example / "report.md").read_text() == "# md\n"
    assert sorted(p.name for p in example.iterdir()) == ["report.json", "report.md"]


# ------------------------------------------------------------------ README / meta
def test_seed_passes_every_static_check():
    assert ex.static_problems(SEED) == []


def test_check_readme_detects_deleted_section(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    readme = (example / "README.md").read_text()
    start = readme.index("## Verdict")
    (example / "README.md").write_text(readme[:start])
    problems = "\n".join(ex.static_problems(example))
    assert "'## Verdict' must appear exactly once (found 0)" in problems


def test_check_readme_detects_duplicate_and_empty_section(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    readme = (example / "README.md").read_text()
    (example / "README.md").write_text(
        readme.replace("## Publication claim\n", "## Publication claim\n\n## Extra\n", 1)
        + "\n## Source\n\nagain\n"
    )
    problems = "\n".join(ex.static_problems(example))
    assert "'## Publication claim' is empty" in problems
    assert "'## Source' must appear exactly once (found 2)" in problems


def test_check_readme_requires_source_url_and_every_input_key(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    readme = (example / "README.md").read_text()
    readme = readme.replace("- `provenance`:", "- provenance:")
    readme = readme.replace("https://github.com/elecnix/lifedraft/blob/594b6f8/", "")
    (example / "README.md").write_text(readme)
    problems = "\n".join(ex.static_problems(example))
    assert "does not mention top-level input key(s) ['provenance']" in problems
    assert "must contain meta.json source_url" in problems


def test_verdict_mismatch_between_readme_and_meta(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    meta = json.loads((example / "meta.json").read_text())
    meta["verdict"] = "DIFFERS (explained)"
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    problems = "\n".join(ex.static_problems(example))
    assert ("README verdict 'AGREES' does not equal meta.json verdict "
            "'DIFFERS (explained)'") in problems


def test_differs_engine_issue_format(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")

    _set_verdict(example, "DIFFERS (engine issue #42)", "DIFFERS (engine issue #42)",
                 linked=[300, 42])
    assert ex.static_problems(example) == []

    _set_verdict(example, "DIFFERS (engine issue #42)", "DIFFERS (engine issue #42)",
                 linked=[300])
    assert "does not list it" in "\n".join(ex.static_problems(example))

    for bad in ("DIFFERS (engine issue #0)", "DIFFERS (engine issue #abc)",
                "DIFFERS (engine issue 42)", "AGREE", "DIFFERS", "agrees"):
        _set_verdict(example, bad, bad, linked=[300])
        problems = "\n".join(ex.static_problems(example))
        assert "is not one of AGREES" in problems, bad
        assert "must be exactly AGREES" in problems, bad

    _set_verdict(example, "DIFFERS (explained)", "DIFFERS (explained)", explanation="")
    assert "needs the explanation" in "\n".join(ex.static_problems(example))
    _set_verdict(example, "DIFFERS (explained)", "DIFFERS (explained)",
                 explanation="The publication rounds to the nearest hundred.")
    assert ex.static_problems(example) == []


def test_meta_rejects_unknown_and_missing_keys_and_bad_types(tmp_path):
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    good = json.loads((SEED / "meta.json").read_text())
    good["source"] = "src"
    assert ex.check_meta(example, good) == []

    extra = dict(good, notes="x")
    del extra["year"]
    problems = "\n".join(ex.check_meta(example, extra))
    assert "unknown ['notes']" in problems and "missing ['year']" in problems

    bad = dict(good, source="other", source_url="http://example.org",
               publication_id="", title=3, authors=[], year=True,
               retrieval_date="24/09/2026", linked_issues=[0, "7"])
    problems = "\n".join(ex.check_meta(example, bad))
    for needle in ("must equal the directory name", "https:// URL",
                   "publication_id must be", "title must be", "authors must be",
                   "year must be", "is not an ISO date",
                   "linked_issues must be"):
        assert needle in problems, needle
    assert ex.check_meta(example, ["not", "an", "object"]) == [
        "src/case-one/meta.json is not a JSON object"]


def test_invalid_meta_still_fails_and_blocks_cross_check(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    (example / "meta.json").write_text("{not json")
    problems = "\n".join(ex.static_problems(example))
    assert "meta.json is not readable JSON" in problems
    assert "were not cross-checked" in problems


# ------------------------------------------------------------------ input / DP#15
def test_check_input_reports_contract_refusal(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    doc = json.loads((example / "input.json").read_text())
    del doc["people"]
    (example / "input.json").write_text(json.dumps(doc))
    _, problems = ex.check_input(example)
    assert len(problems) == 1
    assert "refused by input_contract.load_and_map (ContractValidationError)" in problems[0]
    assert "people" in problems[0]


def test_check_input_reports_unparseable_json(tmp_path):
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    (example / "input.json").write_text("{")
    doc, problems = ex.check_input(example)
    assert doc is None and "not readable JSON" in problems[0]


def test_home_path_and_email_detected():
    for text in ("see /home/someone/x", "see /Users/someone/x", "see C:\\Users\\x\\y",
                 "see ~/notes", "see /root/x"):
        assert ex.scan_personal_data("README.md", text), text
    assert ex.scan_personal_data("report.md", "contact primary@example.com")
    assert ex.scan_personal_data("README.md", "SIN 123-456-789")
    assert ex.scan_personal_data("input.json", "123 456 789")
    # Computed floats in report.json are not scanned for SIN shapes.
    assert ex.scan_personal_data("report.json", "123 456 789") == []
    assert ex.scan_personal_data("README.md", "A clean line with 2026-06-30.") == []


def test_home_path_in_input_provenance_fails_static_contract(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    doc = json.loads((example / "input.json").read_text())
    doc["provenance"]["/accounts/0/balance/amount"]["source"] = (
        "file:///home/user/documents/statement.pdf#page=1")
    (example / "input.json").write_text(json.dumps(doc, indent=2) + "\n")
    problems = "\n".join(ex.static_problems(example))
    assert "input.json" in problems and "absolute home path" in problems


# ------------------------------------------------------------------ run step (stubs)
def _stub(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "stub_optimize.py"
    script.write_text("import os, sys, json\nargs = sys.argv[1:]\n" + body)
    return script


_WRITE_OUTPUTS = (
    "out_json = args[args.index('--json') + 1]\n"
    "out_md = args[args.index('--md') + 1]\n"
    "open(out_json, 'w').write(json.dumps({'home': os.environ['HOME'], 'argv': args,"
    " 'cwd': os.getcwd()}))\n"
    "open(out_md, 'w').write('# md\\n')\n"
)


def test_run_optimize_refuses_nonzero_exit(tmp_path):
    stub = _stub(tmp_path, "print('Traceback: boom'); sys.exit(3)\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ex.ExamplesError, match="exited 3") as info:
        ex.run_optimize(tmp_path / "input.json", work, workers=1, optimize_py=stub)
    assert "boom" in str(info.value)


def test_run_optimize_refuses_rc0_without_outputs(tmp_path):
    stub = _stub(tmp_path, "print('Error: unknown objective')\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ex.ExamplesError, match="exited 0 but wrote no full.json"):
        ex.run_optimize(tmp_path / "input.json", work, workers=1, optimize_py=stub)


def test_run_optimize_refuses_rc0_with_empty_output(tmp_path):
    stub = _stub(tmp_path, _WRITE_OUTPUTS + "open(out_md, 'w').write('')\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ex.ExamplesError, match="wrote no full.md"):
        ex.run_optimize(tmp_path / "input.json", work, workers=1, optimize_py=stub)


def test_run_optimize_uses_isolated_home(tmp_path):
    stub = _stub(tmp_path, _WRITE_OUTPUTS)
    work = tmp_path / "work"
    work.mkdir()
    full_json, full_md = ex.run_optimize(
        tmp_path / "input.json", work, workers=1, optimize_py=stub)
    seen = json.loads(full_json.read_text())
    assert seen["home"] == str(work / "home")
    assert seen["home"] != os.environ["HOME"]
    assert Path(seen["cwd"]).resolve() == work.resolve()
    argv = seen["argv"]
    assert argv[argv.index("--workers") + 1] == "1"
    assert "--save-session" not in argv
    assert argv[argv.index("--input") + 1] == str((tmp_path / "input.json").resolve())
    assert full_md.read_text() == "# md\n"


def test_run_optimize_omits_workers_when_none(tmp_path):
    stub = _stub(tmp_path, _WRITE_OUTPUTS)
    work = tmp_path / "work"
    work.mkdir()
    full_json, _ = ex.run_optimize(tmp_path / "input.json", work, workers=None,
                                   optimize_py=stub)
    assert "--workers" not in json.loads(full_json.read_text())["argv"]


def _write_meta_mode(example: Path, mode) -> None:
    (example / "meta.json").write_text(json.dumps({"mode": mode}) + "\n")


def test_regenerate_refuses_example_without_input(tmp_path):
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    _write_meta_mode(example, "optimize")
    with pytest.raises(ex.ExamplesError, match="no input.json"):
        ex.regenerate(example, workers=1)


def test_regenerate_projects_stub_engine_output(tmp_path):
    """regenerate() glues run -> project -> dump; a stub engine that writes a
    fabricated full report proves the glue without the 40 s real run."""
    full = _full_report()
    (tmp_path / "full.json").write_text(json.dumps(full))
    stub = _stub(
        tmp_path,
        "import shutil\n"
        "shutil.copy(%r, args[args.index('--json') + 1])\n"
        "open(args[args.index('--md') + 1], 'w').write('# md\\n')\n"
        % str(tmp_path / "full.json"),
    )
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    (example / "input.json").write_text("{}")
    _write_meta_mode(example, "optimize")
    json_text, md_text = ex.regenerate(example, workers=None, optimize_py=stub)
    raw = (tmp_path / "full.json").read_bytes()
    assert json_text == ex.dump_report_json(ex.project_report(full, full_bytes=raw))
    assert md_text == "# md\n"
    assert sorted(p.name for p in example.iterdir()) == ["input.json", "meta.json"]


# ------------------------------------------------------------------ CLI
def test_cli_regen_rejects_path_outside_examples(tmp_path, capsys):
    assert ex.main(["regen", str(tmp_path)], python_version=ex.CANONICAL_PYTHON) == 1
    assert "is not an example directory" in capsys.readouterr().err


def test_cli_regen_rejects_unknown_example(capsys):
    assert ex.main(["regen", str(REPO_ROOT / "examples" / "lifedraft" / "nope")],
                   python_version=ex.CANONICAL_PYTHON) == 1


def test_cli_fails_on_missing_examples_root(tmp_path, capsys):
    assert ex.main(["regen"], root=tmp_path / "examples", python_version=ex.CANONICAL_PYTHON) == 1
    assert "does not exist" in capsys.readouterr().err


def test_cli_requires_a_subcommand():
    with pytest.raises(SystemExit) as info:
        ex.main([])
    assert info.value.code != 0


def test_cli_regen_writes_reports_through_stub_engine(tmp_path, capsys):
    full = _full_report()
    (tmp_path / "full.json").write_text(json.dumps(full))
    stub = _stub(
        tmp_path,
        "import shutil\n"
        "shutil.copy(%r, args[args.index('--json') + 1])\n"
        "open(args[args.index('--md') + 1], 'w').write('# md\\n')\n"
        % str(tmp_path / "full.json"),
    )
    root = tmp_path / "examples"
    example = root / "src" / "case-one"
    example.mkdir(parents=True)
    (example / "input.json").write_text("{}")
    _write_meta_mode(example, "optimize")
    assert ex.main(["regen", "--workers", "1"], root=root, optimize_py=stub,
                   python_version=ex.CANONICAL_PYTHON) == 0
    assert "regenerated src/case-one (optimize mode)" in capsys.readouterr().out
    assert json.loads((example / "report.json").read_text())["projection"]["version"] == 1
    assert (example / "report.md").read_text() == "# md\n"


# ------------------------------------------------------------------ canonical Python
def test_canonical_python_is_the_ci_pr_leg():
    workflow = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text()
    major, minor = ex.CANONICAL_PYTHON
    assert f"""fromJSON('["{major}.{minor}"]')""" in workflow
    assert ex.is_canonical_python((major, minor))
    assert not ex.is_canonical_python((3, 11))


def test_cli_regen_refuses_non_canonical_python(tmp_path, capsys):
    root = tmp_path / "examples"
    (root / "src" / "case-one").mkdir(parents=True)
    assert ex.main(["regen"], root=root, python_version=(3, 11)) == 1
    assert "must run under Python 3.12" in capsys.readouterr().err
    assert list((root / "src" / "case-one").iterdir()) == []


def _projected_text(full: dict) -> str:
    return ex.dump_report_json(ex.project_report(full, full_bytes=_bytes(full)))


def test_compare_reports_near_tolerates_last_place_float_drift(tmp_path):
    full = _full_report()
    full["scenarios"][0]["ltv"] = 0.1 + 0.2          # 0.30000000000000004
    committed = _projected_text(full)
    drifted = copy.deepcopy(full)
    drifted["scenarios"][0]["ltv"] = 0.3              # one ulp away
    # _bytes(drifted) differs from _bytes(full), so the full-report hash moves too.
    drifted_text = _projected_text(drifted)
    example = _committed(tmp_path, committed, "# md\n")
    assert ex.compare_reports(example, drifted_text, "# md\n") != []
    assert ex.compare_reports_near(example, drifted_text, "# md\n") == []


def test_compare_reports_near_still_catches_a_hand_edit(tmp_path):
    full = _full_report()
    fresh = _projected_text(full)
    example = _committed(tmp_path, fresh.replace('"net_benefit": 200000', '"net_benefit": 200001'),
                         "# md\n")
    problems = ex.compare_reports_near(example, fresh, "# md\n")
    assert len(problems) == 1
    assert "report.scenarios[0].net_benefit: 200001 -> 200000" in problems[0]
    assert "tools/examples.py regen" in problems[0]
    # A float moved beyond last-place drift is caught too.
    full2 = copy.deepcopy(full)
    full2["scenarios"][0]["ltv"] = 0.5000001
    example2 = _committed(tmp_path / "b", _projected_text(full2), "# md\n")
    assert ex.compare_reports_near(example2, fresh, "# md\n")


def test_compare_reports_near_compares_markdown_bytes_and_structure(tmp_path):
    full = _full_report()
    fresh = _projected_text(full)
    example = _committed(tmp_path, fresh, "# md edited\n")
    problems = ex.compare_reports_near(example, fresh, "# md\n")
    assert len(problems) == 1 and "report.md" in problems[0]
    fewer = copy.deepcopy(full)
    del fewer["scenarios"][2]
    fewer["total_scenarios"] = 2
    example2 = _committed(tmp_path / "b", _projected_text(fewer), "# md\n")
    problems = "\n".join(ex.compare_reports_near(example2, fresh, "# md\n"))
    assert "report.scenarios: length 2 -> 3" in problems
    example3 = _committed(tmp_path / "c", '{"no": "projection"}\n', "# md\n")
    assert "no projection block" in "\n".join(
        ex.compare_reports_near(example3, fresh, "# md\n"))
    (example3 / "report.json").unlink()
    assert "missing" in "\n".join(ex.compare_reports_near(example3, fresh, "# md\n"))


# ================================================================== #319: modes
_ALL_BAD_MODES = [None, "", "Simulate", "SIMULATE", "optimise", 1, True,
                  ["simulate"], {"x": 1}]


def _seed_meta(source: str = "src") -> dict:
    meta = json.loads((SEED / "meta.json").read_text())
    meta["source"] = source
    return meta


def test_every_committed_example_declares_a_valid_mode():
    for example in ex.discover_examples():
        meta = json.loads((example / "meta.json").read_text())
        assert meta["mode"] in ex.EXAMPLE_MODES, ex.example_id(example)
        assert ex.read_mode(example) == meta["mode"]


def test_meta_requires_mode_key(tmp_path):
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    meta = _seed_meta()
    assert ex.check_meta(example, meta) == []
    del meta["mode"]
    problems = "\n".join(ex.check_meta(example, meta))
    assert "missing ['mode']" in problems
    assert "'mode' is required: declare simulate | optimize" in problems
    assert "no default, DP#32" in problems


@pytest.mark.parametrize("bad", _ALL_BAD_MODES, ids=repr)
def test_meta_rejects_unknown_mode(tmp_path, bad):
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    meta = dict(_seed_meta(), mode=bad)
    problems = ex.check_meta(example, meta)
    assert len(problems) == 1, problems
    assert f"mode {bad!r} is not one of simulate | optimize" in problems[0]


def test_static_contract_fails_on_missing_or_invalid_mode(tmp_path):
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    assert ex.static_problems(example) == []
    meta = json.loads((example / "meta.json").read_text())
    meta["mode"] = "Optimize"
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    assert "mode 'Optimize' is not one of" in "\n".join(ex.static_problems(example))
    del meta["mode"]
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    assert "'mode' is required" in "\n".join(ex.static_problems(example))


def test_read_mode_raises_on_missing_meta_missing_key_and_unknown_value(tmp_path):
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    with pytest.raises(ex.ExamplesError, match="meta.json is missing"):
        ex.read_mode(example)
    (example / "meta.json").write_text("{not json")
    with pytest.raises(ex.ExamplesError, match="not readable JSON"):
        ex.read_mode(example)
    (example / "meta.json").write_text('["simulate"]')
    with pytest.raises(ex.ExamplesError, match="not a JSON object"):
        ex.read_mode(example)
    (example / "meta.json").write_text('{"source": "src"}')
    with pytest.raises(ex.ExamplesError, match="has no 'mode'"):
        ex.read_mode(example)
    for bad in _ALL_BAD_MODES:
        _write_meta_mode(example, bad)
        with pytest.raises(ex.ExamplesError, match="is not one of simulate | optimize"):
            ex.read_mode(example)
    for good in ex.EXAMPLE_MODES:
        _write_meta_mode(example, good)
        assert ex.read_mode(example) == good


def _marker_stub(tmp_path: Path, name: str) -> tuple[Path, Path]:
    marker = tmp_path / f"{name}.ran"
    script = tmp_path / f"{name}.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    return script, marker


def test_regenerate_refuses_missing_mode_before_running_engine(tmp_path):
    opt, opt_marker = _marker_stub(tmp_path, "optimize_stub")
    sim, sim_marker = _marker_stub(tmp_path, "examples_stub")
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    (example / "input.json").write_text("{}")
    for meta in (None, {}, {"mode": None}, {"mode": "Simulate"}, {"mode": "optimise"}):
        if meta is None:
            if (example / "meta.json").exists():
                (example / "meta.json").unlink()
        else:
            (example / "meta.json").write_text(json.dumps(meta))
        with pytest.raises(ex.ExamplesError, match="mode|meta.json is missing"):
            ex.regenerate(example, workers=1, optimize_py=opt, examples_py=sim)
    assert not opt_marker.exists() and not sim_marker.exists()


def _sim_full(rows=None) -> dict:
    if rows is None:
        rows = [_sim_row(1, 100_000.5), _sim_row(2, 150_000.25), _sim_row(3, 212_345.678901)]
    return {
        "engine_entry": ex.SIMULATE_ENGINE_ENTRY,
        "run": {"strategy": "strategy_a", "rate_path": "Default",
                "use_readvanceable": False, "deduct_later": False,
                "start_year": 2030, "projection_years": len(rows),
                "retirement_ages": [{"id": "p1", "role": "primary", "retirement_age": 65}]},
        "year_by_year": rows,
    }


def _sim_row(year: int, total_assets: float) -> dict:
    row = {column: 0.0 for column in ex.SIMULATE_SERIES_COLUMNS}
    row.update(year=year, total_assets=total_assets, ruined=False)
    row["extra_scalar_not_in_series"] = year * 10.125
    row["nested_not_scalar"] = {"a": 1}
    row["list_not_scalar"] = [1, 2]
    return row


_SIM_STUB = (
    "out = args[args.index('--out') + 1]\n"
    "assert args[0] == 'simulate-once', args\n"
    "open(out, 'w').write(open(%r).read())\n"
)


def test_regenerate_dispatches_optimize_to_optimize_py_and_simulate_to_simulate_once(tmp_path):
    full = _full_report()
    (tmp_path / "full_opt.json").write_text(json.dumps(full))
    sim_full = _sim_full()
    (tmp_path / "full_sim.json").write_text(json.dumps(sim_full))
    opt_log = tmp_path / "optimize.log"
    sim_log = tmp_path / "simulate.log"
    opt = tmp_path / "opt_stub.py"
    opt.write_text(
        "import sys, shutil\nargs = sys.argv[1:]\n"
        f"open({str(opt_log)!r}, 'a').write('ran\\n')\n"
        f"shutil.copy({str(tmp_path / 'full_opt.json')!r}, args[args.index('--json') + 1])\n"
        "open(args[args.index('--md') + 1], 'w').write('# md\\n')\n")
    sim = tmp_path / "sim_stub.py"
    sim.write_text(
        "import sys\nargs = sys.argv[1:]\n"
        f"open({str(sim_log)!r}, 'a').write('ran\\n')\n"
        + _SIM_STUB % str(tmp_path / "full_sim.json"))
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
    (example / "input.json").write_text(json.dumps(
        json.loads((SIM / "input.json").read_text())))

    _write_meta_mode(example, "optimize")
    json_text, md_text = ex.regenerate(example, workers=1, optimize_py=opt, examples_py=sim)
    assert opt_log.read_text() == "ran\n" and not sim_log.exists()
    assert json.loads(json_text)["projection"]["function"] == ex.PROJECTION_FUNCTION
    assert md_text == "# md\n"

    _write_meta_mode(example, "simulate")
    json_text, md_text = ex.regenerate(example, workers=1, optimize_py=opt, examples_py=sim)
    assert opt_log.read_text() == "ran\n" and sim_log.read_text() == "ran\n"
    raw = (tmp_path / "full_sim.json").read_bytes()
    report = ex.project_simulation(sim_full, full_bytes=raw)
    assert json_text == ex.dump_report_json(report)
    assert md_text == ex.render_simulation_markdown(report)


def _sim_run_stub(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "stub_examples.py"
    script.write_text("import os, sys, json\nargs = sys.argv[1:]\n" + body)
    return script


def test_run_simulation_refuses_nonzero_exit(tmp_path):
    stub = _sim_run_stub(tmp_path, "print('Traceback: boom'); sys.exit(3)\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ex.ExamplesError, match="simulate-once exited 3") as info:
        ex.run_simulation(tmp_path / "input.json", work, examples_py=stub)
    assert "boom" in str(info.value)


def test_run_simulation_refuses_rc0_without_output(tmp_path):
    stub = _sim_run_stub(tmp_path, "print('nothing written')\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ex.ExamplesError, match="exited 0 but wrote no full.json") as info:
        ex.run_simulation(tmp_path / "input.json", work, examples_py=stub)
    assert "nothing written" in str(info.value)


def test_run_simulation_refuses_rc0_with_empty_output(tmp_path):
    stub = _sim_run_stub(tmp_path, "open(args[args.index('--out') + 1], 'w').write('')\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ex.ExamplesError, match="exited 0 but wrote no full.json"):
        ex.run_simulation(tmp_path / "input.json", work, examples_py=stub)


def test_run_simulation_uses_isolated_home(tmp_path):
    stub = _sim_run_stub(
        tmp_path,
        "open(args[args.index('--out') + 1], 'w').write(json.dumps({'home': "
        "os.environ['HOME'], 'argv': args, 'cwd': os.getcwd(), 'exe': sys.executable}))\n")
    work = tmp_path / "work"
    work.mkdir()
    out = ex.run_simulation(tmp_path / "input.json", work, examples_py=stub)
    seen = json.loads(out.read_text())
    assert seen["home"] == str(work / "home")
    assert seen["home"] != os.environ["HOME"]
    assert list((work / "home").iterdir()) == []
    assert Path(seen["cwd"]).resolve() == work.resolve()
    assert seen["exe"] == sys.executable
    assert seen["argv"] == ["simulate-once", "--input",
                            str((tmp_path / "input.json").resolve()),
                            "--out", str(work / "full.json")]


def test_run_simulation_defaults_to_this_module():
    import inspect
    default = inspect.signature(ex.run_simulation).parameters["examples_py"].default
    assert default == REPO_ROOT / "tools" / "examples.py"


# ------------------------------------------------------------------ simulate projection
def test_project_simulation_keeps_engine_order_strict_columns_terminal_and_typed_digest():
    # Engine order is not sorted by any metric; the projection must keep it.
    rows = [_sim_row(1, 300_000.5), _sim_row(2, 100_000.25), _sim_row(3, 212_345.678901234)]
    full = _sim_full(rows)
    raw = _bytes(full)
    report = ex.project_simulation(full, full_bytes=raw)
    assert set(report) == {"projection", "mode", "engine_entry", "run", "years",
                           "series", "terminal"}
    assert report["mode"] == "simulate"
    assert report["years"] == 3
    assert report["series"] == [{c: r[c] for c in ex.SIMULATE_SERIES_COLUMNS} for r in rows]
    assert [r["total_assets"] for r in report["series"]] == [300_000.5, 100_000.25,
                                                             212_345.678901234]
    last = rows[-1]
    assert report["terminal"] == {k: v for k, v in last.items()
                                  if k not in ("nested_not_scalar", "list_not_scalar")}
    assert report["terminal"]["total_assets"] == 212_345.678901234   # unrounded, last row
    assert report["terminal"]["extra_scalar_not_in_series"] == 30.375
    assert report["engine_entry"] == full["engine_entry"]
    assert report["run"] == full["run"]
    assert report["projection"] == {
        "version": 1,
        "function": "tools/examples.py::project_simulation",
        "source": "FamilySimulation.run()",
        "full_report_bytes": len(raw),
        "full_report_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "series_columns": list(ex.SIMULATE_SERIES_COLUMNS),
    }
    assert ex.bare_hex_strings(report) == []
    assert "net_benefit" not in ex.SIMULATE_SERIES_COLUMNS
    assert set(ex.KEY_YEAR_COLUMNS) - {"net_benefit"} <= set(ex.SIMULATE_SERIES_COLUMNS)
    # The optimize projection's cross-version keys are shared, so the nightly
    # near-compare serves both modes.
    assert set(ex._CROSS_VERSION_UNPINNED) <= set(report["projection"])


def test_project_simulation_refuses_unknown_top_level_key():
    full = _sim_full()
    full["new_section"] = {}
    with pytest.raises(ex.ExamplesError,
                       match=r"unknown \['new_section'\].*SIMULATE_PROJECTION_VERSION"):
        ex.project_simulation(full, full_bytes=_bytes(full))


@pytest.mark.parametrize("key", ["engine_entry", "run", "year_by_year"])
def test_project_simulation_refuses_missing_top_level_key(key):
    full = _sim_full()
    del full[key]
    with pytest.raises(ex.ExamplesError, match=rf"missing \['{key}'\]"):
        ex.project_simulation(full, full_bytes=_bytes(full))


def test_project_simulation_refuses_empty_series():
    full = _sim_full(rows=[])
    with pytest.raises(ex.ExamplesError, match="empty 'year_by_year'"):
        ex.project_simulation(full, full_bytes=_bytes(full))


def test_project_simulation_refuses_missing_series_column():
    full = _sim_full()
    del full["year_by_year"][2]["lif_balance"]
    with pytest.raises(ex.ExamplesError, match=r"row 2 has no column 'lif_balance'"):
        ex.project_simulation(full, full_bytes=_bytes(full))


def test_project_simulation_refuses_non_object():
    with pytest.raises(ex.ExamplesError, match="not a JSON object"):
        ex.project_simulation([], full_bytes=b"[]")


def test_render_simulation_markdown_is_deterministic_and_pathless():
    full = _sim_full()
    report = ex.project_simulation(full, full_bytes=_bytes(full))
    md = ex.render_simulation_markdown(report)
    assert md == ex.render_simulation_markdown(copy.deepcopy(report))
    assert md.endswith("\n") and not md.endswith("\n\n")
    assert ex.scan_personal_data("report.md", md) == []
    assert str(REPO_ROOT) not in md
    sections = [h for h, _ in ex.parse_readme_sections(md)]
    assert sections == ["Run", "Terminal year", "Year by year"]
    assert "| total_assets | 212,345.68 |" in md          # 2-decimal render
    assert "| ruined | false |" in md
    assert "| strategy | strategy_a |" in md
    assert "Year 3 of 3" in md
    header = "| " + " | ".join(ex.SIMULATE_SERIES_COLUMNS) + " |"
    assert header in md
    assert md.count("\n| 1 | ") == 1 and md.count("\n| 3 | ") == 1
    report["run"]["strategy"] = "a|b"
    assert "| strategy | a\\|b |" in ex.render_simulation_markdown(report)
    # It passes through the markdown cap unchanged.
    assert ex.project_markdown(md) == md


def test_committed_simulate_report_is_within_caps_and_typed():
    raw = (SIM / "report.json").read_bytes()
    assert len(raw) <= ex.REPORT_JSON_CAP
    assert len((SIM / "report.md").read_bytes()) <= ex.REPORT_MD_CAP
    report = json.loads(raw)
    assert report["mode"] == "simulate"
    assert report["projection"]["version"] == ex.SIMULATE_PROJECTION_VERSION
    assert report["projection"]["full_report_digest"].startswith("sha256:")
    assert ex.bare_hex_strings(report) == []
    # report.md is exactly the render of the committed report.json.
    assert (SIM / "report.md").read_text() == ex.render_simulation_markdown(report)


# ------------------------------------------------------------------ simulate input contract
def _single_point_doc() -> dict:
    return json.loads((SIM / "input.json").read_text())


def _sweep(doc: dict, shape: str) -> None:
    seed = json.loads((SEED / "input.json").read_text())
    decisions = doc["decisions"]
    if shape == "two_candidate_ages":
        decisions["retirement_age"][0]["candidate_ages"] = [60, 65]
    elif shape == "zero_candidate_ages":
        decisions["retirement_age"][1]["candidate_ages"] = []
    elif shape in ("contribution_strategy", "income", "resp_action", "estate_elections"):
        decisions[shape] = seed["decisions"][shape][:1]
    elif shape == "deposit_products":
        decisions[shape] = [{"id": "hisa"}]
    elif shape == "borrow_to_invest":
        decisions[shape] = [{"id": "draw_50k"}]
    elif shape == "mortgage.unknown":
        decisions["mortgage"]["zz_options"] = []
    elif shape.startswith("mortgage."):
        sub = shape.split(".", 1)[1]
        decisions["mortgage"][sub] = seed["decisions"]["mortgage"][sub][:1]
    elif shape == "objective":
        decisions["objective"] = "max_net_benefit"
    elif shape == "superficial_loss":
        decisions["superficial_loss"] = {"substitute_pairs": []}
    elif shape == "sensitivity.sweeps":
        doc["sensitivity"]["sweeps"] = {"investment_return": [0.05]}
    elif shape == "sensitivity.presets":
        doc["sensitivity"]["presets"] = seed["sensitivity"]["presets"]
    elif shape == "sensitivity.unknown":
        doc["sensitivity"]["zz"] = {}
    elif shape == "unknown_decisions_key":
        decisions["zz_new_decision"] = []
    elif shape == "funding_options":
        doc["properties"][0]["funding_options"] = [{"id": "a"}, {"id": "b"}]
    else:
        raise AssertionError(shape)


_SWEEP_SHAPES = {
    "two_candidate_ages": "decisions.retirement_age[0].candidate_ages must hold exactly one",
    "zero_candidate_ages": "decisions.retirement_age[1].candidate_ages must hold exactly one",
    "contribution_strategy": "decisions.contribution_strategy must be []",
    "income": "decisions.income must be []",
    "resp_action": "decisions.resp_action must be []",
    "estate_elections": "decisions.estate_elections must be []",
    "deposit_products": "decisions.deposit_products must be []",
    "borrow_to_invest": "decisions.borrow_to_invest must be []",
    "mortgage.refinance_options": "decisions.mortgage.refinance_options must be []",
    "mortgage.renewal_options": "decisions.mortgage.renewal_options must be []",
    "mortgage.structure_options": "decisions.mortgage.structure_options must be []",
    "mortgage.unknown": "decisions.mortgage.zz_options is not classified",
    "objective": "decisions.objective is declared, but a single run does not consume it",
    "superficial_loss": "decisions.superficial_loss is declared",
    "sensitivity.sweeps": "sensitivity.sweeps must be {}",
    "sensitivity.presets": "sensitivity.presets must be {}",
    "sensitivity.unknown": "sensitivity.zz is not classified",
    "unknown_decisions_key": "decisions.zz_new_decision is not classified",
    "funding_options": "/properties/0/funding_options is an optimizer-ranked alternative",
}


def test_simulate_input_problems_accepts_the_single_point_example():
    assert ex.simulate_input_problems(_single_point_doc()) == []


@pytest.mark.parametrize("shape", sorted(_SWEEP_SHAPES))
def test_simulate_input_problems_refuses_each_sweep_shape(shape):
    doc = _single_point_doc()
    _sweep(doc, shape)
    problems = ex.simulate_input_problems(doc)
    assert len(problems) == 1, problems
    assert _SWEEP_SHAPES[shape] in problems[0]
    assert "mode optimize" in problems[0] or "not classified" in problems[0]


def test_simulate_input_problems_refuses_malformed_documents():
    assert ex.simulate_input_problems([]) == ["input.json is not a JSON object"]
    problems = "\n".join(ex.simulate_input_problems({}))
    assert "decisions must be an object" in problems
    assert "sensitivity must be an object" in problems
    doc = _single_point_doc()
    doc["decisions"]["retirement_age"] = {"p1": 60}
    doc["decisions"]["mortgage"] = []
    problems = "\n".join(ex.simulate_input_problems(doc))
    assert "decisions.retirement_age must be a list" in problems
    assert "decisions.mortgage must be an object" in problems


def test_seed_sweep_is_refused_in_simulate_mode(tmp_path):
    """The seed stays in optimize mode because a single run would collapse
    its declared sweep; flipping it to simulate must fail loudly."""
    problems = "\n".join(ex.simulate_input_problems(json.loads((SEED / "input.json").read_text())))
    for needle in ("retirement_age[0].candidate_ages", "contribution_strategy",
                   "income", "mortgage.refinance_options", "sensitivity.sweeps"):
        assert needle in problems, needle
    _git_init(tmp_path)
    example = _write_example(tmp_path / "examples")
    assert ex.static_problems(example) == []
    meta = json.loads((example / "meta.json").read_text())
    meta["mode"] = "simulate"
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    static = "\n".join(ex.static_problems(example))
    assert "src/case-one/input.json: decisions.retirement_age[0].candidate_ages" in static
    stub, marker = _marker_stub(tmp_path, "examples_stub")
    with pytest.raises(ex.ExamplesError, match="cannot be run in simulate mode"):
        ex.regenerate(example, workers=1, examples_py=stub)
    assert not marker.exists()


def _schema_defs() -> dict:
    return json.loads((REPO_ROOT / "schema" / "defs" / "decisions.json").read_text())["$defs"]


def test_simulate_decisions_classification_covers_schema():
    defs = _schema_defs()
    assert set(ex.SIMULATE_DECISIONS) == set(defs["decisions"]["properties"])
    assert set(ex.MORTGAGE_OPTION_KEYS) == set(defs["mortgage_decisions"]["properties"])
    assert set(ex.SIMULATE_SENSITIVITY_KEYS) == set(defs["sensitivity"]["properties"])
    assert set(ex.SIMULATE_DECISIONS.values()) == {
        "consumed", "single_candidate", "empty_list", "empty_options", "absent"}


def test_every_schema_sweep_key_is_classified_for_simulate():
    """A new ``*_options`` / candidate list anywhere in the schema must be
    classified for simulate mode, not silently collapsed by a single run."""
    import re
    names: set = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                names.update(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for path in (REPO_ROOT / "schema").rglob("*.json"):
        walk(json.loads(path.read_text()))
    found = {n for n in names if re.fullmatch(r"[a-z_]*(_options|candidate[a-z_]*)", n)}
    classified = (set(ex.MORTGAGE_OPTION_KEYS) | set(ex.SIMULATE_REFUSED_ANYWHERE)
                  | {"candidate_ages"})
    assert found and found <= classified, sorted(found - classified)


# ------------------------------------------------------------------ simulate child (real engine)
def _sim_input_copy(tmp_path: Path, mutate=None) -> Path:
    doc = _single_point_doc()
    if mutate is not None:
        mutate(doc)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def _simulate(path: Path) -> dict:
    return json.loads(ex.simulate_once(path))


def test_simulate_consumed_decisions_reach_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "a").mkdir()
    base = _simulate(_sim_input_copy(tmp_path / "a"))
    assert base["run"]["retirement_ages"][0] == {"id": "p1", "role": "primary",
                                                 "retirement_age": 60}

    def later_retirement(doc):
        doc["decisions"]["retirement_age"][0]["candidate_ages"] = [65]
    (tmp_path / "b").mkdir()
    moved = _simulate(_sim_input_copy(tmp_path / "b", later_retirement))
    assert moved["run"]["retirement_ages"][0]["retirement_age"] == 65
    assert moved["year_by_year"][-1]["total_assets"] != base["year_by_year"][-1]["total_assets"]

    def shorter_horizon(doc):
        doc["decisions"]["horizon"]["until_age"] -= 5
    (tmp_path / "c").mkdir()
    shorter = _simulate(_sim_input_copy(tmp_path / "c", shorter_horizon))
    assert len(shorter["year_by_year"]) == len(base["year_by_year"]) - 5
    assert shorter["run"]["projection_years"] == base["run"]["projection_years"] - 5


@pytest.mark.parametrize("shape", [
    "contribution_strategy", "income", "resp_action", "estate_elections",
    "mortgage.refinance_options", "mortgage.renewal_options", "mortgage.structure_options",
])
def test_simulate_empty_list_decisions_are_not_read_by_single_run(tmp_path, monkeypatch, shape):
    """Keeps SIMULATE_DECISIONS' "the single run never reads it" measured: with
    the refusal lifted, declaring one candidate leaves the real single run's
    output byte-identical. If the engine ever starts reading one, this fails and
    the classification (and its refusal message) must be revisited."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    base = ex.simulate_once(SIM / "input.json")
    monkeypatch.setattr(ex, "simulate_input_problems", lambda doc: [])
    doc = _single_point_doc()
    _sweep(doc, shape)
    if shape == "income":      # the first scenario has no override; take one that has
        doc["decisions"]["income"] = json.loads(
            (SEED / "input.json").read_text())["decisions"]["income"][1:2]
        assert doc["decisions"]["income"][0]["overrides"]
    path = tmp_path / "input.json"
    path.write_text(json.dumps(doc))
    assert ex.simulate_once(path) == base


def test_simulate_once_refuses_a_sweep_before_the_engine(tmp_path):
    path = _sim_input_copy(tmp_path, lambda d: _sweep(d, "two_candidate_ages"))
    with pytest.raises(ex.ExamplesError, match="cannot be run in simulate mode"):
        ex.simulate_once(path)


def test_cli_simulate_once_writes_full_document(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / "full.json"
    assert ex.main(["simulate-once", "--input", str(SIM / "input.json"), "--out", str(out)],
                   python_version=ex.CANONICAL_PYTHON) == 0
    raw = out.read_bytes()
    doc = json.loads(raw)
    assert set(doc) == {"engine_entry", "run", "year_by_year"}
    assert doc["engine_entry"] == ex.SIMULATE_ENGINE_ENTRY
    assert set(doc["run"]) == {"strategy", "rate_path", "use_readvanceable", "deduct_later",
                               "start_year", "projection_years", "retirement_ages"}
    assert len(doc["year_by_year"]) == doc["run"]["projection_years"] > 0
    # Canonical bytes: compact, sorted, and the committed digest pins them.
    assert raw == json.dumps(doc, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
    committed = json.loads((SIM / "report.json").read_text())["projection"]
    assert committed["full_report_digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert committed["full_report_bytes"] == len(raw)


def test_cli_simulate_once_runs_under_non_canonical_python(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / "full.json"
    assert ex.main(["simulate-once", "--input", str(SIM / "input.json"), "--out", str(out)],
                   python_version=(3, 11)) == 0
    assert out.stat().st_size > 0


def test_cli_simulate_once_reports_refusal(tmp_path, capsys):
    path = _sim_input_copy(tmp_path, lambda d: _sweep(d, "income"))
    out = tmp_path / "full.json"
    assert ex.main(["simulate-once", "--input", str(path), "--out", str(out)]) == 1
    assert "decisions.income must be []" in capsys.readouterr().err
    assert not out.exists()


def test_simulate_example_readme_states_the_engine_chosen_run_block():
    """The README's Encoding names what the engine chose on its own, and it
    must match report.json `run`, so the prose cannot misstate what ran."""
    run = json.loads((SIM / "report.json").read_text())["run"]
    sections = dict(ex.parse_readme_sections((SIM / "README.md").read_text()))
    encoding = sections["Encoding"]
    assert f"strategy `{run['strategy']}`" in encoding
    assert f"rate path `{run['rate_path']}`" in encoding
    assert f"`use_readvanceable` {json.dumps(run['use_readvanceable'])}" in encoding
    assert f"`deduct_later` {json.dumps(run['deduct_later'])}" in encoding


# ------------------------------------------------------------------ simulate sabotage suite (REAL engine)
# Each test copies the committed simulate example into a git-initialised tmp
# tree and uses the real ex.regenerate (no stub, no monkeypatch): the
# module-scoped fixture regenerates the unsabotaged copy once (~1 s).
def _sim_tree(base: Path) -> Path:
    _git_init(base)
    return _write_example(base / "examples", source="lifedraft",
                          slug="single-run-two-adult", source_example=SIM)


@pytest.fixture(scope="module")
def sim_regenerated(tmp_path_factory):
    example = _sim_tree(tmp_path_factory.mktemp("sim-baseline"))
    return ex.regenerate(example, workers=1)


def test_simulate_example_unsabotaged_copy_matches(tmp_path, sim_regenerated):
    example = _sim_tree(tmp_path)
    assert ex.static_problems(example) == []
    assert ex.compare_reports(example, *sim_regenerated) == []
    assert sim_regenerated == ((SIM / "report.json").read_text(),
                               (SIM / "report.md").read_text())


def test_simulate_sabotage_hand_edited_report_json_number_fails(tmp_path, sim_regenerated):
    import re
    example = _sim_tree(tmp_path)
    text = (example / "report.json").read_text()
    at = text.index('"total_assets"', text.index('"terminal"'))
    digit = re.search(r"\d", text[at + 15:]).start() + at + 15
    edited = text[:digit] + str((int(text[digit]) + 1) % 10) + text[digit + 1:]
    (example / "report.json").write_text(edited)
    problems = ex.compare_reports(example, *sim_regenerated)
    assert len(problems) == 1
    assert "lifedraft/single-run-two-adult/report.json differs" in problems[0]
    assert "Never hand-edit a report" in problems[0]
    assert "tools/examples.py regen examples/lifedraft/single-run-two-adult" in problems[0]


def test_simulate_sabotage_hand_edited_report_md_number_fails(tmp_path, sim_regenerated):
    example = _sim_tree(tmp_path)
    text = (example / "report.md").read_text()
    at = text.index("| total_assets | ") + len("| total_assets | ")
    edited = text[:at] + str((int(text[at]) + 1) % 10) + text[at + 1:]
    (example / "report.md").write_text(edited)
    problems = ex.compare_reports(example, *sim_regenerated)
    assert len(problems) == 1 and "report.md differs" in problems[0]
    assert "tools/examples.py regen examples/lifedraft/single-run-two-adult" in problems[0]


def test_simulate_sabotage_deleted_readme_section_fails(tmp_path):
    example = _sim_tree(tmp_path)
    readme = (example / "README.md").read_text()
    (example / "README.md").write_text(readme.replace("## Verdict\n", "", 1))
    problems = "\n".join(ex.static_problems(example))
    assert "'## Verdict' must appear exactly once (found 0)" in problems


def test_simulate_sabotage_meta_only_verdict_change_fails(tmp_path):
    example = _sim_tree(tmp_path)
    meta = json.loads((example / "meta.json").read_text())
    meta["verdict"] = "DIFFERS (explained)"
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    problems = "\n".join(ex.static_problems(example))
    assert ("README verdict 'AGREES' does not equal meta.json verdict "
            "'DIFFERS (explained)'") in problems


def test_simulate_sabotage_zero_match_tree_raises(tmp_path):
    with pytest.raises(ex.ExamplesError, match="does not exist"):
        ex.discover_examples(tmp_path / "examples")
    example = _sim_tree(tmp_path)
    root = tmp_path / "examples"
    moved = tmp_path / "examples.off"
    root.rename(moved)
    with pytest.raises(ex.ExamplesError, match="does not exist"):
        ex.discover_examples(root)
    root.mkdir()
    (root / "README.md").write_text("guide\n")
    with pytest.raises(ex.ExamplesError, match="zero examples"):
        ex.discover_examples(root)
    # An example that lost its mode is still COLLECTED, then fails.
    (root / "README.md").unlink()
    root.rmdir()
    moved.rename(root)
    meta = json.loads((example / "meta.json").read_text())
    del meta["mode"]
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    assert ex.discover_examples(root) == [example]
    assert "'mode' is required" in "\n".join(ex.static_problems(example))
    with pytest.raises(ex.ExamplesError, match="has no 'mode'"):
        ex.regenerate(example, workers=1)


def test_simulate_sabotage_missing_mode_fails(tmp_path):
    example = _sim_tree(tmp_path)
    meta = json.loads((example / "meta.json").read_text())
    del meta["mode"]
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    assert "'mode' is required" in "\n".join(ex.static_problems(example))
    with pytest.raises(ex.ExamplesError, match="has no 'mode'"):
        ex.regenerate(example, workers=1)


def test_simulate_sabotage_mode_flip_without_regen_fails(tmp_path):
    """Flip to optimize without regenerating: the static contract still
    passes (optimize accepts a single point), but the real regeneration now
    runs optimize.py (~7 s) and its report differs from the committed one."""
    example = _sim_tree(tmp_path)
    meta = json.loads((example / "meta.json").read_text())
    meta["mode"] = "optimize"
    (example / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    assert ex.static_problems(example) == []
    problems = ex.compare_reports(example, *ex.regenerate(example, workers=1))
    assert len(problems) == 2
    assert "report.json differs" in problems[0] and "report.md differs" in problems[1]


def test_simulate_sabotage_multi_candidate_retirement_age_refused(tmp_path):
    example = _sim_tree(tmp_path)
    doc = json.loads((example / "input.json").read_text())
    doc["decisions"]["retirement_age"][0]["candidate_ages"] = [60, 65]
    (example / "input.json").write_text(json.dumps(doc, indent=2) + "\n")
    static = "\n".join(ex.static_problems(example))
    assert ("lifedraft/single-run-two-adult/input.json: decisions.retirement_age[0]"
            ".candidate_ages must hold exactly one age") in static
    with pytest.raises(ex.ExamplesError, match="cannot be run in simulate mode"):
        ex.regenerate(example, workers=1)


def test_simulate_sabotage_input_change_without_regen_fails(tmp_path, sim_regenerated):
    """The byte guard sees what the engine computes, not only committed text."""
    example = _sim_tree(tmp_path)
    doc = json.loads((example / "input.json").read_text())
    doc["accounts"][0]["balance"]["amount"] += 1000
    (example / "input.json").write_text(json.dumps(doc, indent=2) + "\n")
    problems = ex.compare_reports(example, *ex.regenerate(example, workers=1))
    assert len(problems) == 2
