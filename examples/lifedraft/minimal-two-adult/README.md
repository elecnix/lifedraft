# lifedraft / minimal-two-adult

The seed example of the runnable-examples bank (issue #300). It exists so the
layout, the generator (`tools/examples.py`) and the CI guard
(`tests/test_examples_guard.py`) have one real example to run against from day
one. It reproduces no external publication: its "publication" is this
repository's own synthetic household.

## Source

- Title: lifedraft synthetic example household (two-generation trim)
- Authors: lifedraft contributors
- Year: 2026
- URL: https://github.com/elecnix/lifedraft/blob/594b6f8/schema/example.json

This is the scaffold's self-referential seed, not a research publication. The
input is `tests/_example_doc.py::minimal_example()` at commit 594b6f8: the
shipped `schema/example.json`, trimmed to the two-generation sub-family the
adapter maps (the four-generation original is refused by `load_and_map`, see
#901/#72), with every optional illustrative block stripped. It was frozen once
with:

```sh
python -c "import sys, json; sys.path.insert(0, 'tests'); from _example_doc import minimal_example; open('examples/lifedraft/minimal-two-adult/input.json', 'w').write(json.dumps(minimal_example(), indent=2, ensure_ascii=False) + '\n')"
```

and is never re-synchronised automatically, so a later edit to
`schema/example.json` does not move this example.

**One deliberate deviation from `minimal_example()`:** its
`provenance["/accounts/0/balance/amount"].source` is a `file://` URI into a home
directory (an illustration inherited from `schema/example.json`). That is exactly
the shape the examples guard refuses (an absolute home path, DP#15), so the
frozen copy carries `urn:lifedraft:example:rrsp-statement-2026-q2#page=1`
instead. The value is schema-valid (a non-empty string; `confidence: measured`
still has its `source` and `as_of`) and no engine code reads it. Nothing else
differs.

## Publication claim

The synthetic two-generation household runs end to end through `optimize.py`,
and the compact projection of its full `--json` report, together with its
`--md` report, reproduces byte for byte on regeneration.

## Encoding

Every value is fabricated (round figures, role-based ids `p1`, `p2`, `ca`,
`cb`; DP#4, DP#15). Top-level keys of `input.json`, one by one:

- `schema_version`: the contract version the document is written against.
- `as_of`: the balance-sheet date every balance and rate is dated from.
- `currency`: CAD.
- `dollars`: nominal dollars.
- `jurisdiction`: Canada, Quebec.
- `people`: two adults (`p1`, `p2`) and two children (`ca`, `cb`), with birth
  dates, incomes and contribution room.
- `accounts`: ten owned accounts (RRSPs, a spousal RRSP, TFSAs, a LIRA, a joint
  non-registered account, a joint RESP, an FHSA and an LSIF), each with a dated
  balance.
- `liabilities`: a mortgage, a HELOC, a personal line of credit and a car loan.
- `properties`: the principal residence.
- `private_loans`: two intra-family loans.
- `gifts`: one gift filling a child's contribution room.
- `cash_flows`: two dated one-off flows (a tuition payment and a retention
  bonus).
- `household_budget`: annual living costs, the discretionary fraction and the
  expense segments.
- `estate`: the default spousal rollover, per-account overrides, and an empty
  `life_insurance` list (stripped by `minimal_example()`).
- `assumptions`: return model and beliefs, inflation, salary growth, savings
  rate, rate paths, retirement, mortality and the other modelling beliefs.
- `decisions`: what the optimizer sweeps (horizon, candidate retirement ages,
  contribution strategies, income, mortgage, RESP action, estate elections).
- `sensitivity`: the sensitivity presets and sweeps.
- `provenance`: how five of the values are known (with the one scrub described
  under Source).

## Engine vs publication

The engine's conclusions are in `report.json`, which is regenerated, never
hand-edited. Read them there rather than from this prose, which would go stale
on the next legitimate regeneration:

- `scenarios[0].strategy` is the winning strategy, and `scenarios[*]` lists all
  ranked scenarios in engine order with `net_benefit` and `future_value`
  (terminal assets).
- `winner_year_by_year` is the winner's per-year series for the key columns.
- `projection.full_report_sha256` pins the full 13 MB `--json` output.

The "publication" claims only that this run is reproducible, and the guard's
byte comparison is what establishes it.

One engine-report gap is visible in `report.md` and recorded here rather than
fixed: the year-by-year heading reads `#1 Scenario: ?`, because scenario entries
carry their label in `strategy` and have no `label` key (only `category_bests`
entries do). That is a rendering issue in the engine's Markdown report, not
something an example may patch; it should be filed as its own follow-up issue.

## Verdict

AGREES

This verdict is vacuous by construction: the seed's only "publication" is the
engine's own reproducibility, so it can only disagree if a regeneration stops
being byte-identical, and the CI guard fails in that case anyway. A real
example (for instance a future `examples/cffp/<slug>/`) compares the engine
against an external publication's numbers.
