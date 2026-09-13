# Workflows

Claude Code dynamic workflows (`.js` scripts orchestrated by the Workflow tool).
Each stage below runs a **fresh agent** that receives only the accumulated output
of its predecessors — no shared context — and returns a schema-validated result.

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
                                 └── ends when state == green
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
  monitor restarts from scratch. The workflow ends when CI is green, not before.
