# Copilot instructions

This repository is the **release-copilot** app. Its rules live in
[.github/instructions/release-copilot/](instructions/release-copilot/), applied by
path (`applyTo`) — `app.instructions.md` covers the whole app, the others add
rules for the tool layer, the ADK app, the frontend, tests and the Helm chart.
The full guide is [AGENTS.md](../AGENTS.md).

Moving into a monorepo (app in `release-copilot/`, chart in `helm/release-copilot/`):
copy `.github/instructions/release-copilot/` to the monorepo's `.github/instructions/`
as it is — every `applyTo` already lists the monorepo paths — and do NOT copy
this file; the monorepo's own `copilot-instructions.md` is shared by every team.
Add at most one line there: "release-copilot rules: .github/instructions/release-copilot/".

Commands (from the app's root folder):

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src adk_release_agent tests
node --test tests/js/*.test.mjs
```
