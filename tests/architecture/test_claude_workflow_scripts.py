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

For every ``.claude/workflows/*.js`` this module checks four things:

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
    """Run checks 1-4 on one script; return EVERY failure, prefixed with the
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
