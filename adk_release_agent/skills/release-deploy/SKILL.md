---
name: release-deploy
description: "Explains how deploy/add/stage/ship requests for a specific chart:version are handled. Deploys go to UAT only, through a deterministic, confirmation-gated Workflow — NOT through free-form chat tools."
---

Use this skill when the user asks to deploy, add, stage, ship, or bump a SPECIFIC
chart/image version to UAT.

PROD is NOT a deploy target. Everything ships through the intake queue → CARE or
DF release → promote, so there is no single-chart prod deploy. If the user asks to
deploy a chart:version to prod/PRD/production, do not preview a deploy and do not
ask which environment they meant — answer with the route:

> PROD is reached through a release, not a single-chart deploy. Queue it (Add to
> next release), then raise the CARE or DF release, and promote it.

A request to promote or release an EXISTING release ("promote the release to prd",
"release prod", "ship the release" — no chart:version named) is the release-ops
skill: call `promote_release` (or `promote_df_release`). Never answer those with an
explanation of the deploy flow.

This skill is intentionally tool-less. Deploy requests are NOT executed from free-form
chat. They are routed to a deterministic ADK Workflow graph
(`adk_release_agent.deploy_workflow`) that:
1. previews the exact deployment JSON and mints a `CONFIRM-xxxxxx` token,
2. pauses on a human-in-the-loop confirmation node (ADK `RequestInput`),
3. applies only after the user replies with the exact token.

Your job here:
- Confirm the request looks like a UAT deploy and tell the user it will be previewed
  first and require the exact `CONFIRM-xxxxxx` token before anything is applied.
- If no `chart:version` can be parsed, ask the user for explicit `chart:version` input.
- UAT deploys update `uat/deployment.json`. Nothing here writes a PRD file.
