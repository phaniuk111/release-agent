---
name: release-status
description: "Answer questions about deployed UAT/PRD charts, today's PRD release window, allowed images, and recent workflow runs."
---

Use this skill when the user asks what is deployed, what is pending, what can be released today, which charts or versions are live, which images are allowed, or what recent workflow runs exist.

Rules:
- Treat GitHub deployment JSON and GitHub Actions as the source of truth.
- Always use `check_release_window` for deployed UAT/PRD state and today's PRD release PR.
- Use `list_allowed_images` for catalog questions.
- Use `get_recent_runs` and `get_workflow_status` for workflow status.
- Do not mutate anything. If the user wants to deploy or remove, route them to the appropriate deterministic deploy flow or scoped ops action.
