-- Release event log for the Dev Portal (intake queue + release/deploy history).
-- APPEND-ONLY by design: every action (queue / withdraw / released / deployed /
-- removed) is an INSERT; queue state and per-environment deployed state are
-- DERIVED by the app (latest event wins). Never UPDATE or DELETE from the app.
--
-- Provision this separately (DBA / terraform / bq CLI) and pass the names to
-- the app via Helm values: config.BQ_DATASET / config.BQ_TABLE / config.BQ_LOCATION.
-- Replace PROJECT / DATASET / TABLE below (defaults: release_agent.release_intents).
--
-- Runtime service-account permissions (read/write rows, never schema):
--   roles/bigquery.dataEditor  on the DATASET (insert + select)
--   roles/bigquery.jobUser     on the PROJECT (run queries)

CREATE TABLE IF NOT EXISTS `PROJECT.DATASET.TABLE`
(
  event_id        STRING    OPTIONS (description = 'UUID per event; used as insertId for best-effort dedup on retries'),
  event_type      STRING    OPTIONS (description = 'queued | withdrawn | released | deployed | removed'),
  event_ts        TIMESTAMP OPTIONS (description = 'UTC time the event was written'),
  requested_by    STRING    OPTIONS (description = 'requester email (queued/withdrawn events)'),
  artifact_name   STRING    OPTIONS (description = 'chart / image name, e.g. acme-capability-svc'),
  artifact_version STRING   OPTIONS (description = 'version / tag'),
  prl1_only       BOOL      OPTIONS (description = 'routing flag: ship to UAT+PRL1 only, never PRD (queued events)'),
  df_only         BOOL      OPTIONS (description = 'routing flag: Dataflow image, excluded from helm deploys (queued events)'),
  note            STRING    OPTIONS (description = 'free-text note to DevOps (queued) or action tag (release_promoted / removed_from_live / df_workflow_dispatched)'),
  deployment_repo STRING    OPTIONS (description = 'target GitHub repo, owner/repo'),
  release_name    STRING    OPTIONS (description = 'release the artifact shipped in (released events)'),
  pr_number       INT64     OPTIONS (description = 'release / promotion / deploy PR number'),
  build_verified  BOOL      OPTIONS (description = 'courtesy build check at queue time; NULL = not checked'),
  environment     STRING    OPTIONS (description = 'deployed/removed events: uat | prd | prl1 | dataflow-uat'),
  jira_ticket     STRING    OPTIONS (description = 'e.g. REL-1234; surfaces in the CHG draft on release day'),
  change_details  STRING    OPTIONS (description = 'dev what-changed-and-why; aggregated into change_description')
)
PARTITION BY DATE(event_ts)
OPTIONS (
  description = 'Dev Portal release event log — append-only; queue and deployed state derived by the app'
);
