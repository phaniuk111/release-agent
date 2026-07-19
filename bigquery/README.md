# BigQuery provisioning — release event log

The Dev Portal's intake queue and release/deploy analytics use ONE append-only
BigQuery table. It is provisioned **separately** from the app (terraform / CI —
never by the runtime), and the app receives the names via Helm values — the
runtime service account needs no schema permissions.

The schema's single source of truth is
[release_intents.schema.json](release_intents.schema.json); the terraform
module consumes it via `file()`, and it works directly with `bq mk` too.

## Option A — Terraform (recommended)

```hcl
module "release_events" {
  source                  = "./bigquery/terraform"
  project_id              = "my-project"
  runtime_service_account = "release-copilot@my-project.iam.gserviceaccount.com"
  # dataset_id = "release_agent"   table_id = "release_intents"   location = "US"
  # partition_expiration_days = 0  # retention for requested_by emails, per data policy
}
```

Creates the dataset, the day-partitioned table (partitioned on `event_ts`,
`deletion_protection = true`), and least-privilege IAM for the runtime SA
(`roles/bigquery.dataEditor` on the dataset + `roles/bigquery.jobUser` on the
project).

## Option B — bq CLI with the JSON schema

```bash
bq mk --dataset --location=US PROJECT:release_agent
bq mk --table \
  --time_partitioning_field event_ts --time_partitioning_type DAY \
  --description "Dev Portal release event log (append-only)" \
  PROJECT:release_agent.release_intents \
  bigquery/release_intents.schema.json
```

## Wire the app (Helm values)

```yaml
config:
  BQ_DATASET: "release_agent"
  BQ_TABLE: "release_intents"
  BQ_LOCATION: "US"
  BQ_AUTO_CREATE: "false"   # cluster: table pre-provisioned; true only for local dev
```

Empty `BQ_DATASET` disables the whole feature (queue endpoints report disabled;
releases/deploys are unaffected — capture is best-effort by design).

## Rules of the road

- **Append-only.** The app only ever INSERTs; queue state and per-environment
  deployed state are derived (latest event per key wins). Never UPDATE/DELETE
  from the app — the event log is also the audit history.
- **Schema changes** are additive nullable columns: edit
  `release_intents.schema.json`, `terraform apply` (BigQuery accepts additive
  schema updates in place), and mirror the column in `_SCHEMA` in
  `src/release_agent/tools/release_queue.py`.
- Retention/PII: `requested_by` holds emails — set
  `partition_expiration_days` per your data policy if required.
