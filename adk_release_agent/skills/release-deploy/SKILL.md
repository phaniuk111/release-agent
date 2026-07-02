---
name: release-deploy
description: "Explains how deploy/add/promote/stage/ship requests are handled. Deploys run through a deterministic, confirmation-gated Workflow — NOT through free-form chat tools."
---

Use this skill when the user asks to deploy, promote, add, stage, ship, bump, or release a chart/image version to UAT or PROD.

This skill is intentionally tool-less. Deploy requests are NOT executed from free-form
chat. They are routed to a deterministic ADK Workflow graph
(`adk_release_agent.deploy_workflow`) that:
1. previews the exact deployment JSON and mints a `CONFIRM-xxxxxx` token,
2. pauses on a human-in-the-loop confirmation node (ADK `RequestInput`),
3. applies only after the user replies with the exact token.

Your job here:
- Confirm the request looks like a deploy and tell the user it will be previewed
  first and require the exact `CONFIRM-xxxxxx` token before anything is applied.
- Never call `open_release_pr`, `apply_json_update`, `dispatch_workflow`, or any
  other release-defining mutation directly — a safety plugin will block them.
- If no `chart:version` can be parsed, ask the user for explicit `chart:version` input.
- UAT deploys update `uat/deployment.json`; PROD deploys stage both UAT and PRD entries
  according to the existing release process.
