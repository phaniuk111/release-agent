---
applyTo: "release-copilot/**,helm/release-copilot/**,src/release_agent/**,adk_release_agent/**,tests/**"
---
# release-copilot — app-wide rules

A release copilot for a bank-style GitOps flow: a FastAPI chat UI plus a Google
ADK agent (Gemini on Vertex) that **previews every change and requires a human
to confirm it**. Facts come from tools (GitHub, BigQuery), never from the model.

These rules apply to the release-copilot app only (in a monorepo: the
`release-copilot/` folder and its chart in `helm/release-copilot/`). The full
guide — repo map, every invariant, workflows — is `AGENTS.md` in the app's root
folder; the other files in this folder add rules per area. When a rule changes,
change it in AGENTS.md first and keep these in step.

## Where things are

Paths are relative to the app's root folder (`release-copilot/` in a monorepo).


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
  promotions): a yes/no approval. There are no removals — a chart leaves the
  next release only by being withdrawn from the release queue. Free-form chat can never
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
  `portal_events` (`tools/events_store.py`, `bigquery/portal_events.schema.json`)
  is a second append-only table under the same rules.
- **Notifications** (`tools/notifications.py`, `notifications_api.py`, preview
  feature `notifications`): per person, by the VERIFIED email — with identity on a
  typed email never stands in for a failed sign-in; with it off the typed email is
  used and the answer says `unverified`. A notification that changes is a new row
  with the SAME subject (one bell item per `scope`); read/dismissed are new rows;
  a notify call is best-effort and never raises.
- **Tickets on the banner** (`tools/tickets.py`, `static/core/tickets.js`):
  `/api/release-status` lists every CARE release PR open on a guarded branch in `release_prs` (PRD and PRL1 together; `blocking_pr` is the first, the same object) and adds `tickets` to each and to `df.release_pr`
  only when a PR exists; the banner shows them after the PR number as links,
  grey "no CHG yet" for a prefix the PR does not name (never a state), nothing on a read error.
- **Deployment tracking** (`adk_release_agent/deploy_tracker.py`, started in
  `_lifespan` only with `DEPLOY_TRACKING_ENABLED=true`; feature `deploy-tracking`,
  out of preview by default): watches the portal's pending PRs and the PRD/PRL1 PRs the mono repo's
  action raises in the deployment repo (recorded `deploy/watching`; no
  requester, so no bell) and their runs, and tells the requester of a
  portal-raised one; never merges, re-runs or rolls back. Its `NOTIFY_WORKFLOW`
  dispatch (fixed inputs, Start/Complete only) is the one write `approvals.py`
  allows without a yes. Each dispatch's outcome (run id + link, error, or why
  skipped) is kept on its `merged`/`finished` row. `deploy_tracker.report()` is the
  read-only Release pill "Release notifications" (out of preview by default): live checks of what a dispatch
  needs and this week's open PRL1/PRD release PRs, each watched or not — no history
  (`/api/deploy-tracking`, `static/forms/deploy_tracking.js`, wording in
  `static/core/deploy_tracking.js`) — not gated on the switch, since a
  switched-off tracker is one of the things it shows.
- **Auto-triage** (`tools/support/watch.py`, `adk_release_agent/support_watch.py`,
  routes in `support_api.py`, preview feature `auto-triage`): Support triage and
  the Investigate pipeline on a timer. Findings live in `portal_events` (domain
  `watch`), the bell is told per incident, a person's resolve/dismiss/reopen is
  recorded against the VERIFIED caller, and the loop runs only when
  `SUPPORT_WATCH_MINUTES > 0` and `SUPPORT_TABLE` is set. Read-only throughout.
- **Re-run a job** (`tools/rerun_job.py`, `rerun_api.py`, preview feature
  `rerun-job`; design/RERUN_JOB.md): re-runs ONE job by any name in the picked
  queued images' build runs — preview, a single-use plan bound to (owner,
  thread), then a yes. Never a build/publish job or one whose dependents build;
  the GitHub budget is checked before any start; an API limit stops the batch,
  a permission refusal the repo; no write is ever retried. A passed re-run
  appends a `reverified` event; one bell per batch.
- **Secrets and identity.** GitHub PATs are memory-only and masked, and are
  dropped with a conversation idle for `SESSION_IDLE_MINUTES` (60); nothing
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
