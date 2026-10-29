# Dev Portal — notifications, Auto-triage, deployment tracking

Status: **built** (2026-10-10 → 2026-10-12); end-to-end tested on GKE (Auto-triage, bell, tickets, tracker) and against real GitHub (the NOTIFY_WORKFLOW dispatch). Section 7 is what a deployment sets to turn it on. Read AGENTS.md first; every rule there
holds. This is the spec the parallel builds follow, so the pieces fit: the
interfaces below are contracts, not suggestions. Anything not fixed here is
the builder's call, following the nearest existing code.

Every feature ships behind a preview feature (`features.py`): `notifications`,
`auto-triage`, `deploy-tracking`. Nothing here is a mutation of a deployment.
The one GitHub write (triggering the team's send-notification workflow) is an
allowed-without-yes rule in `approvals.py`, named explicitly.

## 1. The events table — `portal_events` (tools/events_store.py)

ONE append-only BigQuery table for the new features: notifications,
Auto-triage findings, deployment tracking. The existing tables
(`release_intents`, `support_findings`, `bq_cost_findings`) are untouched —
migration is a later decision. Schema: `bigquery/portal_events.schema.json`
(FLAT and typed: the envelope, the human-readable fields, the shared fields,
each domain's own typed columns; `details` is a STRING holding JSON text for
long overflow only — the watcher's timeline, a proposal — never a field a
query filters on). Terraform: a fourth table block in
`bigquery/terraform/main.tf` (variable `portal_events_table`, default
`portal_events`, day-partitioned on `event_ts`, clustered on
`domain, subject, recipient`, same deletion protection and retention variable
as the others). Settings: `PORTAL_EVENTS_DATASET` ("" = memory store),
`PORTAL_EVENTS_TABLE` (default `portal_events`), `PORTAL_EVENTS_PROJECT`
(default BQ_PROJECT / the release log's project).

Rules (invariant 3 applies): INSERT only; "read", "cleared", "dismissed" are
NEW rows; every read is "latest row per (domain, subject, subject_version)
wins" — or per `scope` where the domain uses it. A BigQuery outage returns an
error dict, never raises, never blocks anything. Memory store is a list with a
lock, per process (one replica today; the design says so in values.yaml).

```python
# tools/events_store.py — the ONE writer/reader for portal_events
def record(row: dict) -> dict          # {"ok", "event_id"} | {"ok": False, "error"}; fills event_id/event_ts
def record_many(rows: list[dict]) -> dict
def latest(domain: str, *, since_days: int = 14, recipient: str = "", subject: str = "",
           subject_version: str = "") -> dict   # {"ok", "rows": [latest per subject(+version)]}
def history(domain: str, *, since_days: int = 14, **filters) -> dict   # every row, newest first
def enabled() -> bool                  # dataset configured
def store_name() -> str                # "bigquery" | "memory" (the cards say where findings are kept)
```

Column vocabulary (keep to it; add a column only through the schema file +
`_SCHEMA` in events_store.py together, additive nullable only):
`event_id, event_ts, domain, event_type, subject, subject_version, actor,
recipient, scope, status, severity, title, body, link, repo, pr_number,
environment, jira, model, model_calls, seconds` + typed domain columns:
release/deploy `prl1_only, df_only, build_verified, build_run_url,
target_envs, allowed_failures, note, run_id, run_url, workflow, conclusion,
merge_sha, chg, rmg`; watch `category, count, owner,
runbook, cause, established, action, action_why, fallback, notified,
business_date (DATE), incident_id`; overflow `details` (STRING, JSON text).

## 2. Notifications (tools/notifications.py, /api/notifications, static/notify.js)

Per person, by VERIFIED email (identity.py); with identity off, the typed
email is the recipient and the response says `unverified: true`.

```python
def notify(recipient: str, *, kind: str, title: str, body: str = "", link: str = "",
           scope: str = "", severity: str = "info", subject: str = "", **cols) -> dict
    # domain="notification", event_type="created" (or "updated" when the scope already has a
    # created row: SAME subject, so the bell shows ONE item whose text changed), status="unread"
def list_for(recipient: str, *, since_days: int = 14) -> dict   # latest per subject, newest first
def mark(recipient: str, subjects: list[str], status: str, actor: str) -> dict   # "read" | "dismissed" rows
    # each subject checked by a point read (recipient + subject), so age does not matter; > 200 subjects refused
def read_all(recipient: str, actor: str) -> dict   # every unread of list_for (the recipient's newest 2000), written in batches
def unread_count(recipient: str) -> int
```

Kinds and where a click goes (`link`): `release_pr` (the PR), `uat_pr` (the
PR), `df_run` (the run; `chat:<message>` on failure), `requeue`
(`portal:queue`), `chg` (the PR comment), `deploy_started` (the run),
`deploy_finished` (`portal:deploy:<repo>#<pr>` → the tracking card),
`auto_triage` (`portal:support:<incident_id>:<date>`). `portal:` links are
handled in the frontend; everything else opens a new tab.

Routes (verified identity only; a recipient never sees another's rows):
`GET /api/notifications?since_days=14` → `{ok, unread, items:[...], checked_at}`;
`POST /api/notifications/mark {subjects:[...], status:"read"|"dismissed"}`;
`POST /api/notifications/read-all`.

Frontend (`static/notify.js`, pure rules in `static/core/notify.js` with
`tests/js/notify.test.mjs`): bell in the header between "What can I do?" and
"New Thread" (see the mock in the session: dot filled = unread, hollow = read;
green info, amber warning, red critical; title + one body line + age; "Mark
all read"; footer "Checked Ns ago" + "See all" → a card in the chat listing
the last 14 days). Polls `GET /api/notifications` every 60 s while the tab is
visible (`document.visibilityState`), at once on focus. Hidden entirely when
the feature is not on for this person (`window.PORTAL_UI.features`).

Producers (each one line at the place the fact is already known, best-effort,
never raising): `pr_reconcile` resolution → `release_pr` to `requested_by`
(merged: "Release PR #N merged — K charts on <env>"; closed: "… closed
without merging"); the UAT deploy's SIT→UAT wait → `uat_pr`; the DF dispatch's
run outcome → `df_run`; `requeue_from_history` → `requeue`.

## 3. CHG / RMG tickets (tools/tickets.py)

Deterministic. `tickets_in(text, prefixes) -> list[Ticket]` tokenises on
whitespace and punctuation (NO regex — a tokenizer like parsing.py's), keeps
tokens `<PREFIX>-<digits>` or `<PREFIX><digits>` for the configured prefixes
(`TICKET_PREFIXES: "CHG,RMG"`, case-insensitive). Only the ticket is read —
never a state: the PR comments do not say one.
`pr_tickets(repo, pr_number) -> {chg: {id, source, url}, rmg: {...}}`
reads the PR body then the comments oldest→newest; the newest mention per
prefix wins; `source` is "body" or "comment"; `url` the comment's html_url.
Cached per (repo, pr) for 10 minutes (the banner already costs GitHub calls).

Surfaces: `/api/release-status` → `tickets` on every release PR in
`release_prs` (UAT→PRD and UAT→PRL1 together) and on `df.release_pr`; the
banner (`status.js`) shows `CHG-12345 · RMG-678` as links, grey "no CHG yet"
when none; recorded on the event log's pending row (`chg`, `rmg`) and as a
`chg` notification to
the requester the first time a CHG appears.

## 4. Deployment tracking (adk_release_agent/deploy_tracker.py)

A timed sweep, started in the app lifespan like the idle sweeper, never
overlapping (`begin()` lock, like watch.py). Cadence: nothing in flight →
idle (one event-log read and one listing of the guard branches); a PR open → every
`TRACK_OPEN_MINUTES` (5); a merge with runs in flight → every
`TRACK_RUN_SECONDS` (60); give up with a "still running" notification after
`TRACK_RUN_MAX_MINUTES` (90).

On merge (detected by `pr_reconcile`'s check, merge SHA kept): record
`deploy/merged` with `merge_sha`; notify `deploy_started` to the requester;
if `NOTIFY_WORKFLOW` (owner/repo + workflow file) is set, dispatch it with the
fixed inputs `{release_notification_type: "Start", change_request: <CHG>,
environment, create_release_draft: "true", test_notification: "false"}`
(`environment` is PROD for the PRD branch, PRL1 for the PRL1 branch, else the
base branch as it is) and keep its run id — `return_run_details`, never a guess. Then find the runs the merge
started: `head_sha == merge_sha` and workflow name in `DEPLOY_WORKFLOWS`
(comma list; empty = every workflow run on that SHA). When every one has a
conclusion: `deploy/finished` with `conclusion` (success | failure + the
failing jobs/steps via controls.py), notify `deploy_finished` (same scope as
`deploy_started`: ONE bell item that changes), dispatch `NOTIFY_WORKFLOW`
with `release_notification_type: "Complete"` — a failure dispatches nothing. A re-run on the same run id updates the
same scope ("completed (after re-run)"). Token: `_resolve_github_token` as
is — the session PAT when a thread is active, else `GH_TOKEN` (the bot; the
values.yaml comment says so). Nothing here merges, re-runs or rolls back.

Guard-branch PRs. In the CARE mono flow, merging in the mono repo makes that
repo's own action raise PRs in the DEPLOYMENT repo (`DEPLOY_REPO`) into the guard
branches (`release_chain.guard_branches("care")`, UAT → PRD and UAT → PRL1). The
portal did not raise them, so they have no `pending` event. Each pass lists the
newest open PR into each guard branch (`release_window._open_release_prs`) and
records an unseen one as `deploy/watching` (scope `deploy:<repo>#<n>`, `title`
"UAT → PRD", the base branch in `details`). Every watched PR is then fetched once
per pass until it merges — the same merged → runs → finished path, same scope —
or closes unmerged (`deploy/closed`, nothing sent). Its window ends like a
pending PR's. State is only the event log, so a restart carries on. A PR that is
also a portal pending PR is followed once, as pending (the requester's bell).
Nobody in the portal raised a watched PR, so it has no requester and no bell
item; the `NOTIFY_WORKFLOW` dispatch is what tells people. A PR that opens and
merges between two passes is never seen open, so each pass also reads the newest
closed PRs into the two branches and takes one merged since `catch_up_since`
with no tracker row: recorded `deploy/watching`, followed in the same pass. The
bound is at most CATCH_UP_PASSES (3) × TRACK_OPEN_MINUTES back and never before
the process started with the memory store (a restart forgot what was sent); with
BigQuery it may reach back to the tracker's latest row, so a merge during a
restart is still caught. Switching tracking on never announces an old merge.

Cost per pass: each pending PR is fetched ONCE — the sweep hands what it read
to `pr_reconcile.reconcile_pending(outcomes=…)` and to `tickets.pr_tickets(pr=…)`.
The CHG/RMG first-seen read (a BigQuery `history` query) runs only for a PR that
is open or newly merged, never for a settled one. A merge whose
`merge_commit_sha` GitHub has not filled in yet is counted in flight and read
again next pass: nothing is matched to runs, recorded or dispatched without a SHA.

## 5. Auto-triage (tools/support/watch.py, adk_release_agent/support_watch.py)

Port from `backstage_poc` (same file names, same `SUPPORT_WATCH_*` settings,
same routes `/api/support/watch`, `/watch/run`, `/watch/status`), with:
- findings in `portal_events` (domain `watch`) through events_store, not the
  separate `support_watch` table; `SUPPORT_WATCH_DATASET` is gone —
  `PORTAL_EVENTS_DATASET` decides;
- notifications through `notifications.notify` (kind `auto_triage`, title
  "Auto-triage: <incident title>", scope `watch:<date>:<incident_id>`) to
  `SUPPORT_NOTIFY_RECIPIENTS` (comma list of emails) and the runbook owner
  when it is an email; no Backstage API here;
- a per-investigation deadline `SUPPORT_WATCH_INVESTIGATE_SECONDS` (240): a
  timed-out investigation is recorded as "no cause established — evidence
  timed out" and the pass moves on;
- the interval measured from the pass START: next = max(start + N min,
  end + 1 min); the status says `next_at`;
- compare the JOBS inside an incident, not only its id: an incident whose
  job list grew since its finding is re-notified ("now 5 jobs");
- a finding the watcher CLEARED (its incident left the table) whose incident
  is in the table again the same day is new news: a `reopened` row (status
  open), a fresh investigation under the same cap, and the bell item updated
  to "Auto-triage: Back — <title>" (`watch.returned`). Dismissed and resolved
  stay as the person left them;
- a finding is stored and notified as EACH investigation completes;
- `POST /api/support/watch/run` (an operator's API, not a button: the card
  offers no Run now and no date — Auto-triage runs on its own, Support triage is
  the on-demand view) starts a pass in the background and answers `{ok,
  started}` at once; a pass cancelled mid-way releases the watcher's lock
  (`error: "cancelled"`).
The name a person sees is **Auto-triage** everywhere: pill, strip, card, bell.

UI: a pill `Auto-triage` in the Support group (`form: 'auto-triage'`); a strip
line under Release status (`status.js`: "Auto-triage · every 10 min · last
check HH:MM · N needs attention · M open · next HH:MM", amber/red by health);
the feed card (`forms/auto_triage.js`, pure rules in `core/auto_triage.js` +
`tests/js/auto_triage.test.mjs`: `groupFindings`, `watcherHealth`,
`statusActions`, `statusText`, `ago` — ported from the plugin's
`watchFormat.ts`): header + refresh; three tiles (Auto-triage health,
Needs attention, Open — they wrap in a narrow window); groups Needs attention /
Open / Resolved, dismissed and cleared; each finding: priority badge, title,
since/investigated, source · owner · category · rows, then Runbook / Finding /
Do now lines, a Rows section (the incident's facts, error text, runs, jobs — kept on the finding,
so the card needs no trip to Support triage), buttons Ask why · Mark resolved · Dismiss ·
Was this right? (closed: Reopen + who). An Insights drawer section
"Auto-triage" (last check, counts, accuracy). With LLM_ENABLED=false
everything renders; Ask why is absent, the finding is the model-free one.

## 6. Tests and checks (every build)

- pytest for every module (store memory + BigQuery seams monkeypatched;
  identity on/off; feature off → 404/refused; latest-wins; one-item-per-scope;
  the sweep cadence as pure functions of state; tokens never logged).
- `tests/js/*.test.mjs` for every `static/core/*` rule.
- `PYTHONPATH=src .venv/bin/python -m pytest -q`, ruff, `node --test
  tests/js/*.test.mjs`, `helm lint helm/release-copilot`.
- `python3 scripts/private_names_check.py --staged` clean (it carries the
  deployment's own terms from the git-ignored `.private/denylist.txt`).
  Example names only (example-org, svc-a, df-position-agg).
- AGENTS.md map + the matching `.github/instructions/release-copilot/*` rule
  updated in the same commit; values.yaml gets every new setting with a
  comment; `bigquery/README.md` mentions the fourth table.
- Commit per feature on the build's branch, message in the repo's style, the
  Co-Authored-By trailer; never push.

## 7. Turning it on — what a deployment must set

Every feature is off until configured — its pill, strip and bell do not even show until switched on, testers included — and preview-gated: list testers in
`PREVIEW_USERS` (or remove `notifications`, `auto-triage`, `deploy-tracking`
from `PREVIEW_FEATURES` to open them to everyone). All settings go in the
chart's `values.yaml` under `config:`.

**Storage (all three features).** Create the `portal_events` table — the
fourth block of `bigquery/terraform/main.tf`, or `bq mk` with
`bigquery/portal_events.schema.json` (day partition on `event_ts`, clustering
`domain, subject, recipient`) — and set `PORTAL_EVENTS_DATASET` to its dataset.
The runtime identity needs `roles/bigquery.dataEditor` on that dataset (and
`bigquery.jobUser` on the project, which the release log already needs).
Without it everything runs from pod memory: a restart forgets findings,
notifications and which merges were announced.

**Notifications (the bell).** Nothing else to set. Recipients are VERIFIED
emails (the mesh's RCToken, `IDENTITY_HEADER`); with identity off the bell
asks for an email instead — for local testing only.

**CHG / RMG on the banner.** `RELEASE_GUARD_BRANCHES` must list BOTH terminal
branches (e.g. `"PRD,PRL1"`) so the banner shows every promotion PR in flight;
`TICKET_PREFIXES` defaults to `CHG,RMG`. Only the ticket ids are shown.

**Auto-triage.** `SUPPORT_TABLE` (already needed by Support triage),
`SUPPORT_WATCH_MINUTES: "10"` (0 = off), `SUPPORT_NOTIFY_RECIPIENTS` (who gets
the bell items). The loop runs inside the portal pod: keep `replicaCount: 1`
(more replicas would each run it — see the scaling decision below).

**Deployment tracking + the send-notification workflow.**
- `DEPLOY_TRACKING_ENABLED: "true"`.
- `NOTIFY_WORKFLOW: "<org>/<repo>:<workflow file>"` — the team's
  send-notification workflow. It must have `workflow_dispatch` (GitHub
  dispatches only from the default branch) with the inputs
  `release_notification_type` (Start|Complete), `change_request`,
  `environment` (PROD|PRL1), `create_release_draft`, `test_notification`.
  The portal sends Start when a PR into a guard branch is merged and Complete
  when every run on that merge commit succeeded; a failure sends nothing (the
  bell still says where it failed). `environment` is PROD for the
  `PRD_BRANCH` base and PRL1 for the `PRL1_BRANCH` base.
- `NOTIFY_TEST: "true"` for a dry run (test Teams channel), then `"false"`.
- `DEPLOY_REPO` — the deployment repo whose guard branches are watched.
- Optional: `DEPLOY_WORKFLOWS` (only these workflow names count as "the
  deployment" on the merge commit; empty = every run on it),
  `TRACK_OPEN_MINUTES: "1"` (Start within ~1 minute of the merge; default 5).
- GitHub token (`githubToken.existingSecret`, the bot): read on the deployment
  repo (PRs, comments, Actions runs) and **Actions: write** on the
  NOTIFY_WORKFLOW repo. The tracker has no chat session, so it always uses it.
- Known limit: a guard PR opened AND merged between two checks is caught only
  inside `catch_up_since` (a few passes, and not across a restart with the memory
  store); older ones show as "unseen" in the Release notifications card.

**Scaling decision (2026-10-11).** One replica now. When the portal must run
more than one: on Backstage, run Auto-triage and the tracker as scheduled tasks
with `scope: 'global'`; for the Python portal alone, add Postgres and take a
`pg_try_advisory_lock` per loop on a dedicated connection (chat sessions move
there too). BigQuery stays for history, never as a live channel.
