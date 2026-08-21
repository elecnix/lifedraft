"""DP#15 enforcement: "Personal data never enters version control" (see
DESIGN_PRINCIPLES.md #15), generalised repo-wide after issue #716.

## The incident this test closes

Issue #716 named one file (`tests/test_issue_88_resp_quebec_computed.py`)
using a real child's name and real birth year as an `RESPChild` fixture. A
sweep for #716 found the breach was wider than that single file: real first
names, a real birth year, and real RRSP/TFSA contribution-room figures --
data that can only have come from this household's actual notices of
assessment -- were sitting in several tracked test files *and* in
`.pi/agents/issue-analyzer.md`, which handed those figures to every
LLM agent as standing context on every run. All of that has been scrubbed
to role-based names (DP#4) and fabricated figures as part of this change.

A narrow version of this exact guard already existed
(`tests/test_lsif_wiring.py`, `TestNoPersonalDataInCode`) -- but it only
`grep`-checked `optimize.py`, so nothing stopped the same personal data
from accumulating in five other files. A detector that isn't wired to the
whole repo isn't a detector, per AGENTS.md's own thesis ("when this is
wrong, will anyone find out?") -- so per DP#9 (no two spellings of the
same rule), that narrow check is deleted and this module is the one
DP#15 guard, walking every file `git` tracks: source, tests, docs, `.pi/`,
and CI workflows.

## Why the forbidden tokens are hashes, not literals

A test file that lists the real names and figures *as a Python string* to
forbid them **is itself the leak** -- exactly the failure mode DP#15
exists to prevent, just moved into the guard instead of the fixture. Three
designs were considered:

1. **Untracked, gitignored local token file.** Clean in principle, but a
   check that depends on a file which by definition does not exist in CI
   either always skips there (an enforcement gap disguised as an
   enforcement mechanism -- precisely today's bug, relocated) or requires
   provisioning a secret into every CI runner, which is more moving parts
   than this problem needs.
2. **Tokens fetched from a private "inputs" repo at test time.** Same
   shape of problem: it makes the guard's very ability to run depend on
   an external resource being present, and "skip loudly if absent" still
   means the one channel that must never go quiet (CI, on every PR) is
   the one most likely to not have that resource wired up.
3. **Salted hash comparison, self-contained, always runs.** Chosen. The
   token list below is SHA-256 hashes (with a fixed, non-secret salt --
   the salt defeats generic rainbow-table lookups of the bare token, it
   does not need to be secret itself) of the normalized real tokens. No
   forbidden token appears in plaintext anywhere in this file's content --
   not in code, not in a comment, not as a docstring "example" -- and
   because the file is tracked and deliberately not allowlisted, the guard
   enforces that against itself on every run rather than merely asserting
   it in prose. (An earlier draft of this very file *did* assert it in
   prose while violating it; see the postscript.) The check runs
   unconditionally, in every CI job, on every file, with no external
   dependency and therefore no way to silently stop running.

This is a real security trade-off, stated plainly: a hash of a short,
guessable string (a first name, a 6-digit dollar figure) is not resistant
to a determined offline dictionary attack by someone who *already has
repository access* -- salting only defeats precomputed rainbow tables, not
a targeted brute force. That is an acceptable trade here because the
threat this guard closes is not "a sophisticated adversary with repo
access reverses our hashes" -- it is "the plaintext sits in a tracked file
where `grep`, GitHub code search, and every LLM agent that reads this repo
for context ingest it directly," which is the concrete harm #716 found
(`.pi/agents/issue-analyzer.md` was handing real income and RRSP-room
figures to every agent as standing context). Hashing fully closes that
channel. It does not, and cannot, make committing personal data to a
private repo's history safe -- DP#15 forbids doing that in the first
place, hashes or not.

An untracked local file (option 1) is still supported as a *supplemental*,
optional source for extending the token list without a code change -- see
`_load_supplemental_tokens()` -- but it is never load-bearing for CI: the
hashes below are the enforcement mechanism, and `test_supplemental_local_token_source_if_present`
is a separate, allowed-to-skip test that only reports whether the optional
file was used, never a substitute for the main check.

## Why bare years and round salaries are NOT in the forbidden set

DESIGN_PRINCIPLES.md #15 itself says fabricated round figures in test data
are fine; DP#4 asks for role-based names, not that every plausible number
disappear. A repo-wide `grep` before writing this test confirmed
`250000`, `72000`, `1979`, and `1980` are each used as generic, role-based
fixture data (`'role': 'primary', 'birth_year': 1979, ...`) in well over a
hundred unrelated call sites across `tests/` and `countries/canada/tests/`,
including as literal *default parameter values* in production code
(`countries/canada/retirement.py`'s `MemberRetirementData.birth_year`,
`countries/canada/claiming_age_optimizer.py`'s `optimize_claiming_ages`).
Forbidding those bare numbers here would either break the suite outright
or force this guard into a permanently-maintained exception list the size
of the test suite -- exactly the false-positive trap DP#15's own text
warns against. The actual violation was never "1979 appears in a test";
it was "1979 appears two words away from a real name in a docstring." Once
the name half is removed (which this guard *does* enforce, unconditionally,
everywhere), a bare `1979` is indistinguishable from the fabricated
default it already is throughout this codebase, and stops being personal
data for this test's purposes. What *is* hashed and forbidden outright is
the small set of six-figure registered-account contribution-room amounts
that were uniquely tied to a real name, appeared nowhere else in the tree,
and could only have come from this household's notices of assessment.
Those numbers are not quoted here -- they exist in this file only as the
hashes in `FORBIDDEN_HASHES` below, which is the entire point. One further
figure from the same sweep was tried and dropped when it turned out to
collide with an unrelated, legitimate computed total elsewhere in the test
suite -- see the comment next to `FORBIDDEN_HASHES` for that finding.

(That said: a production dataclass shipping a real person's birth year as
its *default* value, rather than DP#13's "clearly-dated placeholder"
pattern used elsewhere in this same codebase for exactly this purpose --
see `LSIFPurchase.birth_year = 2000` in `countries/canada/lsif_credit.py`
-- is its own smell, arguably worse than a test fixture because it's a
silent, unconditional fallback in library code. It is out of scope for
this guard, and out of scope for #716, because changing it touches
dozens of call sites relying on the current default and needs its own
verified change, not a name-swap. Filed for a human to triage.)

## Postscript: what this guard caught on its first run

Recorded here because it is the evidence that the enforcement loop is
actually closed, and because it is a better argument for this test than
anything asserted above it.

The first CI run of this guard failed, on two independent findings:

1. **This file.** The hashing was right; the *prose explaining the
   hashing* was not. The module docstring quoted the two real
   contribution-room figures outright while claiming, two paragraphs
   earlier, that "no plaintext personal data is stored anywhere in this
   file"; the `_NUMBER_RE` comments used a real figure as the regex's
   worked example three times; and `_normalize`'s docstring spelled out
   the spouse's real given name, twice, to illustrate accent-stripping.
   The author walked directly into the trap this very docstring names --
   "a test file that lists the real names and figures ... IS the data" --
   while writing the sentence that names it. Every example in this file is
   now fabricated. Note that the guard did not catch this while the file
   was still untracked (`iter_tracked_files()` is `git ls-files`); it
   caught it the moment the file was committed, which is the moment it
   started to matter.

2. **A file written by a different agent, an hour later**, and already
   merged to `main` on another PR: an ops runbook containing
   `loginctl enable-linger <username>` with the maintainer's literal
   OS username. That is the genuine hard case for this guard -- the same
   string is simultaneously a legitimate ops detail and the "primary
   earner" name that must never sit beside a financial figure. It was
   fixed in the *doc* (`$USER`, which is better documentation anyway),
   not by weakening the guard: the narrow rule (`/home/...` paths are ops
   and are stripped; bare names are not) is correct, and broadening it
   into a general username exemption would punch a hole exactly where
   this guard is most needed.

Both findings are the point. A detector that never fires on its own
authors is a detector nobody has tested.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from typing import FrozenSet, List, Tuple

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Not secret -- defeats generic precomputed rainbow tables of bare tokens,
# nothing more. See the module docstring for the threat model this does
# and does not cover.
_SALT = "lifedraft-dp15-guard-v1"

# SHA-256(_SALT + normalized_token) for every real name and every
# real, uniquely-identifying financial figure found by the #716 sweep.
# No forbidden token appears in plaintext anywhere in this file -- not in
# the code, not in a comment, and not in a docstring "example". That is a
# property this file's own guard now enforces against itself on every CI
# run (it is a tracked file like any other, and it is NOT allowlisted);
# it is not merely a promise in a comment. See the module docstring for
# why hashing was chosen over a gitignored token file, and the postscript
# there for what happened the first time this claim was tested.
#
# Scope, stated honestly: this guard governs the *working tree*. It cannot
# retroactively purge the pre-scrub plaintext that already exists in the
# repository's git history, and it does not pretend to -- that would take a
# history rewrite of a shared repo, which is out of scope for #716. What it
# does guarantee is that no *new* commit reintroduces it.
FORBIDDEN_HASHES: FrozenSet[str] = frozenset({
    "8e23fb22cfe953be68b39513ee749ef1a4558d306b294205d77d275ce1e7b7bf",  # name (primary earner, given name)
    "c3ceca128d8f708e83de12d7c2957b63023587805335e2d7ddb94bd04eb3b007",  # name (spouse, given name; accent-stripped form)
    "699941c784225e5a05171a86be17446e284d15cebe41e4fba770103b54781cdc",  # name (child, given name)
    "746305879bcb1ee9e23f9e071e9534aea9fd44f4e621481ef0c4d5c0e320ffb2",  # name (child, given name)
    "7e4432729947dcb538c9e409aaf7aeaf8a66fa2f10faeba258a8104e450be33e",  # name (child, given name; accent-stripped form)
    "b30e7d81ce231095cc648d96bfe80fd4124fe1c1b209a238a045208f32528e7e",  # name (household surname)
    "fb028038c3bce60e8de99c1885adf153cc20709e0a1164633224a395c3968169",  # RRSP room figure, unique to this household
    "7848750befe7d8bdf049e4719b251ef1c8e2e3eb5e44245096446845c9198fc3",  # RRSP room figure, unique to this household
    # A third figure (a real TFSA-room number from the same #716 sweep) was
    # tried here and dropped: it collided with an unrelated, legitimate
    # computed total in tests/test_partial_features.py (a retirement
    # drawdown scenario summing to the same six-digit number by
    # coincidence). That is exactly the false-positive DP#15's own text
    # warns against, so the number was removed from this set -- the name
    # half of that original violation is still fully covered by the name
    # hashes above, unconditionally, everywhere.
})

# Files that legitimately contain the maintainer's real name as public
# package/author metadata -- not financial or personal-life data under
# DP#15's own definition ("incomes, account balances, RRSP room, mortgage
# details, children's names and birth years"). Same category as a git
# commit's `Author:` trailer, which this guard also never sees (it scans
# file content, not git history) and would not flag if it did (see the
# _AUTHOR_TRAILER_RE strip below). Keep this list short and explicit --
# it is a code-reviewable allowlist, not a silent carve-out.
_AUTHOR_METADATA_FILES = frozenset({"pyproject.toml"})

# Stripped out of each line before token extraction so that legitimate,
# non-personal content doesn't get tokenized in the first place:
#   - POSIX home-directory paths (`/home/<user>/...`, and `~/...`
#     shorthand). Tracked operational guidance legitimately contains a
#     home path with the maintainer's account name in it; a filesystem
#     path is an ops detail, not a financial record.
#   - git trailer-style attribution lines (Co-Authored-By / Signed-off-by
#     / Author:), in case any ever land in a tracked file (changelog,
#     vendored patch, etc.).
#
# NOTE the deliberate narrowness: this strips names inside *paths*, and
# nothing else. It is emphatically NOT a general "the maintainer's username
# is always allowed" exemption -- that would punch a hole exactly where this
# guard matters most (a real name sitting next to a real figure). A bare
# username outside a path is still a violation, and the guard's first CI run
# proved that rule earns its keep by catching one in an ops runbook (see the
# module docstring's postscript). Fix such a doc with `$USER`; do not widen
# this regex.
_HOME_PATH_RE = re.compile(r"(?:/home/|~/)\S*")
_AUTHOR_TRAILER_RE = re.compile(
    r"^\s*(Co-Authored-By|Signed-off-by|Author)\s*:.*$", re.IGNORECASE | re.MULTILINE
)

# Candidate tokens: unicode letter-runs of length >= 3 (names), and
# numbers in plain, comma-grouped, or `k`-suffixed form (financial
# figures). Over-extraction here is harmless by design -- an ordinary
# word or an unrelated number only becomes a hit if its hash happens to
# collide with one of the hashes above, which is not a real risk.
# Under-extraction is the actual failure mode to avoid, so this is
# deliberately generous.
#
# Every worked example in the comments below uses a FABRICATED number
# (`432,100`) -- not one of the real forbidden figures. Writing the real
# figure here "just to illustrate the regex" is precisely the leak this
# module exists to stop, and is exactly the mistake this file's first CI
# run caught its own author making (see the module docstring's postscript).
_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_NUMBER_RE = re.compile(
    r"\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?"  # 432,100 / $432,100
    r"|\$?\d{4,7}(?:\.\d+)?"            # 432100 / $432100
    r"|\b\d{1,4}[kK]\b"                 # 432k
)


def _normalize(text: str) -> str:
    """Lowercase + strip diacritics (NFKD decompose, drop combining marks)
    so that an accented name and its unaccented spelling collapse to the
    same normalized form, and therefore to the same hash -- e.g. the
    fabricated pair 'Renée' and 'Renee' both normalize to 'renee'. (The
    example is fabricated on purpose: the real accented name this matters
    for is exactly the kind of token this file must never spell out.)"""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _hash(normalized_token: str) -> str:
    return hashlib.sha256((_SALT + normalized_token).encode("utf-8")).hexdigest()


def _normalize_number(raw: str) -> str:
    raw = raw.strip().lstrip("$")
    if raw[-1] in "kK":
        return str(int(float(raw[:-1]) * 1000))
    return str(int(raw.replace(",", "").split(".")[0]))


def iter_tracked_files(root: str = ROOT) -> List[str]:
    """Every file `git` tracks in this repo -- source, tests, docs, `.pi/`,
    CI workflows, everything. Deliberately not a hand-maintained directory
    walk: a file that's tracked but sits outside whatever list someone
    remembered to enumerate is exactly how #716 happened."""
    out = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line]


