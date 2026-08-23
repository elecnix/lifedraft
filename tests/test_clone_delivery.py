"""Tests for tools/clone_delivery.py — the clone-detection delivery layer (#1093).

The defect being guarded: findings that reach no human are findings never
made. These tests pin the three delivery guarantees —

  * annotations are capped at GitHub's 10-per-step render ceiling, and
    truncation is announced ("…and K more"), never silent;
  * the job summary states the total count and the cap;
  * the PR conversation carries (at most) one marker comment whose body
    matches the current delta, and a clean delta deletes it.

The annotation wire format exercised here is dupdelta's own escaping spec
(`%` first, then CR/LF for messages; plus `:`/`,` for property values), so a
regression in parsing shows up as a corrupted finding, not as green silence.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    """tools/ is not a package (no __init__.py), so load the module by path."""
    spec = importlib.util.spec_from_file_location(
        "clone_delivery", REPO_ROOT / "tools" / "clone_delivery.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clone_delivery"] = mod
    spec.loader.exec_module(mod)
    return mod


tool = _load_tool()


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------
def _escape_prop(value):
    """dupdelta's property-value escaping: % first, then the two delimiters."""
    return value.replace("%", "%25").replace(":", "%3A").replace(",", "%2C")


def _annotation_line(file="a.py", line=3, message="Possible clone (sim 0.9)", title=None):
    """Build one dupdelta-style workflow command, applying dupdelta's escapes.

    Only the VALUES are escaped; the `,` between properties and the `=`/`::`
    protocol punctuation stay literal, exactly as the runner's parser needs.
    """
    parts = [f"file={_escape_prop(file)}"]
    if line is not None:
        parts.append(f"line={line}")
    if title is not None:
        parts.append(f"title={_escape_prop(title)}")
    msg = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning {','.join(parts)}::{msg}"


class FakeClient:
    """Records CommentClient calls instead of touching the network."""

    def __init__(self, existing_comment_id=None):
        self.existing_comment_id = existing_comment_id
        self.upserts = []
        self.deletes = []

    def find_marker_comment(self, pr):
        return self.existing_comment_id

    def upsert(self, pr, body):
        self.upserts.append((pr, body))

    def delete(self, comment_id):
        self.deletes.append(comment_id)


# --------------------------------------------------------------------------
# parse_annotations
# --------------------------------------------------------------------------
def test_parses_a_plain_annotation_with_file_and_line():
    records = tool.parse_annotations(_annotation_line() + "\n")
    assert len(records) == 1
    assert records[0]["level"] == "warning"
    assert records[0]["file"] == "a.py"
    assert records[0]["line"] == "3"
    assert records[0]["message"] == "Possible clone (sim 0.9)"
    assert records[0]["raw"].startswith("::warning file=a.py")


def test_parses_annotation_without_properties():
    records = tool.parse_annotations("::warning::bare message\n")
    assert records == [
        {"level": "warning", "raw": "::warning::bare message", "message": "bare message"}
    ]


def test_ignores_non_annotation_output_lines():
    text = (
        "No new duplication relative to merge-base\n" + _annotation_line() + "\nordinary log line\n"
    )
    assert len(tool.parse_annotations(text)) == 1


def test_message_escapes_are_decoded_percent_last():
    # A literal % in the original message became %25; decoding must not turn
    # it into part of another escape sequence.
    raw = _annotation_line(message="100% done\nsecond line")
    record = tool.parse_annotations(raw)[0]
    assert record["message"] == "100% done\nsecond line"


def test_property_escapes_for_colon_and_comma_are_decoded():
    raw = "::warning file=C%3A\\repo%2Cdir\\a.py,line=1::m"
    record = tool.parse_annotations(raw)[0]
    assert record["file"] == "C:\\repo,dir\\a.py"


def test_title_property_survives_the_round_trip():
    record = tool.parse_annotations(_annotation_line(title="Clone detection"))[0]
    assert record["title"] == "Clone detection"


