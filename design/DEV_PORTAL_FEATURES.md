# Dev Portal — notifications, Auto-triage, deployment tracking

Status: **being built** (2026-10-10). Read AGENTS.md first; every rule there
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
merge_sha, chg, rmg, chg_state, rmg_state`; watch `category, count, owner,
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
(`TICKET_PREFIXES: "CHG,RMG"`, case-insensitive), and reads the ticket's state
from the words on the same line (`TICKET_STATE_WORDS: "created,approved,
rejected,closed,cancelled"`; the last state word on the line wins).
`pr_tickets(repo, pr_number) -> {chg: {id, state, source, url}, rmg: {...}}`
reads the PR body then the comments oldest→newest; the newest mention per
prefix wins; `source` is "body" or "comment"; `url` the comment's html_url.
Cached per (repo, pr) for 10 minutes (the banner already costs GitHub calls).

Surfaces: `/api/release-status` → `blocking_pr.tickets` and
`df.release_pr.tickets`; the banner (`status.js`) shows `CHG-12345 ·
RMG-678 approved` as links, grey "CHG pending" when none; recorded on the
event log's pending row (`chg`, `rmg`, states) and as a `chg` notification to
the requester the first time a CHG appears.

## 4. Deployment tracking (adk_release_agent/deploy_tracker.py)

A timed sweep, started in the app lifespan like the idle sweeper, never
overlapping (`begin()` lock, like watch.py). Cadence: no portal PR open →
idle (one event-log read, no GitHub call); a pending PR open → every
`TRACK_OPEN_MINUTES` (5); a merge with runs in flight → every
`TRACK_RUN_SECONDS` (60); give up with a "still running" notification after
`TRACK_RUN_MAX_MINUTES` (90).

On merge (detected by `pr_reconcile`'s check, merge SHA kept): record
`deploy/merged` with `merge_sha`; notify `deploy_started` to the requester;
if `NOTIFY_WORKFLOW` (owner/repo + workflow file) is set, dispatch it with
inputs `{stage: "starting", release, chg, rmg, pr, requested_by}` and keep its
run id — `return_run_details`, never a guess. Then find the runs the merge
started: `head_sha == merge_sha` and workflow name in `DEPLOY_WORKFLOWS`
(comma list; empty = every workflow run on that SHA). When every one has a
conclusion: `deploy/finished` with `conclusion` (success | failure + the
failing jobs/steps via controls.py), notify `deploy_finished` (same scope as
`deploy_started`: ONE bell item that changes), dispatch `NOTIFY_WORKFLOW`
with `stage: "completed" | "failed"`. A re-run on the same run id updates the
same scope ("completed (after re-run)"). Token: `_resolve_github_token` as
is — the session PAT when a thread is active, else `GH_TOKEN` (the bot; the
values.yaml comment says so). Nothing here merges, re-runs or rolls back.

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
`watchFormat.ts`): header + refresh; four tiles (Auto-triage health,
Needs attention, Open, Notifications on/off + store); groups Needs attention /
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
