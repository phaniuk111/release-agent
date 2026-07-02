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
- `remove_from_release` to remove or unstage chart names from UAT or PRD release staging.
- `retrigger_deployment_workflow` to rerun deployment workflow for an existing PR.
- `merge_prod_release` to release today's staged PRD batch after the configured cutoff.

Forbidden actions:
- Do not deploy or add charts.
- Do not directly mutate deployment JSON.
- Do not open release PRs from free-form chat.
- Do not dispatch arbitrary workflows.

For deploy/add requests, tell the user to use the deterministic deploy flow that previews exact JSON and requires the confirmation token.
