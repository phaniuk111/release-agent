# Parked work and open decisions

One list, so nothing lives only in a chat transcript. Each item says what it
is, what it needs to restart, and what would make it worth restarting.

## Parked — designed, not built

### Alert noise reduction — `design/ALERT_HYGIENE.md`
Analyse one team's Cloud Monitoring incidents (multi-tenant project, scoped by
`team` label or name prefix), rank repetitive self-closing alerts, suggest
condition changes, replay-test each against 90 days of metrics. Reads only;
writes nothing; Terraform is where the team applies a suggestion.
- **Needs to restart:** `roles/monitoring.viewer` on the project, plus one
  scope key (a `user_labels.team` value is best; a display-name prefix works).
- **First step:** stage 0 — a one-hour, one-off script, same scope filter,
  no code in the repo. Returns the top five noisy policies with count, per
  week, median duration, self-closed %, peak hour.
- **Worth continuing if:** > ~30% of incidents self-close within 5 minutes,
  or the top three policies produce more than half the volume. Otherwise
  a quarterly by-hand review is enough.
- **Known specifics:** alerts go to email, nobody acknowledges — "real" is
  four measured signals (long-lived, large, co-firing, not self-healing),
  never a label. Most policies are log-based metrics templated per service —
  the report collapses a template into one row and one suggestion.

## Built — 2026-09-19

### BigQuery cost report — `design/BQ_COST.md`
Shipped as the **BQ cost report** pill in its own *Monitoring* group (the
"different audience" question was answered with a group of its own, not a
separate agent), the `bq-cost` skill and `/api/bq-cost/*` — released, not
preview (the PromQL checks stay in the preview *Check* row). What the
build learned that the design did not know is recorded at the top of
`BQ_COST.md` (anonymous datasets deny region-wide views, no partition count
in `JOBS`, parameterised dry runs price as 0 bytes, the scan labels and
excludes its own jobs).
- **Still true:** no JVM / no ZetaSQL; the model narrates and proposes, the
  numbers come from `INFORMATION_SCHEMA` and dry runs only; nothing applies
  itself.
- **Not built (by choice):** the weekly `CronJob` + Teams/email post from
  §8 — the on-demand pill and chat are enough until someone asks for a
  digest. The entry point exists (`python -m release_agent.tools.bq_cost`).
- **Enterprise switch-on:** see the follow-ups below.

## Skipped — deliberately

### Grafana-dashboard weekly memo
A narrative memo generated from a dashboard's PromQL panels. Skipped because
a memo has no action at the end of it and would go unread; the useful part
(week-over-week drift per service) only pays off joined to the release log,
and the alert-noise work above is the better use of the same access. The
research and the dashboard analysis (42 panels, 6 template vars, thresholds
mostly Grafana's default 80) are in the 2026-09-19 session transcript.

## Open decisions from the codebase review (2026-09-17)

- **`ScopeGuardPlugin`** — 181 lines, shipped as `SCOPE_GUARD: "log"`: pays a
  Gemini classifier call per off-topic-looking message and refuses nothing.
  Either set `enforce` or delete it. Decision, not a bug.
- **`config.py` alias collapse** — 63 of 169 env-alias spellings are unused;
  81 of 91 fields could be one-liners. ~230 lines. Not done because a
  removed spelling could break an env var the cluster relies on; do it when
  the ConfigMap can be audited alongside.
- **Vertex sessions / cross-process CONFIRM resume** — ~130 lines that only
  matter with `ADK_SESSION_BACKEND: vertex` and `replicaCount > 1`. Commit
  to it (then fix the replica count) or delete it and state that a restart
  cancels pending confirmations.
- **Inline HTML shell in `app_fastapi.py`** — 189 lines of markup in a
  Python string. Move to `static/index.html`; keep the server-side
  `{PORTAL_UI}` injection. Net zero lines, easier to edit.
- **`values.yaml` prose** — comments outnumber settings 2.6:1; the helm
  README already tabulates the keys. A move, not a delete: the CA-bundle and
  PDB notes are hard-won.

## Enterprise follow-ups (owner's side)

- Re-copy `adk-release-agent` — the DF-dispatch fix (`ab781f7`) and the
  controls-only queue gate (`39a030b`) postdate the last copy.
- Run the DF dispatch diagnostic to read GitHub's refusal reason (it is a
  scratch script; ask to have it committed as `scripts/df_why.py`).
- Confirm `QUEUE_ALLOWED_FAILING_CONTROLS: "1691"` is set in the ConfigMap —
  the allowance cannot apply if the key is empty.
- Confirm the `BUILD_TAG_STEP` / `BUILD_TAG_MARKER` values in the ConfigMap
  match what the pipeline logs (`Create new tag` / `New tag is:`).
- BQ cost report switch-on: grant the portal's GSA
  `roles/bigquery.resourceViewer` + `roles/bigquery.metadataViewer` +
  `roles/bigquery.jobUser` on the team's BigQuery project (no `dataViewer`);
  set `BQ_COST_REGION: "region-europe-west3"`, `BQ_COST_PROJECT` if it is
  not the Vertex project, no preview keys needed — it is released
  (add `bq-cost` / `Monitoring` to the preview keys only to restrict it). Optional memory: the
  `bq_cost_findings` table via the terraform variable + `BQ_COST_DATASET`.
- GCP: `sql-553@…` has a user-managed key — delete it if unused.
- `backstage_poc` branch: `eod1` in `backstage/app-config.yaml` console URLs
  (internal name on a public repo).
