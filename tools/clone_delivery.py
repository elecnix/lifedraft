#!/usr/bin/env python3
"""Deliver clone-detection findings where a reviewer actually looks (#1093).

The clone-detection job is advisory on purpose: it never fails the build. But
"advisory" must not mean "unread" — before this tool, the only signals were
check-run annotations (which render on the Files changed tab, behind a click)
and a job summary (behind another click). A PR with three genuine clone pairs
and a PR with none were indistinguishable in the conversation a reviewer
actually reads, and the retrospective this issue ordered found exactly the
predicted outcome: findings had merged unnoticed (#1027, #1013, #1032).

This tool closes the delivery gap for a job that runs dupdelta directly:

  1. It caps the workflow-command annotations at GitHub's 10-per-step render
     ceiling and, when findings are truncated, emits one final annotation that
     says so and points at the job summary. GitHub's own cap is silent — a
     reader who saw 10 annotations had no way to know they saw 10 of 13.
  2. It appends a note to the job summary stating the total finding count and
     that the annotation list is capped, so "I read the annotations" is never
     mistaken for "I saw all of the findings".
  3. It posts or updates a single PR comment (found by a hidden marker) that
     states the count in the conversation itself. When a later push on the
     same PR scans clean, the stale comment is deleted rather than left to
     report duplication that no longer exists.

The job stays warn-only: this tool never exits nonzero on findings, only on
its own inability to deliver them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib import request as _urlrequest
from urllib.error import HTTPError

# GitHub renders at most 10 annotations per step; anything beyond is dropped
# from the UI with no indication that anything was dropped. We emit at most
# this many, the last of them being the "K more" pointer when truncated.
ANNOTATION_CAP = 10

# Hidden marker that makes the report comment findable across pushes. A
# comment is updated in place, never duplicated, and deleted when the delta
# runs clean — so a PR carries at most one, and it never lies about the
# current head.
COMMENT_MARKER = "<!-- clone-detection-report -->"

# dupdelta renders annotations as workflow commands: a line-oriented text
# protocol `::level key=value,key=value::message`. Property values escape
# `:` and `,` (the property delimiters) as %3A / %2C; messages escape only
# `%`, CR, LF. A line that does not match is ordinary output, not a finding.
_ANNOTATION_LINE_RE = re.compile(r"^::(?P<level>[a-z]+)(?: (?P<props>[^:]*))?::(?P<msg>.*)$")


def _unescape(value: str) -> str:
    """Decode the percent-escapes dupdelta applies.

    Encoding order was `%` first, then CR/LF, then `:`/`,` — so decoding must
    put `%25` last, after every other escape has had its turn.
    """
    return (
        value.replace("%0D", "\r")
        .replace("%0A", "\n")
        .replace("%3A", ":")
        .replace("%2C", ",")
        .replace("%25", "%")
    )


def parse_annotations(text: str) -> list[dict]:
    """Extract annotation records (plus their raw lines) from dupdelta output.

    Each record carries the ORIGINAL raw line under ``"raw"``: re-emitting the
    bytes dupdelta wrote is the only way to guarantee we never corrupt a
    finding by re-escaping it.
    """
    records: list[dict] = []
    for line in text.splitlines():
        match = _ANNOTATION_LINE_RE.match(line)
        if not match:
            continue
        record: dict = {"level": match.group("level"), "raw": line}
        props = match.group("props")
        if props:
            for pair in props.split(","):
                key, _, value = pair.partition("=")
                record[key] = _unescape(value)
        record["message"] = _unescape(match.group("msg"))
        records.append(record)
    return records


def cap_annotations(
    annotations: list[dict], total: int, cap: int = ANNOTATION_CAP
) -> tuple[list[dict], int]:
    """Split findings into the ones that render inline and the hidden rest.

    ``total`` (dupdelta's own count) is authoritative: if annotation parsing
    ever yields fewer records than dupdelta counted, the shortfall is treated
    as hidden — never silently dropped from the arithmetic. At most ``cap - 1``
    real annotations survive, reserving the last rendered slot for the
    "K more" pointer.
    """
    if total <= cap:
        return list(annotations), 0
    kept = list(annotations[: cap - 1])
    return kept, total - len(kept)


def pointer_annotation(hidden: int) -> str:
    """The final annotation that replaces silent truncation with a signpost."""
    return (
        f"::warning title=Clone detection::{hidden} more new-duplication finding(s) are not"
        f" shown inline — GitHub renders at most {ANNOTATION_CAP} annotations per step."
        f" The job summary carries the complete list."
    )


def render_summary_note(total: int, hidden: int) -> str:
    """Paragraph appended to the job summary, stating count and cap."""
    lines = [f"**Clone detection: {total} new duplication finding(s) this PR.**"]
    if hidden:
        lines.append(
            f"Only {ANNOTATION_CAP - 1} annotations render inline (GitHub's per-step cap is"
            f" {ANNOTATION_CAP}); the other {hidden} appear ONLY here."
        )
    else:
        lines.append(
            f"Annotations render inline on the diff (GitHub caps at {ANNOTATION_CAP} per step);"
            f" this summary always carries the complete list."
        )
    return "\n\n".join(lines) + "\n"


def render_comment_body(
    total: int, annotations: list[dict], hidden: int, run_url: str | None
) -> str:
    """The PR-conversation comment: count first, findings second, pointer last."""
    lines = [
        COMMENT_MARKER,
        f"## 🔁 Clone detection: {total} new duplication finding(s)",
        "",
        "This check is advisory — it never blocks a merge. Every finding below is"
        " duplication **this PR adds or worsens** against its merge base; standing,"
        " already-triaged duplication is not reported.",
        "",
    ]
    for record in annotations:
        where = record.get("file", "<no file>")
        if "line" in record:
            where += f":{record['line']}"
        title = record.get("title")
        message = record["message"].splitlines()[0] if record.get("message") else ""
        label = f"{title}: {message}" if title else message
        lines.append(f"- `{where}` — {label}")
    if hidden:
        lines.append(f"- …and {hidden} more — see the job summary for the complete list.")
    if run_url:
        lines += ["", f"Full report: {run_url} (see the job summary)."]
    return "\n".join(lines) + "\n"


def _http_request(
    method: str, url: str, token: str, payload: dict | None = None
) -> tuple[int, str]:
    """One GitHub REST call. Returns (status, body); raises on transport errors."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = _urlrequest.Request(url, data=data, method=method)  # noqa: S310
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _urlrequest.urlopen(req) as response:  # noqa: S310
            return response.status, response.read().decode()
    except HTTPError as err:
        return err.code, err.read().decode()


class CommentClient:
    """Find / upsert / delete the one marker comment on a PR's conversation."""

    def __init__(self, repo: str, token: str, request_fn=_http_request):
        self._repo = repo
        self._token = token
        self._request = request_fn

    def _url(self, path: str) -> str:
        return f"https://api.github.com/repos/{self._repo}/{path}"

    def find_marker_comment(self, pr: int) -> int | None:
        status, body = self._request(
            "GET", self._url(f"issues/{pr}/comments?per_page=100"), self._token
        )
        if status != 200:
            raise SystemExit(
                f"clone-delivery: listing PR comments failed: HTTP {status}: {body[:200]}"
            )
        for comment in json.loads(body):
            if comment.get("body", "").startswith(COMMENT_MARKER):
                return comment["id"]
        return None

    def upsert(self, pr: int, body: str) -> None:
        existing = self.find_marker_comment(pr)
        if existing is not None:
            status, err = self._request(
                "PATCH", self._url(f"issues/comments/{existing}"), self._token, {"body": body}
            )
        else:
            status, err = self._request(
                "POST", self._url(f"issues/{pr}/comments"), self._token, {"body": body}
            )
        if status not in (200, 201):
            raise SystemExit(
                f"clone-delivery: posting PR comment failed: HTTP {status}: {err[:200]}"
            )

    def delete(self, comment_id: int) -> None:
        status, err = self._request(
            "DELETE", self._url(f"issues/comments/{comment_id}"), self._token
        )
        if status != 204:
            raise SystemExit(
                f"clone-delivery: deleting stale PR comment failed: HTTP {status}: {err[:200]}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--annotations-file", required=True, help="dupdelta stdout (raw annotation lines)"
    )
    parser.add_argument(
        "--findings-file", required=True, help="dupdelta --findings-out file (the count)"
    )
    parser.add_argument("--summary", help="$GITHUB_STEP_SUMMARY path to append the cap note to")
    parser.add_argument("--repo", help="owner/name, for posting the PR comment")
    parser.add_argument("--pr", type=int, help="pull request number")
    parser.add_argument("--run-url", help="URL of this Actions run, linked from the comment")
    args = parser.parse_args(argv)

    annotations = parse_annotations(Path(args.annotations_file).read_text())
    total = int(Path(args.findings_file).read_text().strip())

    client = None
    if args.repo and args.pr:
        token = os.environ.get("GH_TOKEN")
        if token is None:
            token = os.environ.get("GITHUB_TOKEN")
        if not token:
            # A finding that cannot reach a human is the defect this tool
            # exists to close — fail loudly rather than deliver nothing.
            raise SystemExit(
                "clone-delivery: GH_TOKEN/GITHUB_TOKEN is required to post the PR comment"
            )
        client = CommentClient(args.repo, token)

    if total == 0:
        # Clean delta: retire any comment left by an earlier push on this PR,
        # so a comment never reports duplication that no longer exists.
        if client is not None:
            stale = client.find_marker_comment(args.pr)
            if stale is not None:
                client.delete(stale)
                print(f"Removed stale clone-detection comment from PR {args.pr}.")
        return 0

    kept, hidden = cap_annotations(annotations, total)
    for record in kept:
        print(record["raw"])
    if hidden:
        print(pointer_annotation(hidden))

    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write("\n" + render_summary_note(total, hidden))

    if client is not None:
        # The comment mirrors the capped inline set and points at the summary
        # for the rest — it must never say "…and K more" while listing them.
        client.upsert(args.pr, render_comment_body(total, kept, hidden, args.run_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
