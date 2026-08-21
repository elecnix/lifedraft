# lifedraft

**A financial life simulator that plays out your entire household's future — year by
year, decade by decade — and ranks your big money moves by what you actually keep
after tax.**

It does not tell you what to do. You describe your situation and your questions, it
fast-forwards each option through 40+ years of tax rules, account rules, and market
beliefs, and shows you the scoreboard: what each choice leaves you with, including
the ugly scenarios next to the average ones.

> **Not financial advice.** Lifedraft ranks options; it does not choose, and it
> can be wrong. See the [LICENSE](LICENSE) for the warranty disclaimer.

---

## What is this, in one paragraph?

Most planning tools either hide the math behind a friendly dashboard or hand you a
confident number built on silent guesses. This project is the opposite: an open-source
simulation engine where **every assumption is an input you can see and argue with**.
You write a config file describing your household — people, accounts, debts,
properties, beliefs about returns — plus the decisions you're weighing. The engine
simulates each option across the decades and ranks them by after-tax outcome.
**It ranks; it does not choose.** It is a calculator with receipts, not an advisor.

## What it covers today

The engine is generic; the rule modules that ship with it cover **mortgages** —
payments, renewals, refinancing, readvanceable structures — and **retirement** —
contribution and drawdown sequencing, pension start timing, retirement income.
Other domains are added as rule modules, which are data packages, not engine code.

## Four questions, one tool

Most tools answer one question: *which option wins?* This engine answers four,
each its own command:

- **Which strategy wins?** — Ranks every option you're weighing by after-tax
  outcome, with risk measures shown next to the averages.
- **What's it worth to know?** — For every number in your file that is a guess,
  puts a dollar price on resolving it, ranked. Your first run isn't a 200-field
  form; it's the short list of lookups that actually pay.
- **What should I do next?** — Turns the ranking into dated, costed actions
  sorted by deadline and flagged when a window closes for good.
- **Where did this number come from?** — Audits every value in your file:
  measured, stated, derived, assumed, or unknown.

Worked examples for all four live in `docs/TUTORIAL.md`.

## Who is this for?

Deliberately, right now: **DIY nerds**. People who keep a spreadsheet with 40 tabs
held together by hope, who want to check their advisor's math, who read the tax
footnotes. If you can edit a JSON file, you can run your own numbers.

There is **no friendly user interface, and that is on purpose.** Two reasons:

1. **Community-built front-ends are welcome.** The engine is a library with a clean
   input contract and machine-readable output (`--json`, `--txt`, `--md`, `--html`).
   If you want to build a web app, a CLI, or a chat wrapper on top, the seams to build
   against are first-class, not afterthoughts.
2. **A tool this easy to use starts to look like financial advice.** Offering polished
   guidance to random people raises regulatory questions that vary by jurisdiction —
   what's legal, what's grey area, what's clearly illegal. Rather than guess, this
   project lets the community build, use, and discover where those lines are.

**Today's expected interface is an AI chatbot with a shell tool**: the agent installs
the engine, writes and validates your config file with you, runs it, and relays the
questions and results back to you in plain language. The rigor lives in the engine;
the conversation lives on top.

---

## Design principles you'll feel as a user

These aren't decoration — most of them are enforced by tests in the repo itself.

- **It ranks, it doesn't choose.** The optimizer orders options by outcome; the
  decision stays yours. The simulator models tax consequences; it does not make
  financial decisions.
- **Your returns are your beliefs.** Expected returns, volatility, inflation: these
  are visible inputs, not hidden opinions baked into the code. Return models are
  pluggable data — swap in historical sequences or anything else.
- **Mechanisms, not branded products.** The code models how a readvanceable mortgage
  or a term deposit *works*, never a bank's product name. Your specific offers are
  data in your config.
- **Dates, not approximations.** Rules fire on real dates — minimum-withdrawal ages,
  grant windows, eligibility counted in months — computed from stored dates, never
  from stale derived ages or years.
