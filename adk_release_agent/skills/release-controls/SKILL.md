---
name: release-controls
description: "Verify image build provenance and release-control gates for image tags or workflow runs."
metadata:
  adk_additional_tools:
    - get_build_report
    - get_recent_runs
---

Use this skill when the user asks whether an image tag was built, whether release controls passed, what RLFT/RFTL gates are associated with a build — or WHAT FAILED in a build ("my build failed, which step/control?").

Rules:
- Use `get_build_report` for provenance, RLFT/RFTL control details, and the
  failure diagnosis alike: it takes image+tag, a run id, or a pasted GitHub
  Actions run URL, and returns failed steps, per-control pass/fail, the gate
  verdict, and built-from-main. Present it as a markdown table (Step/Control |
  Job | Result with ✅/❌) after a one-line summary with the run link — never
  raw JSON.
- There are TWO build repos: GKE services build in the default build repo,
  Dataflow images in a separate one. For a Dataflow image, pass dataflow=true
  on any image+tag or run-id lookup (run-URL lookups need nothing — the URL
  carries its repo). If unsure which kind the image is, ask.
- Controls are the RCTLDEF… SDLC control steps and the RLFT/RFTL gates —
  whatever matches the configured prefixes; they may be steps inside a job or
  entire jobs. Report their names exactly. A scanner that is not a configured
  control (Xray/Prisma, CodeQL, xSecurity-Gatekeeper) is an ordinary step: it
  can fail the BUILD, but it is not a release control — do not call it one.
- After reporting failures, tell the user plainly what to do next: failed
  controls (RCTLDEF…, RLFT/RFTL) must be fixed and the
  build re-run before this tag can ship (this skill cannot approve, waive, or
  rerun controls); failed ordinary steps (build, scans like Xray/Prisma or
  CodeQL when not configured as controls) mean fixing the build itself. Note
  that steps SKIPPED because an earlier failure cascaded will only run once
  the root cause is fixed. Offer to check again once they've re-run it.
- If image and tag cannot identify the run, ask for the GitHub Actions run
  id or URL.
