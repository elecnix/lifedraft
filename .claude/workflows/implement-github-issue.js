export const meta = {
  name: 'implement-github-issue',
  description: 'Drive a GitHub issue end-to-end: fetch → plan → test-plan → implement → validate(fix-loop) → draft PR → CI green',
  whenToUse: 'Given a GitHub issue link or number, drive it through to a real PR whose required checks are green. Requires args: { issue: <number|url>, repo: "<owner>/<repo>" }.',
  phases: [
    { title: 'Fetch', detail: 'fetch issue details from GitHub and state the ask' },
    { title: 'Plan', detail: 'fresh planner produces an implementation plan' },
    { title: 'Test plan', detail: 'fresh reviewer defines the invariants and sabotage checks' },
    { title: 'Implement', detail: 'fresh implementer writes code + tests in an isolated worktree' },
    { title: 'Validate', detail: 'fresh validator verdicts; a fixer loop iterates until pass' },
    { title: 'Open PR', detail: 'fresh agent opens the draft pull request' },
    { title: 'CI', detail: 'fresh monitor; on failure a fixer tightens the loop until checks are green' },
  ],
}

// ---- inputs ---------------------------------------------------------------
// args.issue = GitHub issue number or https://github.com/<owner>/<repo>/issues/<n> URL
// args.repo  = "<owner>/<repo>" (defaults to this repo's remote when omitted)
const REPO = args.repo || 'elecnix/lifedraft'
const ISSUE = args.issue
if (!ISSUE) throw new Error('args.issue is required: a GitHub issue number or URL')

// ---- schemas (validated at the tool-call layer; agents call StructuredOutput) ----
const FETCH_SCHEMA = {
  type: 'object',
  properties: {
    'issueNumber': { type: 'integer' },
    'title': { type: 'string' },
    'url': { type: 'string' },
    'state': { type: 'string' },
    'body': { type: 'string' },
    'labels': { type: 'array', items: { type: 'string' } },
    'comments': { type: 'array', items: {
      type: 'object', properties: { 'author': { type: 'string' }, 'body': { type: 'string' } },
      required: ['author', 'body']
    } },
    'ask': { type: 'string' },
    'constraintsNoticed': { type: 'array', items: { type: 'string' } },
    'repoContext': { type: 'string' }
  },
  required: ['issueNumber', 'title', 'url', 'state', 'body', 'labels', 'comments', 'ask', 'constraintsNoticed', 'repoContext']
}

const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    'summary': { type: 'string' },
    'steps': { type: 'array', items: { type: 'string' } },
    'files': { type: 'array', items: { type: 'string' } },
    'risks': { type: 'array', items: { type: 'string' } },
    'acceptanceCriteria': { type: 'array', items: { type: 'string' } },
    'outOfScope': { type: 'array', items: { type: 'string' } },
    'testsToAdd': { type: 'array', items: { type: 'string' } }
  },
  required: ['summary', 'steps', 'files', 'risks', 'acceptanceCriteria', 'outOfScope', 'testsToAdd']
}

const TESTPLAN_SCHEMA = {
  type: 'object',
  properties: {
    'invariants': { type: 'array', items: { type: 'string' } },
    'sabotageChecks': { type: 'array', items: { type: 'string' } },
    'testCommands': { type: 'array', items: { type: 'string' } },
    'verificationApproach': { type: 'string' },
    'safetyWarnings': { type: 'array', items: { type: 'string' } }
  },
  required: ['invariants', 'sabotageChecks', 'testCommands', 'verificationApproach', 'safetyWarnings']
}

const IMPL_SCHEMA = {
  type: 'object',
  properties: {
    'worktreePath': { type: 'string' },
    'branchName': { type: 'string' },
    'commitSha': { type: 'string' },
    'pushed': { type: 'boolean' },
    'filesChanged': { type: 'array', items: { type: 'string' } },
    'testsAdded': { type: 'array', items: { type: 'string' } },
    'testsRun': { type: 'string' },
    'acceptanceMet': { type: 'array', items: { type: 'string' } },
    'problems': { type: 'array', items: { type: 'string' } }
  },
  required: ['worktreePath', 'branchName', 'commitSha', 'pushed', 'filesChanged', 'testsAdded', 'testsRun', 'acceptanceMet', 'problems']
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    'verdict': { type: 'string', enum: ['pass', 'fail'] },
    'rationale': { type: 'string' },
    'evidenceRead': { type: 'string' },
    'failures': { type: 'array', items: {
      type: 'object',
      properties: { 'finding': { type: 'string' }, 'evidence': { type: 'string' }, 'requiredFix': { type: 'string' } },
      required: ['finding', 'evidence', 'requiredFix']
    } }
  },
  required: ['verdict', 'rationale', 'evidenceRead', 'failures']
}

const FIX_SCHEMA = {
  type: 'object',
  properties: {
    'commitSha': { type: 'string' },
    'pushed': { type: 'boolean' },
    'filesFixed': { type: 'array', items: { type: 'string' } },
    'testsRun': { type: 'string' },
    'summary': { type: 'string' }
  },
  required: ['commitSha', 'pushed', 'filesFixed', 'testsRun', 'summary']
}

