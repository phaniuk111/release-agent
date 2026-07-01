---
name: release-deploy
description: "Deterministically deploy or stage chart versions by previewing exact deployment JSON and requiring a confirmation token before mutation."
---

Use this skill when the user asks to deploy, promote, add, stage, ship, bump, or release a chart/image version to UAT or PROD.

Required flow:
1. Call `prepare_deploy_preview` with the user's request.
2. Show the exact proposed JSON and confirmation token.
3. Wait for the user's exact `CONFIRM-xxxxxx` token.
4. Call `apply_confirmed_deploy` only with the user's confirmation text.

Rules:
- Never skip the preview.
- Never invent or alter the confirmation token.
- Never call `open_release_pr`, `apply_json_update`, or `dispatch_workflow` directly.
- If no chart:version can be parsed, ask the user for explicit `chart:version` input.
- UAT deploys update `uat/deployment.json`; PROD deploys stage both UAT and PRD entries according to the existing release process.
