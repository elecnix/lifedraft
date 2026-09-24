# lifedraft / single-run-two-adult

The seed of the `simulate` mode (issue #319). It is the same synthetic
two-generation household as `lifedraft/minimal-two-adult`, but with every
decision pinned to ONE point, so the example regenerates from one
`FamilySimulation.run()` (about 1 s) instead of the full optimizer. It
reproduces no external publication: its "publication" is this repository's own
synthetic household.

## Source

- Title: lifedraft synthetic example household (two-generation trim, one decision point)
- Authors: lifedraft contributors
- Year: 2026
- URL: https://github.com/elecnix/lifedraft/blob/594b6f8/schema/example.json

The input is `examples/lifedraft/minimal-two-adult/input.json` (itself a frozen
trim of that URL, with its one home-path provenance scrub; see that example's
README), reduced to a single decision point. It was frozen once with:

```sh
python -c "import json; d = json.load(open('examples/lifedraft/minimal-two-adult/input.json')); dec = d['decisions']; [c.update(candidate_ages=c['candidate_ages'][:1]) for c in dec['retirement_age']]; [dec.update({k: []}) for k in ('contribution_strategy', 'income', 'resp_action', 'estate_elections')]; [dec['mortgage'].update({k: []}) for k in list(dec['mortgage'])]; d['sensitivity'] = {'presets': {}, 'sweeps': {}}; open('examples/lifedraft/single-run-two-adult/input.json', 'w').write(json.dumps(d, indent=2, ensure_ascii=False) + '\n')"
```

Nothing else differs from the seed's input, and it is never re-synchronised
automatically.

## Publication claim

One `FamilySimulation.run()` of this household, reached through
`input_contract.load_and_map` and `SimulationConfig.from_dict` with no strategy,
rate-path or readvance override, reproduces byte for byte on regeneration: its
compact projection (`report.json`) and its render (`report.md`).

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
  `life_insurance` list.
- `assumptions`: return model and beliefs, inflation, salary growth, savings
  rate, rate paths, retirement, mortality and the other modelling beliefs.
- `decisions`: ONE point. `horizon` (p1 until age 95) and one candidate
  retirement age per adult (60 each) are the only decisions the single run
  reads. `contribution_strategy`, `income`, `resp_action`, `estate_elections`
  and every `mortgage` option list are `[]`: FamilySimulation never reads them,
  so declaring them would claim a sweep the single run silently drops, and
  `tools/examples.py` refuses that in simulate mode.
- `sensitivity`: `{"presets": {}, "sweeps": {}}`; a single run reads neither.
- `provenance`: how five of the values are known (unchanged from the seed).

What the engine chose on its own (DP#16 auto-detection, nothing the document
declares) is recorded in `report.json` under `run`, read from the engine
objects: strategy `Readvanceable Mortgage Priority`, rate path `Default`,
`use_readvanceable` true, `deduct_later` false.

## Engine vs publication

The engine's output is in `report.json`, which is regenerated, never
hand-edited. Read it there rather than from this prose:

- `terminal.total_assets` is the household's terminal total assets, and
  `terminal` holds every scalar field of the last simulated year, unrounded.
- `series` is the year-by-year series for `projection.series_columns`. Its
  `year` is a 1-indexed offset from `run.start_year`, not a calendar year.
- `run` records the strategy, rate path, readvance and deduct-later flags,
  horizon and each adult's mapped retirement age that the single run used.
- `projection.full_report_digest` (`sha256:<hex>`) pins every byte of the
  unprojected `simulate-once` output, including every `YearResult` field.

`terminal.net_benefit` is `0.0` and must not be cited: it is a `YearResult`
field default that `FamilySimulation.run()` never writes (net benefit is the
optimizer's comparison against a baseline). The series leaves it out for that
reason.

The "publication" claims only that this run is reproducible, and the guard's
byte comparison is what establishes it.

## Verdict

AGREES

This verdict is vacuous by construction: the only "publication" is the
engine's own reproducibility, so it can only disagree if a regeneration stops
being byte-identical, and the CI guard fails in that case anyway.
