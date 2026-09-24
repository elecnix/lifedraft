"""Unit tests for tools/examples.py (issue #300).

Fast by construction: nothing here runs the real optimize.py. The run step is
exercised with stub scripts written to tmp_path, and the projection with
fabricated full reports (round numbers, role-based labels; DP#4, DP#15). The
real engine is driven by tests/test_examples_guard.py.

Each of the issue's required sabotages has a unit-level twin here, so the
detector is seen to fire without waiting for a regeneration:
hand-edited report number, deleted README section, verdict changed only in
meta.json, and a zero-match examples tree.
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


def _write_example(root: Path, source: str = "src", slug: str = "case-one") -> Path:
    """A statically-valid example directory copied from the committed seed."""
    example = root / source / slug
    example.mkdir(parents=True)
    for name in ex.REQUIRED_FILES:
        (example / name).write_bytes((SEED / name).read_bytes())
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
        "full_report_sha256": hashlib.sha256(raw).hexdigest(),
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


def test_regenerate_refuses_example_without_input(tmp_path):
    example = tmp_path / "src" / "case-one"
    example.mkdir(parents=True)
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
    json_text, md_text = ex.regenerate(example, workers=None, optimize_py=stub)
    raw = (tmp_path / "full.json").read_bytes()
    assert json_text == ex.dump_report_json(ex.project_report(full, full_bytes=raw))
    assert md_text == "# md\n"
    assert sorted(p.name for p in example.iterdir()) == ["input.json"]


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
    assert ex.main(["regen", "--workers", "1"], root=root, optimize_py=stub,
                   python_version=ex.CANONICAL_PYTHON) == 0
    assert "regenerated src/case-one" in capsys.readouterr().out
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
