# Workflows

Claude Code dynamic workflows (`.js` scripts orchestrated by the Workflow tool).
Each stage below runs a **fresh agent** that receives only the accumulated output
of its predecessors — no shared context — and returns a schema-validated result.

## CI guard

`tests/architecture/test_claude_workflow_scripts.py` runs in the normal pytest suite (so `tests.yml` runs it on every PR) and checks every `*.js` in this directory. It compiles each script the way the Workflow runtime runs it: the body inside an async function, so top-level `await` and `return` are legal, with `export const meta` kept top-level. It also checks that `meta` is a pure object literal with a non-empty `name` and `description`, that the `meta.phases[].title` set equals the set of `phase('...')` arguments exactly (and every agent `phase:` option names one of them), and that no file here contains an absolute home-directory path. Use `~/...` instead. See #264.

The guard needs `node` on PATH and **fails** without it; it never skips. It compiles with node's `vm.Script` and deliberately does not use `node --check`. That flag rejects the legal top-level `return`, and on node 24 it exits 0 on unparseable files that contain ESM syntax, so it would pass the exact breakage the guard exists to catch.

## `implement-github-issue`

Drive a GitHub issue end-to-end to a **green draft PR**. The whole point is that
no single context is ever asked to both *judge* and *write*: every stage is a
fresh agent, and the same goal, plan, and test-plan are replayed verbatim into
each one so the later stages audit the earlier ones.

```
args: { issue, repo }
   ┌───────────────┐  gh issue view → ask/goal
   │  1 FETCH      │  (the "what is asked, what is done")
   └──────┬────────┘
   ┌──────▼────────┐  goal + verbatim issue text (+ comments)
   │  2 PLAN       │  → implementation plan (steps/files/risks/AC/tests)
   └──────┬────────┘
   ┌──────▼────────┐  ask + plan
   │  3 TEST-PLAN  │  → invariants + sabotage checks + exact commands
   └──────┬────────┘
   ┌──────▼────────┐  goal + plan + test-plan; worktree at
   │  4 IMPLEMENT  │  ~/Source/lifedraft/impl-<n>-<slug>
   └──────┬────────┘
   ┌──────▼─────────────────────┐  ┌─────────┐
   │  5 VALIDATE ←───────────── │ │ FIXER   │  loop until verdict == pass
   │    (fresh validator)       │ │ (fresh) │  every fail feeds requiredFix
   └──────┬─────────────────────┘ └─────────┘
   ┌──────▼────────┐
   │  6 OPEN PR    │  draft PR to main
   └──────┬────────┘
   ┌──────▼──────────────────────┐  ┌────────────┐
   │  7 CI MONITOR ←──┬───────── │ │ CI FIXER   │  any failed check → fix +
   │    pending/      │          │ │ + certify  │  re-monitor from scratch
   └──────────────────┴───────── │ └────────────┘
                                 └── exits the loop when state == green
   ┌───────────────┐
   │  8 READY      │  re-proves green on the current head, then gh pr ready
   └───────────────┘
```

### How to invoke

```js
Workflow({ name: 'implement-github-issue',
           args: { issue: 'https://github.com/elecnix/lifedraft/issues/123',
                   repo: 'elecnix/lifedraft' } })
```

- `args.issue` — a GitHub issue number or URL (required).
- `args.repo` — `"<owner>/<repo>"`; defaults to `elecnix/lifedraft`.

### Behaviour worth knowing

- **Fresh context, always.** Plan does not remember the fetch; the validator does
  not remember the implementer; the CI monitor does not remember itself between
  rounds. Everything is passed in the prompt. This is deliberate: it is what lets
  the validator audit rather than rubber-stamp.
- **The same worktree is reused across fixes.** The implementer creates
  `~/Source/lifedraft/impl-<n>-<slug>` once; every later fixer edits it in place
  and pushes, so the validator and CI always see the whole accumulated diff.
- **Repo policy is baked in.** Never `main`, never `--no-verify`, no allowlist
  entries, the explicit-refspec `origin/main` fetch, DP#15, state-method-beside-result.
- **Verdict is only `pass` on first-hand evidence.** The validator re-runs the
  test-plan commands, spot-checks at least one sabotage, and only then reports.
- **CI failures loop.** A failed check spawns a fixer, then a certifier, then the
  monitor restarts from scratch. A certifier that cannot certify stops the run.
- **Code-writing stages own the branch, never the PR.** The first dogfood run
  (#247 → #263) caught the implementer opening the PR, waiting on CI and marking
  it ready itself, so the PR reached reviewers before the validator had judged
  it. The implementer and fixers are now told the PR is out of scope. If a PR
  exists anyway when the PR stage runs, that stage records a
  `stageViolations` entry in the result and puts the PR back into draft.
- **Only the final READY stage marks the PR ready.** It checks that CI is green
  for the exact head commit the pipeline validated, which honours "mark ready
  once CI is green" without ever making a PR ready too early.
