---
name: release-pr
description: "Find deployment PRs and summarize CHG, RMG, RLFT, and RFTL evidence from PR comments and runs."
metadata:
  adk_additional_tools:
    - find_prs
    - get_pr_details
    - get_pr_comments
    - summarize_pr_controls
    - get_recent_runs
    - get_workflow_status
---

Use this skill when the user asks to find, track, inspect, or summarize a deployment PR, release ticket, CHG/RMG approval, or RLFT/RFTL gate.

Rules:
- Use `find_prs` to locate PRs from image, tag, branch, ticket, or free text.
- Use `get_pr_details` and `get_pr_comments` before summarizing a PR.
- Use `summarize_pr_controls` for CHG/RMG/RLFT/RFTL status.
- Report exact PR numbers, URLs, ticket IDs, and gate states found by the tools.
- Never fabricate missing ticket numbers or control status.
- This skill is read-only.