def _read_text(root: str, relpath: str) -> str | None:
    full = os.path.join(root, relpath)
    try:
        with open(full, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\x00" in raw:
        return None  # binary
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("latin-1")
        except UnicodeDecodeError:
            return None


@dataclass(frozen=True)
class Hit:
    file: str
    line: int
    matched_hash_prefix: str  # first 12 hex chars only -- enough to correlate
    # a re-run without printing the token itself or enough hash to look
    # meaningfully closer to reversible than the full hash already is.


def scan_text(text: str, relpath: str = "<text>") -> List[Hit]:
    """Return every forbidden-token hit in `text`. Never returns or logs
    the matched plaintext -- only file, line, and a hash prefix -- so a
    failing CI log doesn't become a second place the data leaked to."""
    hits: List[Hit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = _AUTHOR_TRAILER_RE.sub("", line)
        line = _HOME_PATH_RE.sub(" ", line)
        normalized_line = _normalize(line)

        candidates = set(_WORD_RE.findall(normalized_line))
        for raw_num in _NUMBER_RE.findall(line):
            try:
                candidates.add(_normalize_number(raw_num))
            except (ValueError, IndexError):
                continue

        for token in candidates:
            digest = _hash(token)
            if digest in FORBIDDEN_HASHES:
                hits.append(Hit(relpath, lineno, digest[:12]))
    return hits


def _load_supplemental_tokens(root: str = ROOT) -> Tuple[FrozenSet[str], str | None]:
    """Optional extension point: an untracked, gitignored local file
    (one raw token per line) whose hashes get OR'd into the forbidden
    set for this run only. Never required, never committed, and its
    absence never skips the main guard -- see the module docstring for
    why this is supplemental rather than load-bearing."""
    path = os.environ.get(
        "DP15_LOCAL_TOKENS_FILE", os.path.join(root, ".dp15_local_tokens.txt")
    )
    if not os.path.isfile(path):
        return frozenset(), None
    with open(path, encoding="utf-8") as f:
        tokens = [line.strip() for line in f if line.strip()]
    return frozenset(_hash(_normalize(t)) for t in tokens), path


# ═══════════════════════════════════════════════════════════════════════════
# The guard. Must run in every CI job, on every tracked file, unconditionally.
# ═══════════════════════════════════════════════════════════════════════════

def test_no_forbidden_personal_data_in_tracked_files():
    supplemental_hashes, supplemental_path = _load_supplemental_tokens()
    active_hashes = FORBIDDEN_HASHES | supplemental_hashes
    if supplemental_path:
        print(f"DP#15 guard: also enforcing tokens loaded from {supplemental_path}")

    all_hits: List[Hit] = []
    for relpath in iter_tracked_files():
        if relpath in _AUTHOR_METADATA_FILES:
            continue
        text = _read_text(ROOT, relpath)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            line = _AUTHOR_TRAILER_RE.sub("", line)
            line = _HOME_PATH_RE.sub(" ", line)
            normalized_line = _normalize(line)
            candidates = set(_WORD_RE.findall(normalized_line))
            for raw_num in _NUMBER_RE.findall(line):
                try:
                    candidates.add(_normalize_number(raw_num))
                except (ValueError, IndexError):
                    continue
            for token in candidates:
                digest = _hash(token)
                if digest in active_hashes:
                    all_hits.append(Hit(relpath, lineno, digest[:12]))

    if all_hits:
        lines = "\n".join(
            f"  {h.file}:{h.line}  (matched hash {h.matched_hash_prefix}...)"
            for h in all_hits
        )
        raise AssertionError(
            "DP#15 violation: a forbidden personal-data token was found in a "
            "tracked file. The matched plaintext is intentionally not printed "
            "here (see this module's docstring) -- open the file at the line "
            "below and remove or replace the real name/figure with a "
            "role-based name (DP#4) or a fabricated round number:\n" + lines
        )


def test_supplemental_local_token_source_if_present():
    """Reports (and, if present, enforces) an optional local extension of
    the token list. Allowed to skip -- unlike the main guard above, this
    is never the only thing standing between personal data and version
    control, so its absence is not a silent enforcement gap."""
    _, path = _load_supplemental_tokens()
    if path is None:
        pytest.skip(
            "No local supplemental token file "
            f"(${{DP15_LOCAL_TOKENS_FILE:-{os.path.join(ROOT, '.dp15_local_tokens.txt')}}}) "
            "present -- this is expected in CI and on a fresh clone. The "
            "hashed token set in FORBIDDEN_HASHES is enforced unconditionally "
            "regardless of this file; see test_no_forbidden_personal_data_in_tracked_files."
        )
    print(f"DP#15 guard: supplemental token file present at {path}")


# ═══════════════════════════════════════════════════════════════════════════
# Self-tests: prove the mechanism works using synthetic, fabricated tokens
# only. None of these embed real personal data -- that would recreate the
# exact leak this module exists to prevent.
# ═══════════════════════════════════════════════════════════════════════════

_CANARY_TOKEN = "zzqqnotrealpiicanarytoken"  # letters only -- matches _WORD_RE's token shape
_CANARY_HASH = _hash(_normalize(_CANARY_TOKEN))


def _scan_with_canary(text: str) -> List[Hit]:
    hits = scan_text(text)
    # scan_text() only checks the real FORBIDDEN_HASHES; re-check against
    # the canary hash the same way the real guard checks supplemental
    # hashes, to prove the extraction -> normalize -> hash -> compare
    # pipeline actually works end to end.
    extra: List[Hit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        normalized_line = _normalize(line)
        for token in _WORD_RE.findall(normalized_line):
            if _hash(token) == _CANARY_HASH:
                extra.append(Hit("<canary-text>", lineno, _CANARY_HASH[:12]))
    return hits + extra


class TestDetectorMechanism:
    def test_canary_token_is_detected(self):
        text = f"some unrelated prose mentioning the {_CANARY_TOKEN} word in passing"
        hits = _scan_with_canary(text)
        assert hits, "the detector failed to catch its own synthetic canary token"

    def test_ordinary_prose_is_not_flagged(self):
        text = (
            "The optimizer explores mortgage renewal scenarios for a "
            "primary earner and spouse with role-based fixtures."
        )
        assert scan_text(text) == []

    def test_home_path_strip_removes_whatever_username_it_contains(self):
        """A home path must be stripped *before* tokenizing, so that the
        account name inside it never reaches the hash comparison.

        Asserted against `_HOME_PATH_RE` directly, using a fabricated
        username, rather than by feeding `scan_text` a real name and
        checking it comes back clean. That latter (tempting) formulation is
        a test that cannot fail for the right reason: written with a
        fabricated username it passes even if the strip is deleted outright
        (the fabricated name is not a forbidden token, so nothing would flag
        it either way), and written with a *real* username it turns this
        file into the very leak it exists to prevent. Testing the mechanism
        instead of the token gets real coverage with neither compromise.
        """
        for line in (
            "Never `find /home/example-user`; it is too large.",
            "Runner lives under ~/actions-runner/lifedraft/",
            "See /home/example-user/Source/repo/file.py for the fixture.",
        ):
            stripped = _HOME_PATH_RE.sub(" ", line)
            assert "example-user" not in stripped
            assert "actions-runner" not in stripped

    def test_bare_username_outside_a_path_is_still_tokenized(self):
        """The path strip must NOT become a general username exemption.

        A bare account name outside a filesystem path (e.g. an ops runbook's
        `loginctl enable-linger <user>`) must survive the strip and reach the
        hash comparison -- that is exactly the shape this guard caught in a
        merged doc on its first CI run. Uses a fabricated username and
        asserts on the *extraction*, not on a forbidden-hash hit, so the
        test proves the token is still reachable without naming anyone.
        """
        line = "loginctl enable-linger exampleuser"
        stripped = _HOME_PATH_RE.sub(" ", line)
        assert "exampleuser" in stripped, "bare username must not be stripped"
        assert "exampleuser" in _WORD_RE.findall(_normalize(stripped)), \
            "a bare username must still be extracted as a candidate token"

    def test_round_salary_figures_are_not_flagged(self):
        # DP#15's own text: fabricated round figures in fixtures are fine.
        # These specific numbers are reused as generic fixture data well
        # over a hundred times across the suite -- see module docstring.
        text = "gross_income=250000, spouse_income=72000, birth_year=1979"
        assert scan_text(text) == []

    def test_author_metadata_file_is_exempted(self):
        assert "pyproject.toml" in _AUTHOR_METADATA_FILES

    def test_supplemental_loader_returns_empty_when_file_absent(self):
        hashes, path = _load_supplemental_tokens(root="/nonexistent-root-for-test")
        assert hashes == frozenset()
        assert path is None


# ═══════════════════════════════════════════════════════════════════════════
# The actual repo scan, so a regression is caught locally with a clear
# reproduction command rather than only in CI.
# ═══════════════════════════════════════════════════════════════════════════

def test_tracked_file_list_is_nonempty():
    """Sanity check on the scan mechanism itself: if `git ls-files` ever
    returned nothing (e.g. run outside a git checkout), the main guard
    above would trivially "pass" by scanning zero files -- a silent
    no-op that is exactly the kind of false green this codebase exists
    to prevent (AGENTS.md). Fail loudly instead."""
    assert len(iter_tracked_files()) > 100