const PR_SCHEMA = {
  type: 'object',
  properties: {
    'number': { type: 'integer' },
    'url': { type: 'string' },
    'title': { type: 'string' },
    'draft': { type: 'boolean' },
    'branch': { type: 'string' },
    'baseBranch': { type: 'string' }
  },
  required: ['number', 'url', 'title', 'draft', 'branch', 'baseBranch']
}

const CI_SCHEMA = {
  type: 'object',
  properties: {
    'state': { type: 'string', enum: ['green', 'fail', 'pending'] },
    'checksSummary': { type: 'string' },
    'failedChecks': { type: 'array', items: {
      type: 'object',
      properties: { 'name': { type: 'string' }, 'url': { type: 'string' }, 'logUrl': { type: 'string' }, 'detail': { type: 'string' } },
      required: ['name']
    } },
    'botComments': { type: 'array', items: { type: 'string' } },
    'notes': { type: 'string' }
  },
  required: ['state', 'checksSummary', 'failedChecks', 'botComments', 'notes']
}

// ---- shared instruction fragments baked into every worktree-touching prompt ----
const REPO_RULES = `
Container-mandated rules for ${REPO} (repo CLAUDE.md is injected already; this names the load-bearing ones):
- Never work on main. All mutating work happens in a git worktree. The bare-repo fetch needs the explicit refspec:
  git -C ~/Source/lifedraft/main fetch origin '+refs/heads/main:refs/remotes/origin/main'
- Never --no-verify. Do not bypass hooks or guards. Do not add allowlist entries to silence a guard; fix the code.
- DP#15: NO real personal/financial data anywhere (not in code, tests, comments, commit messages, PR body). Use fabricated round numbers and role-based names.
- State your method beside your result (\"ran <command>, got <output>\"). A bare \"verified X\" is a defect in this repo.
- The engine wants the loud failure: prefer a crash over a plausible answer from absent data. Zero is a value, never a fallback.
- Definition of done: behaviour fixed AND a regression detector lands in the SAME PR; full suite read by the agent; no guard silenced; no personal data; method stated.`

const WORKTREE_PREP = (wt, branch) => `
Worktree: create it on FIRST use; on reuse, NEVER destroy the work already on the branch.
  if [ -d ${wt} ]; then
    # REUSE: the branch already carries the implementation under review. Do NOT reset it.
    git -C ${wt} fetch origin '+refs/heads/${branch}:refs/remotes/origin/${branch}' || true
    git -C ${wt} checkout ${branch}
  else
    git -C ~/Source/lifedraft/main fetch origin '+refs/heads/main:refs/remotes/origin/main'
    git -C ~/Source/lifedraft/main worktree add ${wt} -b ${branch} origin/main
  fi
  cd ${wt}
  git status --porcelain   # must be empty before you start
  if [ ! -d .venv ]; then VIRTUAL_ENV=$PWD/.venv uv venv && VIRTUAL_ENV=$PWD/.venv uv pip install -q -e ".[dev]"; fi
Run the full suite with a memory budget capped (multiple agents share the box):
  PYTEST_MEM_BUDGET_MB=8192 VIRTUAL_ENV=$PWD/.venv .venv/bin/python -m pytest -q
Never 'git reset --hard origin/main' inside a reused worktree: that would discard the commits under review. A plain
'git fetch origin main' silently does nothing in this bare-repo setup; use the explicit refspec above.`

// ---- helpers ---------------------------------------------------------------
function slug(title) {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'issue'
}
function issueText(f) {
  // rebroadcast the exact issue, verbatim, so later agents never depend on the live URL
  const comments = (f.comments || []).map(c => '@' + c.author + ': ' + c.body).join('\n\n')
  return [
    'ISSUE #' + f.issueNumber + ' — ' + f.title,
    'URL: ' + f.url,
    '',
    '--- ACTUAL ISSUE BODY ---',
    f.body || '(no body)',
    '',
    '--- ISSUE COMMENTS ---',
    comments || '(none)',
  ].join('\n')
}

phase('Fetch')

