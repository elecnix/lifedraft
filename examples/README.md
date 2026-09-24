# Runnable, research-backed examples

Each example reproduces a household-level scenario from a published study
through the engine, and CI keeps it honest. The first external source is CFFP
(Chaire de recherche en fiscalité et en finances publiques, Université de
Sherbrooke). Examples are contributed one PR at a time. The layout, the
generator and the CI guard are strict so that a contribution cannot drift from
the engine or fake its output (issue #300).

`lifedraft/minimal-two-adult/` is the scaffold's seed. It reproduces no
publication; see its README for why its `AGREES` verdict is vacuous.

For household scenarios that are *not* tied to a publication, see
[`docs/community-scenarios/`](../docs/community-scenarios/).

## Layout

```
examples/
  README.md                 this guide (the only file allowed outside an example)
  <source>/                 lowercase-hyphenated, e.g. cffp, lifedraft
    <slug>/                 lowercase-hyphenated, one per scenario
      input.json            the household, as a contract document
      report.json           projected from `optimize.py --json`   (generated)
      report.md             `optimize.py --md` output             (generated)
      README.md             source, claim, encoding, comparison, verdict
      meta.json             machine-readable source and verdict
```

An example directory holds **exactly** these five files: no more (not even a
gitignored `full.json`), no fewer. Every directory two levels below
`examples/` is an example. Discovery walks directories, so an example missing a
file is still collected and fails; it never silently drops out. A source
directory with no example in it, a stray file, or a name that is not
lowercase-hyphenated fails collection.

| file | cap |
|---|---|
| `input.json` | 200,000 B |
| `report.json` | 200,000 B |
| `report.md` | 100,000 B (trimmed at a `## ` boundary with a visible marker when larger) |
| `README.md` | 50,000 B |
| `meta.json` | 4,000 B |

### `input.json`

A contract document (see the repo README and `schema/input_schema.json`). It must
pass `input_contract.load_and_map`. A refusal is a finding: if the publication's
household cannot be expressed, file the engine issue and say so in the verdict.
Do not bend the household until it loads. **DP#15/DP#4:** fabricated round
numbers and role-based ids (`p1`, `p2`, `ca`) only, even when the publication
uses a named persona. No home paths, no e-mail addresses, no SIN-shaped numbers:
the guard scans for all three.

`.gitignore` ignores `input.json` and `*.json` everywhere (DP#15). The
negations `!examples/*/*/input.json`, `!examples/*/*/report.json` and
`!examples/*/*/meta.json` let the three JSON files of an example be committed.
The guard runs `git check-ignore` on every file, so an example whose files git
would silently skip fails.

### `meta.json`

The key set must be **exactly** this set:

| key | type | rule |
|---|---|---|
| `source` | string | equals the `<source>` directory name |
| `source_url` | string | starts with `https://`; also quoted literally in the README's Source section |
| `publication_id` | string | non-empty; the publisher's own identifier when it has one |
| `title` | string | non-empty |
| `authors` | list of strings | non-empty |
| `year` | integer | 1900..2100 |
| `retrieval_date` | string | ISO date (`YYYY-MM-DD`) the source was read |
| `verdict` | string | the verdict grammar below |
| `linked_issues` | list of integers | distinct, each >= 1; must contain N for `DIFFERS (engine issue #N)` |

### `README.md`

Five `## ` sections, each exactly once, none empty:

1. `## Source`: title, authors, year, and the URL (the same string as
   `meta.json` `source_url`).
2. `## Publication claim`: what the publication says about this household.
3. `## Encoding`: how the claim is encoded in `input.json`, field by field. It
   must name every top-level key of `input.json` in backticks (`` `people` ``,
   `` `accounts` `` and so on).
4. `## Engine vs publication`: what the engine concluded and what the
   publication concluded. Point at `report.json` fields by name
   (`scenarios[0].strategy`, `winner_year_by_year`) rather than restating
   figures that go stale.
5. `## Verdict`: its first non-blank line is **exactly** one of:

   ```
   AGREES
   DIFFERS (explained)
   DIFFERS (engine issue #N)
   ```

   and equals `meta.json` `verdict`. `DIFFERS (explained)` must be followed by
   the explanation (for example, the publication rounds, or uses another tax
   year). `DIFFERS (engine issue #N)` means the engine is wrong or missing a
   rule: **file the issue first**, then list N in `linked_issues`. N is
   checked for format offline, not looked up on GitHub.

## Contribution flow

1. `mkdir -p examples/<source>/<slug>` (lowercase-hyphenated names).
2. Write `input.json` (fabricated round numbers, role-based ids) and check it
   with `python -c "import input_contract; input_contract.load_and_map('examples/<source>/<slug>/input.json')"`.
3. Generate the reports, **under Python 3.12** (see below):

   ```sh
   python tools/examples.py regen examples/<source>/<slug>
   ```

4. Write `README.md` and `meta.json` from what the reports say.
5. Run the guard for your example:

   ```sh
   python -m pytest -q tests/test_examples_guard.py -k <slug>
   ```

**Never hand-edit `report.json` or `report.md`.** The guard regenerates them
through the real `optimize.py` and compares bytes, so a hand-edited number fails
CI with a diff and the instruction to run `tools/examples.py regen`.

When an engine change moves an example's reports, the regen guard fails on
that PR. Run `python tools/examples.py regen` (no path regenerates every
example), commit the new reports, and **explain the move in the PR**. If a
README quotes a figure that moved, update that README in the same PR; the guard
cannot check prose, so this rule is on the author and the reviewer.

## The generator and the projection

`tools/examples.py regen [<path>...] [--workers N]` runs
`optimize.py --input <example>/input.json --json … --md …` in a subprocess. The
subprocess runs in a scratch directory with an **empty `HOME`**, so a local
`$HOME/.cache/lifedraft/tax` cache cannot leak into a committed report. The CLI
refuses an exit code other than 0, and also an exit 0 that wrote no output.

`report.md` is the engine's own `--md` output, byte for byte, unless it is over
its cap. `report.json` is `project_report(full)` of the ~13 MB `--json` output,
and its contract is documented in the `tools/examples.py` docstring:

- `projection`: version, function, source, and the **byte size and typed
  digest of the full report** (`full_report_digest`, written `sha256:<hex>`).
  Byte-identity of `report.json` therefore also pins every byte of the
  unprojected output. The digest is typed, never a bare hex string, because
  CI's secret scan (detect-secrets) flags any quoted all-hex string as a
  "Hex High Entropy String", and the digest changes on every regen. The
  static check refuses a bare hex string of 16+ characters in any example
  JSON file; fix the shape, never add it to `.secrets.baseline`.
- `title`, `situation`, `model_fidelity`, `optimal_refi_level`,
  `resp_cashout`, `equity_grants`, `runway`, `runway_sweep`,
  `asset_location`, `total_scenarios`: copied verbatim.
- `scenarios`: every scenario in the **engine's** rank order (never re-sorted),
  with `rank` and every scalar field, unrounded. A scenario's label is its
  `strategy`, and its terminal assets are `future_value` (equal to the last
  `total_assets` of its year-by-year series). The engine has no field literally
  named "label" or "terminal assets" on a scenario.
- `category_bests`: scalar fields only.
- `winner_year_by_year`: the #1 scenario's rows restricted to `year`,
  `total_assets`, `net_benefit`, `total_debt`, `mortgage_balance`,
  `heloc_balance`, `total_rrsp`, `total_tfsa`, `non_reg_balance`,
  `resp_balance`, `total_family_income`, `after_tax_income`,
  `drawdown_income`, `living_costs`, `oas_clawback`, `ruined`.

A top-level key the engine adds or drops, or a missing year column, makes
projection refuse loudly. **Any change to the projection bumps
`PROJECTION_VERSION` and regenerates every example in the same PR.**

### Python version

The reports are canonical under **Python 3.12**, the version CI's PR leg runs.
Measured on the seed: under 3.10 and 3.11 about 16 `report.json` floats differ
by one unit in the last place (CPython 3.12 made `sum()` of floats
compensated). `report.md` is identical, and numpy/scipy versions made no
difference. So `regen` refuses to run under another minor version. On the
nightly 3.10/3.11 legs the guard compares `report.md` bytes and `report.json`
values with a 1e-9 relative float tolerance, leaving out the full-report
hash. On 3.12 it compares bytes.

## The CI guard

`tests/test_examples_guard.py` runs two tests per example (ids
`<source>/<slug>`), and xdist spreads them across workers:

- `test_example_static_contract`: the file set, caps and git-ignore state;
  `load_and_map`; the `meta.json` schema; the README sections, source URL,
  encoding keys and verdict; the personal-data scan.
- `test_example_regenerates_byte_identical`: regenerates through the real
  engine and compares against the committed reports. It never writes.

If discovery finds no example (for instance, `examples/` was renamed), the guard
raises `ExamplesError` at collection, so the whole pytest run errors instead of
reporting a green "skipped".

**Cost.** The regen test runs the full optimizer serially (`--workers 1`).
Measured for the seed on the maintainer's workstation with `--durations`: 36.2 s
when run alone (36.2 s user CPU, 252 MB peak RSS; repeat runs took 25 to 33 s),
and 48.4 s inside a full `-n 4` suite run, where it competes for CPU. The static
test takes under 1 s. Cost grows linearly, about 30 to 50 CPU-s per example, so
N examples add roughly N × 40 / W seconds of wall time on a W-worker CI run
(W = 4 on the hosted runner). Past about 20 examples, decide deliberately
whether to shard them or move them to a separate job; do not let them grow
silently.
