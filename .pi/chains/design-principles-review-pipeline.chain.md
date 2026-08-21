---
name: design-principles-review-pipeline
description: |
  Five-step pipeline: review code for design principle violations (static),
  run the model and audit its trajectory for behavioural defects (dynamic),
  enumerate the rule space and check every rule exists and fires (absence),
  compare all findings against existing GitHub issues to find gaps, then
  create issues for any new findings.
---

## design-principles-reviewer
phase: Review
label: Find DP violations (static)
as: violations
output: violations.md
outputMode: file-only
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Review the current codebase for design principles violations. Read DESIGN_PRINCIPLES.md first, then systematically check every design principle against the code. Report each violation with its principle number, file/line, evidence, and remediation.

## behavioural-auditor
phase: Review
label: Run the model, audit the trajectory (dynamic)
as: behavioural_findings
output: behavioural-findings.md
outputMode: file-only
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Run the canonical golden household (`tests/test_golden_trajectory_581.py`) through every invariant registered in the trajectory harness (`tests/trajectory_invariants.py`), then audit the resulting year-by-year trajectory for behavioural defects the registry does NOT yet cover — smells that are invisible in a source-code review but obvious once the model actually runs: accounts that compound at 0%, registered accounts that never decumulate past RRIF age, debt that grows with no matching invested dollar, programs that outlive their eligibility window, and sensitivity sweeps whose runs return identical numbers. Build on the #581 harness; do not fork it or hand-roll a second scenario. Distinguish known xfails (#575/#576/#577/#578 — the harness working as designed) from regressions (an invariant that should hold on main and doesn't — critical). Every new finding needs a reproduction (the offending year-by-year numbers, not a description) and a proposed `@invariant` function so the harness gets permanently stronger. This is a runtime audit, not a code review — read source only to explain a cause you already found in the numbers, never to find the bug in the first place.

## absence-auditor
phase: Review
label: Enumerate the rule space, check coverage (absence)
as: absence_findings
output: absence-findings.md
outputMode: file-only
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Enumerate every leaf in the input schema, every rule the jurisdiction's tax code implies, and every account type's lifecycle rule. For each one, determine whether it is implemented, and — separately — whether anything in the production call path actually reaches it (not just tests). A rule that is implemented but never called is the same defect as a rule that was never written. Report status per rule (implemented-and-fires / implemented-not-reached / not-implemented / unclear) with grep or run evidence, plus a coverage summary (total enumerated, count per status).

## design-principles-gap-finder
phase: Analysis
label: Compare findings with existing issues
as: gaps
output: gaps.md
outputMode: file-only
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Compare the following findings against existing GitHub issues in elecnix/lifedraft. Determine which are already tracked and which are new gaps with no existing issue. Search both open and closed issues using the gh CLI. The findings come from three different review angles — static design-principle violations, behavioural (trajectory) defects, and absence (rule-coverage) gaps — keep each finding's source angle attached as you carry it forward, since it determines how the next step labels and prioritizes the resulting issue.

Design-principle violations (static):
{outputs.violations}

Behavioural findings (dynamic — the model was actually run):
{outputs.behavioural_findings}

Absence findings (rule-space coverage):
{outputs.absence_findings}

## design-principles-issue-creator
phase: Action
label: Create issues for new findings
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Based on the following gap analysis, create one GitHub issue per new finding in the elecnix/lifedraft repo. Only create issues for findings that have no existing tracking issue.

Label by source angle, not uniformly:
- Static design-principle violations get the `design-principles` label.
- Behavioural findings (from behavioural-auditor) are bugs, not style violations — label them `bug` (plus `design-principles` only if a specific principle is also violated). Title them `[bug]`, not `[DP#N]`. Include a `severity` line (critical/high/medium/low, as assessed by behavioural-auditor) and the exact reproduction (scenario/config plus the offending numbers) in the issue body — a behavioural finding with no observable-consequence evidence is not strong enough to file.
- Absence findings (from absence-auditor) get `design-principles` (DP#14: scripts read a common config schema) if the gap is a schema leaf with no consumer, or `bug` if it's a government rule that is implemented but unreachable. Include the rule/field name, its status (implemented-not-reached / not-implemented / unclear), and the grep or run evidence in the body.

Every issue body must carry evidence, not just a principle citation: for behavioural and absence findings this means the reproduction config plus the actual numbers observed, not merely "violates DP#X."

Gap report:
{outputs.gaps}