// 1) Fetch + orient. Outputs the "goal" that every later stage carries forward.
const fetched = await agent(
  'You are the FETCH agent of an issue-implementation pipeline. You have ONE job: read the GitHub issue at ' + ISSUE + '\n' +
  'in repo ' + REPO + ' and come back with an unambiguous statement of what is being asked and what "done" means.\n' +
  'Do not modify any files. Do not create a worktree. You exist only to gather and orient.\n\n' +
  'Steps:\n' +
  '1. Normalize the issue reference. Run: gh issue view ' + ISSUE + ' --repo ' + REPO +
  ' --json number,title,body,url,state,labels,comments --jq .   (if ' + ISSUE + ' is a URL, pass its number)\n' +
  '2. Read the title, body, and every comment carefully.\n' +
  '3. If the issue links an upstream resource, document, or file, fetch/read that too with the available tools so your orientation is grounded.\n' +
  '4. Return the schema below.\n\n' +
  'Your ask field — the heart of the deliverable — must be a crisp paragraph stating, in the repo’s own terms:\n' +
  '   - WHAT is being asked (the concrete change/deliverable)\n' +
  '   - WHO it must behave for, if relevant\n' +
  '   - what "done" looks like (observable, checkable)\n' +
  '   - which area of the repo it touches (engine / config / schema / report / tooling / docs / CI), so downstream\n' +
  '     agents know which design principles and which guard rails apply (see CLAUDE.md "Design principles — index"\n' +
  '     and "The guards will fight you" tables).\n' +
  '5. constraintsNoticed: any hard constraints in the issue text or repo rules that bound the solution\n' +
  '   (e.g. "doc-only, no .py changes", "DP#15 no personal data", "must add a regression test", exact values not to change).\n' +
  '6. repoContext: 2-4 sentences of orientation from actually reading the repo (which file does the issue point\n' +
  '    at, what is around it), so the planner does not start blind.\n\n' +
  'Enforcement: this repo’s golden invariant and guards matter downstream. If the issue OBVIOUSLY targets a live simulation\n' +
  'behaviour, note in constraintsNoticed that the golden invariant (see CLAUDE.md) must not move. State method beside result.\n\n' +
  REPO_RULES,
  { phase: 'Fetch', label: 'fetch-issue', schema: FETCH_SCHEMA, effort: 'medium' }
)
if (!fetched) throw new Error('Fetch agent returned null — aborting.')
log('Issue #' + fetched.issueNumber + ' fetched: "' + fetched.title + '"')

const ISSUE_FULL = issueText(fetched)
// '~' is expanded by the agent's shell; never bake a user's absolute home path into the repo.
const WT_DIR = '~/Source/lifedraft/impl-' + fetched.issueNumber + '-' + slug(fetched.title)
const BRANCH = 'fix/' + fetched.issueNumber + '-' + slug(fetched.title)

phase('Plan')

// 2) Plan. Fresh context: receives the goal + the verbatim issue text.
const plan = await agent(
  'You are the PLANNER of an issue-implementation pipeline. You start fresh. Your input is a goal produced by a\n' +
  'FETCH agent plus the verbatim issue text it gathered.\n\n' +
  '=== THE ASK (goal from FETCH agent) ===\n' +
  fetched.ask + '\n\n' +
  '=== THE ISSUE, VERBATIM ===\n' +
  ISSUE_FULL + '\n\n' +
  '=== CONSTRAINTS NOTICED BY FETCH ===\n' +
  fetched.constraintsNoticed.join('\n') + '\n\n' +
  '=== REPO CONTEXT FROM FETCH ===\n' +
  fetched.repoContext + '\n\n' +
  'Your job: read the actual repo, then produce a concrete, reviewable IMPLEMENTATION PLAN. You do NOT modify any files.\n\n' +
  'Procedure:\n' +
  '1. Find the files the issue points at in the CURRENT tree (you are checked out in a repo worktree; read with your tools).\n' +
  '2. Read the relevant DESIGN_PRINCIPLES.md rows the task touches, and the guard table in CLAUDE.md. Note which guards will\n' +
  '   fire on this change and how the implementation satisfies them without silent zero fallbacks (DP#32) or allowlists.\n' +
  '3. Plan in numbered steps that are small enough to review and atomic enough to commit together.\n' +
  '4. testsToAdd: name the specific unit/integration test(s) that prove the fix and guard its regression (DoD#1:\n' +
  '   a detector that fails if the behaviour regresses must land IN the same PR).\n' +
  '5. Decide what to change and HOW to prove it. The implementer will work in ' + WT_DIR + ' (branch ' + BRANCH + ') regardless.\n' +
  '6. risks: name the trap shapes this codebase has actually fallen into (see CLAUDE.md "Traps this codebase has actually\n' +
  '   fallen into") that this change is vulnerable to, and the specific countermeasure.\n\n' +
  'Return the plan schema. Be concrete: file paths, function names, exact invariants. No vague steps like "fix the issue".\n\n' +
  'Enforcement: plan for the loud failure. If a silent-zero shape is even plausible, the plan must call for a crash/refusal.',
  { phase: 'Plan', label: 'plan', schema: PLAN_SCHEMA, effort: 'high' }
)
if (!plan) throw new Error('Plan agent returned null')
log('Plan ready: ' + plan.steps.length + ' steps across ' + plan.files.length + ' files')

phase('Test plan')

