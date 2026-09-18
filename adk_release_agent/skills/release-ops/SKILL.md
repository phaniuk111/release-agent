---
name: release-ops
description: "Perform tightly scoped release operations: remove or unstage charts from the release, and promote a CARE or DF release to the next environment."
metadata:
  adk_additional_tools:
    - remove_from_release
    - promote_release
    - promote_df_release
    - find_prs
    - get_pr_details
---

Use this skill only when the user gives a direct operation command, not when they ask a question about how an operation works.

Allowed actions:
- `remove_from_release` to unstage chart names from the open release, or to remove them from a live environment.
- `promote_release` to promote the current release's FILE-SET to the next environment branch (target=uat, prd or prl1). Use for 'promote release to uat/prd/prl1'. Terminal targets (prd, prl1) pause on a yes/no approval.
- CARE and DF releases are DIFFERENT releases with different repos and branch chains. "DF", "Dataflow" or "df release" → `promote_df_release`; otherwise → `promote_release` (CARE). A DF release lands on its UAT branch directly (no SIT), so its usual promotion is to prd — if the tool says a target is not in the chain, tell the user which targets are. Never call `promote_release` for a DF release.

PROD is reached ONE way: by promoting the release file-set to prd. There is no
single-chart prod deploy and no daily prod batch to finalise. "release prod",
"ship the release", "release to production" all mean `promote_release` with
target=prd (or `promote_df_release` for a Dataflow release) — do it, do not ask
which release model was meant, and never offer to deploy a chart to prod instead.
Promoting to a terminal target finalizes the release: charts added afterwards
belong to the NEXT release.

Choosing `remove_from_release`'s environment:
- "remove X from the release" / "unstage X" / "don't ship X" → `environment="staging"` (the default). This only edits the open release; live environments are untouched.
- Pass `environment="uat"` or `environment="prod"` ONLY when the user explicitly names that live environment (e.g. "remove X from UAT"). These change what is actually deployed — never infer them from an unqualified "remove from the release".
- If unsure which the user means, ask before calling the tool.

Targeting a non-default deployment repo:
- `remove_from_release` and `promote_release` accept an optional `deployment_repo` (owner/repo). Pass it ONLY when the user names a repo (e.g. "promote the release to prd in my-org/my-deploy-repo" — typically because their release targeted that repo). Never guess it; empty uses the configured default.

Narrating the approval flow:
- Terminal promotions (prd, prl1) and prod removals pause on a **yes/no approval
  prompt** (the runtime shows it). They do NOT use `CONFIRM-xxxxxx` tokens — never
  mention tokens when talking about these operations. A chart deploy is the other
  way round: it is previewed and needs the exact `CONFIRM-xxxxxx` token, never a
  yes/no approval.
- If the user approves, summarize what the tool actually did from its result.
- If the user rejects, address them directly: "You rejected it — nothing was
  released/removed. Ask again when you're ready." Do NOT write "I was unable
  to…", "the system rejected it", or any passive "was rejected": the tool
  worked and the person declined, and blaming the tool or "the system" reads as
  a broken portal. Do not explain the deploy Workflow or tokens.
- After a rejection, STOP. Do not call another mutating tool in the same turn,
  and do not offer a different one for approval: a person who just said no must
  not be asked again in the same breath.

For deploy/add requests, tell the user to use the deterministic deploy flow that previews exact JSON and requires the confirmation token — it deploys to UAT only.
