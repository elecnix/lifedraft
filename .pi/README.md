# Pi Subagent Agents & Chains

This project uses [Pi subagents](https://github.com/earendil-works/pi-coding-agent) for automated workflows. Agent definitions live in `.pi/agents/` and chain definitions in `.pi/chains/`.

## Chains

### issue-pipeline

Identifies the top 5 priority issues, spawns async implementers to create or advance PRs, runs quality gate reviews as each finishes, merges PRs that pass all quality gates, reruns quality gates after conflict resolution, and returns a structured summary of all issues, PRs, and quality gate results.

```
issue-analyzer  →  impl-orchestrator
  (triage)           (spawn 5 async implementers)
                          | as each completes (async notification)
                        async quality gate review
                          |
                        merge or iterate
                          |
                        return structured summary
```

**How to invoke:**

```
/run-chain issue-pipeline -- Analyze all open issues, implement the top 5, review, and merge passing ones.
```

Task description is mandatory. Example tasks:

- `Analyze all open GitHub issues, prioritize the top 5, implement PRs, review, and merge passing ones.`
- `Focus on jurisdiction-labeled issues; implement the top 5, review, and merge.`
- `Re-triage all issues and push forward the top 5 priority PRs.`

---

### design-principles-review-pipeline

Reviews the codebase for design principle violations (static), runs the model and audits its trajectory for behavioural defects (dynamic), enumerates the rule space and checks every rule exists and fires (absence), finds which findings aren't already tracked as GitHub issues, and creates issues for new findings.

```
design-principles-reviewer  →  behavioural-auditor  →  absence-auditor  →  design-principles-gap-finder  →  design-principles-issue-creator
    (review code, static)      (run model, dynamic)    (enumerate rules)    (compare vs existing issues)      (create GH issues)
```

A purely static reviewer can only flag code that is wrong; it cannot notice a
rule that was never written, or a value that compounds at 0% for the whole
projection because nothing ever runs. `behavioural-auditor` runs the
canonical golden household through the trajectory-invariant harness landed by
issue #581 (`tests/trajectory_invariants.py` + `tests/test_golden_trajectory_581.py`),
then audits the year-by-year output — not the source — for defects **no
registered invariant covers yet**, and proposes each new finding as a new
`@invariant` so the harness compounds instead of the audit evaporating. It
builds on that harness; it does not fork it or hand-roll a second scenario.
`absence-auditor` enumerates every input-schema leaf, tax rule, and account
lifecycle rule the jurisdiction implies, and checks each one is not just
implemented but actually reached by a production run — corroborating with a
perturb-and-diff against the same golden household (see #593). Findings from
all three phases carry through `design-principles-gap-finder` with their
source angle attached, and `design-principles-issue-creator` labels each one
accordingly — `design-principles` for static/absence-schema findings, `bug`
(with severity and a reproduction) for behavioural findings and absence
findings that are a dead rule rather than a dead schema leaf.

**How to invoke:**

```
/run-chain design-principles-review-pipeline -- Review the codebase for design principle violations, find gaps against existing issues, and create issues for new violations.
```

Task description is mandatory. Example tasks:

- `Review the codebase for design principle violations and create issues for any that aren't already tracked.`
- `Check countries/canada/ for DP violations and file issues for new ones.`
- `Focus on DP#11 and DP#17 violations in the simulation module.`
- `Run the full pipeline including behavioural and absence audits, and file bugs for anything the model gets wrong at runtime.`

---

### jurisdiction-audit

Scans the codebase for jurisdictions and government programs, tags existing issues with jurisdiction labels, researches official rules for each program, and creates issues for coverage gaps.

```
jurisdiction-scanner  →  jurisdiction-tagger  →  program-researcher  →  coverage-gap-issuer
  (scan code)            (tag GH issues)         (research rules)        (create gap issues)
```

**How to invoke:**

```
/run-chain jurisdiction-audit -- Scan the codebase for all jurisdictions and programs, tag existing issues, research official rules, and create issues for coverage gaps.
```

Task description is mandatory. Example tasks:

- `Audit all jurisdictions and programs in the codebase for coverage gaps.`
- `Focus on Canadian provinces — research official rules and find unimplemented or untested programs.`
- `Full jurisdiction audit: scan, tag, research, and create gap issues for everything.`

---

## Agents

| Agent | Thinking | Role | Used by chains |
|-------|----------|------|----------------|
| **issue-analyzer** | high | Triages open GH issues using product owner priorities (test coverage, Quebec/Canada programs, personal finance accuracy, low-hanging fruit), ranks top 5 | issue-pipeline |
| **impl-orchestrator** | high | Spawns async implementers, runs quality gates, merges passing PRs, reruns quality gates after conflict resolution, returns structured summary | issue-pipeline |
| **pr-implementer** | high | Creates/advances PRs, test-first workflow, self-reviews for slop, fixes CI and review feedback | (spawned by impl-orchestrator) |
| **pr-quality-gate** | high | Reviews a PR through 4 parallel quality gates, returns GO/NO-GO with structured findings | (spawned by impl-orchestrator) |
| **reviewer** | high | Reviews a PR from a single angle, returns concerns with severity levels (critical/high/medium/minor) | (spawned by pr-quality-gate) |
| **design-principles-reviewer** | high | Orchestrates parallel scout subagents per DP to find violations (static) | design-principles-review-pipeline |
| **behavioural-auditor** | high | Runs the model over a long horizon, audits the trajectory for wrong numbers (dynamic) | design-principles-review-pipeline |
| **absence-auditor** | high | Enumerates the rule space (schema leaves, tax rules, account lifecycles), checks each exists and fires | design-principles-review-pipeline |
| **design-principles-gap-finder** | low | Compares findings (static + behavioural + absence) against existing GH issues to find new ones | design-principles-review-pipeline |
| **design-principles-issue-creator** | low | Creates GH issues for new findings, labeled and evidenced per source angle | design-principles-review-pipeline |
| **jurisdiction-scanner** | low | Scans code for jurisdictions and programs tree | jurisdiction-audit |
| **jurisdiction-tagger** | low | Creates and applies jurisdiction/country/province labels | jurisdiction-audit |
| **program-researcher** | high | Orchestrates fractal fan-out for rule research | jurisdiction-audit |
| **coverage-gap-issuer** | high | Creates GH issues for coverage gaps | jurisdiction-audit |

## Invoking agents directly

You can also run individual agents with `/run`:

```
/run pr-quality-gate -- Review PR #71 for quality gate readiness
/run impl-orchestrator -- Implement the top 5 priority issues
/run pr-implementer -- Create a PR for issue #30
```

All agents reference `DESIGN_PRINCIPLES.md` for quality standards. Agents inherit the current session's model rather than specifying a fixed model.

## Agent Design Patterns

This project follows patterns from the [pi-subagents](https://github.com/nicobailon/pi-subagents) framework:

- **Review-only agents** (`reviewer`, `pr-quality-gate`) use `systemPromptMode: replace` to reduce prompt bloat and prevent off-task behavior. Implementation agents use `systemPromptMode: append` to inherit coding abilities.
- **Fresh context** for reviewers: `pr-quality-gate` spawns `reviewer` subagents with `context: "fresh"` so they inspect the actual diff, not inherited conversation history.
- **Three-level delegation**: orchestrator → quality gate → reviewer subagents. Each level has a clear role: orchestrator decides, quality gate synthesizes, reviewers inspect.
- **Acceptance contracts**: implementers receive explicit acceptance criteria (tests pass, lint clean, no slop, PR is draft with priority label).
- **Review loop stop rules**: stop after 3 quality gate rounds, stop if only cosmetic minor concerns remain, stop if an unapproved product/scope decision surfaces.
- **Self-review for slop**: implementers check their own diffs for AI-generated patterns (restating-code comments, dead helpers, verbose names) before creating PRs.
- **Output files go to chain temp directory**, not the code repository. They are ephemeral artifacts for inter-agent communication.

## Workflow Conventions

All agents follow these project conventions (defined in `.pi/APPEND_SYSTEM.md`):

1. **Read the task and retrieve context** before starting work.
2. **Rename the session** to reflect the task, ticket number, repo, and feature name.
3. **Create or reuse a branch** after fetching from origin main.
4. **Work in a worktree** at `~/Source/lifedraft/<branch-name>` — never on main.
5. **Create a draft PR** after all tests, lint, and type checks pass.
6. **Monitor the PR** using the GitHub monitoring tool.
7. **Address review comments** by replying and resolving each individual thread — never top-level comments.
8. **Fix CI silently** — do not post comments about CI status.

### Analysis vs Implementation

- **Analysis and orchestration** agents run on the main worktree (`~/Source/lifedraft/main`) and pull origin main before every major step.
- **Implementation** agents work in isolated worktrees (`~/Source/lifedraft/<branch-name>`).

### Merge Authorization

Only `impl-orchestrator` is authorized to merge PRs, and only when all four quality gates return GO (zero concerns at any severity), CI is green, and there are no unresolved review threads. All other agents must not merge PRs. The `impl-orchestrator` also returns a structured summary of all issues, PRs, and quality gate results to the calling agent. If a PR has merge conflicts after passing the quality gate, the implementer resolves the conflicts and the orchestrator reruns the quality gate before merging — a rebased PR must pass the quality gate again.

## Concurrent Loop Runner

`loop.py` at the repo root runs the project's long-running chains concurrently on independent intervals, gated by a shared semaphore so only one `pi` invocation runs at a time:

- `design-principles-review-pipeline` (prefix `dp`, default 1800s)
- `issue-pipeline` (prefix `issue`, default 10s)
- `jurisdiction-audit` (prefix `juris`, default 3600s)

```bash
./loop.py                                  # run all three loops
./loop.py --no-juris                       # skip jurisdiction-audit
./loop.py --issue-interval 30 --top-issues 3
./loop.py --dp-model glm-5.1 --juris-provider ollama-cloud
```

Each loop invokes `pi -p /run-chain <chain> -- <task>` on its interval. Per-loop `--<prefix>-provider`/`--<prefix>-model` flags override the default model, and `--no-<prefix>` disables a loop entirely. `Ctrl-C` / `SIGTERM` triggers a graceful shutdown after the in-flight run completes.