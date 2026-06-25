# Design & Specification Documents

This folder contains all planning and specification artifacts for the **Release Copilot** (LangGraph GitHub release chatbot).

## Files

- **[spec.md](./spec.md)** — Focused, implementation-oriented spec and PoV plan.
- **[DESIGN.md](./DESIGN.md)** — Comprehensive technical design document (architecture, state, tools, security, K8s, full PR plan, alternatives, etc.). Produced via structured design process.
- **[DESIGN_SUMMARY.md](./DESIGN_SUMMARY.md)** — Concise executive summary + key decisions + PR plan.

## Relationship to Code

These documents describe the code living in the parent directory (`../src/`, `../Dockerfile`, `../k8s/`, etc.).

The implementation aims to follow the design in `DESIGN.md` and `spec.md`.

## Updating

When making significant changes to the agent, please keep these documents in sync (especially the PR plan status and any deviations from the design).