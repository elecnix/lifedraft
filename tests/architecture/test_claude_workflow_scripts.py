"""CI guard for ``.claude/workflows/*.js`` (issue #264).

## Why this exists

``.claude/workflows/implement-github-issue.js`` (added in #246) is production
tooling, and nothing in CI checked it. Three separate defects in it reached a
pushed branch during #246, and each was caught by hand or by the cite reviewer,
never by a test:

- **Syntax.** Inline backticks inside a template-literal prompt terminated the
  template early (``SyntaxError: Unexpected identifier '$'``). The Workflow
  runtime would only have refused the script when someone launched it.
- **Machine-specific paths.** An absolute home-directory path was hardcoded for
  the worktree location. It leaks a username and breaks on any other machine.
- **Phase drift.** The runtime groups progress by matching each
  ``phase('<title>')`` call to a ``meta.phases[].title``, exact string. A phase
  added on one side only degrades silently.

For every ``.claude/workflows/*.js`` this module checks five things:

1. **It compiles the way the Workflow runtime runs it.** The runtime executes
   the script body inside an async function, so top-level ``await`` and
   ``return`` are legal, while ``export const meta = {...}`` stays top-level.
   The guard models that: it removes the ``export `` prefix from the single
   ``export const meta`` declaration *in place* (line numbers unchanged) and
   compiles ``(async function () {\\n<body>\\n})`` with node's ``vm.Script``.
   The wrapper is compiled, never invoked, so the body never runs.
2. **``meta`` is a pure object literal** (no identifiers, calls, spreads,
   computed keys, shorthand properties, methods, or ``${}`` interpolation) with
   a non-empty string ``name`` and ``description``. A small Python parser proves
   purity; only then does node evaluate the same literal text in an empty
   ``vm`` context, and the two results must be JSON-equal, so the parser cannot
   silently disagree with JavaScript about escapes.
3. **``meta.phases[].title`` equals the set of ``phase('...')`` arguments,
   exactly, both ways**, and every agent ``{ phase: '...' }`` option names a
   meta title. A non-literal argument is an error, never dropped.
4. **No absolute home-directory path** (``/home/<user>``, ``/Users/<user>``,
   ``C:\\Users\\<user>``) in any file under ``.claude/workflows/``. The portable
   ``~/...`` form is allowed.
5. **Commit and PR titles come from the plan, never from a slug** (#268). A
   ``slug(`` call may appear only on a ``const WT_DIR =`` or ``const BRANCH =``
   line (one call, one statement); no string may hardcode a conventional-commit
   type literal such as ``<type>(#``; and every string fragment that opens with
   ``(#`` must be preceded by ``COMMIT_TYPE +`` or ``commitType +``, so a split
   literal like ``'fix' + '(#'`` cannot launder a hardcoded type either.

For ``implement-github-issue.js`` specifically, the real ``composeCommitTitle``,
``COMMIT_TYPES``, ``MAX_TITLE_LEN`` and ``PLAN_SCHEMA`` are *extracted from the
live script* and run in node against a fixed case table (never reimplemented
here), and the wiring of the resulting ``PR_TITLE`` into the refusal point, the
commit hints and the PR stage is checked statically (#268).

## Why ``node --check`` is deliberately NOT used

``node --check`` is wrong twice over. It rejects the legal top-level
``return``, so a guard built on it would have to be weakened to pass. Worse,
it can report success on broken code: on node v24.14.0, ``node --check`` on a
``.js`` file containing ESM syntax (``export``/``import``) exits 0 even when the
rest of the file is unparseable (``export const meta = {a:1}`` followed by
``)))(((`` exits 0; the same garbage without ``export`` exits 1). A guard built
on it would pass exactly the failure #264 describes whenever module detection
kicks in. ``vm.Script`` compiles a classic script and never goes through module
detection; ``test_sabotage_esm_shaped_garbage_is_not_a_false_pass`` locks that
hole shut (tracked as #265). The node helper must also echo a per-call nonce and the SHA-256 of
the body it compiled, so a node that exits 0 without checking anything (or a
shim that prints the sentinel) cannot pass.

## Loud failure

- ``node`` missing from PATH **fails** (``pytest.fail``), never skips.
  ``shutil.which`` is resolved on every call, never cached at import, so the
  PATH sabotage actually bites.
- Zero scripts discovered **fails**. The main test is deliberately not
  parametrized over the glob: an empty parameter set collects as SKIPPED under
  pytest's default ``empty_parameter_set_mark``, which is the silent green this
  issue forbids.
- There is no allowlist. A regex false positive (``phase(`` or a home path in
  prompt prose) fails loudly; the fix is to reword the prose.

The ``vm.Script`` wrapper is a *model* of the Workflow runtime as #264
describes it, not the runtime itself; the positive and negative controls below
pin that model's semantics.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
WORKFLOWS_DIR = REPO_ROOT / ".claude" / "workflows"
OK_SENTINEL = "WORKFLOW_SCRIPT_OK"
NODE_TIMEOUT_S = 60

_META_DECL_RE = re.compile(r"^export\s+const\s+meta\s*=", re.MULTILINE)
_PHASE_CALL_RE = re.compile(r"(?<![\w$.])phase\s*\(")
_PHASE_OPTION_RE = re.compile(r"(?<![\w$.])phase\s*:\s*")
HOME_PATH_RE = re.compile(r"(?<![\w.~])/(?:home|Users)/[A-Za-z0-9._-]+")
WINDOWS_HOME_PATH_RE = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s'\"`]+")

# The node helper. It reads one JSON request on stdin, compiles the body as the
# Workflow runtime would run it (never invoking it), optionally evaluates the
# already-proven-pure meta literal in an empty context, and prints ONE result
# line: the sentinel, then JSON carrying the request nonce and the SHA-256 of
# the body it actually compiled. Exit code 1 on a compile error, 0 otherwise.
_NODE_CHECKER = r"""
'use strict';
const vm = require('vm');
const crypto = require('crypto');
const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', () => {
  const req = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const out = {
    nonce: req.nonce,
    body_sha256: crypto.createHash('sha256').update(req.body, 'utf8').digest('hex'),
    compile_error: null,
    meta_json: null,
    meta_error: null,
  };
  const describe = (e) => String((e && e.stack) || e).split('\n').slice(0, 6).join('\n');
  try {
    new vm.Script('(async function () {\n' + req.body + '\n})',
                  { filename: req.filename, lineOffset: -1 });
  } catch (e) {
    out.compile_error = describe(e);
  }
  if (req.meta_literal !== null) {
    try {
      const value = vm.runInNewContext('(' + req.meta_literal + ')', Object.create(null),
                                       { timeout: 1000 });
      out.meta_json = JSON.stringify(value);
    } catch (e) {
      out.meta_error = describe(e);
    }
  }
  process.stdout.write('__SENTINEL__ ' + JSON.stringify(out) + '\n');
  process.exitCode = out.compile_error === null ? 0 : 1;
});
""".replace("__SENTINEL__", OK_SENTINEL)


class WorkflowScriptError(Exception):
    """A structural defect in a workflow script (named in the message)."""


class MetaNotLiteral(WorkflowScriptError):
    """``meta`` (or a phase title) is not a pure literal."""


# --------------------------------------------------------------------------
# discovery and node resolution: both fail loudly, neither ever skips
# --------------------------------------------------------------------------

def discover_workflow_scripts(directory: Path) -> list[Path]:
    """Every ``*.js`` directly under ``directory``, sorted. Zero is a failure."""
    if not directory.is_dir():
        pytest.fail(
            f"workflow scripts directory {directory} does not exist; the "
            ".claude/workflows guard (issue #264) has nothing to check and "
            "refuses to pass green on nothing."
        )
    scripts = sorted(directory.glob("*.js"))
    if not scripts:
        pytest.fail(
            f"no *.js workflow scripts found in {directory}; the "
            ".claude/workflows guard (issue #264) has nothing to check and "
            "refuses to pass green on nothing."
        )
    return scripts


def require_node() -> str:
    """Resolve ``node`` on PATH now (never cached). Missing node FAILS."""
    node = shutil.which("node")
    if node is None:
        pytest.fail(
            "node is not on PATH; the .claude/workflows guard (issue #264) "
            "cannot run and refuses to pass without it. Install Node.js or add "
            "actions/setup-node to the job."
        )
    return node


# --------------------------------------------------------------------------
# splitting the source the way the runtime does
# --------------------------------------------------------------------------

def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _meta_declaration(source: str) -> re.Match:
    matches = list(_META_DECL_RE.finditer(source))
    if len(matches) != 1:
        raise WorkflowScriptError(
            "expected exactly one top-level `export const meta =` declaration, "
            f"found {len(matches)}"
        )
    return matches[0]


def runtime_body(source: str) -> str:
    """The script with the single ``export `` prefix of ``export const meta``
    removed in place: line numbers are unchanged and ``meta`` stays a
    top-level ``const`` of the wrapped body."""
    m = _meta_declaration(source)
    const_at = source.index("const", m.start())
    # keep any newline inside `export\s+` so every later line keeps its number
    return (source[:m.start()] + "\n" * source.count("\n", m.start(), const_at)
            + source[const_at:])


# --------------------------------------------------------------------------
# check (2): a strict literal parser for meta (and for phase() arguments)
# --------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_NUMBER_RE = re.compile(
    r"-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)(?![\w$])")
_SIMPLE_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
    "'": "'", '"': '"', "\\": "\\", "`": "`", "$": "$",
}


class _LiteralParser:
    def __init__(self, source: str, pos: int):
        self.s = source
        self.i = pos

    # -- helpers --
    def line(self, offset: int | None = None) -> int:
        return _line_of(self.s, self.i if offset is None else offset)

    def token(self) -> str:
        if self.i >= len(self.s):
            return "<end of file>"
        if self.s.startswith("...", self.i):
            return "..."
        if self.s.startswith("${", self.i):
            return "${"
        m = _IDENT_RE.match(self.s, self.i)
        return m.group(0) if m else self.s[self.i]

    def fail(self, what: str) -> None:
        raise MetaNotLiteral(f"line {self.line()}: {what}")

    def skip_ws(self) -> None:
        s = self.s
        while self.i < len(s):
            if s[self.i].isspace():
                self.i += 1
            elif s.startswith("//", self.i):
                nl = s.find("\n", self.i)
                self.i = len(s) if nl == -1 else nl
            elif s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                if end == -1:
                    self.fail("unterminated /* comment")
                self.i = end + 2
            else:
                return

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    # -- values --
    def value(self):
        self.skip_ws()
        c = self.peek()
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in ("'", '"'):
            return self.string(c)
        if c == "`":
            return self.template()
        if self.s.startswith("...", self.i):
            self.fail("spread '...' is not a pure literal")
        m = _NUMBER_RE.match(self.s, self.i)
        if m:
            self.i = m.end()
            text = m.group(0)
            neg = text.startswith("-")
            body = text[1:] if neg else text
            if body[:2].lower() == "0x":
                num = int(body, 16)
            elif re.fullmatch(r"\d+", body):
                num = int(body)
            else:
                num = float(body)
            return -num if neg else num
        m = _IDENT_RE.match(self.s, self.i)
        if m:
            word = m.group(0)
            if word in ("true", "false", "null"):
                self.i = m.end()
                return {"true": True, "false": False, "null": None}[word]
            self.fail(f"identifier {word!r} in value position (identifiers and "
                      "calls are not pure literals)")
        self.fail(f"unexpected token {self.token()!r} (not a pure literal)")

    def obj(self) -> dict:
        self.i += 1  # '{'
        result: dict = {}
        while True:
            self.skip_ws()
            c = self.peek()
            if c == "}":
                self.i += 1
                return result
            if self.s.startswith("...", self.i):
                self.fail("spread '...' is not a pure literal")
            if c == "[":
                self.fail("computed key '[...]' is not a pure literal")
            if c in ("'", '"'):
                key = self.string(c)
            else:
                m = _IDENT_RE.match(self.s, self.i)
                if not m:
                    self.fail(f"unexpected token {self.token()!r} where a "
                              "property key was expected")
                key = m.group(0)
                self.i = m.end()
            self.skip_ws()
            c = self.peek()
            if c in (",", "}"):
                self.fail(f"shorthand property {key!r} is not a pure literal")
            if c == "(":
                self.fail(f"method {key!r} is not a pure literal")
            if c != ":":
                self.fail(f"unexpected token {self.token()!r} after property "
                          f"key {key!r} (methods and accessors are not pure "
                          "literals)")
            self.i += 1
            if key in result:
                self.fail(f"duplicate key {key!r}")
            result[key] = self.value()
            self.skip_ws()
            c = self.peek()
            if c == ",":
                self.i += 1
            elif c == "}":
                continue
            else:
                self.fail(f"unexpected token {self.token()!r} after the value "
                          f"of {key!r} (only ',' or '}}' may follow a literal)")

    def arr(self) -> list:
        self.i += 1  # '['
        result: list = []
        while True:
            self.skip_ws()
            c = self.peek()
            if c == "]":
                self.i += 1
                return result
            if c == ",":
                self.fail("array hole is not a pure literal")
            if self.s.startswith("...", self.i):
                self.fail("spread '...' is not a pure literal")
            result.append(self.value())
            self.skip_ws()
            c = self.peek()
            if c == ",":
                self.i += 1
            elif c != "]":
                self.fail(f"unexpected token {self.token()!r} in array "
                          "(only ',' or ']' may follow a literal)")

    def _escape(self, out: list) -> None:
        # self.i is on the backslash
        s = self.s
        self.i += 1
        if self.i >= len(s):
            self.fail("unterminated escape")
        c = s[self.i]
        if c == "\r" and s.startswith("\r\n", self.i):
            self.i += 2
            return
        if c in ("\n", "\r", "\u2028", "\u2029"):
            self.i += 1
            return
        if c in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[c])
            self.i += 1
            return
        if c == "0" and not (self.i + 1 < len(s) and s[self.i + 1].isdigit()):
            out.append("\0")
            self.i += 1
            return
        if c.isdigit():
            self.fail("octal escape is not allowed in meta")
        if c == "x":
            hexs = s[self.i + 1:self.i + 3]
            if not re.fullmatch(r"[0-9a-fA-F]{2}", hexs):
                self.fail("malformed \\x escape")
            out.append(chr(int(hexs, 16)))
            self.i += 3
            return
        if c == "u":
            if s.startswith("{", self.i + 1):
                end = s.find("}", self.i + 2)
                hexs = s[self.i + 2:end] if end != -1 else ""
                if not re.fullmatch(r"[0-9a-fA-F]{1,6}", hexs):
                    self.fail("malformed \\u{...} escape")
                out.append(chr(int(hexs, 16)))
                self.i = end + 1
                return
            hexs = s[self.i + 1:self.i + 5]
            if not re.fullmatch(r"[0-9a-fA-F]{4}", hexs):
                self.fail("malformed \\u escape")
            out.append(chr(int(hexs, 16)))
            self.i += 5
            return
        out.append(c)  # identity escape
        self.i += 1

    @staticmethod
    def _join(out: list) -> str:
        # combine \uD83D\uDE00-style surrogate pairs the way JS strings do
        return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16")

    def string(self, quote: str) -> str:
        start = self.i
        self.i += 1
        out: list = []
        s = self.s
        while True:
            if self.i >= len(s) or s[self.i] in ("\n", "\r"):
                self.fail(f"unterminated string starting on line {self.line(start)}")
            c = s[self.i]
            if c == quote:
                self.i += 1
                return self._join(out)
            if c == "\\":
                self._escape(out)
            else:
                out.append(c)
                self.i += 1

    def template(self) -> str:
        start = self.i
        self.i += 1
        out: list = []
        s = self.s
        while True:
            if self.i >= len(s):
                self.fail(f"unterminated template starting on line {self.line(start)}")
            if s.startswith("${", self.i):
                self.fail("template interpolation '${' is not a pure literal")
            c = s[self.i]
            if c == "`":
                self.i += 1
                return self._join(out)
            if c == "\\":
                self._escape(out)
            elif c == "\r":
                out.append("\n")
                self.i += 2 if s.startswith("\r\n", self.i) else 1
            else:
                out.append(c)
                self.i += 1


def parse_meta_literal(source: str) -> tuple[dict, tuple[int, int], str]:
    """Parse ``export const meta = {...}`` as a pure literal.

    Returns ``(meta, (decl_start, literal_end), literal_text)``. Raises
    ``MetaNotLiteral`` / ``WorkflowScriptError`` naming the line and token.
    """
    m = _meta_declaration(source)
    p = _LiteralParser(source, m.end())
    p.skip_ws()
    if p.peek() != "{":
        p.fail(f"meta must be an object literal, found {p.token()!r}")
    lit_start = p.i
    meta = p.obj()
    lit_end = p.i
    # The statement must end here: `;`, a newline, or a comment. Anything that
    # continues the expression (`.x`, `(...)`, `|| y`) makes meta impure.
    q = _LiteralParser(source, lit_end)
    while q.i < len(source) and source[q.i] in " \t":
        q.i += 1
    if q.peek() == ";":
        q.i += 1
    else:
        q.skip_ws()
        if q.peek() and q.peek() in "([`+-*/%.,?=|&<>!":
            if not (source.startswith("//", q.i) or source.startswith("/*", q.i)):
                q.fail(f"unexpected token {q.token()!r} after the closing brace "
                       "of meta (the literal must end the statement)")
    return meta, (m.start(), lit_end), source[lit_start:lit_end]


def check_meta_fields(meta: dict) -> tuple[list[str], list[str] | None]:
    """Returns (errors, phase titles or None when ``phases`` is absent)."""
    errors = []
    for key in ("name", "description"):
        if key not in meta:
            errors.append(f"meta.{key} is missing")
        elif not isinstance(meta[key], str) or not meta[key].strip():
            errors.append(f"meta.{key} must be a non-empty string, got {meta[key]!r}")
    if "phases" not in meta:
        return errors, None
    phases = meta["phases"]
    if not isinstance(phases, list):
        errors.append(f"meta.phases must be an array, got {type(phases).__name__}")
        return errors, []
    titles: list[str] = []
    for idx, entry in enumerate(phases):
        title = entry.get("title") if isinstance(entry, dict) else None
        if not isinstance(title, str) or not title.strip():
            errors.append(f"meta.phases[{idx}] must be an object with a "
                          f"non-empty string title, got {entry!r}")
            continue
        if title in titles:
            errors.append(f"duplicate meta.phases title {title!r}")
        titles.append(title)
    return errors, titles


# --------------------------------------------------------------------------
# check (3): phase() calls and agent `phase:` options
# --------------------------------------------------------------------------

def _string_literal_at(source: str, pos: int) -> tuple[str, int]:
    """Parse exactly one string literal at ``pos``; returns (value, end)."""
    p = _LiteralParser(source, pos)
    c = p.peek()
    if c in ("'", '"'):
        return p.string(c), p.i
    if c == "`":
        return p.template(), p.i
    raise MetaNotLiteral(f"expected a string literal, found {p.token()!r}")


def phase_calls(source: str, meta_span: tuple[int, int]) -> tuple[list[tuple[str, int]], list[str]]:
    """Every ``phase('<title>')`` call outside the meta span.

    Returns ``(calls, errors)``. A non-literal argument is an error naming the
    line; it is never silently dropped."""
    calls, errors = [], []
    for m in _PHASE_CALL_RE.finditer(source):
        if meta_span[0] <= m.start() < meta_span[1]:
            continue
        line = _line_of(source, m.start())
        j = m.end()
        while j < len(source) and source[j] in " \t":
            j += 1
        snippet = source[m.start():source.find("\n", m.start())].strip()
        try:
            title, end = _string_literal_at(source, j)
        except MetaNotLiteral as e:
            errors.append(f"{line}: phase() argument must be a single string "
                          f"literal ({e}): {snippet!r}")
            continue
        while end < len(source) and source[end] in " \t":
            end += 1
        if not source.startswith(")", end):
            errors.append(f"{line}: phase() argument must be a single string "
                          f"literal followed by ')': {snippet!r}")
            continue
        calls.append((title, line))
    return calls, errors


def phase_options(source: str, meta_span: tuple[int, int]) -> tuple[list[tuple[str, int]], list[str]]:
    """Every agent ``{ phase: '<title>' }`` option outside the meta span."""
    options, errors = [], []
    for m in _PHASE_OPTION_RE.finditer(source):
        if meta_span[0] <= m.start() < meta_span[1]:
            continue
        line = _line_of(source, m.start())
        try:
            title, _ = _string_literal_at(source, m.end())
        except MetaNotLiteral as e:
            snippet = source[m.start():source.find("\n", m.start())].strip()
            errors.append(f"{line}: agent `phase:` option must be a string "
                          f"literal ({e}): {snippet!r}")
            continue
        options.append((title, line))
    return options, errors


# --------------------------------------------------------------------------
# check (4): home-directory paths
# --------------------------------------------------------------------------

def home_paths(label: str, text: str) -> list[str]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rx in (HOME_PATH_RE, WINDOWS_HOME_PATH_RE):
            for m in rx.finditer(line):
                findings.append(f"{label}:{lineno}: absolute home-directory "
                                f"path {m.group(0)!r} (use ~/... instead)")
    return findings


# --------------------------------------------------------------------------
# check (5): commit/PR titles come from the plan, never from a slug (#268)
# --------------------------------------------------------------------------

COMMIT_TYPES = ("fix", "feat", "docs", "refactor", "test", "ci", "chore")
MAX_TITLE_LEN = 72
_SLUG_CALL_RE = re.compile(r"(?<![\w$.])slug\s*\(")
_SLUG_DEF_RE = re.compile(r"^\s*function\s+slug\s*\(")
# one statement only: `const BRANCH = ... slug(x); const T = slug(x)` is not allowed
_SLUG_ALLOWED_LINE_RE = re.compile(r"^const\s+(?:WT_DIR|BRANCH)\s*=[^;]*$")
_HARDCODED_TYPE_RE = re.compile(r"(?<![\w$])(?:" + "|".join(COMMIT_TYPES) + r")\(#")
_TITLE_FRAGMENT_RE = re.compile(r"""(['"`])\(#""")
_PLAN_TYPED_PREFIX_RE = re.compile(r"(?<![\w$.])(?:COMMIT_TYPE|commitType)\s*\+\s*\Z")


def commit_title_findings(name: str, source: str) -> list[str]:
    """Check (5). Line-based over the whole source; no allowlist, no skip."""
    findings = []
    for lineno, line in enumerate(source.split("\n"), start=1):
        calls = list(_SLUG_CALL_RE.finditer(line))
        if calls and _SLUG_DEF_RE.match(line):
            calls = calls[1:]  # the definition itself is not a call
        allowed = 1 if _SLUG_ALLOWED_LINE_RE.match(line) else 0
        for _ in calls[allowed:]:
            findings.append(
                f"{name}:{lineno}: slug() may only name the worktree/branch "
                "(const WT_DIR / const BRANCH); a PR title or commit hint built "
                "from a slug is #268")
        for m in _HARDCODED_TYPE_RE.finditer(line):
            findings.append(
                f"{name}:{lineno}: hardcoded conventional-commit type {m.group(0)!r}; "
                "the type must come from the plan (#268)")
    for m in _TITLE_FRAGMENT_RE.finditer(source):
        if _PLAN_TYPED_PREFIX_RE.search(source[max(0, m.start() - 200):m.start()]):
            continue
        lineno = _line_of(source, m.start())
        snippet = source[m.start():source.find("\n", m.start())].strip()[:60]
        findings.append(
            f"{name}:{lineno}: title fragment {snippet!r} is not preceded by "
            "`COMMIT_TYPE +` or `commitType +`; the conventional-commit type must "
            "come from the plan (#268)")
    return findings


# --------------------------------------------------------------------------
# check (1) + the node side of check (2)
# --------------------------------------------------------------------------

def run_node_checker(filename: str, body: str, meta_literal: str | None) -> dict:
    """Compile ``body`` as the runtime would and (optionally) evaluate the
    already-proven-pure meta literal. Raises WorkflowScriptError unless node
    demonstrably ran the check on these exact bytes."""
    node = require_node()
    nonce = secrets.token_hex(16)
    request = json.dumps({"nonce": nonce, "filename": filename, "body": body,
                          "meta_literal": meta_literal})
    try:
        proc = subprocess.run([node, "-e", _NODE_CHECKER], input=request,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=NODE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise WorkflowScriptError(f"node checker timed out after {NODE_TIMEOUT_S}s")
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(OK_SENTINEL + " ")]
    if len(lines) != 1:
        raise WorkflowScriptError(
            f"node checker printed {len(lines)} {OK_SENTINEL} result lines "
            f"(rc={proc.returncode}); refusing to treat that as a pass. "
            f"stdout={proc.stdout[-500:]!r} stderr={proc.stderr[-1000:]!r}")
    try:
        out = json.loads(lines[0][len(OK_SENTINEL) + 1:])
    except ValueError as e:
        raise WorkflowScriptError(f"node checker result is not JSON ({e}): {lines[0][:300]!r}")
    if not isinstance(out, dict) or out.get("nonce") != nonce:
        raise WorkflowScriptError("node checker result does not echo this call's nonce; "
                                  "refusing to trust it")
    if out.get("body_sha256") != hashlib.sha256(body.encode("utf-8")).hexdigest():
        raise WorkflowScriptError("node checker compiled different bytes than the script "
                                  "body it was sent")
    expected_rc = 0 if out.get("compile_error") is None else 1
    if proc.returncode != expected_rc:
        raise WorkflowScriptError(
            f"node checker exit code {proc.returncode} contradicts its result "
            f"(expected {expected_rc}); stderr={proc.stderr[-1000:]!r}")
    return out


def _one_line(text: str) -> str:
    return " | ".join(part for part in text.splitlines() if part.strip())


def check_workflow_script(path: Path) -> list[str]:
    """Run checks 1-5 on one script; return EVERY failure, prefixed with the
    file name (never just the first)."""
    require_node()  # before anything else: missing node is a failure, always
    name = path.name
    source = path.read_text(encoding="utf-8")
    errors: list[str] = []

    body = None
    try:
        body = runtime_body(source)
    except WorkflowScriptError as e:
        errors.append(f"{name}: {e}")

    meta = meta_span = meta_literal = None
    try:
        meta, meta_span, meta_literal = parse_meta_literal(source)
    except WorkflowScriptError as e:
        if body is not None:  # the count error is already reported above
            errors.append(f"{name}: meta is not a pure object literal: {e}")

    meta_titles = None
    if meta is not None:
        field_errors, meta_titles = check_meta_fields(meta)
        errors.extend(f"{name}: {e}" for e in field_errors)

    if body is not None:
        try:
            out = run_node_checker(name, body, meta_literal)
        except WorkflowScriptError as e:
            errors.append(f"{name}: {e}")
        else:
            if out.get("compile_error") is not None:
                errors.append(
                    f"{name}: does not compile as the Workflow runtime runs it "
                    f"(async function body): {_one_line(out['compile_error'])}")
            if meta_literal is not None:
                if out.get("meta_error") is not None:
                    errors.append(f"{name}: node could not evaluate the meta literal: "
                                  f"{_one_line(out['meta_error'])}")
                elif out.get("meta_json") is None or json.loads(out["meta_json"]) != meta:
                    errors.append(f"{name}: the Python meta parser disagrees with node's "
                                  f"evaluation of the same literal: python={meta!r} "
                                  f"node={out.get('meta_json')!r}")

    span = meta_span if meta_span is not None else (0, 0)
    calls, call_errors = phase_calls(source, span)
    errors.extend(f"{name}:{e}" for e in call_errors)
    options, option_errors = phase_options(source, span)
    errors.extend(f"{name}:{e}" for e in option_errors)
    if meta is not None:
        declared = set(meta_titles if meta_titles is not None else [])
        called = {title for title, _ in calls}
        if meta_titles is None and calls:
            errors.append(f"{name}: meta has no 'phases' key but the script "
                          f"calls phase() {len(calls)} time(s)")
        missing_call = sorted(declared - called)
        missing_meta = sorted(called - declared)
        if missing_call:
            errors.append(f"{name}: meta.phases titles with no phase() call: {missing_call}")
        if missing_meta:
            errors.append(f"{name}: phase() calls with no meta.phases title: {missing_meta}")
        for title, line in options:
            if title not in declared:
                errors.append(f"{name}:{line}: agent `phase:` option {title!r} is "
                              "not a meta.phases title")

    errors.extend(home_paths(name, source))
    errors.extend(commit_title_findings(name, source))
    return errors


def check_workflow_dir(directory: Path) -> tuple[list[str], int]:
    """Check every script in ``directory`` and scan every other file there for
    home paths. Returns (all errors, number of scripts checked)."""
    scripts = discover_workflow_scripts(directory)
    errors: list[str] = []
    checked = 0
    for script in scripts:
        errors.extend(check_workflow_script(script))
        checked += 1
    for other in sorted(p for p in directory.rglob("*") if p.is_file()):
        if other in scripts:
            continue
        rel = other.relative_to(directory).as_posix()
        errors.extend(home_paths(rel, other.read_text(encoding="utf-8", errors="replace")))
    return errors, checked


# ==========================================================================
# the real guard
# ==========================================================================

def test_every_workflow_script_is_runtime_valid():
    """Every .claude/workflows/*.js compiles as the runtime runs it, has a pure
    meta literal, keeps meta.phases and phase() in exact agreement, and carries
    no home-directory path (issue #264)."""
    discovered = discover_workflow_scripts(WORKFLOWS_DIR)
    errors, checked = check_workflow_dir(WORKFLOWS_DIR)
    assert checked == len(discovered) and checked >= 1, (
        f"checked {checked} scripts but discovered {len(discovered)}")
    assert not errors, (
        f"{len(errors)} problem(s) in .claude/workflows (issue #264):\n  "
        + "\n  ".join(errors))


# ==========================================================================
# sabotages: each check must demonstrably bite
# ==========================================================================

_TEMPLATE_OPENING_ANCHOR = re.compile(r"= `\n")

MINIMAL_SCRIPT = """\
export const meta = {
  name: 'fixture-workflow',
  description: 'fabricated fixture for the #264 guard',
  phases: [
    { title: 'Alpha', detail: 'first' },
    { title: 'Beta', detail: 'second' },
  ],
}

const PROMPT = `
Do the fabricated thing.`

phase('Alpha')
const a = await agent(PROMPT, { phase: 'Alpha', label: 'a' })
phase('Beta')
const b = await agent('second prompt', { phase: 'Beta', label: 'b' })
return { a, b }
"""


def _write(tmp_path: Path, text: str, name: str = "fixture-workflow.js") -> Path:
    dest = tmp_path / name
    dest.write_text(text, encoding="utf-8")
    return dest


def _live_copy(tmp_path: Path, live: Path, mutated: str) -> Path:
    original = live.read_text(encoding="utf-8")
    assert mutated != original, "sabotage did not change the script; it would be vacuous"
    sub = tmp_path / f"sabotage-{secrets.token_hex(4)}"
    sub.mkdir()
    return _write(sub, mutated, live.name)


def _live_scripts_with(pattern: re.Pattern, what: str) -> list[tuple[Path, str]]:
    found = [(p, p.read_text(encoding="utf-8")) for p in discover_workflow_scripts(WORKFLOWS_DIR)]
    found = [(p, s) for p, s in found if pattern.search(s)]
    if not found:
        pytest.fail(f"sabotage anchor missing: no live workflow script contains {what}; "
                    "update the sabotage")
    return found


def _inject_after_template_opening(source: str, text: str) -> tuple[str, int]:
    m = _TEMPLATE_OPENING_ANCHOR.search(source)
    if m is None:
        pytest.fail("sabotage anchor missing: no template literal opening '= `' "
                    "followed by a newline; update the sabotage")
    i = m.end()
    return source[:i] + text + source[i:], _line_of(source, i)


def _only(errors: list[str], needle: str) -> str:
    hits = [e for e in errors if needle in e]
    assert len(hits) == 1, f"expected exactly one error containing {needle!r}, got: {errors}"
    return hits[0]


def test_minimal_fixture_is_valid():
    """Positive control for every fixture-based sabotage below."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        assert check_workflow_script(_write(Path(d), MINIMAL_SCRIPT)) == []


def test_runtime_wrapper_accepts_top_level_await_and_return(tmp_path):
    """The wrapper is not stricter than the runtime: top-level await and return
    are legal, and `export const meta` stays top-level."""
    script = _write(tmp_path, "export const meta = {name:'x', description:'y'}\n"
                              "const r = await agent('p')\nreturn r\n")
    assert check_workflow_script(script) == []


def test_guard_compiles_but_never_runs_the_body(tmp_path):
    marker = tmp_path / "marker"
    body = (f"require('fs').writeFileSync({json.dumps(str(marker))}, 'ran')\n"
            f"globalThis.process && process.exit(7)\nreturn 1\n")
    script = _write(tmp_path, "export const meta = {name:'x', description:'y'}\n" + body)
    assert check_workflow_script(script) == []
    assert not marker.exists(), "the guard executed the workflow body"


def test_sabotage_stray_backtick_in_template_literal(tmp_path):
    for live, source in _live_scripts_with(_TEMPLATE_OPENING_ANCHOR, "a '= `' template opening"):
        # (a) a backtick pair: reports the exact injected line
        mutated, line = _inject_after_template_opening(source, "run `git status` first\n")
        errors = check_workflow_script(_live_copy(tmp_path, live, mutated))
        err = _only(errors, "does not compile")
        assert f"{live.name}:{line}" in err and "SyntaxError" in err, err
        # (b) a single stray backtick
        mutated, _ = _inject_after_template_opening(source, "run ` first\n")
        errors = check_workflow_script(_live_copy(tmp_path, live, mutated))
        err = _only(errors, "does not compile")
        assert live.name in err and "SyntaxError" in err, err


def test_sabotage_esm_shaped_garbage_is_not_a_false_pass(tmp_path):
    """`node --check` exits 0 on these (node 24, ESM detection); the guard must not."""
    garbage = _write(tmp_path, "export const meta = {name:'x', description:'y'}\n)))(((\n",
                     "garbage.js")
    err = _only(check_workflow_script(garbage), "does not compile")
    assert "garbage.js:2" in err and "SyntaxError" in err, err
    imp = _write(tmp_path, "export const meta = {name:'x', description:'y'}\n"
                           "import x from 'y'\nreturn x\n", "imports.js")
    err = _only(check_workflow_script(imp), "does not compile")
    assert "imports.js:2" in err and "SyntaxError" in err, err
    other_export = _write(tmp_path, "export const meta = {name:'x', description:'y'}\n"
                                    "export function helper() {}\n", "exports.js")
    err = _only(check_workflow_script(other_export), "does not compile")
    assert "exports.js:2" in err and "SyntaxError" in err, err


@pytest.mark.parametrize("text,count", [
    ("const meta = {name:'x', description:'y'}\nreturn 1\n", 0),
    ("export const meta = {name:'x', description:'y'}\n"
     "export const meta = {name:'x', description:'y'}\n", 2),
])
def test_sabotage_second_export_or_missing_meta_fails(tmp_path, text, count):
    errors = check_workflow_script(_write(tmp_path, text))
    _only(errors, f"exactly one top-level `export const meta =` declaration, found {count}")


def _meta_fixture(meta_lines: str) -> str:
    return "export const meta = {\n" + meta_lines + "}\nreturn 1\n"


@pytest.mark.parametrize("meta_lines,needle", [
    ("  name: NAME,\n  description: 'd',\n", "line 2: identifier 'NAME'"),
    ("  name: 'n',\n  description: f(),\n", "line 3: identifier 'f'"),
    ("  name: `a${x}`,\n  description: 'd',\n", "line 2: template interpolation '${'"),
    ("  ...base,\n  name: 'n',\n  description: 'd',\n", "line 2: spread '...'"),
    ("  name,\n  description: 'd',\n", "line 2: shorthand property 'name'"),
    ("  [k]: 'v',\n  name: 'n',\n  description: 'd',\n", "line 2: computed key"),
    ("  name() { return 'n' },\n  description: 'd',\n", "line 2: method 'name'"),
    ("  name: 'a' + 'b',\n  description: 'd',\n", "line 2: unexpected token '+'"),
    ("  name: 'n',\n  description: 'd',\n  phases: [ { title: T } ],\n", "line 4: identifier 'T'"),
    ("  name: 'n',\n", "meta.description is missing"),
    ("  name: 'n',\n  description: '',\n", "meta.description must be a non-empty string"),
    ("  name: 42,\n  description: 'd',\n", "meta.name must be a non-empty string"),
    ("  name: 'n',\n  description: 'd',\n  phases: [ { title: 'A' }, { title: 'A' } ],\n",
     "duplicate meta.phases title 'A'"),
    ("  name: 'n',\n  description: 'd',\n  phases: [ { title: 'A' }, { detail: 'x' } ],\n",
     "meta.phases[1] must be an object with a non-empty string title"),
])
def test_sabotage_meta_not_pure_literal(tmp_path, meta_lines, needle):
    errors = check_workflow_script(_write(tmp_path, _meta_fixture(meta_lines)))
    _only(errors, needle)


def test_sabotage_meta_not_an_object_or_continued(tmp_path):
    errors = check_workflow_script(_write(
        tmp_path, "export const meta = makeMeta()\nreturn 1\n", "call.js"))
    _only(errors, "meta must be an object literal, found 'makeMeta'")
    errors = check_workflow_script(_write(
        tmp_path, "export const meta = {name:'n', description:'d'}\n.name\nreturn 1\n",
        "continued.js"))
    _only(errors, "unexpected token '.' after the closing brace of meta")


def test_meta_parser_matches_node_evaluation(tmp_path):
    """The Python parser and node agree on the live meta and on tricky escapes."""
    for live in discover_workflow_scripts(WORKFLOWS_DIR):
        source = live.read_text(encoding="utf-8")
        meta, _, literal = parse_meta_literal(source)
        out = run_node_checker(live.name, runtime_body(source), literal)
        assert out["meta_error"] is None and json.loads(out["meta_json"]) == meta
    tricky = (
        "export const meta = {\n"
        r"  name: 'esc\x41pe\u00e9\u{1F600}\'q\'', " "\n"
        r'  description: "it\'s \"quoted\" \\ back\
continued\ttab\0",' "\n"
        "  'quoted-key': `multi\nline \\` tick`,\n"
        "  nums: [1, -2.5, 0x10, 1e3, .5, true, false, null],\n"
        "  nested: { a: [ { b: 'c' }, ], },  // trailing commas + comment\n"
        "  /* block */ emoji: '\\uD83D\\uDE00',\n"
        "}\nreturn 1\n")
    meta, _, literal = parse_meta_literal(tricky)
    assert meta["name"] == "escApe\u00e9\U0001F600'q'"
    assert meta["description"] == 'it\'s "quoted" \\ backcontinued\ttab\0'
    assert meta["quoted-key"] == "multi\nline ` tick"
    assert meta["nums"] == [1, -2.5, 16, 1000.0, 0.5, True, False, None]
    assert meta["emoji"] == "\U0001F600"
    out = run_node_checker("tricky.js", runtime_body(tricky), literal)
    assert out["compile_error"] is None and out["meta_error"] is None
    assert json.loads(out["meta_json"]) == meta
    assert check_workflow_script(_write(tmp_path, tricky)) == []


_META_TITLE_RE = re.compile(r"title\s*:\s*'")


def _set_diff_sides(errors: list[str]) -> tuple[str, str]:
    return (_only(errors, "meta.phases titles with no phase() call"),
            _only(errors, "phase() calls with no meta.phases title"))


def test_sabotage_phase_renamed_in_meta_only(tmp_path):
    renamed_any = 0
    for live, source in _live_scripts_with(_META_TITLE_RE, "a meta.phases title"):
        meta, (start, end), _ = parse_meta_literal(source)
        _, titles = check_meta_fields(meta)
        for title in titles or []:
            pattern = re.compile(r"(title\s*:\s*)'" + re.escape(title) + "'")
            meta_text = source[start:end]
            if len(pattern.findall(meta_text)) != 1:
                continue
            mutated = source[:start] + pattern.sub(
                lambda m: m.group(1) + "'" + title + "y'", meta_text) + source[end:]
            errors = check_workflow_script(_live_copy(tmp_path, live, mutated))
            meta_side, call_side = _set_diff_sides(errors)
            assert repr(title + "y") in meta_side and repr(title) not in meta_side, meta_side
            assert repr(title) in call_side and repr(title + "y") not in call_side, call_side
            renamed_any += 1
    if not renamed_any:
        pytest.fail("sabotage anchor missing: no uniquely-declared meta title to rename")


def test_sabotage_phase_renamed_in_call_only(tmp_path):
    """Rename EACH call in turn: the check is neither first- nor last-match."""
    renamed_any = 0
    for live, source in _live_scripts_with(_PHASE_CALL_RE, "a phase() call"):
        meta, span, _ = parse_meta_literal(source)
        calls, _ = phase_calls(source, span)
        titles = [t for t, _ in calls]
        for title, line in calls:
            if titles.count(title) != 1:
                continue
            lines = source.split("\n")
            new = re.sub(r"phase\s*\(\s*(['\"`])" + re.escape(title) + r"\1",
                         "phase('" + title + "y'", lines[line - 1], count=1)
            lines[line - 1] = new
            errors = check_workflow_script(_live_copy(tmp_path, live, "\n".join(lines)))
            meta_side, call_side = _set_diff_sides(errors)
            assert repr(title) in meta_side and repr(title + "y") not in meta_side, meta_side
            assert repr(title + "y") in call_side and repr(title) not in call_side, call_side
            renamed_any += 1
    if not renamed_any:
        pytest.fail("sabotage anchor missing: no uniquely-called phase() to rename")


def test_sabotage_phase_missing_from_meta_entirely(tmp_path):
    text = MINIMAL_SCRIPT.replace("  phases: [\n    { title: 'Alpha', detail: 'first' },\n"
                                  "    { title: 'Beta', detail: 'second' },\n  ],\n", "")
    assert text != MINIMAL_SCRIPT
    errors = check_workflow_script(_write(tmp_path, text))
    _only(errors, "meta has no 'phases' key but the script calls phase() 2 time(s)")
    assert "['Alpha', 'Beta']" in _only(errors, "phase() calls with no meta.phases title")


def test_sabotage_phase_call_non_literal_argument(tmp_path):
    for live, source in _live_scripts_with(re.compile(r"^phase\('", re.M), "a phase('...') line"):
        lines = source.split("\n")
        idx = max(i for i, ln in enumerate(lines) if ln.startswith("phase('"))
        lines[idx] = re.sub(r"^phase\('[^']*'\)", "phase(CI_TITLE)", lines[idx])
        errors = check_workflow_script(_live_copy(tmp_path, live, "\n".join(lines)))
        err = _only(errors, "phase() argument must be a single string literal")
        assert err.startswith(f"{live.name}:{idx + 1}:") and "CI_TITLE" in err, err
    for i, bad in enumerate(["phase(`${x}`)", "phase('Al' + 'pha')"]):
        text = MINIMAL_SCRIPT.replace("phase('Alpha')", bad)
        assert text != MINIMAL_SCRIPT
        errors = check_workflow_script(_write(tmp_path, text, f"nonliteral{i}.js"))
        err = _only(errors, "phase() argument must be a single string literal")
        assert err.startswith(f"nonliteral{i}.js:13:"), err


def test_sabotage_agent_phase_option_not_in_meta(tmp_path):
    """Every agent `phase:` option in turn: none may be skipped."""
    changed_any = 0
    for live, source in _live_scripts_with(_PHASE_OPTION_RE, "an agent phase: option"):
        _, span, _ = parse_meta_literal(source)
        options, _ = phase_options(source, span)
        for n, (title, line) in enumerate(options):
            occurrences = [m for m in _PHASE_OPTION_RE.finditer(source)
                           if not (span[0] <= m.start() < span[1])]
            m = occurrences[n]
            lit_end = _string_literal_at(source, m.end())[1]
            mutated = source[:m.end()] + "'Nope'" + source[lit_end:]
            errors = check_workflow_script(_live_copy(tmp_path, live, mutated))
            _only(errors, f"{live.name}:{line}: agent `phase:` option 'Nope'")
            changed_any += 1
    assert changed_any >= 1
    text = MINIMAL_SCRIPT.replace("{ phase: 'Beta',", "{ phase: BETA,")
    assert text != MINIMAL_SCRIPT
    errors = check_workflow_script(_write(tmp_path, text))
    _only(errors, "fixture-workflow.js:16: agent `phase:` option must be a string literal")


@pytest.mark.parametrize("injected,match", [
    ("cd /home/someone/work\n", "/home/someone"),
    ("cd /Users/someone/work\n", "/Users/someone"),
    ("cd C:\\\\Users\\\\someone\\\\work\n", "C:\\\\Users\\\\someone"),
])
def test_sabotage_home_path_injected(tmp_path, injected, match):
    for live, source in _live_scripts_with(_TEMPLATE_OPENING_ANCHOR, "a '= `' template opening"):
        mutated, line = _inject_after_template_opening(source, injected)
        errors = check_workflow_script(_live_copy(tmp_path, live, mutated))
        err = _only(errors, "absolute home-directory path")
        assert err.startswith(f"{live.name}:{line}: ") and repr(match) in err, err


def test_home_path_scan_covers_readme_and_allows_tilde(tmp_path):
    copy = tmp_path / "workflows"
    shutil.copytree(WORKFLOWS_DIR, copy)
    readmes = sorted(p for p in copy.rglob("*") if p.is_file() and p.suffix != ".js")
    if not readmes:
        pytest.fail("sabotage anchor missing: no non-script file in .claude/workflows")
    target = readmes[0]
    n_lines = len(target.read_text(encoding="utf-8").splitlines())
    target.write_text(target.read_text(encoding="utf-8") + "\nsee /home/someone/x\n",
                      encoding="utf-8")
    errors, _ = check_workflow_dir(copy)
    err = _only(errors, "absolute home-directory path")
    assert err.startswith(f"{target.name}:{n_lines + 2}: ") and "/home/someone" in err, err
    # negative control: the portable ~ form is fine
    for live, source in _live_scripts_with(_TEMPLATE_OPENING_ANCHOR, "a '= `' template opening"):
        mutated, _ = _inject_after_template_opening(source, "cd ~/Source/x\n")
        assert check_workflow_script(_live_copy(tmp_path, live, mutated)) == []


def test_discovery_fails_on_zero_scripts(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(pytest.fail.Exception, match=re.escape(str(empty))):
        discover_workflow_scripts(empty)
    with pytest.raises(pytest.fail.Exception, match=re.escape(str(empty))):
        check_workflow_dir(empty)
    missing = tmp_path / "does-not-exist"
    with pytest.raises(pytest.fail.Exception, match=re.escape(str(missing))):
        discover_workflow_scripts(missing)


def test_every_script_is_checked_not_just_the_first(tmp_path):
    d = tmp_path / "wf"
    d.mkdir()
    _write(d, MINIMAL_SCRIPT, "a-ok.js")
    broken = MINIMAL_SCRIPT.replace("Do the fabricated thing.", "Do the `fabricated` thing.")
    assert broken != MINIMAL_SCRIPT
    _write(d, broken, "b-broken.js")
    errors, checked = check_workflow_dir(d)
    assert checked == 2
    err = _only(errors, "does not compile")
    assert err.startswith("b-broken.js:") and "b-broken.js:11" in err, err
    assert not any(e.startswith("a-ok.js") for e in errors), errors


def test_node_missing_from_path_fails_loudly(tmp_path, monkeypatch):
    script = _write(tmp_path, MINIMAL_SCRIPT)
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    for call in (require_node, lambda: check_workflow_script(script)):
        with pytest.raises(pytest.fail.Exception) as info:
            call()
        assert "node is not on PATH" in str(info.value) and "#264" in str(info.value)


_FAKE_NODES = {
    # exits 0 having checked nothing (the `node --check` failure shape)
    "silent-exit-0": ("import sys\nsys.exit(0)\n",
                      f"printed 0 {OK_SENTINEL} result lines (rc=0)"),
    # prints the constant sentinel but cannot know this call's nonce
    "constant-sentinel": (f"print({OK_SENTINEL!r} + ' ' + '{{\"compile_error\": null}}')\n",
                          "does not echo this call's nonce"),
    # echoes the nonce but did not compile the bytes it was sent
    "wrong-bytes": ("import sys, json\nr = json.load(sys.stdin)\n"
                    f"print({OK_SENTINEL!r} + ' ' + json.dumps({{'nonce': r['nonce'], "
                    "'body_sha256': '0' * 64, 'compile_error': None}))\n",
                    "compiled different bytes"),
    # right nonce and bytes, claims success, but exits 1
    "contradictory-rc": ("import sys, json, hashlib\nr = json.load(sys.stdin)\n"
                         "h = hashlib.sha256(r['body'].encode('utf-8')).hexdigest()\n"
                         f"print({OK_SENTINEL!r} + ' ' + json.dumps({{'nonce': r['nonce'], "
                         "'body_sha256': h, 'compile_error': None}))\nsys.exit(1)\n",
                         "exit code 1 contradicts its result"),
}


@pytest.mark.parametrize("kind", sorted(_FAKE_NODES))
def test_fake_node_cannot_pass_a_broken_script(tmp_path, monkeypatch, kind):
    """The guard never trusts node's exit code alone: a `node` that does not
    demonstrably compile these exact bytes fails the script, loudly."""
    code, needle = _FAKE_NODES[kind]
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    shim = fake_bin / "node"
    shim.write_text(f"#!{sys.executable}\n{code}", encoding="utf-8")
    shim.chmod(0o755)
    broken = MINIMAL_SCRIPT.replace("Do the fabricated thing.", "Do the `fabricated` thing.")
    assert broken != MINIMAL_SCRIPT
    script = _write(tmp_path, broken)
    monkeypatch.setenv("PATH", str(fake_bin))
    assert shutil.which("node") == str(shim)
    _only(check_workflow_script(script), needle)


def test_node_missing_fails_the_real_guard_in_a_subprocess(tmp_path):
    """End to end: the real guard, run by pytest with no node on PATH, FAILS
    (never skips)."""
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("COV_CORE_", "COVERAGE_", "PYTEST_XDIST"))}
    env.update(PATH=str(empty_bin), PERF_TIMINGS_FILE=str(tmp_path / "timings.json"))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
         "--no-header",
         f"{Path(__file__).relative_to(REPO_ROOT).as_posix()}::test_every_workflow_script_is_runtime_valid"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "1 failed" in out and "node is not on PATH" in out, out
    assert "skipped" not in out, out


def test_module_has_no_skip_path():
    """No future edit may quietly add a skip/xfail route to this guard."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = ("skip", "skipif", "importorskip", "xfail")
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            hits.append(f"line {node.lineno}: .{node.attr}")
        if isinstance(node, ast.ImportFrom) and node.module == "pytest":
            hits.extend(f"line {node.lineno}: from pytest import {a.name}"
                        for a in node.names if a.name in forbidden)
    assert not hits, f"skip/xfail path in the #264 guard: {hits}"


# ==========================================================================
# #268: titles from the plan. The REAL helper and schema, extracted and run.
# ==========================================================================

PIPELINE_SCRIPT = WORKFLOWS_DIR / "implement-github-issue.js"
COMPOSE_SENTINEL = "COMPOSE_TITLE_RESULT"
_JS_UNDEFINED = "__JS_UNDEFINED__"
_ORIGINAL_PLAN_REQUIRED = ("summary", "steps", "files", "risks", "acceptanceCriteria",
                           "outOfScope", "testsToAdd")

_COMPOSE_ANCHORS = (
    re.compile(r"^const COMMIT_TYPES = ", re.M),
    re.compile(r"^const MAX_TITLE_LEN = ", re.M),
    re.compile(r"^function composeCommitTitle\(", re.M),
    re.compile(r"^const PLAN_SCHEMA = \{", re.M),
)

# Reads one JSON request on stdin: the extracted source text and the case table.
# Inputs travel as JSON, never spliced into JS source. Prints ONE sentinel line
# carrying the request nonce.
_COMPOSE_NODE = r"""
'use strict';
const vm = require('vm');
const chunks = [];
const emit = (o) => process.stdout.write('__SENTINEL__ ' + JSON.stringify(o) + '\n');
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', () => {
  const req = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const out = { nonce: req.nonce, load_error: null, schema: null, types: null, max_len: null, results: [] };
  const decode = (v) => (v === req.undefined_marker ? undefined : v);
  let api = null;
  try {
    api = vm.runInNewContext(req.code + '\n;({ f: composeCommitTitle, S: PLAN_SCHEMA, T: COMMIT_TYPES, L: MAX_TITLE_LEN })',
                             Object.create(null), { timeout: 1000 });
  } catch (e) {
    out.load_error = String((e && e.stack) || e).split('\n').slice(0, 4).join(' | ');
  }
  if (api !== null) {
    out.schema = JSON.parse(JSON.stringify(api.S));
    out.types = JSON.parse(JSON.stringify(api.T));
    out.max_len = api.L;
    for (const c of req.cases) {
      try {
        const v = api.f(...c.args.map(decode));
        out.results.push({ id: c.id, kind: 'return', value_type: typeof v, value: v === undefined ? null : v });
      } catch (e) {
        out.results.push({ id: c.id, kind: 'throw',
                           err_name: (e && e.constructor && e.constructor.name) || typeof e,
                           message: (e && e.message !== undefined) ? String(e.message) : String(e) });
      }
    }
  }
  emit(out);
});
""".replace("__SENTINEL__", COMPOSE_SENTINEL)


def _extract_top_level(source: str, header_re: re.Pattern) -> str:
    """The top-level declaration whose first line matches ``header_re`` at
    column 0: through the first following line that is exactly ``}`` when the
    header opens a block, else the single line. A missing or ambiguous anchor
    FAILS; this never returns ''."""
    matches = list(header_re.finditer(source))
    if len(matches) != 1:
        pytest.fail(f"extraction anchor missing or ambiguous: {header_re.pattern!r} matched "
                    f"{len(matches)} times in the workflow script (#268); refusing to run the "
                    "behavioural harness on nothing")
    start = matches[0].start()
    eol = source.find("\n", start)
    first = source[start:eol if eol != -1 else len(source)]
    if not first.rstrip().endswith("{"):
        return first
    lines = source[start:].split("\n")
    for i, line in enumerate(lines[1:], start=1):
        if line == "}":
            return "\n".join(lines[:i + 1])
    pytest.fail(f"extraction anchor {header_re.pattern!r}: no closing '}}' at column 0 (#268)")


def _boundary_subject(total: int) -> tuple[str, str]:
    """A subject whose composed ``refactor(#9999): `` title is exactly
    ``total`` characters. Padded with a NON-space so trim() cannot shorten it."""
    prefix = "refactor(#9999): "
    subject = "x y".ljust(total - len(prefix), "z")
    if len(prefix + subject.strip()) != total:
        pytest.fail(f"boundary case for {total} chars is built wrong: the trimmed title is "
                    f"{len(prefix + subject.strip())} chars")
    return subject, prefix + subject


def compose_cases() -> list[tuple[str, list, str, str]]:
    """(id, args, 'return'|'throw', exact value | message needle)."""
    s72, t72 = _boundary_subject(72)
    s73, _ = _boundary_subject(73)
    ok_subject = "derive titles from the plan"
    cases: list[tuple[str, list, str, str]] = [
        ("trimmed-good", ["docs", "  add usage notes for the fetch stage  ", 268], "return",
         "docs(#268): add usage notes for the fetch stage"),
        ("len-72", ["refactor", s72, 9999], "return", t72),
        ("len-73", ["refactor", s73, 9999], "throw", "title is 73 characters"),
    ]
    cases += [(f"type-ok-{t}", [t, ok_subject, 7], "return", f"{t}(#7): {ok_subject}")
              for t in COMMIT_TYPES]
    bad_types = {"bugfix": "bugfix", "FIX": "FIX", "Fix": "Fix", "empty": "",
                 "undefined": _JS_UNDEFINED, "null": None, "number": 5, "padded": " fix"}
    cases += [(f"type-{k}", [v, ok_subject, 7], "throw", "commitType must be one of")
              for k, v in bad_types.items()]
    cases += [
        ("subject-undefined", ["fix", _JS_UNDEFINED, 7], "throw", "commitSubject must be a string"),
        ("subject-null", ["fix", None, 7], "throw", "commitSubject must be a string"),
        ("subject-number", ["fix", 42, 7], "throw", "commitSubject must be a string"),
        ("subject-empty", ["fix", "", 7], "throw", "empty or whitespace"),
        ("subject-spaces", ["fix", "   ", 7], "throw", "empty or whitespace"),
        ("subject-tab-newline", ["fix", "\t\n", 7], "throw", "empty or whitespace"),
        ("subject-slug", ["fix", "tooling-claude-workflows-scripts-have-no", 7], "throw",
         "never a slug"),
        ("subject-prefixed", ["fix", "fix(#5): do x", 7], "throw", "already carries a type prefix"),
        ("subject-bare-prefix", ["fix", "docs: do x", 7], "throw", "already carries a type prefix"),
        ("subject-dquote", ["fix", 'say "hi" now', 7], "throw", "unsafe in a shell-quoted title"),
        ("subject-backtick", ["fix", "run `x` now", 7], "throw", "unsafe in a shell-quoted title"),
        ("subject-dollar", ["fix", "cost $5 now", 7], "throw", "unsafe in a shell-quoted title"),
        ("subject-backslash", ["fix", "a\\b c", 7], "throw", "unsafe in a shell-quoted title"),
        ("subject-newline", ["fix", "a\nb c", 7], "throw", "unsafe in a shell-quoted title"),
        ("subject-nul", ["fix", "a\u0000b c", 7], "throw", "unsafe in a shell-quoted title"),
    ]
    cases += [(f"issue-{k}", ["fix", ok_subject, v], "throw", "must be a positive integer")
              for k, v in {"zero": 0, "negative": -1, "fraction": 1.5, "string": "12",
                           "undefined": _JS_UNDEFINED}.items()]
    return cases


def commit_title_behaviour_errors(source: str, node_program: str = _COMPOSE_NODE) -> list[str]:
    """Run the REAL composeCommitTitle / COMMIT_TYPES / MAX_TITLE_LEN /
    PLAN_SCHEMA, extracted from ``source``, in an empty node vm context against
    the case table. Returns human-readable mismatches; [] means pass. Raises
    WorkflowScriptError when node's answer cannot be trusted."""
    code = "\n".join(_extract_top_level(source, a) for a in _COMPOSE_ANCHORS)
    node = require_node()
    cases = compose_cases()
    nonce = secrets.token_hex(16)
    request = json.dumps({"nonce": nonce, "code": code, "undefined_marker": _JS_UNDEFINED,
                          "cases": [{"id": c[0], "args": c[1]} for c in cases]})
    try:
        proc = subprocess.run([node, "-e", node_program], input=request, capture_output=True,
                              text=True, encoding="utf-8", timeout=NODE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise WorkflowScriptError(f"compose harness timed out after {NODE_TIMEOUT_S}s")
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(COMPOSE_SENTINEL + " ")]
    if len(lines) != 1 or proc.returncode != 0:
        raise WorkflowScriptError(
            f"compose harness printed {len(lines)} {COMPOSE_SENTINEL} lines (rc={proc.returncode}); "
            f"refusing to trust it. stderr={proc.stderr[-800:]!r}")
    out = json.loads(lines[0][len(COMPOSE_SENTINEL) + 1:])
    if not isinstance(out, dict) or out.get("nonce") != nonce:
        raise WorkflowScriptError("compose harness result does not echo this call's nonce")
    if out["load_error"] is not None:
        return [f"the extracted helper/schema did not load in node: {out['load_error']}"]

    errors: list[str] = []
    types = out["types"]
    if not isinstance(types, list) or sorted(types) != sorted(COMMIT_TYPES):
        errors.append(f"COMMIT_TYPES is {types!r}, expected exactly {list(COMMIT_TYPES)}")
    if out["max_len"] != MAX_TITLE_LEN:
        errors.append(f"MAX_TITLE_LEN is {out['max_len']!r}, expected {MAX_TITLE_LEN}")
    schema = out["schema"]
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    ctype = props.get("commitType", {})
    enum = ctype.get("enum")
    if ctype.get("type") != "string" or not isinstance(enum, list) or \
            len(enum) != len(COMMIT_TYPES) or set(enum) != set(COMMIT_TYPES):
        errors.append(f"PLAN_SCHEMA.properties.commitType enum/type is {ctype!r}, expected "
                      f"type 'string' and enum exactly {list(COMMIT_TYPES)}")
    csubj = props.get("commitSubject", {})
    if csubj.get("type") != "string" or csubj.get("minLength") != 1:
        errors.append(f"PLAN_SCHEMA.properties.commitSubject is {csubj!r}, expected "
                      "type 'string' with minLength 1")
    required = schema.get("required", []) if isinstance(schema, dict) else []
    for key in _ORIGINAL_PLAN_REQUIRED + ("commitType", "commitSubject"):
        if key not in required:
            errors.append(f"PLAN_SCHEMA.required is missing {key!r}")
        if key not in props:
            errors.append(f"PLAN_SCHEMA.properties is missing {key!r}")

    results = {}
    for r in out["results"]:
        if r["id"] in results:
            raise WorkflowScriptError(f"compose harness reported case {r['id']!r} twice")
        results[r["id"]] = r
    for case_id, args, expect, want in cases:
        r = results.get(case_id)
        shown = f"case {case_id!r} {args!r}"
        if r is None:
            errors.append(f"{shown}: node returned no result")
        elif expect == "return":
            if r["kind"] != "return" or r["value_type"] != "string" or r["value"] != want:
                errors.append(f"{shown}: expected to return {want!r}, got {r!r}")
        elif r["kind"] != "throw":
            errors.append(f"{shown}: expected a 'Plan refused' throw, but it returned {r['value']!r}")
        elif r["err_name"] != "Error" or not r["message"].startswith("Plan refused") \
                or want not in r["message"]:
            errors.append(f"{shown}: expected Error 'Plan refused: ...{want}...', got "
                          f"{r['err_name']}: {r['message']!r}")
    return errors


def _mutate(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        pytest.fail(f"sabotage anchor missing: {old!r} occurs {source.count(old)} times in the "
                    "live script; update the sabotage")
    mutated = source.replace(old, new)
    assert mutated != source, "sabotage did not change the script; it would be vacuous"
    return mutated


def _pipeline_source() -> str:
    if not PIPELINE_SCRIPT.is_file():
        pytest.fail(f"{PIPELINE_SCRIPT} is missing; the #268 checks have nothing to check")
    return PIPELINE_SCRIPT.read_text(encoding="utf-8")


def test_compose_commit_title_behaviour():
    """The live helper refuses every bad shape with 'Plan refused', returns
    exact titles for good ones, and PLAN_SCHEMA carries the enum and both
    required fields (#268)."""
    assert commit_title_behaviour_errors(_pipeline_source()) == []


_COMPOSE_SABOTAGES = [
    ("max-len-720", "const MAX_TITLE_LEN = 72", "const MAX_TITLE_LEN = 720",
     ["MAX_TITLE_LEN is 720", "case 'len-73'"]),
    ("length-gte", "if (title.length > MAX_TITLE_LEN)", "if (title.length >= MAX_TITLE_LEN)",
     ["case 'len-72'"]),
    ("empty-check-off", "if (subject === '') {", "if (false) {",
     ["case 'subject-empty'", "case 'subject-spaces'"]),
    ("no-trim", "const subject = commitSubject.trim()", "const subject = commitSubject",
     ["case 'trimmed-good'", "case 'subject-spaces'"]),
    ("chore-to-wip", "'ci', 'chore']", "'ci', 'wip']",
     ["COMMIT_TYPES is", "commitType enum/type", "case 'type-ok-chore'"]),
    ("append-build", "'ci', 'chore']", "'ci', 'chore', 'build']",
     ["COMMIT_TYPES is", "commitType enum/type"]),
    ("drop-required-subject", "'testsToAdd', 'commitType', 'commitSubject']",
     "'testsToAdd', 'commitType']", ["PLAN_SCHEMA.required is missing 'commitSubject'"]),
    ("drop-required-type", "'testsToAdd', 'commitType', 'commitSubject']",
     "'testsToAdd', 'commitSubject']", ["PLAN_SCHEMA.required is missing 'commitType'"]),
    ("drop-min-length", "'commitSubject': { type: 'string', minLength: 1 }",
     "'commitSubject': { type: 'string' }", ["commitSubject is"]),
    ("slug-shape-off", "if (!/\\s/.test(subject)) {", "if (false) {", ["case 'subject-slug'"]),
    ("shell-chars", r'/["`$\\\u0000-\u001f\u007f]/', r'/["\\\u0000-\u001f\u007f]/',
     ["case 'subject-dollar'", "case 'subject-backtick'"]),
    ("type-coerced",
     "function composeCommitTitle(commitType, commitSubject, issueNumber) {\n",
     "function composeCommitTitle(commitType, commitSubject, issueNumber) {\n"
     "  if (!COMMIT_TYPES.includes(commitType)) commitType = 'chore'\n",
     ["case 'type-bugfix'", "case 'type-FIX'", "case 'type-undefined'"]),
    # the ReferenceError trap: any exception must not count as a refusal
    ("slug-fallback",
     "throw new Error('Plan refused: commitSubject is empty or whitespace, got ' + JSON.stringify(commitSubject))",
     "return commitType + '(#' + issueNumber + '): ' + slug(String(issueNumber))",
     ["case 'subject-empty'", "ReferenceError"]),
]


@pytest.mark.parametrize("sabotage", _COMPOSE_SABOTAGES, ids=[s[0] for s in _COMPOSE_SABOTAGES])
def test_sabotage_compose_commit_title_weakened(sabotage):
    _, old, new, needles = sabotage
    errors = commit_title_behaviour_errors(_mutate(_pipeline_source(), old, new))
    joined = "\n".join(errors)
    for needle in needles:
        assert needle in joined, f"harness missed the sabotage ({needle!r} not in):\n{joined}"


def test_sabotage_slug_fallback_also_trips_static_check(tmp_path):
    """S7's other half: a slug() call inside the helper is a check-5 finding."""
    source = _pipeline_source()
    old = "throw new Error('Plan refused: commitSubject is empty or whitespace, got ' + JSON.stringify(commitSubject))"
    mutated = _mutate(source, old, "return commitType + '(#' + issueNumber + '): ' + slug(String(issueNumber))")
    line = _line_of(source, source.index(old))
    errors = check_workflow_script(_live_copy(tmp_path, PIPELINE_SCRIPT, mutated))
    err = _only(errors, "slug() may only name the worktree/branch")
    assert err.startswith(f"{PIPELINE_SCRIPT.name}:{line}: "), err


def test_compose_harness_fails_when_helper_missing():
    mutated = _pipeline_source().replace("composeCommitTitle", "composeTitle")
    assert mutated != _pipeline_source()
    with pytest.raises(pytest.fail.Exception, match="extraction anchor missing"):
        commit_title_behaviour_errors(mutated)


@pytest.mark.parametrize("kind,old,new,needle", [
    pytest.param("sentinel-twice", "  emit(out);\n", "  emit(out);\n  emit(out);\n", "printed 2", id="sentinel-twice"),
    pytest.param("forged-nonce", "nonce: req.nonce,", "nonce: 'forged',",
                 "does not echo this call's nonce", id="forged-nonce"),
])
def test_compose_harness_refuses_untrustworthy_node_output(kind, old, new, needle):
    assert _COMPOSE_NODE.count(old) == 1, kind
    program = _COMPOSE_NODE.replace(old, new)
    with pytest.raises(WorkflowScriptError, match=re.escape(needle)):
        commit_title_behaviour_errors(_pipeline_source(), node_program=program)


def test_compose_harness_fails_without_node(tmp_path, monkeypatch):
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with pytest.raises(pytest.fail.Exception, match="node is not on PATH"):
        commit_title_behaviour_errors(_pipeline_source())


def test_boundary_subjects_are_not_space_padded():
    """trim() would silently shorten a space-padded boundary case (S10)."""
    for total in (72, 73):
        subject, title = _boundary_subject(total)
        assert len(title) == total and subject == subject.strip() and " " in subject


def test_compose_commit_title_has_no_fallback_shape():
    """No `||`/`??` default, default parameter, try/catch or slug in the helper."""
    fn = _extract_top_level(_pipeline_source(), _COMPOSE_ANCHORS[2])
    header = fn.split("\n", 1)[0]
    assert "=" not in header[header.index("("):header.index(")")], header
    for token in ("||", "??", "catch"):
        assert token not in fn, f"composeCommitTitle contains {token!r}"
    assert not _SLUG_CALL_RE.search(fn), "composeCommitTitle calls slug()"
    assert not re.search(r"\btry\b", fn), "composeCommitTitle contains try"


# ---- check 5 on the live script and on fixtures ---------------------------

def test_live_script_uses_slug_only_for_worktree_and_branch():
    lines = _pipeline_source().split("\n")
    sites = [(i + 1, ln) for i, ln in enumerate(lines)
             if _SLUG_CALL_RE.search(ln) and not _SLUG_DEF_RE.match(ln)]
    assert len(sites) == 2, sites
    assert [ln.split("=")[0].strip() for _, ln in sites] == ["const WT_DIR", "const BRANCH"], sites
    assert sum(1 for ln in lines if _SLUG_DEF_RE.match(ln)) == 1


def _pr_create_line(source: str) -> tuple[str, int]:
    for i, ln in enumerate(source.split("\n"), start=1):
        if "gh pr create --repo" in ln:
            return ln, i
    pytest.fail("sabotage anchor missing: no `gh pr create` line in the live script")


def test_sabotage_pr_title_built_from_slug(tmp_path):
    """S1: the pre-#268 --title line trips BOTH halves of check 5."""
    source = _pipeline_source()
    _, line = _pr_create_line(source)
    mutated = _mutate(source, "--draft --title \"' + PR_TITLE + '\"",
                      "--draft --title \"fix(#' + fetched.issueNumber + '): ' + slug(fetched.title) + '\"")
    errors = check_workflow_script(_live_copy(tmp_path, PIPELINE_SCRIPT, mutated))
    assert _only(errors, "slug() may only name").startswith(f"{PIPELINE_SCRIPT.name}:{line}: ")
    assert _only(errors, "hardcoded conventional-commit type 'fix(#'").startswith(
        f"{PIPELINE_SCRIPT.name}:{line}: ")


@pytest.mark.parametrize("kind,old,new", [
    pytest.param("laundered-variable", "const BRANCH = 'fix/' + fetched.issueNumber + '-' + slug(fetched.title)\n",
     "const BRANCH = 'fix/' + fetched.issueNumber + '-' + slug(fetched.title)\n"
     "const TITLE_SLUG = slug(fetched.title)\n", id="laundered-variable"),
    pytest.param("allowed-line-spoof", "const BRANCH = 'fix/' + fetched.issueNumber + '-' + slug(fetched.title)\n",
     "const BRANCH = 'fix/' + fetched.issueNumber + '-' + slug(fetched.title); "
     "const T = slug(fetched.title)\n", id="allowed-line-spoof"),
])
def test_sabotage_slug_laundered_through_variable(tmp_path, kind, old, new):
    source = _pipeline_source()
    line = _line_of(source, source.index(old)) + (1 if kind == "laundered-variable" else 0)
    errors = check_workflow_script(_live_copy(tmp_path, PIPELINE_SCRIPT, _mutate(source, old, new)))
    hits = [e for e in errors if "slug() may only name" in e]
    assert hits and all(e.startswith(f"{PIPELINE_SCRIPT.name}:{line}: ") for e in hits), errors


@pytest.mark.parametrize("hint", ["address validator findings", "address CI findings"])
def test_sabotage_hardcoded_commit_type_in_hint(tmp_path, hint):
    source = _pipeline_source()
    old = "' + COMMIT_TYPE + '(#' + fetched.issueNumber + '): " + hint
    line = _line_of(source, source.index(old))
    # (a) a whole literal, (b) a split literal that evades the <type>(# regex
    for new, needle in (("fix(#' + fetched.issueNumber + '): " + hint,
                         "hardcoded conventional-commit type 'fix(#'"),
                        ("fix' + '(#' + fetched.issueNumber + '): " + hint,
                         "is not preceded by `COMMIT_TYPE +`")):
        errors = check_workflow_script(_live_copy(tmp_path, PIPELINE_SCRIPT, _mutate(source, old, new)))
        assert _only(errors, needle).startswith(f"{PIPELINE_SCRIPT.name}:{line}: "), errors


def test_sabotage_hardcoded_commit_type_in_prose(tmp_path):
    source = _pipeline_source()
    mutated, line = _inject_after_template_opening(source, "e.g. feat(#12): add a thing\n")
    errors = check_workflow_script(_live_copy(tmp_path, PIPELINE_SCRIPT, mutated))
    assert _only(errors, "hardcoded conventional-commit type 'feat(#'").startswith(
        f"{PIPELINE_SCRIPT.name}:{line}: ")


def test_commit_title_check_on_fixture(tmp_path):
    ok = MINIMAL_SCRIPT.replace(
        "const PROMPT = `",
        "function slug(t) {\n  return t\n}\nconst BRANCH = 'b/' + slug('t')\n"
        "const WT_DIR = slug('t')\nconst PROMPT = `")
    assert ok != MINIMAL_SCRIPT and commit_title_findings("f.js", MINIMAL_SCRIPT) == []
    assert check_workflow_script(_write(tmp_path, ok, "ok.js")) == []
    for t in COMMIT_TYPES:
        bad = ok.replace("return { a, b }", f"const X = '{t}(#1): ' + a\nreturn {{ a, b }}")
        errors = check_workflow_script(_write(tmp_path, bad, f"bad-{t}.js"))
        line = bad.split("\n").index(f"const X = '{t}(#1): ' + a") + 1
        assert _only(errors, f"hardcoded conventional-commit type '{t}(#'").startswith(f"bad-{t}.js:{line}: ")
    typed = ok.replace("return { a, b }", "const X = commitType + '(#1): ' + a\nreturn { a, b }")
    assert check_workflow_script(_write(tmp_path, typed, "typed.js")) == []
    # a word that merely ENDS in a type name is not a hardcoded type
    assert commit_title_findings("f.js", "const s = 'hotfix(#1) prefix(#2)'\n") == []


# ---- the wiring of PR_TITLE: refusal point, hints, PR stage ----------------

def pipeline_title_wiring_errors(source: str) -> list[str]:
    """Static facts about how implement-github-issue.js uses PR_TITLE (#268)."""
    lines = source.split("\n")
    errors: list[str] = []

    def find(pattern: str) -> list[int]:
        rx = re.compile(pattern)
        return [i for i, ln in enumerate(lines) if rx.search(ln)]

    def one(pattern: str, what: str) -> int | None:
        hits = find(pattern)
        if len(hits) != 1:
            errors.append(f"expected exactly one {what}, found {len(hits)}")
            return None
        return hits[0]

    no_plan = one(r"^if \(!plan\) throw ", "`if (!plan) throw` line")
    call = one(r"^const PR_TITLE = composeCommitTitle\(plan\.commitType, plan\.commitSubject, "
               r"fetched\.issueNumber\)$", "bare top-level `const PR_TITLE = composeCommitTitle(...)`")
    ctype = one(r"^const COMMIT_TYPE = plan\.commitType$", "`const COMMIT_TYPE = plan.commitType`")
    test_plan = one(r"^phase\('Test plan'\)$", "phase('Test plan') line")
    if None not in (no_plan, call, test_plan):
        if not no_plan < call < test_plan:
            errors.append("the composeCommitTitle refusal must sit after `if (!plan) throw` and "
                          "before phase('Test plan')")
        elif any("await agent(" in ln for ln in lines[no_plan:call]):
            errors.append("an agent runs between the plan check and the title refusal")
    if None not in (call, ctype) and not call < ctype:
        errors.append("COMMIT_TYPE is read before composeCommitTitle validated it")
    uses = find(r"plan\.commit(?:Type|Subject)")
    if sorted(uses) != sorted(x for x in (call, ctype) if x is not None):
        errors.append(f"plan.commitType/commitSubject read outside the refusal point, lines "
                      f"{[u + 1 for u in uses]}")
    if len(find(r"(?<![\w$.])PR_TITLE\s*=(?!=)")) != 1:
        errors.append("PR_TITLE must be assigned exactly once, by composeCommitTitle")
    if find(r"\btry\s*\{|\bcatch\s*[({]|\.catch\s*\("):
        errors.append("the script must not try/catch: a caught refusal becomes a silent default")
    create = find(r"gh pr create --repo ")
    if len(create) != 1 or "--title \"' + PR_TITLE + '\"" not in lines[create[0]]:
        errors.append("`gh pr create --title` must interpolate PR_TITLE")
    if not find(r"gh pr edit <number> --repo ' \+ REPO \+ ' --title \"' \+ PR_TITLE \+ '\""):
        errors.append("the PR stage must retitle an adopted PR with gh pr edit --title PR_TITLE")
    if not find(r"--json isDraft,title"):
        errors.append("the PR agent must re-read the actual title (gh pr view --json isDraft,title)")
    no_pr = one(r"^if \(!pr\) throw ", "`if (!pr) throw` line")
    if no_pr is not None and not (no_pr + 1 < len(lines)
                                  and lines[no_pr + 1].startswith("if (pr.title !== PR_TITLE) throw ")):
        errors.append("`if (pr.title !== PR_TITLE) throw` must directly follow `if (!pr) throw`")
    if not find(r"exact subject line: ' \+ PR_TITLE"):
        errors.append("the implementer's commit hint must carry PR_TITLE")
    for hint in ("address validator findings", "address CI findings"):
        if not find(re.escape("' + COMMIT_TYPE + '(#' + fetched.issueNumber + '): " + hint)):
            errors.append(f"the {hint!r} commit hint must take its type from COMMIT_TYPE")
    return errors


def test_pr_stage_asserts_returned_title():
    assert pipeline_title_wiring_errors(_pipeline_source()) == []


_WIRING_SABOTAGES = [
    ("drop-title-check", "if (pr.title !== PR_TITLE) throw ", "// if (pr.title !== PR_TITLE) throw ",
     "must directly follow"),
    ("drop-adopted-retitle", "gh pr edit <number> --repo ' + REPO + ' --title \"' + PR_TITLE + '\"",
     "gh pr edit <number> --repo ' + REPO + '", "retitle an adopted PR"),
    ("title-evaporates", "--draft --title \"' + PR_TITLE + '\"", "--draft --title \"' + fetched.title + '\"",
     "must interpolate PR_TITLE"),
    ("refusal-moved-late",
     "const PR_TITLE = composeCommitTitle(plan.commitType, plan.commitSubject, fetched.issueNumber)\n",
     "", "exactly one bare top-level"),
    ("refusal-caught",
     "const PR_TITLE = composeCommitTitle(plan.commitType, plan.commitSubject, fetched.issueNumber)\n",
     "let PR_TITLE; try { PR_TITLE = composeCommitTitle(plan.commitType, plan.commitSubject, "
     "fetched.issueNumber) } catch (e) { PR_TITLE = plan.commitType + '(#' + fetched.issueNumber "
     "+ '): ' + fetched.title }\n", "must not try/catch"),
    ("implementer-hint-lost", "exact subject line: ' + PR_TITLE", "exact subject line: ' + BRANCH",
     "implementer's commit hint"),
]


@pytest.mark.parametrize("sabotage", _WIRING_SABOTAGES, ids=[s[0] for s in _WIRING_SABOTAGES])
def test_sabotage_title_wiring(sabotage):
    kind, old, new, needle = sabotage
    source = _pipeline_source()
    mutated = _mutate(source, old, new)
    if kind == "refusal-moved-late":  # S15: move the call after the Test plan agent
        anchor = "phase('Implement')\n"
        mutated = _mutate(mutated, anchor, old + anchor)
        needle = "before phase('Test plan')"
    errors = pipeline_title_wiring_errors(mutated)
    assert any(needle in e for e in errors), errors
