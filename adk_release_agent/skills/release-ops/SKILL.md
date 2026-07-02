---
name: release-ops
description: "Perform tightly scoped release operations: remove or unstage charts, retrigger a deployment workflow, or release today's staged PRD batch after cutoff."
metadata:
  adk_additional_tools:
    - remove_from_release
    - retrigger_deployment_workflow
    - merge_prod_release
    - find_prs
    - get_pr_details
---

Use this skill only when the user gives a direct operation command, not when they ask a question about how an operation works.

Allowed actions:
- `remove_from_release` to unstage chart names from today's PRD release PR, or to remove them from a live environment.
- `retrigger_deployment_workflow` to rerun deployment workflow for an existing PR.
- `merge_prod_release` to release today's staged PRD batch after the configured cutoff.

Choosing `remove_from_release`'s environment:
- "remove X from the release" / "unstage X" / "don't ship X today" → `environment="staging"` (the default). This only edits today's PRD release PR; live environments are untouched.
- Pass `environment="uat"` or `environment="prod"` ONLY when the user explicitly names that live environment (e.g. "remove X from UAT"). These change what is actually deployed — never infer them from an unqualified "remove from the release".
- If unsure which the user means, ask before calling the tool.

Forbidden actions:
- Do not deploy or add charts.
- Do not directly mutate deployment JSON.
- Do not open release PRs from free-form chat.
- Do not dispatch arbitrary workflows.

For deploy/add requests, tell the user to use the deterministic deploy flow that previews exact JSON and requires the confirmation token.
