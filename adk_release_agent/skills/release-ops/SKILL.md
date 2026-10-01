---
name: release-ops
description: "Perform tightly scoped release operations: promote a CARE or DF release to the next environment."
metadata:
  adk_additional_tools:
    - promote_release
    - promote_df_release
    - find_prs
    - get_pr_details
---

Use this skill only when the user gives a direct operation command, not when they ask a question about how an operation works.

Allowed actions:
- `promote_release` to promote the current release's FILE-SET to the next environment branch (target=uat, prd or prl1). Use for 'promote release to uat/prd/prl1'. Terminal targets (prd, prl1) pause on a yes/no approval.
- CARE and DF releases are DIFFERENT releases with different repos and branch chains. "DF", "Dataflow" or "df release" → `promote_df_release`; otherwise → `promote_release` (CARE). A DF release lands on its UAT branch directly (no SIT), so its usual promotion is to prd — if the tool says a target is not in the chain, tell the user which targets are. Never call `promote_release` for a DF release.

PROD is reached ONE way: by promoting the release file-set to prd. There is no
single-chart prod deploy and no daily prod batch to finalise. "release prod",
"ship the release", "release to production" all mean `promote_release` with
target=prd (or `promote_df_release` for a Dataflow release) — do it, do not ask
which release model was meant, and never offer to deploy a chart to prod instead.
Promoting to a terminal target finalizes the release: charts added afterwards
belong to the NEXT release.

There are NO removals. Do not offer or attempt to remove a chart from any environment or release:
- "Remove X from the release" / "don't ship X": withdraw it from the release queue (`withdraw_release_intent`, release-queue skill) — the only way to take a chart out.
- "Remove X from UAT": a UAT deploy overwrites the file, so tell the user to deploy UAT without the chart (Deploy to CARE UAT). Do not call a tool.
- "Remove X from prod/PRD": not supported here — say so; PRD changes only by promoting a release.

Targeting a non-default deployment repo:
- `promote_release` accepts an optional `deployment_repo` (owner/repo). Pass it ONLY when the user names a repo (e.g. "promote the release to prd in my-org/my-deploy-repo" — typically because their release targeted that repo). Never guess it; empty uses the configured default.

Narrating the approval flow:
- Terminal promotions (prd, prl1) and prod removals pause on a **yes/no approval
  prompt** (the runtime shows it). They do NOT use `CONFIRM-xxxxxx` tokens — never
  mention tokens when talking about these operations. A chart deploy is the other
  way round: it is previewed and needs the exact `CONFIRM-xxxxxx` token, never a
  yes/no approval.
- If the user approves, summarize what the tool actually did from its result.
- If the user rejects, address them directly: "You rejected it — nothing was
  promoted. Ask again when you're ready." Do NOT write "I was unable
  to…", "the system rejected it", or any passive "was rejected": the tool
  worked and the person declined, and blaming the tool or "the system" reads as
  a broken portal. Do not explain the deploy Workflow or tokens.
- After a rejection, STOP. Do not call another mutating tool in the same turn,
  and do not offer a different one for approval: a person who just said no must
  not be asked again in the same breath.

For deploy/add requests, tell the user to use the deterministic deploy flow that previews exact JSON and requires the confirmation token — it deploys to UAT only.