// 3) Test plan (adversarial reviewer). Fresh context: the ask + the plan.
const testPlan = await agent(
  'You are the TEST-DESIGN REVIEWER of an issue-implementation pipeline. You start fresh. You have NOT seen the issue\n' +
  'before; you receive the FETCH agent’s ask and the PLANNER’s plan, and your entire job is to define how the result\n' +
  'will be PROVEN correct — and how a wrong result will be CAUGHT. You do NOT modify any files.\n\n' +
  '=== THE ASK ===\n' +
  fetched.ask + '\n\n' +
  '=== THE ISSUE, VERBATIM ===\n' +
  ISSUE_FULL + '\n\n' +
  '=== THE IMPLEMENTATION PLAN (to be scrutinized) ===\n' +
  JSON.stringify(plan, null, 2) + '\n\n' +
  'Your deliverable is a TEST PLAN. This repo’s whole point: a confident wrong number is worse than a crash, and ~3900 tests\n' +
  'once failed to catch a silent zero (see CLAUDE.md). So think adversarially:\n\n' +
  '1. invariants: a comprehensive, checkable list — after the implementation lands, each of these must hold. Cover:\n' +
  '   - the acceptance criteria from the plan, restated as checkable statements;\n' +
  '   - money/economy invariants if this touches simulation (total assets conserved, ACB<=FMV, RRIF minimum etc. — see CLAUDE.md\n' +
  '     golden invariant and tests/trajectory_invariants.py);\n' +
  '   - schema-contract invariants if it touches the input contract;\n' +
  '   - anti-silent-zero invariants (DP#32): a missing input must NOT default to a plausible value;\n' +
  '   - diff-shape invariants: exactly the intended files changed, nothing else (this repo uses\n' +
  '     git diff origin/main --diff-filter=MDR --name-only — if that is EMPTY, no invariant moved BY CONSTRUCTION; if it\n' +
  '     touches only non-code like README.md, you must argue the construction that no behaviour changed);\n' +
  '   - DP#15: no real personal/financial data anywhere in the diff.\n' +
  '2. sabotageChecks: for each invariant, name a DELIBERATE sabotage this suite must catch — e.g. "a patch that changes TWO\n' +
  '   em dashes instead of one must fail", "a patch that silently substitutes 0 for a missing input must fail", "a patch that\n' +
  '   touches a schema leaf nothing consumes must fail", "a patch that only edits the test to match a broken engine must fail".\n' +
  '   The validator must be able to run at least a spot-check of these by temporarily injecting the sabotage and confirming it\n' +
  '   faults. Give exact injection recipes where possible.\n' +
  '3. testCommands: exact shell commands (with the memory-budget env var) that prove each invariant: the full suite\n' +
  '   PYTEST_MEM_BUDGET_MB=8192 .venv/bin/python -m pytest -q plus any targeted test, plus any git diff / gh check.\n' +
  '4. verificationApproach: the overall strategy — which evidence the validator MUST read first-hand rather than trust.\n' +
  '5. safetyWarnings: anything the implementer/validator must be careful about (never --no-verify, coverage gate ratchet\n' +
  '   clicks only one way and must be regenerated in the same PR (tools/coverage_gate.py --update) if a file’s uncovered-line\n' +
  '   count legitimately changes, do not reimplement the engine in the test (DP#11/DP#26), etc.).\n\n' +
  'The validator downstream will literally execute these commands and checks. Make them unambiguous.',
  { phase: 'Test plan', label: 'test-plan', schema: TESTPLAN_SCHEMA, effort: 'high' }
)
if (!testPlan) throw new Error('Test-plan agent returned null')
log('Test plan ready: ' + testPlan.invariants.length + ' invariants, ' + testPlan.sabotageChecks.length + ' sabotage checks')

phase('Implement')

// 4) Implementer. Fresh context: goal + plan + test plan. Does the real work in an isolated worktree.
const implementation = await agent(
  'You are the IMPLEMENTER of an issue-implementation pipeline. You start fresh. Everything you need is below. You do the\n' +
  'actual implementation. You have one job and it must be verifiable, not asserted.\n\n' +
  '=== THE ASK ===\n' +
  fetched.ask + '\n\n' +
  '=== THE ISSUE, VERBATIM ===\n' +
  ISSUE_FULL + '\n\n' +
  '=== THE IMPLEMENTATION PLAN ===\n' +
  JSON.stringify(plan, null, 2) + '\n\n' +
  '=== THE TEST PLAN — the invariants your work MUST satisfy ===\n' +
  'invariants:\n  ' + testPlan.invariants.join('\n  - ') + '\n' +
  'sabotageChecks:\n  ' + testPlan.sabotageChecks.join('\n  - ') + '\n' +
  'testCommands:\n  ' + testPlan.testCommands.join('\n  - ') + '\n' +
  'verificationApproach: ' + testPlan.verificationApproach + '\n' +
  'safetyWarnings:\n  ' + testPlan.safetyWarnings.join('\n  - ') + '\n\n' +
  '=== WORKTREE / BRANCH (must use exactly these) ===\n' +
  WORKTREE_PREP(WT_DIR, BRANCH) + '\n\n' +
  'Core instructions:\n' +
  '1. Set up the worktree at ' + WT_DIR + ' (branch ' + BRANCH + ') exactly as above, synced to latest origin/main.\n' +
  '2. Implement the plan so that every acceptance criterion and invariant is met, and add every test the plan/test-plan name.\n' +
  '   If the plan calls for tests that reproduce the engine by hand instead of driving FamilySimulation.run() / the fold, STOP\n' +
  '   and write tests that drive the engine instead (DP#11/#26) — that shortcut is how this codebase shipped wrong before.\n' +
  '3. Commit when green locally with a message like: fix(#' + fetched.issueNumber + '): ' + slug(fetched.title) + '\n' +
  '   Include the standard attribution: Co-Authored-By: Claude Code <noreply@anthropic.com>\n' +
  '   Do NOT commit on main; you are in ' + BRANCH + '. Never --no-verify, never add a guard allowlist entry.\n' +
  '4. Push to origin/' + BRANCH + ' so the pipeline’s later stages (and CI) can see it.\n' +
  '5. If a coverage-file uncovered-line count legitimately changed, regenerate the baseline in the SAME commit:\n' +
  '   python tools/coverage_gate.py --update   (do not iterate this through CI).\n\n' +
  'Your testsRun field MUST state the exact commands you ran and their outputs ("ran X, got Y") — this repo treats a bare\n' +
  '"verified" as a defect. If the full suite takes very long, run the targeted tests first to get confidence, then the full\n' +
  'suite once before marking done. In problems, say honestly anything you could NOT verify.\n\n' +
  REPO_RULES,
  { phase: 'Implement', label: 'implement', schema: IMPL_SCHEMA, effort: 'high' }
)
if (!implementation) throw new Error('Implementer agent returned null')
if (!implementation.pushed) throw new Error('Implementer did not push ' + BRANCH + ' — aborting. ' + implementation.problems.join('; '))
log('Implemented on ' + BRANCH + ' (' + implementation.commitSha.slice(0, 8) + '): ' + implementation.filesChanged.length + ' files changed')

