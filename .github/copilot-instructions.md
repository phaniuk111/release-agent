# Copilot instructions — Dev Portal (release-copilot)

A release copilot for a bank-style GitOps flow: a FastAPI chat UI plus a Google
ADK agent (Gemini on Vertex) that **previews every change and requires a human
to confirm it**. Facts come from tools (GitHub, BigQuery), never from the model.

The full guide — repo map, every invariant, workflows — is
[AGENTS.md](../AGENTS.md); path-specific rules are in
[.github/instructions/](instructions/). This file is the short version, and the
only one Copilot code review reads. Keep the three in step: when a rule changes,
change it in AGENTS.md first.

## Where things are

- `src/release_agent/app_fastapi.py` — HTTP API and the chat SSE endpoint.
- `src/release_agent/adk_service.py` — the chat router (deploy parser → classifier → agent; token and yes/no resume paths).
- `src/release_agent/tools/` — the GitHub/BigQuery tool layer; the source of truth for facts.
- `adk_release_agent/` — the ADK app: chat agent, the deterministic deploy and commands Workflows, approval rules, skills.
- `src/release_agent/static/` — plain ES-module frontend: `core/` pure rules, `api.js` every backend call, `forms/` screens.
- `src/release_agent/config.py` — every setting (pydantic, env aliases); cluster values in `helm/release-copilot/values.yaml` under `config:`.
- `tests/` (pytest) and `tests/js/` (node:test).

## Rules that must not break

- **Two confirmation flows, never mixed.** Deploys and releases: deterministic
  preview → exact single-use `CONFIRM-XXXXXX` token. High-impact ops (PRD/PRL1
  promotions, prod removals): a yes/no approval. Free-form chat can never
  mutate deployments. A new request replaces a pending one; anything else is a
  reminder, never a rejection. Pending items are keyed by (owner, thread).
- **One event loop serves every user.** Blocking work (GitHub, git, BigQuery)
  goes off the loop — `asyncio.to_thread`, or `off_event_loop` for chat tools.
- **ADK Workflow nodes never raise** (a failed node re-runs on resume and
  repeats side effects): return failures as outcomes.
- **A UAT deploy overwrites `uat/deployment.json`** with exactly what the
  developer submitted, one at a time — refused while an open PR into SIT/UAT
  changes that file or another deploy is running. Never merge two submissions.
- **BigQuery event log is optional and append-only** (INSERT only; state is
  derived, latest event wins). An outage degrades to an error dict, never
  blocks a release. Schema: `bigquery/release_intents.schema.json` and
  `_SCHEMA` in `release_queue.py` together, additive nullable columns only.
- **Secrets and identity.** GitHub PATs are memory-only and masked; nothing
  sensitive in git, BigQuery or logs. The caller's identity comes only from a
  signature-verified token (`identity.py`), never from a typed email.
- **Commits the portal makes** go through `tools/attribution.py`: the form's
  JIRA leads every commit message and PR title (`titled`, `commit_message`),
  and a `Requested-by:` trailer names the verified caller.

## Conventions

- No regex in parsing paths — use tokenizers (see `agent/parsing.py`).
- Comments explain constraints and why, not mechanics.
- A setting is a pydantic `Field` with `AliasChoices` in `config.py`, plus a
  commented entry in the Helm values when it matters in the cluster.
- Frontend: plain ES modules, no build step, no CDN (Chart.js, Tailwind, Font
  Awesome, Inter are vendored). A rule or sentence the Backstage port would
  also need goes in `static/core/` with a `tests/js/` test.
- Use neutral example names in code, tests and fixtures (`svc-a`,
  `example-org/deploy`, `dev@example.com`, JIRA `ABC-1234`) — this repository
  is public.

## Commands

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src adk_release_agent tests
node --test tests/js/*.test.mjs
uvicorn release_agent.app_fastapi:app --app-dir src
```
