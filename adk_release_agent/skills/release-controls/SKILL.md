---
name: release-controls
description: "Verify image build provenance and release-control gates for image tags or workflow runs."
metadata:
  adk_additional_tools:
    - verify_image_tag_build
    - get_build_controls
    - get_recent_runs
---

Use this skill when the user asks whether an image tag was built, whether release controls passed, or what RLFT/RFTL gates are associated with a build.

Rules:
- Use `verify_image_tag_build` for image and tag provenance.
- Use `get_build_controls` for RLFT/RFTL control details.
- If image and tag cannot identify the run, ask for a GitHub Actions run id.
- Report control names and states exactly as returned by tools.
- This skill is read-only and cannot approve, waive, or rerun controls.