// The branch head moves when a fixer pushes; all later stages must judge the CURRENT head,
// never the implementer's original sha (otherwise round 2+ reports a false HEAD mismatch).
let headSha = implementation.commitSha

phase('Validate')

// 5) Validate + fix loop. Fresh validator each round; a fixer between failed rounds.
const MAX_FIX_ROUNDS = 5
let verdict = null
let fixRound = 0
do {
  verdict = await agent(
    'You are the VALIDATOR of an issue-implementation pipeline. You start fresh and are fully adversarial: your only\n' +
    'loyalty is to the truth of the verdict. The implementer’s work is a claim; you verify it first-hand. Do NOT trust their\n' +
    'report — re-run what is required and READ the output.\n\n' +
    '=== THE ASK ===\n' +
    fetched.ask + '\n\n' +
    '=== THE ISSUE, VERBATIM ===\n' +
    ISSUE_FULL + '\n\n' +
    '=== THE IMPLEMENTATION PLAN ===\n' +
    JSON.stringify(plan, null, 2) + '\n\n' +
    '=== THE TEST PLAN (invariants you must confirm, sabotage you must spot-check) ===\n' +
    'invariants:\n  ' + testPlan.invariants.join('\n  - ') + '\n' +
    'sabotageChecks:\n  ' + testPlan.sabotageChecks.join('\n  - ') + '\n' +
    'testCommands:\n  ' + testPlan.testCommands.join('\n  - ') + '\n' +
    'verificationApproach: ' + testPlan.verificationApproach + '\n\n' +
    '=== WHAT THE IMPLEMENTER REPORTED ===\n' +
    'worktree: ' + implementation.worktreePath + ' (branch ' + implementation.branchName + ', current HEAD ' + headSha + ')\n' +
    'filesChanged: ' + implementation.filesChanged.join(', ') + '\n' +
    'their testsRun: ' + implementation.testsRun + '\n' +
    'their acceptanceMet: ' + implementation.acceptanceMet.join('; ') + '\n' +
    'their problems: ' + implementation.problems.join('; ') + '\n\n' +
    '=== PROCEDURE (do in order, READ the output yourself) ===\n' +
    '1. cd ' + implementation.worktreePath + '; git log -1 --format="%H %s"; git status --porcelain; git diff --stat origin/main.\n' +
    '   Confirm ' + implementation.branchName + ' is a real branch, ' + headSha + ' is HEAD, the working tree is clean.\n' +
    '2. Determine the diff shape: git diff origin/main --diff-filter=MDR --name-only. Assert EACH engine/sim behaviour invariant\n' +
    '   (golden invariant, conservation, ACB<=FMV, RRIF min) holds: either by construction (only non-code files changed, so no\n' +
    '   code path differs) or by running the proof. Name the exact argument you made in rationale.\n' +
    '3. PEDESTAL OF EVIDENCE: run the target test commands first-hand, including the full suite. Use PYTEST_MEM_BUDGET_MB=8192\n' +
    '   as the env. Record results.\n' +
    '4. Spot-check at least ONE sabotage from the test plan by injecting it temporarily, confirming the expected test faults,\n' +
    '   then reverting the injection cleanly (git checkout of the sabotaged file) so the tree returns exactly to ' + headSha + '.\n' +
    '5. Scan the diff for DP#15 violations (grep for anything that looks like a real person/account/figure).\n' +
    '6. Confirm DoD: regression detector in the same PR; no guard silenced via allowlist entry; no --no-verify anywhere in history\n' +
    '   for this branch (git log --oneline ' + implementation.branchName + ').\n\n' +
    'Verdict rules:\n' +
    '- verdict pass ONLY if every invariant holds, the commands pan out, and you actually read the outputs.\n' +
    '- verdict fail if ANY invariant fails, any sabotage went uncaught, any command could not be read, evidence is missing, or a\n' +
    '  silent-zero/guard-evasion shape appears. EVERY failure must carry a requiredFix the fixer can act on.\n\n' +
    REPO_RULES,
    { phase: 'Validate', label: 'validate-' + (fixRound + 1), schema: VERDICT_SCHEMA, effort: 'high' }
  )
  if (!verdict) throw new Error('Validator ' + (fixRound + 1) + ' returned null')

  if (verdict.verdict === 'pass') {
    log('Validator ' + (fixRound + 1) + ' VERDICT: PASS')
    break
  }

  log('Validator ' + (fixRound + 1) + ' VERDICT: FAIL (' + verdict.failures.length + ' failures) — spawning fixer ' + (fixRound + 1))
  fixRound++
  if (fixRound > MAX_FIX_ROUNDS) throw new Error('Validation did not pass after ' + MAX_FIX_ROUNDS + ' fix rounds. Last failures: ' + verdict.failures.map(f => f.finding).join(' | '))

  const fix = await agent(
    'You are the FIXER of an issue-implementation pipeline. You start fresh. The validator judged the current implementation\n' +
    'and failed it. Your job: act on its findings exactly, then push. Re-create the fix in the SAME worktree so the next\n' +
    'validator sees the whole picture.\n\n' +
    '=== THE ASK ===\n' +
    fetched.ask + '\n\n' +
    '=== THE ISSUE, VERBATIM ===\n' +
    ISSUE_FULL + '\n\n' +
    '=== THE IMPLEMENTATION PLAN ===\n' +
    JSON.stringify(plan, null, 2) + '\n\n' +
    '=== THE TEST PLAN ===\n' +
    'invariants:\n  ' + testPlan.invariants.join('\n  - ') + '\n' +
    'testCommands:\n  ' + testPlan.testCommands.join('\n  - ') + '\n' +
    'safetyWarnings:\n  ' + testPlan.safetyWarnings.join('\n  - ') + '\n\n' +
    '=== VALIDATOR’S FAILURES (this is the executable spec of what to fix) ===\n' +
    JSON.stringify(verdict.failures, null, 2) + '\n\n' +
    '=== WHERE THE WORK IS ===\n' +
    'cd ' + implementation.worktreePath + '   (branch ' + implementation.branchName + ', currently at ' + headSha + ' — reuse the existing worktree, do NOT delete it)\n\n' +
    WORKTREE_PREP(WT_DIR, BRANCH) + '\n\n' +
    '1. Fix EVERY validator failure (the finding/evidence/requiredFix triplet tells you what and why). Do not stop at the first.\n' +
    '2. Add/extend tests so each fixed item has a detector that fails if it regresses.\n' +
    '3. Run the targeted tests, then the full suite with PYTEST_MEM_BUDGET_MB=8192. Read the output.\n' +
    '4. Commit on ' + implementation.branchName + ' (not main), message: fix(#' + fetched.issueNumber + '): address validator findings.\n' +
    '   Include attribution: Co-Authored-By: Claude Code <noreply@anthropic.com>. Never --no-verify; never an allowlist entry.\n' +
    '5. Push to origin/' + implementation.branchName + '. Return the new HEAD sha and pushed=true.\n\n' +
    'State method beside result. If you cannot fix something, say so in summary with the blocker.',
    { phase: 'Validate', label: 'fixer-' + fixRound, schema: FIX_SCHEMA, effort: 'high' }
  )
  if (!fix) throw new Error('Fixer ' + fixRound + ' returned null')
  if (!fix.pushed) throw new Error('Fixer ' + fixRound + ' did not push — aborting. ' + fix.summary)
  headSha = fix.commitSha
  log('Fixer ' + fixRound + ' pushed ' + fix.commitSha.slice(0, 8) + ' — re-validating')
} while (verdict.verdict !== 'pass')

