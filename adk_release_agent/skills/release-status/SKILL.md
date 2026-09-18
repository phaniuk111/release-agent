---
name: release-status
description: "Answer questions about deployed UAT/PRD charts, the current release window, allowed images, and which workflow runs exist. NOT for release controls, build gates, or why a build failed — those are release-controls."
metadata:
  adk_additional_tools:
    - check_release_window
    - list_allowed_images
    - get_recent_runs
    - get_workflow_status
---

Use this skill when the user asks what is deployed, what is pending, what can be released now, which charts or versions are live, which images are allowed, or what recent workflow runs exist.

Rules:
- Treat GitHub deployment JSON and GitHub Actions as the source of truth.
- Always use `check_release_window` for deployed UAT/PRD state and whether a release PR is open on a guarded branch.
- Use `list_allowed_images` for catalog questions.
- Use `get_recent_runs` and `get_workflow_status` for whether a run exists and how
  it ended. A question naming CONTROLS, RCTLDEF/RLFT/RFTL gates, build eligibility,
  or WHY a build failed is the release-controls skill — switch to it rather than
  listing raw steps, which answers the question without its verdict.
- Match chart names EXACTLY. If the chart asked about is not in the deployed list,
  say so plainly ("orders-api is not deployed in PRD") and then list what is —
  never answer with a similarly named chart (orders-api is not orders-svc).
- Do not mutate anything. If the user wants to deploy or remove, route them to the appropriate deterministic deploy flow or scoped ops action.
