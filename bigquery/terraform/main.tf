# Dev Portal release event log — dataset + table + least-privilege runtime IAM.
#
# The table schema's single source of truth is ../release_intents.schema.json
# (also usable directly with `bq mk`); this module consumes it via file(), so
# schema changes are made ONCE in the JSON. Append-only table: the app only
# ever INSERTs; queue state and per-environment deployed state are derived.
#
# Usage:
#   module "release_events" {
#     source                  = "./bigquery/terraform"
#     project_id              = "my-project"
#     runtime_service_account = "release-copilot@my-project.iam.gserviceaccount.com"
#   }
# Then set the matching Helm values: config.BQ_DATASET / BQ_TABLE / BQ_LOCATION.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

variable "project_id" {
  description = "GCP project hosting the dataset (same project the app runs against)."
  type        = string
}

variable "dataset_id" {
  description = "BigQuery dataset id — must match Helm value config.BQ_DATASET."
  type        = string
  default     = "release_agent"
}

variable "table_id" {
  description = "Table id — must match Helm value config.BQ_TABLE."
  type        = string
  default     = "release_intents"
}

variable "location" {
  description = "Dataset location — must match Helm value config.BQ_LOCATION."
  type        = string
  default     = "US"
}

variable "runtime_service_account" {
  description = "Email of the app's runtime GSA (Workload Identity). Empty = skip IAM bindings."
  type        = string
  default     = ""
}

variable "partition_expiration_days" {
  description = "Optional retention: expire partitions after N days (requested_by holds emails — set per your data policy). 0 = keep forever."
  type        = number
  default     = 0
}

resource "google_bigquery_dataset" "release_agent" {
  project     = var.project_id
  dataset_id  = var.dataset_id
  location    = var.location
  description = "Dev Portal release event log — intake queue + release/deploy history (append-only)"
}

resource "google_bigquery_table" "release_intents" {
  project     = var.project_id
  dataset_id  = google_bigquery_dataset.release_agent.dataset_id
  table_id    = var.table_id
  description = "Append-only release events (queued | withdrawn | released | deployed | removed); state is derived by the app — never UPDATE/DELETE"

  # Guard the audit log against accidental `terraform destroy`.
  deletion_protection = true

  time_partitioning {
    type          = "DAY"
    field         = "event_ts"
    expiration_ms = var.partition_expiration_days > 0 ? var.partition_expiration_days * 24 * 60 * 60 * 1000 : null
  }

  schema = file("${path.module}/../release_intents.schema.json")
}

# Least privilege for the runtime SA: insert + select on THIS dataset only,
# plus the project-level right to run query jobs. No schema permissions —
# column additions are applied here (terraform), not by the app.
resource "google_bigquery_dataset_iam_member" "runtime_data_editor" {
  count      = var.runtime_service_account == "" ? 0 : 1
  project    = var.project_id
  dataset_id = google_bigquery_dataset.release_agent.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${var.runtime_service_account}"
}

resource "google_project_iam_member" "runtime_job_user" {
  count   = var.runtime_service_account == "" ? 0 : 1
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${var.runtime_service_account}"
}

output "table_id" {
  value       = "${var.project_id}.${var.dataset_id}.${var.table_id}"
  description = "Fully-qualified table id the app will read/write."
}
