# Workflows

Claude Code dynamic workflows (`.js` scripts orchestrated by the Workflow tool).
Each stage below runs a **fresh agent** that receives only the accumulated output
of its predecessors — no shared context — and returns a schema-validated result.

## CI guard

`tests/architecture/test_claude_workflow_scripts.py` runs in the normal pytest suite (so `tests.yml` runs it on every PR) and checks every `*.js` in this directory. It compiles each script the way the Workflow runtime runs it: the body inside an async function, so top-level `await` and `return` are legal, with `export const meta` kept top-level. It also checks that `meta` is a pure object literal with a non-empty `name` and `description`, that the `meta.phases[].title` set equals the set of `phase('...')` arguments exactly (and every agent `phase:` option names one of them), and that no file here contains an absolute home-directory path. Use `~/...` instead. See #264. Check 5 (#268) keeps commit and PR titles out of slugs: a `slug(` call may appear only on the `const WT_DIR =` or `const BRANCH =` line, no string may hardcode a conventional-commit type literal such as `<type>(#`, and every `(#` title fragment must be preceded by `COMMIT_TYPE +` or `commitType +`. For `implement-github-issue.js` the guard also extracts the real `composeCommitTitle`, `COMMIT_TYPES`, `MAX_TITLE_LEN` and `PLAN_SCHEMA` from the script and runs them in node against a fixed case table, and it checks statically that `PR_TITLE` reaches the refusal point, the commit hints and the PR stage. Check 6 (#274) fails any script that contains both `gh pr checks` and `--watch` anywhere in the same file, because that command polls GraphQL, and any literal `sleep` or `--interval` below 60 seconds. For `implement-github-issue.js` it also runs the real `parseIssueNumber` in node against a case table, and checks statically that the issue number is parsed once and interpolated literally into the FETCH commands, that the line right after the FETCH null check refuses a mismatched issue number, and that the CI monitor, CI fixer and READY stages read CI over REST pinned to the head commit, page through every check-run, and carry the rate-limit rule.

The guard needs `node` on PATH and **fails** without it; it never skips. It compiles with node's `vm.Script` and deliberately does not use `node --check`. That flag rejects the legal top-level `return`, and on node 24 it exits 0 on unparseable files that contain ESM syntax, so it would pass the exact breakage the guard exists to catch.

## `implement-github-issue`

Drive a GitHub issue end-to-end to a **green draft PR**. The whole point is that
no single context is ever asked to both *judge* and *write*: every stage is a
fresh agent, and the same goal, plan, and test-plan are replayed verbatim into
each one so the later stages audit the earlier ones.

```
args: { issue, repo }
   ┌───────────────┐  gh api repos/<repo>/issues/<n> (REST) → ask/goal;
   │  1 FETCH      │  <n> is parsed once from args, and the run stops
   └──────┬────────┘  if the fetched issue number differs from it
   ┌──────▼────────┐  goal + verbatim issue text (+ comments)
   │  2 PLAN       │  → implementation plan (steps/files/risks/AC/tests
   │               │    + commitType/commitSubject)
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

- `args.issue` — a GitHub issue number (or its decimal string) or this repo's `https://github.com/<owner>/<repo>/issues/<n>` URL (required). Anything else is refused before any agent runs: `274abc`, `0274`, `#274`, `0`, a `/pull/` URL, a URL with a trailing slash or fragment, or a URL for another repo.
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
- **Titles come from the plan, never from a slug.** The PR title and every commit subject the pipeline suggests is `<type>(#n): <subject>`, where the plan supplies both parts (#268). The type is one of `fix`, `feat`, `docs`, `refactor`, `test`, `ci` or `chore`, enforced by a schema `enum`. The subject is a short imperative sentence written for a human, and the whole title is at most 72 characters, because a squash-merge makes the PR title the commit subject on `main`. Right after the PLAN stage, `composeCommitTitle` refuses (throws, before the TEST-PLAN stage runs) a type outside the set, an empty or whitespace subject, a slug-shaped subject with no space, a subject that already carries a `type:` prefix, a subject containing a double quote, backtick, dollar, backslash or control character (the title goes inside a double-quoted shell argument), or a title over 72 characters. It never falls back to the slug or to a default type. A legitimate subject such as `README: ...` is also refused by the prefix rule; reword it. The fixers commit as `<type>(#n): address validator findings` / `address CI findings` with the plan's type. The slug names only the worktree and the branch (`fix/<n>-<slug>`). A PR that an earlier stage already opened is retitled to the plan's title, and the run stops if the PR's actual title differs.
- **Issue identity is checked.** The script parses the issue number once (`ISSUE_N`) and writes it literally into every command, so no agent is left to resolve a placeholder. Right after FETCH, the run throws if the fetched issue's number is not `ISSUE_N`, before any planning starts (#274). A hand-patched copy once gave the FETCH agent a placeholder, the agent resolved it to the newest issue, and the whole run then worked on the wrong issue without anything noticing.
- **CI is polled over REST on the head SHA, at 60 s or more, and rate limits are waited out rather than reported as CI state.** The CI monitor, the CI fixer and the READY stage read `repos/<repo>/commits/<sha>/check-runs` (with `per_page=100` and `--paginate`) and `.../commits/<sha>/status` for the exact commit the pipeline validated. They never use the GraphQL-backed PR-checks command: two runs polling it once drained the account's GraphQL quota and every `gh` command then failed for every session (#274). Zero check-runs counts as pending, never green, and a combined status of `pending` with `total_count` 0 means there are no statuses, not that one is running. On a rate-limit error the agent reads the reset time with `gh api rate_limit` and waits until it has passed. A rate-limit error is never reported as `pending` or `fail`, and each monitor round logs its `rateLimitWaits`.
- **Only the final READY stage marks the PR ready.** It checks that CI is green
  for the exact head commit the pipeline validated, which honours "mark ready
  once CI is green" without ever making a PR ready too early.
