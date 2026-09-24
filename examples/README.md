# Runnable, research-backed examples

Each example reproduces a household-level scenario from a published study
through the engine, and CI keeps it honest. The first external source is CFFP
(Chaire de recherche en fiscalité et en finances publiques, Université de
Sherbrooke). Examples are contributed one PR at a time. The layout, the
generator and the CI guard are strict so that a contribution cannot drift from
the engine or fake its output (issue #300).

`lifedraft/minimal-two-adult/` is the scaffold's seed and the `optimize`-mode
example; `lifedraft/single-run-two-adult/` is its single-point twin and the
`simulate`-mode example (see [Modes](#modes)). Neither reproduces a
publication; see their READMEs for why their `AGREES` verdicts are vacuous.

For household scenarios that are *not* tied to a publication, see
[`docs/community-scenarios/`](../docs/community-scenarios/).

## Layout

```
examples/
  README.md                 this guide (the only file allowed outside an example)
  <source>/                 lowercase-hyphenated, e.g. cffp, lifedraft
    <slug>/                 lowercase-hyphenated, one per scenario
      input.json            the household, as a contract document
      report.json           projected from `optimize.py --json` or one
                            `FamilySimulation.run()`, per mode    (generated)
      report.md             `optimize.py --md` output, or the render of
                            report.json in simulate mode          (generated)
      README.md             source, claim, encoding, comparison, verdict
      meta.json             machine-readable source, verdict and mode
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
| `mode` | string | **required, no default**: exactly `simulate` or `optimize` (case-sensitive; see [Modes](#modes)). A missing or unknown value fails the static contract, and `regen` refuses before running any engine (DP#32) |
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
   publication concluded. Point at `report.json` fields by name rather than
   restating figures that go stale: in optimize mode `scenarios[0].strategy`,
   `winner_year_by_year`; in simulate mode `terminal.total_assets`, `series`,
   `run`. In simulate mode, `series[*].year` is a 1-indexed offset from
   `run.start_year`, not a calendar year, and `terminal.net_benefit` is a
   field default the single run never computes: do not cite it.
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

## Modes

Every example declares in `meta.json` how its reports are regenerated. There
is no default: an example that does not say is refused (DP#32, and DP#9: no
shim for older examples).

- **`simulate`**: one household, one computed outcome (a life transition, the
  effect of a tax measure, a pension projection). `regen` runs one
  `FamilySimulation.run()` of the declared household, about 1 s. Use this
  unless the claim is a ranking.
- **`optimize`**: the claim is a strategy ranking. `regen` runs the full
  `optimize.py` (hundreds of simulations, 10 to 70 s per example).

**What `simulate` refuses.** A single run cannot honour a declared sweep: the
contract maps `candidate_ages[0]` and drops the other candidates, and
`FamilySimulation` never reads the contribution-strategy, income, RESP,
estate-election or mortgage option lists (measured on the single-run example:
declaring one candidate of each left every year's output identical). So in
simulate mode, `tools/examples.py simulate_input_problems` refuses, in the
static contract and again before `regen` spawns anything:

- a `decisions.retirement_age` entry whose `candidate_ages` is not exactly one
  age;
- a non-empty `contribution_strategy`, `income`, `resp_action`,
  `estate_elections`, `deposit_products` or `borrow_to_invest`;
- a non-empty `decisions.mortgage` option list (`refinance_options`,
  `renewal_options`, `structure_options`);
- `decisions.objective` or `decisions.superficial_loss` being present;
- a non-empty `sensitivity.presets` or `sensitivity.sweeps` (write
  `{"presets": {}, "sweeps": {}}`; `sensitivity` is schema-required);
- a `funding_options` list anywhere (an optimizer-ranked property purchase);
- any `decisions`, `decisions.mortgage` or `sensitivity` key the table
  `SIMULATE_DECISIONS` does not classify. A unit test fails when the schema
  grows a decisions property nobody classified.

A single run takes no strategy or rate path from the document: the engine
chooses them itself (DP#16 auto-detection), and `report.json` records what it
chose under `run`. Say so in the README's Encoding section.

## Contribution flow

1. `mkdir -p examples/<source>/<slug>` (lowercase-hyphenated names), and
   decide the mode: `simulate` unless the claim is a ranking.
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
through the real engine, in the example's mode, and compares bytes, so a
hand-edited number fails CI with a diff and the instruction to run
`tools/examples.py regen`.

When an engine change moves an example's reports, the regen guard fails on
that PR. Run `python tools/examples.py regen` (no path regenerates every
example), commit the new reports, and **explain the move in the PR**. If a
README quotes a figure that moved, update that README in the same PR; the guard
cannot check prose, so this rule is on the author and the reviewer.

## The generator and the projection

`tools/examples.py regen [<path>...] [--workers N]` reads each example's
`mode` first and runs the engine in a subprocess:

- optimize: `optimize.py --input <example>/input.json --json … --md …`
  (`--workers` applies here only);
- simulate: `tools/examples.py simulate-once --input <example>/input.json --out …`,
  which runs `input_contract.load_and_map -> SimulationConfig.from_dict ->
  FamilySimulation(config, adapter=CanadaAdapter(config)).run()` once, with no
  override, and writes the full result as canonical JSON.

Either subprocess runs in a scratch directory with an **empty `HOME`**, so a
local `$HOME/.cache/lifedraft/tax` cache cannot leak into a committed report.
The CLI refuses an exit code other than 0, and also an exit 0 that wrote no
output.

### Optimize projection

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

### Simulate projection

`report.json` is `project_simulation(full)` (`SIMULATE_PROJECTION_VERSION` =
1) of the `simulate-once` document, whose top-level keys must be exactly
`engine_entry`, `run` and `year_by_year` (anything else refuses):

- `projection`: version, function, source (`FamilySimulation.run()`),
  `series_columns`, and the byte size and typed digest
  (`full_report_digest`, `sha256:<hex>`) of the full document, which holds
  every field of every `YearResult`. Byte-identity of `report.json` therefore
  pins all of it.
- `engine_entry`: the engine chain, as text. `run`: read from the engine
  objects, not recomputed: `strategy`, `rate_path`, `use_readvanceable`,
  `deduct_later`, `start_year`, `projection_years`, and each adult's mapped
  `retirement_age`.
- `series`: every year in engine order, restricted to `series_columns` (the
  optimize key columns without `net_benefit`, which the single run never
  computes, plus `cpp_income`, `oas_income`, `gis_income`, `lif_balance`,
  `lira_balance`). A missing column refuses.
- `terminal`: every scalar field of the **last** year, unrounded. `years`: the
  row count (an empty series refuses).

`report.md` is `render_simulation_markdown(report.json)`: the run block, the
terminal year and the series, floats rounded to cents. Nothing is computed
there that report.json does not hold. **Any change to this projection bumps
`SIMULATE_PROJECTION_VERSION` and regenerates every simulate example in the
same PR.**

### Python version

The reports are canonical under **Python 3.12**, the version CI's PR leg runs.
Measured on the seed: under 3.10 and 3.11 about 16 `report.json` floats differ
by one unit in the last place (CPython 3.12 made `sum()` of floats
compensated). `report.md` is identical, and numpy/scipy versions made no
difference. So `regen` refuses to run under another minor version. On the
nightly 3.10/3.11 legs the guard compares `report.md` bytes and `report.json`
values with a 1e-9 relative float tolerance, leaving out the full-report
hash. On 3.12 it compares bytes. Both modes follow this rule: `simulate-once`
itself runs under any version (the nightly legs spawn it), and only `regen`
refuses. Simulate `report.md` rounds floats to cents and is byte-compared on
the nightly legs too, so a last-place drift that happens to cross a rounding
boundary would fail a nightly leg loudly; that is accepted, never tolerated
away.

## The CI guard

`tests/test_examples_guard.py` runs two tests per example (ids
`<source>/<slug>`), and xdist spreads them across workers:

- `test_example_static_contract`: the file set, caps and git-ignore state;
  `load_and_map`; the `meta.json` schema, including `mode`; in simulate mode,
  the sweep refusals above; the README sections, source URL, encoding keys and
  verdict; the personal-data scan.
- `test_example_regenerates_byte_identical`: regenerates through the real
  engine, in the example's mode, and compares against the committed reports.
  It never writes.

If discovery finds no example (for instance, `examples/` was renamed), the guard
raises `ExamplesError` at collection, so the whole pytest run errors instead of
reporting a green "skipped".

`test_every_mode_has_a_committed_example` fails unless at least one committed
example exercises each mode, so both regeneration paths meet the real engine
on every CI run.

**Cost.** Measured for #319 on the maintainer's 16-core workstation, Python
3.12.4, with `python -m pytest -q -p no:xdist -p no:randomly
tests/test_examples_guard.py --durations=0` (serial, one process), twice, while
other work shared the machine (load average about 25, then about 13):

| mode | example | regen test | static test |
|---|---|---|---|
| simulate | `lifedraft/single-run-two-adult` | 1.01 s, 0.98 s | 0.26 s, 0.09 s |
| optimize | `lifedraft/minimal-two-adult` | 60.57 s, 26.33 s | 1.26 s, 0.57 s |

`/usr/bin/time python tools/examples.py regen <example>` gave 0.99 to 1.09 s wall,
0.92 to 1.03 s user and 41 MB peak RSS for the simulate example. The optimize regen
test took 25 to 36 s on a quieter machine (issue #300). A simulate example
costs about 1 s: interpreter start, imports, `load_and_map` and one ~0.15 s
fold. About 240 simulate examples would add about 240 / W seconds of wall time
on a W-worker CI run (W = 4 on the hosted runner, so about a minute), against
roughly 40 minutes if each ran the optimizer.

Keep `optimize` for examples whose claim is a ranking. With one optimize
example, a separate CI job is not justified: it costs well under a minute of
the ~6 min suite. Past about 20 optimize examples, measure again and decide
deliberately whether to shard them or move them to a separate job keyed on
engine-file changes; do not let them grow silently.