- **Rules are versioned by year.** Brackets and limits change over time; the data
  tables are per-year, so a 46-year projection uses each year's actual rules.
- **Reproducible randomness.** Every Monte Carlo run takes an explicit seed; the same
  seed gives the same dice rolls, so a result you quote can be re-derived.
- **Your data stays yours.** Personal figures live in your local config file, outside
  the project entirely. Nothing personal belongs in issues, PRs, or the codebase.
- **No compatibility shims.** When something changes, it changes in one step and the
  error message tells you the new way. There are no deprecation periods to trip over.

## Architecture, at a glance

Four layers, dependencies pointing inward:

```
┌─────────────────────────────────────────────────────┐
│  OPTIMIZATION   ranks strategies by objective       │
│                 (expected value, risk measures,     │
│                 die-with-zero estates, …)           │
├─────────────────────────────────────────────────────┤
│  SIMULATION     pure fold over explicit state:      │
│                 simulate_year_pure(state) → state'  │
│                 run() = fold over years             │
├─────────────────────────────────────────────────────┤
│  SCENARIO       your decisions (anchors) +          │
│                 sensitivity overlays                │
├─────────────────────────────────────────────────────┤
│  DATA           year-versioned rules & limits per   │
│                 jurisdiction, loaded once from a    │
│                 validated JSON Schema contract      │
└─────────────────────────────────────────────────────┘
```

- **One input contract.** Your situation is a JSON document validated against a
  schema (plus a per-jurisdiction overlay). Every field is explicit.
- **A pure simulation core.** Each simulated year is a pure function over explicit
  state; a projection is just folding that function across years. No hidden state,
  no global mutable anything.
- **Jurisdictions are packages, not branches.** Local rules live in data packages
  that mirror the jurisdiction's own structure. The core engine carries jurisdiction
  state as opaque data and never interprets it — adding a jurisdiction means adding
  a package, not modifying the core.
- **Programs are modules.** One module per government program or regime, auto-included
  when its trigger data appears.
- **Invariants run inside the engine.** Year-by-year checks — money conserves, cost
  basis never exceeds value, mandatory minimums actually fire — execute on every real
  run, not just in tests. A breach stops the show loudly.
- **Pluggable everything at the edges.** Return models, objectives, optimizer modes:
  all data-driven choices, so new research or new goals don't require new engine code.

## Quick start

```sh
# install (uv)
uv sync
# or: pip install -e .

# describe your household in a JSON contract (see schema/example.json), then:
python optimize.py    --input my_household.json --json report.json  # which strategy wins
python voi.py         --input my_household.json                     # what's it worth to know
python next_action.py --input my_household.json                     # what should I do next
python provenance.py  --input my_household.json                     # where did this number come from
```

`docs/TUTORIAL.md` builds a household step by step — four small additions, re-running
each time — if you learn better by doing than by reading schemas.

Run the test suite (thousands of tests, including a 46-year golden trajectory checked
year by year):

```sh
python -m pytest -q
```

## Contributing your local reality

This is the part the architecture exists for: **if the engine doesn't know your
local rules, that's a contribution, not a limitation.**

1. Add a jurisdiction rule package with your brackets, program rules, and account
   regimes as year-versioned data modules.
2. Add a JSON Schema overlay for any jurisdiction-specific input fields.
3. Add fixtures and expected values — test logic is shared; jurisdictions contribute
   data, not duplicated test files.

The same invitation applies to product mechanisms (loan types, insurance structures)
and to front-ends. Open an issue first if the mechanism doesn't have a home yet —
modeling it as a mechanism rather than a special case is the house style.

## Status, honestly

The rigor is real; the adoption is theoretical. Thousands of tests, enforced design
principles, coverage and performance gates in CI — and, so far, approximately one
skeptical uncle of prospective users. If that sounds like your kind of project,
there's room at the party.

---

*Not financial advice. A simulation is only as good as its inputs and its rule data —
verify both before betting a decade on either.*
