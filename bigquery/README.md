# BigQuery provisioning — release event log

The Dev Portal's intake queue and release/deploy analytics use ONE append-only
BigQuery table. It is provisioned **separately** from the app (DBA / terraform /
CI), and the app receives the names via Helm values — the runtime service
account needs no schema permissions.

## Create the table

Option A — DDL (bq console or CLI). Edit `PROJECT.DATASET.TABLE` in
[release_intents.sql](release_intents.sql), then:

```bash
bq query --use_legacy_sql=false < bigquery/release_intents.sql
```

Option B — bq CLI with the JSON schema:

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

## Runtime service-account IAM (least privilege)

```bash
# insert + select on the dataset only
bq add-iam-policy-binding --member=serviceAccount:SA_EMAIL \
  --role=roles/bigquery.dataEditor PROJECT:release_agent
# run query jobs
gcloud projects add-iam-policy-binding PROJECT \
  --member=serviceAccount:SA_EMAIL --role=roles/bigquery.jobUser
```

## Rules of the road

- **Append-only.** The app only ever INSERTs; queue state and per-environment
  deployed state are derived (latest event per key wins). Never UPDATE/DELETE
  from the app — the event log is also the audit history.
- **Schema changes** are additive nullable columns, applied by the DDL owner
  (`ALTER TABLE ... ADD COLUMN`). Keep `release_intents.sql`,
  `release_intents.schema.json` and `_SCHEMA` in
  `src/release_agent/tools/release_queue.py` in sync.
- Retention/PII: `requested_by` holds emails — apply your data policy via a
  partition expiration or dataset default if required.