# --------------------------------------------------------------------------
# cap_annotations — the anti-silent-truncation contract
# --------------------------------------------------------------------------
def test_at_or_under_cap_everything_renders_and_nothing_is_hidden():
    annotations = [{"raw": f"raw-{i}"} for i in range(tool.ANNOTATION_CAP)]
    kept, hidden = tool.cap_annotations(annotations, total=tool.ANNOTATION_CAP)
    assert len(kept) == tool.ANNOTATION_CAP
    assert hidden == 0


def test_over_cap_one_slot_is_reserved_for_the_pointer():
    annotations = [{"raw": f"raw-{i}"} for i in range(13)]
    kept, hidden = tool.cap_annotations(annotations, total=13)
    assert len(kept) == tool.ANNOTATION_CAP - 1
    assert hidden == 13 - (tool.ANNOTATION_CAP - 1)
    assert [r["raw"] for r in kept] == [f"raw-{i}" for i in range(tool.ANNOTATION_CAP - 1)]


def test_total_is_authoritative_when_parsing_yields_fewer_records():
    # If dupdelta ever emits more findings than we can parse, the shortfall
    # must surface as HIDDEN, not vanish from the arithmetic.
    kept, hidden = tool.cap_annotations([{"raw": "only-one"}], total=12)
    assert len(kept) == 1
    assert hidden == 11


def test_pointer_annotation_names_the_count_and_the_summary():
    pointer = tool.pointer_annotation(4)
    assert pointer.startswith("::warning ")
    assert "4 more" in pointer
    assert "job summary" in pointer
    assert str(tool.ANNOTATION_CAP) in pointer


# --------------------------------------------------------------------------
# summary note
# --------------------------------------------------------------------------
def test_summary_note_states_total_and_cap_when_untruncated():
    note = tool.render_summary_note(total=3, hidden=0)
    assert "3 new duplication finding(s)" in note
    assert str(tool.ANNOTATION_CAP) in note
    assert "complete list" in note


def test_summary_note_says_hidden_findings_appear_only_there():
    note = tool.render_summary_note(total=13, hidden=4)
    assert "13 new duplication finding(s)" in note
    assert "ONLY here" in note
    assert "4" in note


# --------------------------------------------------------------------------
# comment body
# --------------------------------------------------------------------------
def test_comment_body_leads_with_marker_and_count():
    body = tool.render_comment_body(2, [], 0, None)
    assert body.startswith(tool.COMMENT_MARKER + "\n")
    assert "2 new duplication finding(s)" in body
    assert "advisory" in body.lower()


def test_comment_body_lists_findings_as_file_line_bullets():
    annotations = [
        {"file": "engine.py", "line": "42", "message": "Possible clone (sim 0.9) of other.py:7"},
        {"file": "other.py", "message": "Module vocabulary overlap 0.55 with engine.py"},
    ]
    body = tool.render_comment_body(2, annotations, 0, None)
    assert "- `engine.py:42` — Possible clone (sim 0.9) of other.py:7" in body
    assert "- `other.py` — Module vocabulary overlap 0.55 with engine.py" in body


def test_comment_body_multiline_message_collapses_to_first_line():
    annotations = [{"file": "a.py", "line": "1", "message": "first\nsecond"}]
    body = tool.render_comment_body(1, annotations, 0, None)
    assert "first\nsecond" not in body.split(tool.COMMENT_MARKER)[1]
    assert "`a.py:1` — first" in body


def test_comment_body_truncated_list_carries_explicit_k_more_pointer():
    # THE regression guard for this issue: >N findings must produce an
    # explicit "K more" pointer, never a silently truncated list.
    annotations = [{"file": f"f{i}.py", "line": "1", "message": "clone"} for i in range(9)]
    body = tool.render_comment_body(13, annotations, 4, None)
    assert "…and 4 more — see the job summary for the complete list." in body


def test_comment_body_no_pointer_when_nothing_hidden():
    body = tool.render_comment_body(3, [{"file": "a.py", "line": "1", "message": "m"}], 0, None)
    assert "more" not in body


def test_comment_body_links_the_run_url_when_given():
    body = tool.render_comment_body(1, [], 0, "https://example.test/actions/runs/99")
    assert "https://example.test/actions/runs/99" in body


