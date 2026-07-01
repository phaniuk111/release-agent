# Design & Specification Documents

This folder contains the current planning and specification artifacts for the
**Release Copilot ADK GitHub release chatbot**.

## Files

- **[spec.md](./spec.md)** - Focused implementation spec and PoV plan.
- **[DESIGN.md](./DESIGN.md)** - Technical design for the ADK runtime, tools,
  safety model, deployment, and validation.
- **[DESIGN_SUMMARY.md](./DESIGN_SUMMARY.md)** - Concise summary of architecture
  and decisions.

## Relationship to Code

These documents describe the code living in the parent directory:

- `adk_release_agent/` - ADK root agent, specialist sub-agents, skills, and
  deterministic deploy facade.
- `src/release_agent/` - FastAPI/CLI adapter, static UI, config, parser, and
  GitHub tool layer.
- `helm/`, `k8s/`, and `Dockerfile` - deployment packaging.

The ADK runtime is the production path. The deploy mutation path remains
deterministic: parse request, preview exact deployment JSON, require a thread-local
`CONFIRM-*` token, then call the GitHub tool facade.

## Updating

When making significant changes to the agent, keep these documents in sync with
the ADK app, especially tool exposure, confirmation behavior, and deployment
packaging.
