# Dev Portal (release-copilot) — agent guide

ADK-powered release copilot for a bank-style GitOps flow: FastAPI chat UI + Google ADK
(Gemini/Vertex) agent that previews every mutation and requires human confirmation.
Working branch: `adk-release-agent`. **Never merge or push to `main`.**

## Map (read only what you need)

| Path | What it is |
|---|---|
| `src/release_agent/app_fastapi.py` | FastAPI app: chat SSE endpoint, session-PAT endpoints, queue/insights/deploy-template APIs, inline HTML shell |
| `src/release_agent/adk_service.py` | Router: deterministic deploy parser → classifier fallback → chat agent; CONFIRM-token + yes/no approval resume paths |
| `src/release_agent/agent/parsing.py` | Pure text parsing: image:tag extraction, env detection, JSON payload parser, queue-intent veto. No LLM, no regex |
| `src/release_agent/tools/` | GitHub/BQ tool layer (source of truth for all facts): `promotion.py` (deploy PR chains SIT→UAT→PRD), `release_fileset.py` (CARE/DF release model), `release_chain.py` (each kind's branch chain: CARE SIT→UAT→PRD/PRL1, DF `DF_RELEASE_BRANCHES` e.g. RELEASE_UAT→RELEASE_PRD), `git_snapshot.py` (Dulwich checkout + API commit — no git binary), `clone_probe.py` (diagnostics: which fetch paths the proxy allows), `monitoring.py` (MONITOR_CHECKS PromQL checks + read-only queries for the Monitoring pill/skill), `bq_cost.py` (BigQuery cost report — design/BQ_COST.md: INFORMATION_SCHEMA scan, findings memory), `bq_guard.py` (the ONE gate every cost statement passes: real only for INFORMATION_SCHEMA / `__TABLES__` reads, everything else dry-run, DDL/DML refused — token-based, no regex), `bq_cost_xlsx.py` (that same report dict as .xlsx; no spreadsheet library), `release_queue.py` (BQ event log), `pr_reconcile.py` (settles PRs merged/closed outside the chat), `chg_defaults.py` (change-request fields from facts: release name/number, standard wording), `controls.py` (build verification, RCTLD control parsing), `release_window.py` (release guard), `manifest.py`, `_common.py` (GitHub client, session PAT resolution) |
| `src/release_agent/session_creds.py` | Per-thread GitHub PAT: memory-only, masked, never logged/stored; bound to its owner when identity is on |
| `src/release_agent/features.py` | Preview features: PREVIEW_GROUPS (pill groups) and PREVIEW_FEATURES (API + chat tools refuse) shown only to PREVIEW_USERS — verified emails, or `*` for a testers-only release. Ship new features behind it |
| `src/release_agent/identity.py` | Signed-in user from the mesh's RCToken (IDENTITY_HEADER) — signature-VERIFIED against the issuer's JWKS, never just decoded; the verified email beats any typed one |
| `src/release_agent/static/` | ES-module frontend in three layers (see `static/README.md`): `core/` pure rules shared with the Backstage port (queue routing/wording, validation, formatting; tested in `tests/js/`), `api.js` every backend call, and screens — `forms/` (one module per form: queue form + table, CARE/DF release, deploys, chat parsers), `chat.js` (SSE, markdown, ```chart blocks → vendored Chart.js), `palette.js` (pills + ⌘K), `insights.js`, `status.js` (banner) |
| `adk_release_agent/` | ADK app: `agent.py` (skills-routed chat agent + ROOT_INSTRUCTION), `deploy.py` (preview→token→apply), `deploy_workflow.py` (deterministic Workflow graph: gate → apply / cancel), `tools.py` (ADK wrappers incl. queue eligibility gate), `safety.py` (MutationGuardPlugin), `intent.py` (classify-only fallback), `telemetry.py` (ADK's OTel spans → Langfuse/OTLP; off unless LANGFUSE_HOST is set), `skills/*/SKILL.md` (per-domain instructions + tool unlock lists) |
| `bigquery/` | Table provisioning: terraform module + `release_intents.schema.json` (single source of truth) and `bq_cost_findings.schema.json` (the cost report's optional findings memory) |
| `helm/release-copilot/` | Chart; ALL runtime config flows as env via `values.yaml → config:` (generic ConfigMap) |
| `tests/` | pytest; `conftest.py` blanks BQ so tests never write real telemetry |

## Core invariants (do not break)

1. **Two confirmation flows, never mixed**: chart:version deploys and releases run a
   deterministic preview → exact `CONFIRM-XXXXXX` token. High-impact ops tools
   (merge_prod_release, prod removals, terminal promotions) pause on a yes/no approval.
   Free-form chat can NEVER mutate deployments (MutationGuardPlugin enforces).
   Tokens are SINGLE-USE: spent before anything mutates. A pending approval or
   token is consumed ONLY by an explicit answer (yes/no; the exact token, or
   `no`); a NEW deploy/release request replaces it — the old one is cancelled,
   never applied, and the new one gets its own token — and anything else is a
   reminder, not a rejection (`_reply_kind` in adk_service.py). Both pending
   stores are keyed by (owner, thread). Deploy-graph nodes never
   raise (ADK 2.9 re-runs a failed node on resume, repeating side effects) — a
   failure is returned as an outcome — and do blocking GitHub/git work on a
   thread (`FunctionNode` runs sync code on the event loop). A DF deploy raises
   its Composer DAG PR right after the dispatch and links the run beside it —
   merging once the run is green is the person's call, never automatic.
   The app is ONE event loop shared by every user: chat tools are wrapped with
   `off_event_loop` and other blocking calls go through `asyncio.to_thread`, or
   one person's GitHub wait stalls everyone's chat. A DF dispatch asks GitHub
   for its run id (`return_run_details`) and never guesses between concurrent
   runs; a deploy PR that conflicts with a concurrent merge is rebuilt on the
   new head (`MERGE_CONFLICT`), a review hold is never retried.
2. **Release model (CARE/DF)**: a release is a FILE-SET generated by the deployment
   repo's own `scripts/release/update_release_files.py` from a **transient**
   `release_details.json` (written outside the repo, never committed). Branch chain:
   `release/<slug>` → SIT → UAT → PRD/PRL1 for CARE; a DF release follows its own
   repo's chain (`DF_RELEASE_BRANCHES`, landing on the first — no SIT), with its own guard; promotion copies marker-listed files
   verbatim (`RELEASE-FILES-JSON:` in the release PR body). prl1_only charts never
   reach PRD; df_images never enter helm deploy workflows.
3. **BQ event log is OPTIONAL and APPEND-ONLY**: `release_intents` table, INSERT only;
   queue + per-env deployed state are derived (latest event wins). Empty `BQ_DATASET`
   disables cleanly; BQ outages degrade to error dicts — never block a release.
   Schema changes: edit `bigquery/release_intents.schema.json` + `_SCHEMA` in
   `release_queue.py` together (additive nullable columns only).
   Only what LANDED is `deployed`/`removed`: a change stopped at a PR awaiting
   review is a `pending` event against the PR that completes it, settled later
   by `pr_reconcile` at the PR's merge time (or `abandoned` if closed).
4. **Queue eligibility**: queueing for a release REQUIRES the GitHub Actions run URL;
   failed build or failed control (RCTLDEF*/RLFT/RFTL prefixes,
   case-insensitive, steps or jobs) → refused with the failures listed.
   Exception: QUEUE_ALLOWED_FAILING_CONTROLS (e.g. 1691, a possible false positive)
   queues anyway and shows as OPEN in the release queue only — nothing downstream stops.
5. **Secrets**: PATs are memory-only per thread and masked everywhere; nothing
   sensitive in git or BQ. Spans (`adk_release_agent/telemetry.py`) carry
   metadata only — model, tokens, latency, tool names, errors — and name a
   person by a stable digest, until `TRACE_CONTENT=true` deliberately admits
   prompts, tool arguments and emails for a sink inside the bank.
   Identity comes only from a VERIFIED token (identity.py) —
   a header value is never trusted just because it is present. `.env` and
   `.claude/launch.json` stay untracked.
6. **LLM boundaries**: facts come from tools; charts/tables are model-emitted specs
   rendered by deterministic code (vendored Chart.js, no CDN — Tailwind, Font Awesome and Inter are vendored too, `static/vendor/`); the classifier routes
   only — it never mutates.

## Workflows

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q     # tests (169+)
.venv/bin/python -m ruff check src adk_release_agent tests
uv lock && uv export --no-dev --no-hashes --no-emit-project -o requirements.txt  # after dep changes
# run: uvicorn release_agent.app_fastapi:app --app-dir src (env via .env / launch config)
TAILWIND_CLI=/path/to/tailwindcss-v3.4.17 scripts/css/build.sh   # after using a new Tailwind class; commit static/vendor/tailwind.css
```

- Config: every setting is a pydantic field in `src/release_agent/config.py` with env
  aliases; cluster values live in `helm/release-copilot/values.yaml` under `config:`.
- Docker: NO `git` binary — the release flow checks out the deploy repo with Dulwich
  (depth 1, proxy + CA handed over explicitly) and commits via the Git Data API
  (`tools/git_snapshot.py`); deps come from `uv.lock`, NOT requirements.txt.
- Style: no regex in parsing paths (tokenizers), comments explain constraints not
  mechanics, frontend is plain ES modules (no build step).
- Frontend: a rule or sentence the Backstage plugin would also need goes in
  `static/core/` with a `tests/js/` test; backend calls only via `static/api.js`.