# --------------------------------------------------------------------------
# main() end-to-end against fakes
# --------------------------------------------------------------------------
@pytest.fixture()
def scan_files(tmp_path):
    annotations = tmp_path / "dupdelta-stdout.txt"
    findings = tmp_path / "dupdelta-findings"
    summary = tmp_path / "summary.md"
    summary.write_text("## Duplication report\n")
    return annotations, findings, summary


def test_main_emits_capped_annotations_and_upserts_comment(scan_files, capsys, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    annotations, findings, summary = scan_files
    lines = [_annotation_line(file=f"f{i}.py") for i in range(13)]
    annotations.write_text("\n".join(lines) + "\n")
    findings.write_text("13\n")

    client = FakeClient()
    # Drive main with an injected client by patching the constructor.
    orig = tool.CommentClient
    tool.CommentClient = lambda repo, token: client
    try:
        rc = tool.main(
            [
                "--annotations-file",
                str(annotations),
                "--findings-file",
                str(findings),
                "--summary",
                str(summary),
                "--repo",
                "owner/repo",
                "--pr",
                "7",
                "--run-url",
                "https://ci/runs/1",
            ]
        )
    finally:
        tool.CommentClient = orig

    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    # cap-1 real annotations + the pointer, nothing uncapped leaks to stdout
    assert len(out) == tool.ANNOTATION_CAP
    assert out[: tool.ANNOTATION_CAP - 1] == lines[: tool.ANNOTATION_CAP - 1]
    assert "4 more" in out[-1]

    note = summary.read_text()
    assert "13 new duplication finding(s)" in note
    assert "ONLY here" in note

    assert len(client.upserts) == 1
    pr, body = client.upserts[0]
    assert pr == 7
    assert body.startswith(tool.COMMENT_MARKER)
    assert "…and 4 more" in body
    assert "https://ci/runs/1" in body


def test_main_zero_findings_deletes_stale_comment_and_touches_nothing_else(
    scan_files, capsys, monkeypatch
):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    annotations, findings, summary = scan_files
    annotations.write_text("")
    findings.write_text("0\n")
    before = summary.read_text()

    client = FakeClient(existing_comment_id=4242)
    orig = tool.CommentClient
    tool.CommentClient = lambda repo, token: client
    try:
        rc = tool.main(
            [
                "--annotations-file",
                str(annotations),
                "--findings-file",
                str(findings),
                "--summary",
                str(summary),
                "--repo",
                "owner/repo",
                "--pr",
                "7",
            ]
        )
    finally:
        tool.CommentClient = orig

    assert rc == 0
    assert client.deletes == [4242]
    assert client.upserts == []
    assert summary.read_text() == before
    assert capsys.readouterr().out.strip() != ""  # says what it removed


def test_main_zero_findings_without_prior_comment_is_a_noop(scan_files, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    annotations, findings, summary = scan_files
    annotations.write_text("")
    findings.write_text("0\n")
    client = FakeClient(existing_comment_id=None)
    orig = tool.CommentClient
    tool.CommentClient = lambda repo, token: client
    try:
        rc = tool.main(
            [
                "--annotations-file",
                str(annotations),
                "--findings-file",
                str(findings),
                "--repo",
                "owner/repo",
                "--pr",
                "7",
            ]
        )
    finally:
        tool.CommentClient = orig
    assert rc == 0
    assert client.deletes == [] and client.upserts == []


def test_main_finds_positive_without_repo_posts_nothing_but_annotates(scan_files, capsys):
    annotations, findings, summary = scan_files
    annotations.write_text(_annotation_line() + "\n")
    findings.write_text("1\n")
    rc = tool.main(
        [
            "--annotations-file",
            str(annotations),
            "--findings-file",
            str(findings),
            "--summary",
            str(summary),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == [_annotation_line()]
    assert "1 new duplication finding(s)" in summary.read_text()


def test_main_refuses_to_post_without_a_token(scan_files):
    # A finding that cannot reach a human is the defect this tool closes;
    # missing credentials must be loud, not a silent skip.
    annotations, findings, _ = scan_files
    annotations.write_text(_annotation_line() + "\n")
    findings.write_text("2\n")
    monkey = pytest.MonkeyPatch()
    monkey.delenv("GH_TOKEN", raising=False)
    monkey.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="GH_TOKEN"):
        tool.main(
            [
                "--annotations-file",
                str(annotations),
                "--findings-file",
                str(findings),
                "--repo",
                "owner/repo",
                "--pr",
                "7",
            ]
        )
    monkey.undo()


# --------------------------------------------------------------------------
# CommentClient over the real HTTP path (urllib patched, no network)
# --------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_comment_client_upsert_patches_existing_marker_comment(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url, req))
        if req.get_method() == "GET":
            body = json.dumps(
                [{"id": 55, "body": tool.COMMENT_MARKER + "\nold"}, {"id": 56, "body": "unrelated"}]
            ).encode()
            return _FakeResponse(200, body)
        return _FakeResponse(200, b"{}")

    monkeypatch.setattr(tool._urlrequest, "urlopen", fake_urlopen)
    client = tool.CommentClient("owner/repo", "t0k3n")
    client.upsert(7, "new body")

    methods = [(m, u) for m, u, _ in calls]
    assert (
        "GET",
        "https://api.github.com/repos/owner/repo/issues/7/comments?per_page=100",
    ) in methods
    assert ("PATCH", "https://api.github.com/repos/owner/repo/issues/comments/55") in methods
    assert not any(m == "POST" for m, _, _ in calls)
    patch_req = next((r for m, _, r in calls if m == "PATCH"), None)
    assert patch_req is not None
    assert json.loads(patch_req.data) == {"body": "new body"}
    assert patch_req.get_header("Authorization") == "Bearer t0k3n"


def test_comment_client_upsert_posts_when_no_marker_exists(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url))
        if req.get_method() == "GET":
            return _FakeResponse(200, json.dumps([{"id": 56, "body": "unrelated"}]).encode())
        return _FakeResponse(201, b"{}")

    monkeypatch.setattr(tool._urlrequest, "urlopen", fake_urlopen)
    tool.CommentClient("owner/repo", "t").upsert(7, "body")
    assert ("POST", "https://api.github.com/repos/owner/repo/issues/7/comments") in calls


def test_comment_client_delete_expects_204(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResponse(204)

    monkeypatch.setattr(tool._urlrequest, "urlopen", fake_urlopen)
    tool.CommentClient("owner/repo", "t").delete(55)
    assert calls == ["https://api.github.com/repos/owner/repo/issues/comments/55"]


def test_comment_client_raises_loudly_on_api_failure(monkeypatch):
    import io
    from urllib.error import HTTPError as RealHTTPError

    def fake_urlopen(req, timeout=None):
        raise RealHTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b"nope"))

    monkeypatch.setattr(tool._urlrequest, "urlopen", fake_urlopen)
    client = tool.CommentClient("owner/repo", "t")
    with pytest.raises(SystemExit, match="403"):
        client.find_marker_comment(7)


def test_http_request_returns_error_status_body_via_httperror(monkeypatch):
    import io
    from urllib.error import HTTPError as RealHTTPError

    def fake_urlopen(req, timeout=None):
        raise RealHTTPError("http://x", 422, "Unprocessable", {}, io.BytesIO(b'{"message":"bad"}'))

    monkeypatch.setattr(tool._urlrequest, "urlopen", fake_urlopen)
    status, body = tool._http_request("POST", "http://x", "t", {"body": "b"})
    assert status == 422
    assert json.loads(body)["message"] == "bad"


def test_http_request_success_round_trip(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["method"] = req.get_method()
        seen["auth"] = req.get_header("Authorization")
        seen["content_type"] = req.get_header("Content-type")
        seen["data"] = req.data
        return _FakeResponse(201, b'{"id": 1}')

    monkeypatch.setattr(tool._urlrequest, "urlopen", fake_urlopen)
    status, body = tool._http_request("POST", "http://x", "tok", {"body": "hi"})
    assert (status, json.loads(body)) == (201, {"id": 1})
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer tok"
    assert seen["content_type"] == "application/json"