phase('Open PR')

// 6) Open the draft PR.
const pr = await agent(
  'You are the PR agent of an issue-implementation pipeline. You start fresh. A validated implementation exists on branch\n' +
  BRANCH + '; open a DRAFT pull request to main for it.\n\n' +
  '=== THE ISSUE ===\n' +
  fetched.title + ' — ' + fetched.url + '\n\n' +
  '=== WHAT WAS IMPLEMENTED ===\n' +
  'worktree: ' + implementation.worktreePath + '\n' +
  'branch/head: ' + implementation.branchName + ' @ ' + headSha + '\n' +
  'files changed: ' + (implementation.filesChanged.join(', ') || '(see diff)') + '\n' +
  'implementation summary (their testsRun): ' + implementation.testsRun + '\n' +
  'validator rationale: ' + verdict.rationale + '\n\n' +
  '=== ACTIONS ===\n' +
  '1. git -C ' + implementation.worktreePath + ' fetch origin \'+refs/heads/main:refs/remotes/origin/main\'\n' +
  '   and confirm ' + BRANCH + ' is pushed (git -C ' + implementation.worktreePath + ' log origin/' + BRANCH + ' -1).\n' +
  '2. Write the PR body to a temp file (the description must render well on GitHub: use flowing paragraphs, NOT manual\n' +
  '   hard-wrapping at ~80 cols — that is what the pr-body-format action flags). Include:\n' +
  '   - the issue number and a one-line summary;\n' +
  '   - what changed, why (link the root cause if any);\n' +
  '   - how it was verified (state the method: run X, got Y) per this repo’s reporting discipline;\n' +
  '   - the acceptance criteria met;\n' +
  '   - the attribution footer line: 🤖 Generated with [Claude Code](https://claude.com/claude-code)\n' +
  '   NO personal or financial data anywhere (DP#15).\n' +
  '3. Open the PR as DRAFT:\n' +
  '   gh pr create --repo ' + REPO + ' --head ' + BRANCH + ' --base main --draft --title "fix(#' + fetched.issueNumber + '): ' + slug(fetched.title) + '" --body-file <tmpfile>\n' +
  '   Return the schema (number, url, title, draft=true, branch, baseBranch).',
  { phase: 'Open PR', label: 'open-pr', schema: PR_SCHEMA, effort: 'low' }
)
if (!pr) throw new Error('PR agent returned null')
log('Draft PR #' + pr.number + ' opened: ' + pr.url)

