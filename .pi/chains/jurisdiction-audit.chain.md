---
name: jurisdiction-audit
description: |
  Four-step pipeline: scans the codebase for all jurisdictions and government
  programs (jurisdiction-scanner), tags existing GitHub issues with jurisdiction,
  country, and province/state labels (jurisdiction-tagger), researches official
  rules and community edge cases for each program via parallel web search
  (program-researcher), then creates GitHub issues for coverage gaps, missing
  tests, and unimplemented rules (coverage-gap-issuer). Does NOT add priority
  labels.
---

## jurisdiction-scanner
phase: Discovery
label: Scan codebase for jurisdictions and programs
as: jurisdiction_tree
output: jurisdiction_tree.json
outputMode: file-only
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Scan the entire codebase under the countries/ directory and any other modules that implement government program rules. Build a comprehensive JSON tree of all jurisdictions (countries, provinces/states) and government programs found in the code — not in documentation. For each program, record its module path, key classes, key rules, whether tests exist, and the test file path. Include programs with no tests. Include provinces with no programs. Output the complete jurisdiction tree as JSON.

## jurisdiction-tagger
phase: Labeling
label: Tag existing issues with jurisdiction labels
as: tag_report
output: tag_report.md
outputMode: file-only
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Based on the following jurisdiction tree, create GitHub labels for each country and province/state found in the codebase (jurisdiction, country-canada, province-quebec, province-ontario, etc.), then tag all existing open GitHub issues with the appropriate jurisdiction, country, and province/state labels. Read each issue's title, body, and comments to determine which jurisdiction it relates to. Issues about federal-level programs get the country label but not a province label. Issues about core architecture or cross-cutting concerns that don't mention a specific jurisdiction should NOT get jurisdiction labels. Do NOT add or remove priority labels. Output a report of labels created and issues tagged.

Jurisdiction tree:
{outputs.jurisdiction_tree}

## program-researcher
phase: Research
label: Research rules and compare against code for each program
as: gap_report
output: gap_report.md
outputMode: file-only
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Using the following jurisdiction tree, orchestrate a fractal fan-out of web research and code comparison for every government program. Spawn parallel researcher subagents (concurrency 8, thinking none) to search for official rules and community edge cases. Then spawn parallel reviewer subagents (concurrency 8, thinking low) to compare findings against the code. Produce a consolidated gap report organized by jurisdiction and program, distinguishing between: not implemented, partially implemented, incorrectly implemented, untested, and not handled.

Jurisdiction tree:
{outputs.jurisdiction_tree}

## coverage-gap-issuer
phase: Issue Creation
label: Create GitHub issues for coverage gaps
control:
  needsAttentionAfterMs: 300000
  activeNoticeAfterMs: 300000
  notifyOn:
    - needs_attention

Based on the following gap report, check for existing GitHub issues in elecnix/lifedraft and create new issues for every program with coverage gaps, missing tests, or unimplemented rules. Spawn parallel scout subagents (concurrency 8, thinking none) to assess whether existing issues are still accurate. Even if a program is fully implemented, create an issue if it has no tests. Do NOT add priority labels to any issue. Use the design-principles label AND the appropriate jurisdiction/country/province labels. Output a summary of issues created, updated, closed, and skipped.

Gap report:
{outputs.gap_report}