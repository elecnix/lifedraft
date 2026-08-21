---
name: issue-pipeline
description: |
  Identifies the top 5 priority issues, then orchestrates async implementers
  to create or advance PRs, runs quality gate reviews, merges PRs that pass
  all quality gates, reruns quality gates after conflict resolution, and
  returns a structured summary of all issues, PRs, and quality gate results.
---

## issue-analyzer
phase: Triage
label: Identify top 5 priority issues
as: priorities
output: priorities.json
outputMode: file-only
reads:
  - inputs/active/SITUATION.md
  - inputs/active/input.json
  - DESIGN_PRINCIPLES.md
outputSchema:
  type: object
  required:
    - top5
  properties:
    top5:
      type: array
      minItems: 1
      maxItems: 5
      items:
        type: object
        required:
          - issue_number
          - title
          - action
        properties:
          issue_number:
            type: integer
          title:
            type: string
          pr_number:
            type: integer
          action:
            type: string
            enum:
              - create
              - advance
          urgency:
            type: string
            enum:
              - critical
              - high
              - medium
              - low
          summary:
            type: string
    demoted:
      type: array
      items:
        type: object
        required:
          - issue_number
          - title
        properties:
          issue_number:
            type: integer
          title:
            type: string
          reason:
            type: string
    stale:
      type: array
      items:
        type: object
        required:
          - issue_number
          - title
          - reason
        properties:
          issue_number:
            type: integer
          title:
            type: string
          reason:
            type: string
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Analyze all open GitHub issues in elecnix/lifedraft. Spawn one scout subagent per issue to assess whether the issue is still accurate, whether an existing PR addresses it, and how urgent it is. Synthesize the findings into a ranked list of exactly 5 priorities with recommendations. For each priority, note the issue number, title, whether a PR already exists (and its number), and the recommended action (create new PR or advance existing PR). Do NOT apply any labels — the impl-orchestrator will handle that. Use `structured_output` to return the analysis with: top5 (with issue numbers, PR status, action, and urgency), demoted (issues that lost priority), and stale (issues to close).

## impl-orchestrator
phase: Implementation
label: Implement, review, and merge top 5 priorities
progress: true
output: implementation-summary.md
outputMode: file-only
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Based on the following priority analysis, orchestrate async implementers to create or advance PRs for the top 5 issues. As each implementer completes, run a quality gate review on its PR. Merge PRs that pass all quality gates with green CI and no merge conflicts. If a PR passes the quality gate but has merge conflicts, resume the implementer to resolve conflicts and then rerun the quality gate on the rebased PR before merging. Return a structured summary of all issues, PRs, and quality gate results. You are authorized to merge PRs.

Priority analysis:
{outputs.priorities}