phase('CI')

// 7) CI monitor + fix loop. Monitor polls until it can read a definite state; on failure spawns a fixer
// that pushes, then a fresh validator certifies the fix, then the monitor restarts from scratch.
const MAX_CI_ROUNDS = 9
let ciState = null
let ciRound = 0
while (ciRound < MAX_CI_ROUNDS) {
  ciState = await agent(
    'You are the CI MONITOR of an issue-implementation pipeline. You start fresh. PR #' + pr.number + ' (draft) in ' + REPO +
    '\nwas just opened from branch ' + BRANCH + '. Your job: determine the real CI state and report it — you do NOT fix anything\n' +
    'here; you return pending / green / fail and the pipeline decides.\n\n' +
    '=== ACTIONS ===\n' +
    '1. Run gh pr checks ' + pr.number + ' --repo ' + REPO + ' --json name,state,bucket,link,description,workflow --jq .\n' +
    '   The ONLY valid --json fields are bucket, completedAt, description, event, link, name, startedAt, state, workflow —\n' +
    '   there is no conclusion/url/detailsUrl field, so requesting one is an error, not a finding. Categorize with bucket:\n' +
    '   fail => FAIL, pending => still running, pass/skipping => ok. If bucket is absent, fall back to state:\n' +
    '   FAILURE/ERROR/ACTION_REQUIRED/TIMED_OUT/CANCELLED/STALE => FAIL; PENDING/QUEUED/IN_PROGRESS/EXPECTED => running;\n' +
    '   SUCCESS/NEUTRAL/SKIPPED => ok.\n' +
    '2. If any check is still running (the Tests job on a PR runs the full suite, minutes), BLOCK and watch:\n' +
    '   timeout 540 gh pr checks ' + pr.number + ' --repo ' + REPO + ' --watch --interval 30 || true\n' +
    '   then re-run step 1. You may repeat this bounded watch up to ~4 times; each returns a fresh snapshot. Do not let it\n' +
    '   extend past ~40 minutes of tries; report pending if it still has not concluded.\n' +
    '3. Read the repo’s workflow files (.github/workflows/*) to know which runs to expect; every workflow that declares\n' +
    '   runs-on ubuntu-latest runs on GitHub-hosted runners (repo is public, minutes are free).\n' +
    '4. Capture log URLs for every failed check from step 1, and if a failed check exists, fetch its logs best-effort\n' +
    '   (gh run view --log-failed) and summarize the failing assertion into failedChecks[].detail.\n' +
    '5. Report bot comments on the PR (e.g. the cite reviewer / pr-body-format advisories) into botComments:\n' +
    '   gh pr view ' + pr.number + ' --repo ' + REPO + ' --json comments,reviews\n' +
    '   These are informational only; only failed-check STATE drives the fail verdict.\n\n' +
    'Return exactly one of: green (all checks SUCCESS/NEUTRAL/SKIPPED), fail (any FAILURE/TIME_OUT/CANCELLED/ACTION_REQUIRED with\n' +
    'its name/url/logUrl/detail), or pending (still running / could not read). Be honest: if you could not read conclusively,\n' +
    'report pending, not green.',
    { phase: 'CI', label: 'ci-monitor-' + (ciRound + 1), schema: CI_SCHEMA, effort: 'medium' }
  )
  if (!ciState) throw new Error('CI monitor ' + (ciRound + 1) + ' returned null')
  log('CI monitor ' + (ciRound + 1) + ': ' + ciState.state + (ciState.failedChecks.length ? ' (failures: ' + ciState.failedChecks.map(c => c.name).join(', ') + ')' : ''))

  if (ciState.state === 'green') break
  ciRound++

  if (ciState.state === 'fail') {
    log('CI FAILURE — spawning CI fixer ' + ciRound)
    const ciFix = await agent(
      'You are the CI FIXER of an issue-implementation pipeline. You start fresh. The CI monitor found failing checks on\n' +
      'PR #' + pr.number + ' (' + BRANCH + '). Investigate the real failures and fix them, then push.\n\n' +
      '=== FAILING CHECKS (name/url/logUrl/detail) ===\n' +
      JSON.stringify(ciState.failedChecks, null, 2) + '\n\n' +
      '=== MONITOR NOTES ===\n' +
      ciState.notes + '\n\n' +
      '=== THE ASK ===\n' +
      fetched.ask + '\n\n' +
      '=== THE PLAN ===\n' +
      JSON.stringify(plan, null, 2) + '\n\n' +
      '=== THE TEST PLAN ===\n' +
      JSON.stringify(testPlan, null, 2) + '\n\n' +
      '=== CURRENT WORK ===\n' +
      'cd ' + implementation.worktreePath + '   (branch ' + BRANCH + '; worktree already set up — do NOT reset it)\n\n' +
      '1. git -C ' + implementation.worktreePath + ' fetch origin \'+refs/heads/main:refs/remotes/origin/main\' and make sure\n' +
      '   you are on ' + BRANCH + ' with the pushed HEAD.\n' +
      '2. For each failed check, READ its actual failure: pull the run logs (gh run view --log-failed with the run id from the\n' +
      '   check link, or gh pr checks ' + pr.number + ' --repo ' + REPO + ' --json name,link,bucket) and extract the exact failing\n' +
      '   assertion/step.\n' +
      '   Never guess what failed.\n' +
      '3. Fix the root cause, add/extend a detector test, run the targeted check locally, then the full suite with\n' +
      '   PYTEST_MEM_BUDGET_MB=8192. For a coverage-gate failure, regenerate the baseline in the same PR: python tools/coverage_gate.py --update.\n' +
      '4. Commit on ' + BRANCH + ' (fix(#' + fetched.issueNumber + '): address CI findings) with attribution\n' +
      '   (Co-Authored-By: Claude Code <noreply@anthropic.com>), push to origin/' + BRANCH + '. Never --no-verify; never an allowlist\n' +
      '   entry. State method beside result. If you could not fix something, say so in summary with the blocker.',
      { phase: 'CI', label: 'ci-fixer-' + ciRound, schema: FIX_SCHEMA, effort: 'high' }
    )
    if (!ciFix) throw new Error('CI fixer ' + ciRound + ' returned null')
    if (!ciFix.pushed) throw new Error('CI fixer ' + ciRound + ' did not push — aborting. ' + ciFix.summary)

    // certify the fix before letting the monitor restart from scratch
    const certify = await agent(
      'You are the CI-FIX CERTIFIER. You start fresh. The CI fixer pushed ' + ciFix.commitSha + ' to ' + BRANCH + ' (PR #' + pr.number +
      '\nafter CI failures. Certify the fix is real BEFORE monitoring restarts: cd ' + implementation.worktreePath + '; git log -1 --format="%H %s";\n' +
      'confirm HEAD == ' + ciFix.commitSha + ', working tree clean; if a failing check was a test/coverage failure, re-run that exact\n' +
      'check locally with PYTEST_MEM_BUDGET_MB=8192 and confirm green. Do NOT modify anything. Set pushed=true only if the tree is clean\n' +
      'and the local check you re-ran is green. Return a one-line summary of what you verified.',
      { phase: 'CI', label: 'ci-certify-' + ciRound, schema: FIX_SCHEMA, effort: 'medium' }
    )
    if (!certify) throw new Error('CI certifier ' + ciRound + ' returned null')
    log('CI fix ' + ciFix.commitSha.slice(0, 8) + ' pushed and certified — re-monitoring from scratch')
  } else {
    log('CI still pending (round ' + ciRound + ') — re-monitoring')
  }
}
if (!ciState || ciState.state !== 'green') throw new Error('CI did not go green within ' + MAX_CI_ROUNDS + ' monitor rounds. Last state: ' + (ciState && ciState.state))

log('Done. PR #' + pr.number + ' is green on CI: ' + pr.url)

return {
  issue: { number: fetched.issueNumber, title: fetched.title, url: fetched.url },
  ask: fetched.ask,
  plan: plan.summary,
  implementation: { branch: implementation.branchName, headSha: headSha, files: implementation.filesChanged },
  pr: { number: pr.number, url: pr.url, title: pr.title },
  ciState: ciState.state,
  fixRoundsUsed: fixRound,
  ciRoundsUsed: ciRound,
}
