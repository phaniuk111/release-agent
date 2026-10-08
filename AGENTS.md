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
| `src/release_agent/tools/` | GitHub/BQ tool layer (source of truth for all facts): `promotion.py` (deploy PR chains SIT→UAT→PRD), `release_fileset.py` (CARE/DF release model), `care_release.py` (CARE mono mode: one committed release file → a PR a person merges; the portal never merges it), `json_splice.py` (edits a committed JSON file's top-level values in place — every other byte kept, unknown keys refused; no regex), `release_chain.py` (each kind's branch chain: CARE SIT→UAT→PRD/PRL1, DF `DF_RELEASE_BRANCHES` e.g. RELEASE_UAT→RELEASE_PRD), `git_snapshot.py` (Dulwich checkout + API commit — no git binary), `clone_probe.py` (diagnostics: which fetch paths the proxy allows), `monitoring.py` (MONITOR_CHECKS PromQL checks + read-only queries for the Monitoring pill/skill), `bq_cost.py` (BigQuery cost report — design/BQ_COST.md: INFORMATION_SCHEMA scan, findings memory), `bq_guard.py` (the ONE gate every cost statement passes: real only for INFORMATION_SCHEMA / `__TABLES__` reads, everything else dry-run, DDL/DML refused — token-based, no regex), `bq_cost_xlsx.py` (that same report dict as .xlsx; no spreadsheet library), `support/` (Support, read-only, nothing knows a real table — the table, columns, statuses and owners are config, the runbook a plain-English skill in the image: `config.py` the column → role mapping and the data project, `discover.py` the mapping and status words found from the table itself when they are not configured (only SUPPORT_TABLE is required), `queries.py` the statements through `bq_guard` (BigQuery counts, only problems come back) and the live mapping check, `report.py` PURE results → what failed and how widely, `incidents.py` the L1 layer (runbook, action, owner, priority, ticket note), `runbook.py` the runbook skill (`skills/support-runbook/SKILL.md`) — its known issues' header lines for the cards (works with no model), its whole text for the AI, `triage.py` the entry points, `source_logs.py` / `dataflow_job.py` / `release_lookup.py` the evidence readers (container + audit logs, and a failed run's own lines — every entry naming its run id, from `SUPPORT_RUN_LOG_MINUTES` before its row was written; one Dataflow job and its code-decided kind; what changed before a date), `evidence.py` the Investigate collector (every read in parallel, the model's pruned bundle, a model-free rendering), `signals.py` PURE leads and `with_judgement`, `timeline.py` PURE evidence timeline, `cache.py` the ONE shared read cache — collector and chat tools key the same read the same way, single-flight, `feedback.py` "was this right?" append-only + accuracy), `release_queue.py` (BQ event log), `pr_reconcile.py` (settles PRs merged/closed outside the chat), `chg_defaults.py` (change-request fields from facts: release name/number, standard wording), `controls.py` (build verification, RCTLD control parsing), `release_window.py` (release guard), `manifest.py`, `_common.py` (GitHub client, session PAT resolution) |
| `src/release_agent/support_api.py` | The Support routes (triage, the live mapping check, feedback), included by app_fastapi; gated by the `support-triage` feature — out of preview by default (list it in PREVIEW_FEATURES to gate it again) |
| `src/release_agent/session_creds.py` | Per-thread GitHub PAT: memory-only, masked, never logged/stored; bound to its owner when identity is on |
| `src/release_agent/commands.py` | LLM_ENABLED=false: recognises the pills' commands (no regex) and writes tool results as markdown; running them is `commands_workflow.py`'s job |
| `src/release_agent/features.py` | Preview features: PREVIEW_GROUPS (pill groups) and PREVIEW_FEATURES (API + chat tools refuse) shown only to PREVIEW_USERS — verified emails, or `*` for a testers-only release. Ship new features behind it |
| `src/release_agent/identity.py` | Signed-in user from the mesh's RCToken (IDENTITY_HEADER) — signature-VERIFIED against the issuer's JWKS, never just decoded; the verified email beats any typed one |
| `src/release_agent/static/` | ES-module frontend in three layers (see `static/README.md`): `core/` pure rules shared with the Backstage port (queue routing/wording, validation, formatting; tested in `tests/js/`), `api.js` every backend call, and screens — `forms/` (one module per form: queue form + table, release history, CARE/DF release, deploys, chat parsers), `chat.js` (SSE, markdown, ```chart blocks → vendored Chart.js), `palette.js` (pills + ⌘K), `insights.js`, `status.js` (banner) |
| `adk_release_agent/` | ADK app: `agent.py` (skills-routed chat agent + ROOT_INSTRUCTION), `deploy.py` (preview→token→apply), `deploy_workflow.py` (deterministic Workflow graph: gate → apply / cancel), `commands_workflow.py` (LLM_ENABLED=false: the pills' commands as a Workflow — gate → run / reject, yes/no pause via RequestInput), `approvals.py` (which tool calls need a yes — ONE rule set for the agent and the commands workflow), `tools.py` (ADK wrappers incl. queue eligibility gate), `support_tools.py` (the Support chat tools: triage, the single-source evidence readers and `investigate_evidence`; both hand the model `priority_policy`, the text of `skills/support-priority/SKILL.md`), `safety.py` (MutationGuardPlugin), `intent.py` (classify-only fallback), `investigate_workflow.py` (the Investigate button as a fixed pipeline: collect evidence in code → ONE capped model step on `INVESTIGATE_MODEL`; model-free answer when LLM is off or the model fails), `telemetry.py` (ADK's OTel spans → Langfuse/OTLP; off unless LANGFUSE_HOST is set), `skills/*/SKILL.md` (per-domain instructions + tool unlock lists) |
| `bigquery/` | Table provisioning: terraform module + `release_intents.schema.json` (single source of truth), `bq_cost_findings.schema.json` (the cost report's optional findings memory) and `support_findings.schema.json` (the optional "was this right?" memory of investigations) |
| `helm/release-copilot/` | Chart; ALL runtime config flows as env via `values.yaml → config:` (generic ConfigMap) |
| `tests/` | pytest; `conftest.py` blanks BQ so tests never write real telemetry |

## Core invariants (do not break)

1. **Two confirmation flows, never mixed**: chart:version deploys and releases run a
   deterministic preview → exact `CONFIRM-XXXXXX` token. High-impact ops tools
   (merge_prod_release, terminal promotions) pause on a yes/no approval. The portal
   has NO removals: a chart leaves the next release only by being withdrawn from
   the release queue; a UAT deploy overwrites the file with what is submitted.
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
   runs. A UAT deploy OVERWRITES uat/deployment.json with exactly what the
   developer gives, one at a time: it is refused while an open PR into SIT or UAT
   changes that file (merge it — what is in it deploys — or close it) or another
   portal deploy is still running (`promotion.uat_deploy_blocker`, asked as the
   form opens, at preview, and at confirm); its conflicts are left open, never
   rebuilt over someone else's change. UAT changes always flow via SIT (SIT and
   UAT stay in sync): a UAT deploy merges into SIT only; the deployment
   repo's OWN workflow raises SIT -> UAT, which the portal waits for, shows, and
   records as the pending PR — never raises or merges it. The form
   then reads SIT's file, and the running marker is held until that PR appears. Other chain changes (release promotions)
   rebuild a PR that conflicts with a concurrent merge on the new head
   (`MERGE_CONFLICT`); a review hold is never retried. A failure in the chat
   model after a state-changing tool RAN says what ran, never "nothing changed".
   A turn OUTLIVES its reader: it runs detached from the SSE transport (which
   would otherwise cancel it mid-flight when the tab closes), and only the
   free-form chat lane is ended when the reader leaves — through a private stop
   signal given to ADK ONLY while no tool is in flight (ADK 2.11 cancels the
   await, not the worker thread: a tool running when the stop arrives would land
   and be recorded as aborted). The deploy and commands Workflows never get a
   stop, and every Workflow run is iterated to its natural END, pause included —
   a run left half-consumed is finalised later in another context, cancelling
   leftover tasks there.
   A third lane, read-only: "Investigate incident <id> …" (the Support triage
   button; `parsing.investigate_request`) is recognised FIRST in the router and
   runs the investigate Workflow even while a token or an approval is pending,
   without consuming, cancelling or answering either. It is a fixed pipeline —
   evidence collected in code (`tools/support/evidence.py`), then ONE model step
   capped by `INVESTIGATE_MAX_MODEL_CALLS`; code, not the model, decides whether
   the evidence establishes a cause and which action a failure kind implies
   (`with_judgement` — what a failed run's own lines say comes first, before
   anything its source logged that day), and the advisor is refused unless two pieces of evidence
   contradict each other. One finding per incident is shared for ten minutes
   (single-flight); a fallback is never shared. The finding is written to the
   chat session's STATE (`last_investigation`, never a message — the chat
   session may hold a paused approval) so follow-ups can use it.
2. **Release model (CARE/DF)**: a release is a FILE-SET generated by the deployment
   repo's own `scripts/release/update_release_files.py` from a **transient**
   `release_details.json` (written outside the repo, never committed). Branch chain:
   `release/<slug>` → SIT → UAT → PRD/PRL1 for CARE; a DF release follows its own
   repo's chain (`DF_RELEASE_BRANCHES`, landing on the first — no SIT), with its own guard; promotion copies marker-listed files
   verbatim (`RELEASE-FILES-JSON:` in the release PR body). prl1_only charts never
   reach PRD; df_images never enter helm deploy workflows. Every release — CARE in
   either mode, and DF — moves its charts from the queue to Release history as
   soon as its PR is raised, merged or held for review (`mark_released`); one
   that does not go through is put back from Release history.
   CARE in mono mode (`CARE_RELEASE_MODE=mono`, `care_release.py`) is the one
   exception: the release is ONE committed file, `CARE_RELEASE_FILE` in
   `CARE_RELEASE_REPO`, changed by a minimal splice (`json_splice.py` — only the
   values that change move; a key the file lacks, like df_images, is refused) on a
   `CARE_RELEASE_BRANCH_PREFIX` branch, and raised as a PR against
   `CARE_RELEASE_BASE_BRANCH` that the portal NEVER merges — a person does, and
   the repo's own workflow updates the deployment repo. Before the release file is
   overwritten, its CURRENT artefacts — the last merged release — are carried into
   `CARE_PREVIOUS_TAGS_FILE` (the repo's `{chart: version}` record; existing
   charts updated, a new one added, others untouched; empty setting = off): the
   PR carries two commits in that order, previous tags then release file, and the
   preview shows both diffs. Same CONFIRM token; apply refuses if either file on
   the base moved since the preview. The PR names who
   raised it (the verified email, else the typed one marked unverified). Its guard
   counts only open PRs into the base whose branch has the prefix, and runs at
   preview AND again at apply; apply reuses an open PR only when its branch
   already carries exactly the approved file — never writing over another
   release or a reviewer's edit. Raising the PR releases its charts in the queue
   at once (`mark_released`): they leave the queue for Release history, and if
   the release does not go through they are put back from there — no merge is
   tracked. Only this mode's
   form opens pre-drafted, with the team's "Low risk." / "No user impact is
   expected." leads (`chg_draft._draft_mono`). DF, and CARE in fileset mode, are
   unchanged — their button-drafted change request included.
3. **BQ event log is OPTIONAL and APPEND-ONLY**: `release_intents` table, INSERT only;
   queue + per-env deployed state are derived (latest event wins). Empty `BQ_DATASET`
   disables cleanly; BQ outages degrade to error dicts — never block a release.
   Schema changes: edit `bigquery/release_intents.schema.json` + `_SCHEMA` in
   `release_queue.py` together (additive nullable columns only).
   Only what LANDED is `deployed`/`removed`: a change stopped at a PR
   awaiting review is a `pending` event against the PR that completes it, settled
   later by `pr_reconcile` at the PR's merge time (or `abandoned` if closed).
4. **Queue eligibility**: queueing for a release REQUIRES the GitHub Actions run URL;
   failed build or failed control (RCTLDEF*/RLFT/RFTL prefixes,
   case-insensitive, steps or jobs) → refused with the failures listed.
   Exception: QUEUE_ALLOWED_FAILING_CONTROLS (e.g. 1691, a possible false positive)
   queues anyway and shows as OPEN in the release queue only — nothing downstream stops.
   A chart put back from the release history is NOT re-gated (`requeue_from_history`,
   `/api/release-queue/requeue`): it qualified once at that version and the run it
   was verified against has not changed, so the new `queued` event copies the
   original's run, ticket, routing, verification result and details. A chart with
   no `queued` event never qualified and takes the gate like a first submission.
5. **Secrets**: PATs are memory-only per thread and masked everywhere; nothing
   sensitive in git or BQ. Spans (`adk_release_agent/telemetry.py`) carry
   metadata only — model, tokens, latency, tool names, errors — and name a
   person by a stable digest, until `TRACE_CONTENT=true` deliberately admits
   prompts, tool arguments and emails for a sink inside the bank.
   Identity comes only from a VERIFIED token (identity.py) —
   a header value is never trusted just because it is present. Every commit
   and PR the portal makes names that verified caller (`tools/attribution.py`:
   a `Requested-by:` trailer + the git author; `{requested_by}` for DF run
   names) — never a typed email, which is a claim, not an identity. Every commit
   message and PR title an action from a form creates (Deploy to CARE/DF UAT,
   CARE/DF Release — each has a required JIRA field) starts with that JIRA, taken
   as typed (`attribution.jira`/`titled`/`commit_message`, merge titles too); a
   release's promotions reuse it (`RELEASE-JIRA:` in the release PR body); chat
   actions without a form carry none. `.env` and `.claude/launch.json` stay untracked.
6. **LLM boundaries**: facts come from tools; charts/tables are model-emitted specs
   rendered by deterministic code (vendored Chart.js, no CDN — Tailwind, Font Awesome and Inter are vendored too, `static/vendor/`); the classifier routes
   only — it never mutates. `LLM_ENABLED=false` runs with no model at all: the
   router skips the classifier and chat agent and sends the chat to the commands
   Workflow (same tools, same `approvals.py` rules, same (owner, thread)-keyed
   yes/no); deploys/releases keep preview → token; drafting, "Ask why" and
   onboarding say to enable LLM access (`window.PORTAL_UI.llm`). Everything
   that pauses stays in ADK, so switching the model on changes who picks the
   tool, never what the tool or its approval does.

## Workflows

GitHub Copilot reads the app's rules from `.github/instructions/release-copilot/`
(per path, by `applyTo`; code review and the cloud agent use them too):
`app.instructions.md` is the short version of this guide, the others add per-area
rules. Their `applyTo` globs list the monorepo paths (`release-copilot/…`,
`helm/release-copilot/…`) and this repo's, so the folder moves into a monorepo
unchanged. When a rule here changes, update those in the same commit. In a
monorepo this file sits in `release-copilot/` and the chart in the root `helm/`;
VS Code reads an AGENTS.md outside the workspace root only when nested AGENTS.md
support is turned on — or open `release-copilot/` as the workspace.

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
- Support triage needs only `SUPPORT_TABLE`: with no `SUPPORT_COLUMNS` the app maps
  the table from its schema (`tools/support/discover.py`, metadata only), and with
  no status lists it adds the table's own status words (the distinct statuses of
  the lookback, sorted by code) to the defaults. Anything configured wins, line by
  line — the chart's `supportTable:` block (drafted by `scripts/support_mapping.py`,
  the same matching rules) is flattened into the `SUPPORT_*` env strings;
  `/api/support/config-check` shows what was configured and what was discovered.
- What the team knows is plain-English skills in the image, versioned with the
  code: `skills/support-runbook/SKILL.md` (how to read the control table; each
  known issue — `match:` / `action:` / `owner:` / `steps:` lines the cards read
  with no model, then "Check / It is this issue if / It is not" for the AI) and
  `skills/support-priority/SKILL.md` (how urgent). "Ask why" gets both inside
  the `support_triage` result and Investigate in its finding instruction
  (`support_tools.team_guidance`, read on every call); the card's badge stays
  the built-in rule (`incidents._priority`), which is what shows with
  LLM_ENABLED=false.
- Public repo: deployment-specific names (the support-triage table, columns,
  status words, owners, runbook entries) never go in tracked files. Locally they
  live in the git-ignored `.private/`; `scripts/private_names_check.py` (hook:
  `ln -sf ../../scripts/hooks/pre-commit .git/hooks/pre-commit`, test:
  `tests/test_private_names.py`) refuses any term listed in
  `.private/denylist.txt`. A real deployment keeps them in its own values.yaml
  and in its own copy of the support skills (this repo's runbook and priority
  policy are generic examples).
- Frontend: a rule or sentence the Backstage plugin would also need goes in
  `static/core/` with a `tests/js/` test; backend calls only via `static/api.js`.
