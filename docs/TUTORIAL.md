# Newcomer tutorial — from zero to a ranked strategy

A hands-on, learn-by-doing guide to `lifedraft`. The [README](../README.md)
is the reference/overview; this file is the *first run*. Every contract below
was executed against the engine and produced the output shown — none are
invented. If a snippet fails for you, your repo is in a different state than
the one this was written against, and that is itself a finding (DP#32).

This tutorial builds one household in four steps. Each step adds one thing
and re-runs. You do not need to understand the whole schema to start — you
need to understand the **required top-level keys**, and that a `null` means
*unknown* while a `0` means *zero* (they are not the same, and the engine
refuses to confuse them).

---

## What this is & how to run

`lifedraft` is a tax-aware financial optimizer for Canadian households.
You hand it a **dated balance sheet of owned entities** (people, accounts,
liabilities, properties) plus **beliefs** (returns, inflation, mortality) and
**questions** (which retirement age, which mortgage refinance, which
contribution split). It ranks strategies by after-tax outcome. **It ranks; it
does not choose** (DP#22, DP#30). Not financial advice.

One command runs the optimizer and writes a machine-readable report:

```sh
PYTHONPATH=. python optimize.py --input <contract.json> --json <out.json>
```

- `--input` — path to a contract JSON document (validated against
  `schema/input_schema.json` composed with the Canada overlay).
- `--json <out.json>` — write the ranked report to a JSON file. Drop it (or
  use `--txt`, `--md`, `--html`) for a console-only run. `--input` defaults to
  `input.json`; `--json` with no argument defaults to `optimizer_report.json`.

> Run from the repo root with the project's virtualenv active (`uv sync` first;
> see the README quick start). `PYTHONPATH=.` is not required after `uv pip
> install -e .` but is harmless and matches the way the test suite invokes the
> modules. The commands below use the venv interpreter explicitly.

---

## Example 1 — the smallest thing that runs

The schema's top-level `required` list is:

```
schema_version, as_of, currency, dollars, jurisdiction, people, accounts,
liabilities, properties, cash_flows, estate, assumptions, decisions, sensitivity
```

(`household_budget`, `provenance`, `private_loans`, `gifts`,
`first_home_purchases`, `installments`, `equity_grants` are **optional** —
DP#16: a module auto-includes when its trigger data is present; absence
disables it, and the run is unchanged.)

Two things are *not* obvious from that list, both learned by running:

1. **The Canada overlay adds required fields** to `assumptions`
   (`resp`, `tax_law_overrides.oas`, `tax_law_overrides.contribution_limit_overrides
   .{rrsp_annual_max,tfsa_annual_room}`) and to `contribution_strategy.allocation`
   (`rrsp_pct`, `spousal_rrsp_pct`, `tfsa_pct`, `fhsa_pct`, `resp_pct`). A
   contract that omits them fails validation loudly, naming every missing
   field — it is never silently accepted.
2. **The optimizer's headline path sweeps an LTV cash-out overlay against the
   property's registered charge.** That overlay machinery reads
   `property.mortgage_balance`, which the adapter only builds when a mortgage
   liability exists. A contract with *no mortgage* passes schema validation
   but the optimizer raises `KeyError: 'mortgage_balance'` at run time. So the
   smallest *runnable* contract for `optimize.py` has one person, one
   principal residence, and one mortgage against it — and nothing else.

This is the floor. Save it as `ex1.json`:

```json
{
  "schema_version": "2026-07",
  "as_of": "2026-07-12",
  "currency": "CAD",
  "dollars": "nominal",
  "jurisdiction": { "country": "canada", "province": "quebec" },
  "people": [
    {
      "id": "p1",
      "label": "primary",
      "legal_name": null,
      "birth_date": "1980-03-14",
      "death_date": null,
      "residency": { "province": "quebec", "since": "1980-03-14" },
      "relationships": [],
      "incomes": [],
      "room": { "rrsp": null, "tfsa": null, "fhsa": null, "resp": null }
    }
  ],
  "accounts": [],
  "liabilities": [
    {
      "id": "mortgage",
      "owner": "p1",
      "kind": "mortgage",
      "balance": { "amount": 200000, "as_of": "2026-06-30" },
      "rate": 0.045,
      "rate_type": "fixed",
      "amortization": { "years": 25, "payment_monthly": 1109 },
      "renewal_date": "2031-07-01",
      "term_start_date": "2026-07-01",
      "collateral": "home"
    }
  ],
  "properties": [
    {
      "id": "home",
      "owner": "p1",
      "kind": "principal",
      "value": { "amount": 400000, "as_of": "2026-06-30" },
      "acb": null,
      "designated_principal_residence_years": [ { "from": "2026-07-01", "to": null } ]
    }
  ],
  "cash_flows": [],
  "estate": {
    "default_spousal_rollover": true,
    "rollover_overrides": [],
    "life_insurance": []
  },
  "assumptions": {
    "return_model": { "type": "fixed", "rate": 0.06 },
    "inflation": 0.02,
    "salary_growth": 0.02,
    "savings_rate": 0.15,
    "default_non_reg_yield": null,
    "rate_paths": {
      "mortgage": { "type": "fixed", "rate": 0.045 },
      "heloc": { "type": "variable", "path": [0.0545] }
    },
    "retirement": {
      "spending_target": 90000,
      "net_replacement_rate": 0.7,
      "drawdown_tax_mode": "net",
      "drawdown_order": ["tfsa", "non_reg", "rrsp", "rrif", "lif"]
    },
    "mortality": [
      { "person": "p1", "assumed_death_age": 95, "assumed_death_date": null }
    ],
    "resp": {
      "eap_tax_rate": 0.15,
      "eap_taxable_portion": 0.6,
      "study_start_age": 18,
      "study_duration_years": 4,
      "used_for_education": true
    },
    "tax_law_overrides": {
      "capital_gains_inclusion": null,
      "frozen_brackets": false,
      "oas": { "disabled": false, "annual_max_override": null },
      "contribution_limit_overrides": { "rrsp_annual_max": null, "tfsa_annual_room": null }
    },
    "products": {},
    "time_step": "yearly"
  },
  "decisions": {
    "horizon": { "person": "p1", "until_age": 95 },
    "retirement_age": [
      { "person": "p1", "candidate_ages": [60, 65] }
    ],
    "contribution_strategy": [
      {
        "id": "balanced",
        "label": "Balanced",
        "allocation": { "rrsp_pct": 0.0, "spousal_rrsp_pct": 0.0, "tfsa_pct": 0.0, "fhsa_pct": 0.0, "resp_pct": 0.0, "non_reg_pct": 0.0 },
        "use_smith": false,
        "deduct_later": false,
        "deduct_later_bracket_target": null
      }
    ],
    "income": [
      { "id": "stay", "label": "Stay", "overrides": [] }
    ],
    "mortgage": {
      "refinance_options": [
        { "id": "no_refi", "label": "No refinance", "cash_out": 0, "ltv": 0.0, "amortization_years": 25 }
      ],
      "renewal_options": [
        { "id": "5yr_fixed", "label": "5-year fixed", "rate": 0.045, "type": "fixed", "term_years": 5 }
      ]
    },
    "resp_action": [
      { "id": "keep", "label": "Keep RESP", "cash_out": false }
    ],
    "estate_elections": [
      { "id": "rollover", "label": "Rollover", "spousal_rollover": true }
    ]
  },
  "sensitivity": {
    "presets": {
      "moderate": {
        "investment_return": 0.06,
        "salary_growth": 0.02,
        "inflation": 0.02,
        "label": "Moderate"
      }
    },
    "sweeps": {
      "investment_return": [0.04, 0.06, 0.08]
    }
  }
}
```

Run it:

```sh
PYTHONPATH=. python optimize.py --input ex1.json --json ex1out.json
```

Verified run (exit code `0`, report written):

```
  📋 INPUTS (from ex1.json)
     Income: $0 → Marginal rate: 25.69%
     House: $400,000 | Mortgage: $200,000
     Cash-out: $120,000
  ...
  🏆 RANKED STRATEGIES — every strategy refinanced to MAX LTV 80% (cash-out $120,000)
  #   Strategy                                                      Net
  1   balanced                                                $     16k
  ...
  📋 JSON: ex1out.json
```

What to look for:

- **`📋 INPUTS`** — the engine read the contract. Income is `$0` (we declared
  no `incomes[]`); the marginal rate is the lowest Quebec bracket applied to a
  notional amount. House and mortgage came straight off `properties[0]` and
  `liabilities[0]`.
- **`Cash-out: $120,000`** — the optimizer's headline path refinances every
  strategy to the max LTV (80% of $400k minus the $200k mortgage ≈ $120k) and
  ranks *that*. The declared `refinance_options` (here a single `no_refi`) are
  swept separately — see Example 4.
- **`MODEL FIDELITY`** block (above the ranking) — the engine prints its own
  known approximations *next to the figure they bias*. Read it before you act
  on a number. (DP: an approximation that biases a headline is printed beside
  it, not buried in a docstring.)
- The JSON report (`ex1out.json`) is large (~1.6 MB) because it carries the
  full year-by-year trajectory for every ranked strategy, plus
  `category_bests`, `optimal_refi_level`, `runway`, etc.

A $16k net benefit on a household with no income and no investments is not a
recommendation — it is the floor telling you it ran. The interesting numbers
appear the moment the household has something to optimize.

---

## Example 2 — add income + an account

Build on Example 1 by adding one employment income and one TFSA. Two facts
the schema forces you to get right:

- A `person.incomes[]` entry has `kind` (one of `employment`,
  `self_employment`, `rental`, `investment`, `ei`, `other`) — `kind` is not
  decoration, it decides RRSP-room accrual and tax treatment. `from`/`to` are
  **dates** (DP#1), and `to: null` means *still ongoing*, not *unknown end*.
- An `account.holdings[].product` is a **key into `assumptions.products`**.
  If you name a product the registry does not define, the contract is
  refused. So adding a holding means also adding its product definition
  (category, equity/bond split, MER, turnover, …). `assumptions.products`
  is the synthetic product registry — synthetic names only, never real
  tickers (DP#15).

This is the **complete** `ex2.json` — Example 1's contract with the income,
the TFSA account, and the product definition added, and every required field
from Example 1 still present (mortgage, property, `assumptions.resp`,
`decisions.estate_elections`, …). Copy-paste it whole; it runs as-is.

```json
{
  "schema_version": "2026-07",
  "as_of": "2026-07-12",
  "currency": "CAD",
  "dollars": "nominal",
  "jurisdiction": { "country": "canada", "province": "quebec" },
  "people": [
    {
      "id": "p1",
      "label": "primary",
      "legal_name": null,
      "birth_date": "1980-03-14",
      "death_date": null,
      "residency": { "province": "quebec", "since": "1980-03-14" },
      "relationships": [],
      "incomes": [
        { "id": "p1_job", "kind": "employment", "amount": 118000, "from": "2015-01-01", "to": null, "employer_rrsp_match": null }
      ],
      "room": { "rrsp": null, "tfsa": { "contribution_room": 18000, "as_of": "2026-01-01" }, "fhsa": null, "resp": null }
    }
  ],
  "accounts": [
    {
      "id": "p1_tfsa",
      "owner": "p1",
      "kind": "tfsa",
      "balance": { "amount": 20000, "as_of": "2026-06-30" },
      "acb": null,
      "holdings": [ { "product": "synthetic_global_equity_index", "weight": 1.0 } ],
      "beneficiary": null,
      "successor_holder": null
    }
  ],
  "liabilities": [
    {
      "id": "mortgage",
      "owner": "p1",
      "kind": "mortgage",
      "balance": { "amount": 200000, "as_of": "2026-06-30" },
      "rate": 0.045,
      "rate_type": "fixed",
      "amortization": { "years": 25, "payment_monthly": 1109 },
      "renewal_date": "2031-07-01",
      "term_start_date": "2026-07-01",
      "collateral": "home"
    }
  ],
  "properties": [
    {
      "id": "home",
      "owner": "p1",
      "kind": "principal",
      "value": { "amount": 400000, "as_of": "2026-06-30" },
      "acb": null,
      "designated_principal_residence_years": [ { "from": "2026-07-01", "to": null } ]
    }
  ],
  "cash_flows": [],
  "estate": { "default_spousal_rollover": true, "rollover_overrides": [], "life_insurance": [] },
  "assumptions": {
    "return_model": { "type": "fixed", "rate": 0.06 },
    "inflation": 0.02,
    "salary_growth": 0.02,
    "savings_rate": 0.15,
    "default_non_reg_yield": null,
    "rate_paths": {
      "mortgage": { "type": "fixed", "rate": 0.045 },
      "heloc": { "type": "variable", "path": [0.0545] }
    },
    "retirement": { "spending_target": 90000, "net_replacement_rate": 0.7, "drawdown_tax_mode": "net", "drawdown_order": ["tfsa", "non_reg", "rrsp", "rrif", "lif"] },
    "mortality": [ { "person": "p1", "assumed_death_age": 95, "assumed_death_date": null } ],
    "resp": { "eap_tax_rate": 0.15, "eap_taxable_portion": 0.6, "study_start_age": 18, "study_duration_years": 4, "used_for_education": true },
    "tax_law_overrides": {
      "capital_gains_inclusion": null,
      "frozen_brackets": false,
      "oas": { "disabled": false, "annual_max_override": null },
      "contribution_limit_overrides": { "rrsp_annual_max": null, "tfsa_annual_room": null }
    },
    "products": {
      "synthetic_global_equity_index": {
        "category": "global_equity_index", "us_equity_pct": 0.6, "intl_equity_pct": 0.4,
        "foreign_income": 0.02, "capital_gains": 0.01, "mer": 0.002,
        "foreign_content": 0.75, "withholding_exposure": 0.011, "turnover": 0.05
      }
    },
    "time_step": "yearly"
  },
  "decisions": {
    "horizon": { "person": "p1", "until_age": 95 },
    "retirement_age": [ { "person": "p1", "candidate_ages": [60, 65] } ],
    "contribution_strategy": [
      { "id": "balanced", "label": "Balanced", "allocation": { "rrsp_pct": 0.0, "spousal_rrsp_pct": 0.0, "tfsa_pct": 0.0, "fhsa_pct": 0.0, "resp_pct": 0.0, "non_reg_pct": 0.0 }, "use_smith": false, "deduct_later": false, "deduct_later_bracket_target": null }
    ],
    "income": [ { "id": "stay", "label": "Stay", "overrides": [] } ],
    "mortgage": {
      "refinance_options": [ { "id": "no_refi", "label": "No refinance", "cash_out": 0, "ltv": 0.0, "amortization_years": 25 } ],
      "renewal_options": [ { "id": "5yr_fixed", "label": "5-year fixed", "rate": 0.045, "type": "fixed", "term_years": 5 } ]
    },
    "resp_action": [ { "id": "keep", "label": "Keep RESP", "cash_out": false } ],
    "estate_elections": [ { "id": "rollover", "label": "Rollover", "spousal_rollover": true } ]
  },
  "sensitivity": {
    "presets": { "moderate": { "investment_return": 0.06, "salary_growth": 0.02, "inflation": 0.02, "label": "Moderate" } },
    "sweeps": { "investment_return": [0.04, 0.06, 0.08] }
  }
}
```

Run it:

```sh
PYTHONPATH=. python optimize.py --input ex2.json --json ex2out.json
```

Verified run (exit code `0`):

```
  📋 INPUTS (from ex2.json)
     Income: $118,000 → Marginal rate: 45.71%
     House: $400,000 | Mortgage: $200,000
     Cash-out: $120,000
  ...
  #   Strategy                                                      Net
  1   no_readvance + Bracket-filling (draw registered up to a target bracket, then TFSA) $     82k
  2   no_readvance                                            $     82k
  ...
  6   balanced                                                $     28k
  ...
  📋 JSON: ex2out.json
```

What to look for:

- The marginal rate jumped from 25.71% to **45.71%** — the $118k salary is now
  in Quebec's top bracket. Tax is computed from the dated income window, not a
  stored rate.
- The ranking **changed**: the `balanced` contribution split (which saved
  nothing into any registered bucket here — all `*_pct` are `0.0`) fell to
  rank 6 at $28k, while the `no_readvance` drawdown-order variants rose to
  $82k. Adding one income and one account moved the recommendation. This is
  the whole reason the engine ranks instead of choosing (DP#22).
- Note `beneficiary: null` and `successor_holder: null` on the TFSA. The
  Canada overlay forbids `successor_holder` on every kind *except* `tfsa`, and
  even on a TFSA it is optional — `null` is the explicit spelling of "none
  declared", not a silent default.

---

## Example 3 — add a mortgage decision

Example 1 already contained a mortgage (the optimizer needed it to run). Now
make the mortgage a **decision**: give it two renewal candidates so the engine
sweeps them. The `decisions.mortgage.renewal_options[]` block is a list of
candidates — each has an `id`, a `label`, a `rate`, a `type` (`fixed` /
`variable`), and a `term_years` (1–10). The optimizer ranks strategies across
these renewal-rate scenarios.

This is the **complete** `ex3.json` — Example 2's contract with a second
`renewal_options` candidate added, and every required field still present.
Copy-paste it whole; it runs as-is.

```json
{
  "schema_version": "2026-07",
  "as_of": "2026-07-12",
  "currency": "CAD",
  "dollars": "nominal",
  "jurisdiction": { "country": "canada", "province": "quebec" },
  "people": [
    {
      "id": "p1",
      "label": "primary",
      "legal_name": null,
      "birth_date": "1980-03-14",
      "death_date": null,
      "residency": { "province": "quebec", "since": "1980-03-14" },
      "relationships": [],
      "incomes": [
        { "id": "p1_job", "kind": "employment", "amount": 118000, "from": "2015-01-01", "to": null, "employer_rrsp_match": null }
      ],
      "room": { "rrsp": null, "tfsa": { "contribution_room": 18000, "as_of": "2026-01-01" }, "fhsa": null, "resp": null }
    }
  ],
  "accounts": [
    {
      "id": "p1_tfsa",
      "owner": "p1",
      "kind": "tfsa",
      "balance": { "amount": 20000, "as_of": "2026-06-30" },
      "acb": null,
      "holdings": [ { "product": "synthetic_global_equity_index", "weight": 1.0 } ],
      "beneficiary": null,
      "successor_holder": null
    }
  ],
  "liabilities": [
    {
      "id": "mortgage",
      "owner": "p1",
      "kind": "mortgage",
      "balance": { "amount": 200000, "as_of": "2026-06-30" },
      "rate": 0.045,
      "rate_type": "fixed",
      "amortization": { "years": 25, "payment_monthly": 1109 },
      "renewal_date": "2031-07-01",
      "term_start_date": "2026-07-01",
      "collateral": "home"
    }
  ],
  "properties": [
    {
      "id": "home",
      "owner": "p1",
      "kind": "principal",
      "value": { "amount": 400000, "as_of": "2026-06-30" },
      "acb": null,
      "designated_principal_residence_years": [ { "from": "2026-07-01", "to": null } ]
    }
  ],
  "cash_flows": [],
  "estate": { "default_spousal_rollover": true, "rollover_overrides": [], "life_insurance": [] },
  "assumptions": {
    "return_model": { "type": "fixed", "rate": 0.06 },
    "inflation": 0.02,
    "salary_growth": 0.02,
    "savings_rate": 0.15,
    "default_non_reg_yield": null,
    "rate_paths": {
      "mortgage": { "type": "fixed", "rate": 0.045 },
      "heloc": { "type": "variable", "path": [0.0545] }
    },
    "retirement": { "spending_target": 90000, "net_replacement_rate": 0.7, "drawdown_tax_mode": "net", "drawdown_order": ["tfsa", "non_reg", "rrsp", "rrif", "lif"] },
    "mortality": [ { "person": "p1", "assumed_death_age": 95, "assumed_death_date": null } ],
    "resp": { "eap_tax_rate": 0.15, "eap_taxable_portion": 0.6, "study_start_age": 18, "study_duration_years": 4, "used_for_education": true },
    "tax_law_overrides": {
      "capital_gains_inclusion": null,
      "frozen_brackets": false,
      "oas": { "disabled": false, "annual_max_override": null },
      "contribution_limit_overrides": { "rrsp_annual_max": null, "tfsa_annual_room": null }
    },
    "products": {
      "synthetic_global_equity_index": {
        "category": "global_equity_index", "us_equity_pct": 0.6, "intl_equity_pct": 0.4,
        "foreign_income": 0.02, "capital_gains": 0.01, "mer": 0.002,
        "foreign_content": 0.75, "withholding_exposure": 0.011, "turnover": 0.05
      }
    },
    "time_step": "yearly"
  },
  "decisions": {
    "horizon": { "person": "p1", "until_age": 95 },
    "retirement_age": [ { "person": "p1", "candidate_ages": [60, 65] } ],
    "contribution_strategy": [
      { "id": "balanced", "label": "Balanced", "allocation": { "rrsp_pct": 0.0, "spousal_rrsp_pct": 0.0, "tfsa_pct": 0.0, "fhsa_pct": 0.0, "resp_pct": 0.0, "non_reg_pct": 0.0 }, "use_smith": false, "deduct_later": false, "deduct_later_bracket_target": null }
    ],
    "income": [ { "id": "stay", "label": "Stay", "overrides": [] } ],
    "mortgage": {
      "refinance_options": [ { "id": "no_refi", "label": "No refinance", "cash_out": 0, "ltv": 0.0, "amortization_years": 25 } ],
      "renewal_options": [
        { "id": "5yr_fixed", "label": "5-year fixed", "rate": 0.045, "type": "fixed", "term_years": 5 },
        { "id": "3yr_fixed", "label": "3-year fixed", "rate": 0.042, "type": "fixed", "term_years": 3 }
      ]
    },
    "resp_action": [ { "id": "keep", "label": "Keep RESP", "cash_out": false } ],
    "estate_elections": [ { "id": "rollover", "label": "Rollover", "spousal_rollover": true } ]
  },
  "sensitivity": {
    "presets": { "moderate": { "investment_return": 0.06, "salary_growth": 0.02, "inflation": 0.02, "label": "Moderate" } },
    "sweeps": { "investment_return": [0.04, 0.06, 0.08] }
  }
}
```

Run it:

```sh
PYTHONPATH=. python optimize.py --input ex3.json --json ex3out.json
```

Verified run (exit code `0`):

```
  📋 INPUTS (from ex3.json)
     Income: $118,000 → Marginal rate: 45.71%
     House: $400,000 | Mortgage: $200,000
     Cash-out: $120,000
  ...
  #   Strategy                                                      Net
  1   no_readvance + Bracket-filling (draw registered up to a target bracket, then TFSA) $     82k
  ...
  📋 JSON: ex3out.json
```

What to look for:

- The `renewal_date` on the mortgage (`2031-07-01`) is a **fact**; the
  `renewal_options` are **questions**. The engine does not confuse them.
- Cross-check with the next-action entry point on the same contract:

  ```sh
  PYTHONPATH=. python next_action.py --input ex3.json
  ```

  Verified run (exit code `0`):

  ```
  NEXT ACTIONS

        by 2031-07-01          Shop or renew the mortgage (mortgage).
                                The current term matures on renewal_date; the lender's current-rate
                                offer window closes at that date.
                                The payment/interest impact depends on rates prevailing at renewal,
                                and on which decisions.mortgage.renewal_options candidate is taken —
                                neither is known today.
  ```

  That action was **derived from the contract's own `renewal_date`** (DP#28):
  a dated, costed obligation sorted by deadline, not a strategy ranking. The
  engine read the date you declared and turned it into a to-do.

---

## Example 4 — a decision the optimizer makes (refinance)

Now add a cash-out **refinance** candidate. `decisions.mortgage
.refinance_options[]` each declare an `id`, a `label`, a `cash_out` dollar
amount, an `ltv`, and an `amortization_years` (a cash-out refinance is a *new
loan* re-amortized over its own term — not the incumbent's remaining
amortization). The optional `advance_split.deductible_non_reg` declares how
much of the advance is routed into a deductible non-registered account first
(ITA s.20(1)(c) interest tracing is established only when the borrowed money
is deployed into income-producing non-reg; money put into RRSP/TFSA is
non-deductible forever, s.18(11)).

This is the **complete** `ex4.json` — Example 3's contract with a second
`refinance_options` candidate and a non-registered account (for the
deductible split to land in) added, and every required field still present.
Copy-paste it whole; it runs as-is.

```json
{
  "schema_version": "2026-07",
  "as_of": "2026-07-12",
  "currency": "CAD",
  "dollars": "nominal",
  "jurisdiction": { "country": "canada", "province": "quebec" },
  "people": [
    {
      "id": "p1",
      "label": "primary",
      "legal_name": null,
      "birth_date": "1980-03-14",
      "death_date": null,
      "residency": { "province": "quebec", "since": "1980-03-14" },
      "relationships": [],
      "incomes": [
        { "id": "p1_job", "kind": "employment", "amount": 118000, "from": "2015-01-01", "to": null, "employer_rrsp_match": null }
      ],
      "room": { "rrsp": null, "tfsa": { "contribution_room": 18000, "as_of": "2026-01-01" }, "fhsa": null, "resp": null }
    }
  ],
  "accounts": [
    {
      "id": "p1_tfsa",
      "owner": "p1",
      "kind": "tfsa",
      "balance": { "amount": 20000, "as_of": "2026-06-30" },
      "acb": null,
      "holdings": [ { "product": "synthetic_global_equity_index", "weight": 1.0 } ],
      "beneficiary": null,
      "successor_holder": null
    },
    {
      "id": "p1_nonreg",
      "owner": "p1",
      "kind": "non_reg",
      "balance": { "amount": 10000, "as_of": "2026-06-30" },
      "acb": 10000,
      "holdings": [ { "product": "synthetic_global_equity_index", "weight": 1.0 } ],
      "beneficiary": null,
      "successor_holder": null
    }
  ],
  "liabilities": [
    {
      "id": "mortgage",
      "owner": "p1",
      "kind": "mortgage",
      "balance": { "amount": 200000, "as_of": "2026-06-30" },
      "rate": 0.045,
      "rate_type": "fixed",
      "amortization": { "years": 25, "payment_monthly": 1109 },
      "renewal_date": "2031-07-01",
      "term_start_date": "2026-07-01",
      "collateral": "home"
    }
  ],
  "properties": [
    {
      "id": "home",
      "owner": "p1",
      "kind": "principal",
      "value": { "amount": 400000, "as_of": "2026-06-30" },
      "acb": null,
      "designated_principal_residence_years": [ { "from": "2026-07-01", "to": null } ]
    }
  ],
  "cash_flows": [],
  "estate": { "default_spousal_rollover": true, "rollover_overrides": [], "life_insurance": [] },
  "assumptions": {
    "return_model": { "type": "fixed", "rate": 0.06 },
    "inflation": 0.02,
    "salary_growth": 0.02,
    "savings_rate": 0.15,
    "default_non_reg_yield": null,
    "rate_paths": {
      "mortgage": { "type": "fixed", "rate": 0.045 },
      "heloc": { "type": "variable", "path": [0.0545] }
    },
    "retirement": { "spending_target": 90000, "net_replacement_rate": 0.7, "drawdown_tax_mode": "net", "drawdown_order": ["tfsa", "non_reg", "rrsp", "rrif", "lif"] },
    "mortality": [ { "person": "p1", "assumed_death_age": 95, "assumed_death_date": null } ],
    "resp": { "eap_tax_rate": 0.15, "eap_taxable_portion": 0.6, "study_start_age": 18, "study_duration_years": 4, "used_for_education": true },
    "tax_law_overrides": {
      "capital_gains_inclusion": null,
      "frozen_brackets": false,
      "oas": { "disabled": false, "annual_max_override": null },
      "contribution_limit_overrides": { "rrsp_annual_max": null, "tfsa_annual_room": null }
    },
    "products": {
      "synthetic_global_equity_index": {
        "category": "global_equity_index", "us_equity_pct": 0.6, "intl_equity_pct": 0.4,
        "foreign_income": 0.02, "capital_gains": 0.01, "mer": 0.002,
        "foreign_content": 0.75, "withholding_exposure": 0.011, "turnover": 0.05
      }
    },
    "time_step": "yearly"
  },
  "decisions": {
    "horizon": { "person": "p1", "until_age": 95 },
    "retirement_age": [ { "person": "p1", "candidate_ages": [60, 65] } ],
    "contribution_strategy": [
      { "id": "balanced", "label": "Balanced", "allocation": { "rrsp_pct": 0.0, "spousal_rrsp_pct": 0.0, "tfsa_pct": 0.0, "fhsa_pct": 0.0, "resp_pct": 0.0, "non_reg_pct": 0.0 }, "use_smith": false, "deduct_later": false, "deduct_later_bracket_target": null }
    ],
    "income": [ { "id": "stay", "label": "Stay", "overrides": [] } ],
    "mortgage": {
      "refinance_options": [
        { "id": "no_refi", "label": "No refinance", "cash_out": 0, "ltv": 0.0, "amortization_years": 25 },
        { "id": "refi_50k", "label": "Refinance, cash out $50k", "cash_out": 50000, "ltv": 0.55, "amortization_years": 25, "advance_split": { "deductible_non_reg": 25000 } }
      ],
      "renewal_options": [
        { "id": "5yr_fixed", "label": "5-year fixed", "rate": 0.045, "type": "fixed", "term_years": 5 },
        { "id": "3yr_fixed", "label": "3-year fixed", "rate": 0.042, "type": "fixed", "term_years": 3 }
      ]
    },
    "resp_action": [ { "id": "keep", "label": "Keep RESP", "cash_out": false } ],
    "estate_elections": [ { "id": "rollover", "label": "Rollover", "spousal_rollover": true } ]
  },
  "sensitivity": {
    "presets": { "moderate": { "investment_return": 0.06, "salary_growth": 0.02, "inflation": 0.02, "label": "Moderate" } },
    "sweeps": { "investment_return": [0.04, 0.06, 0.08] }
  }
}
```

Run it:

```sh
PYTHONPATH=. python optimize.py --input ex4.json --json ex4out.json
```

Verified run (exit code `0`):

```
  📋 INPUTS (from ex4.json)
     Income: $118,000 → Marginal rate: 45.71%
     House: $400,000 | Mortgage: $200,000
     Cash-out: $120,000
  ...
  #   Strategy                                                      Net
  1   no_readvance + Bracket-filling (draw registered up to a target bracket, then TFSA) $     82k
  ...
  📋 JSON: ex4out.json
```

The ranking table headlines the *max-LTV* sweep. The declared `refinance_options`
are swept head-to-head separately and surface in the JSON as
`optimal_refi_level`. Inspect it:

```sh
python -c "import json; print(json.dumps(json.load(open('ex4out.json'))['optimal_refi_level'], indent=2))"
```

Verified output:

```json
[
  {
    "sm_label": "No",
    "dl_label": "No",
    "no_refi": 0,
    "min_refi": 82366.75685968554,
    "max_refi": 0,
    "best_level": "Fill Registered Room"
  }
]
```

What to look for:

- The optimizer swept the refinance levels (`no_refi`, a minimum cash-out,
  a maximum cash-out) and **picked** `best_level: "Fill Registered Room"` —
  i.e. take *some* cash-out but only enough to fill registered contribution
  room, no more. That is a decision the engine made from data; it did not
  require you to name a strategy (DP#6: strategies are discovered from rules,
  not named by convention).
- `no_refi` and `max_refi` both show `0` here while `min_refi` is positive —
  the ranking judged the *full* cash-out worse than no cash-out, with the
  registered-room-filling slice in between winning. The numbers are the
  objective score under the default `max_net_benefit` objective. Re-rank under
  a different objective with `--objective max_after_tax_estate` to see the
  estate-priced view (the README's "Honest limitations" notes the default
  objective does *not* price the deemed disposition).

> The schema also defines `decisions.deposit_products[]` (a HISA / GIC /
> promotional teaser the optimizer ranks take-vs-leave, issue #936). That sweep
> is wired through the `simulate.py` / `scenario_discovery` overlay path
> (`enumerate_overlays`), not the `optimize.py` headline path — so a
> `deposit_products` entry does not change `optimize.py`'s ranked table, but it
> is consumed by the broader simulation enumeration. Declare it when you want
> the take-vs-leave comparison; inspect the sweep via the simulation entry
> points below.

---

## How modules turn on from data (DP#14 / DP#16)

A government-program module runs **iff its trigger data is present** in the
contract. There is no "enable RESP" flag. You declare the data; the module
auto-includes. Absence is not a silent zero — it disables the module, and the
run is byte-identical to one that never asked (DP#32: the golden trajectory
must not move). A few concrete triggers in this codebase:

- **RESP / CESG / QESI** — appears when an `accounts[]` entry of `kind: "resp"`
  is declared (with its `resp` sub-object: `subscribers`, `beneficiaries`,
  `contributions_total`, `cesg_received`, `qesi_received`, `clb_received`),
  and a beneficiary child has `room.resp` set. The grant *rates* and lifetime
  maxima are never config — they stay as code tables in
  `countries/canada/resp_rules.py` (DP#2/DP#12). Remove the RESP account and
  the module does not run.
- **FHSA / HBP** — appears when an `accounts[]` entry of `kind: "fhsa"` is
  declared (with its `fhsa` sub-object: `opened_date`, `first_time_buyer_since`)
  and/or a `first_home_purchases[]` entry. The 15-year repayment window and the
  qualifying-withdrawal rules live in `countries/canada/fhsa.py` /
  `hbp_rules.py`.
- **LIRA / LIF** — `kind: "lira"` or `kind: "lif"` requires a `lira`
  sub-object (`jurisdiction`, `source_pension_plan`, `transfer_date`,
  `reference_rate`); the locked-in rules auto-include from
  `countries/canada/locked_in_account.py`.
- **Smith Manoeuvre** — not a named strategy you select; it is *available* iff
  a readvanceable facility exists (a `kind: "heloc"` liability with
  `readvanceable: true`, or a `decisions.mortgage.structure_options[]`
  candidate with `readvanceable: true`). With no such facility the engine
  prints `Smith Manoeuvre unavailable: no readvanceable line on this contract
  (1 strategy skipped)` — as Examples 1–4 did. Add the facility and the
  SM-strategy variants enter the ranking automatically (DP#7: model the
  mechanism, not the branded product).
- **Standalone borrow-to-invest (issue #1036)** — a *mortgage-free* household
  that wants to borrow against home equity and invest the proceeds in a
  non-registered account (deductible under ITA s.20(1)(c)) declares
  `decisions.borrow_to_invest[]`: an amount ladder (`source` = a declared
  `kind: "heloc"` liability, `amount`, `target_account: "non_reg"`). The
  optimizer sweeps the ladder plus the implicit no-draw baseline and ranks
  under the household's objective — no readvanceable facility required. The
  `kind: "heloc"` liability's `capitalize_interest` (false = service the
  interest in cash, true = capitalize up to the charge) and `balance` (must
  be 0 — the engine starts a HELOC undrawn; a draw is a simulation decision,
  not an opening fact) are now read / refused loudly rather than silently
  dropped. **Caveat:** `borrow_to_invest` is refused when
  `retirement.liquidate_to_target` is true — that combination is an inverted
  incentive (the drawdown spends the borrowed pot while the HELOC is never
  repaid) until the asset-liability unwind coupling (#1037) / net_estate floor
  (#1065) lands.
- **Cash-flow solvency** — `household_budget.annual_living_costs` is optional
  and deliberately *not* in the top-level `required` list. A document that
  omits it simply cannot express the solvency constraint; a document that
  declares it engages `simulation_rules.apply_solvency`. Present-and-set is
  what engages the check; the check is never silently skipped once the data
  is there.

The rule of thumb: **if you want a program to run, declare the account or
fact that triggers it. If you want it off, omit that data.** Never write a `0`
to "disable" something — `0` is a real value and the engine treats it as one
(DP#32).

---

## The four entry points

One command per question. All take `--input <contract.json>`.

- **`optimize.py`** — *given everything, what is optimal?* Ranks strategies
  across contribution allocation, cash-out / LTV, retirement ages, drawdown
  order, income scenarios and estate elections. Prints its own known
  approximations beside the figures they bias. `--json out.json` writes the
  machine-readable report; `--objective max_after_tax_estate` re-ranks under
  the estate-priced view.
- **`voi.py`** — *what should I go find out?* For every input that is a guess,
  sweeps its plausible range and reports the spread in the objective as the
  dollar value of resolving it — ranked. A brand-new user has nothing
  measured, so the first run is the few things worth going to look up, each
  with a price on it. (`--objective` picks which objective the value is
  measured under.)
- **`next_action.py`** — *what should I do next?* Not a strategy ranking.
  Dated, costed actions sorted by deadline and flagged for irreversibility —
  RRIF at 71, CESG ending the year a child turns 17, a mortgage maturity, a
  term policy's renewal — derived from the contract's own dates (DP#28).
  `--json` emits machine-readable actions.
- **`provenance.py`** — *which of my inputs are guesses?* Every value declares
  how it is known: `measured` (source href + page) · `stated` · `derived` ·
  `assumed` · `unknown`. A leaf with no provenance entry is `assumed` by
  definition — so "everything not backed by evidence" cannot be hidden by
  simply not writing an entry.

Run all four on any example contract:

```sh
PYTHONPATH=. python optimize.py     --input ex2.json --json ex2_opt.json
PYTHONPATH=. python voi.py          --input ex2.json
PYTHONPATH=. python next_action.py  --input ex2.json
PYTHONPATH=. python provenance.py   --input ex2.json
```

All four exit `0` on the Example 2 contract. `next_action.py` derives the
2031-07-01 mortgage renewal from `renewal_date`; `provenance.py` labels every
leaf with no `provenance` sidecar entry `assumed[no-entry]`; `voi.py` reports
the value of resolving each uncertain input.

---

## Where to go next

- **[README](../README.md)** — the reference: the four questions, the input
  contract, architecture, the guardrails, honest limitations.
- **[DESIGN_PRINCIPLES.md](../DESIGN_PRINCIPLES.md)** — the 32 principles,
  executable (violating one usually fails a test, not a review). Start with
  DP#32 (zero is a value, not a fallback; absence fails loudly).
- **[AGENTS.md](../AGENTS.md)** — the indexed one-liners for the principles,
  and the workflow/traps for changing this codebase.
- **`schema/example.json`** — the canonical multi-generational example
  contract. The engine's adapter **correctly refuses it** today (the
  simulator is still two-adults-plus-children; see the README's "Honest
  limitations"). A refusal is a feature, not a crash — read its message.
- **`docs/community-scenarios/`** — scenario write-ups (Smith Manoeuvre, FHSA
  / HBP first home, RRSP meltdown, OAS clawback avoidance, …) showing how
  each program is triggered by data.