#!/usr/bin/env bash
# gke-demo-down.sh — tear down the demo cluster. This DESTROYS all data:
# the PVC (sqlite) and any in-memory agent sessions. Queue events in BigQuery
# survive — they are external to the cluster by design.
#
# It reverses everything gke-demo-up.sh creates: helm releases, cluster, fleet
# membership, Artifact Registry images, the project IAM grants made to the
# workload's KSA, and the PVC's backing disk.
#
# Usage:
#   scripts/gke-demo-down.sh            # tear it all down
#   KEEP_IMAGES=true scripts/gke-demo-down.sh   # keep the AR repo for next session
#   KEEP_DISK=true   scripts/gke-demo-down.sh   # keep the sqlite PD
# Env: PROJECT_ID, REGION, CLUSTER_NAME, NAMESPACE, AR_REPO (same as up).

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-flash-keel-412418}"
REGION="${REGION:-us-central1}"
CLUSTER_NAME="${CLUSTER_NAME:-release-copilot-demo}"
NAMESPACE="${NAMESPACE:-release-copilot}"
AR_REPO="${AR_REPO:-poc}"
KEEP_DISK="${KEEP_DISK:-false}"
KEEP_IMAGES="${KEEP_IMAGES:-false}"

say() { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

# Disks backing a PVC are named `pvc-<uuid>` by the PD CSI driver — they do NOT
# carry the cluster name, which is why a name-prefix sweep never matched them and
# left orphans behind. Record them BEFORE the cluster goes away, while the
# `kubernetes.io/created-for/pvc/name` description still ties them to this demo.
DEMO_DISKS=""

# Graceful first: uninstall helm releases so the PVC is released and any
# finalizers run cleanly. If the cluster is already gone, skip.
if gcloud container clusters describe "$CLUSTER_NAME" --region "$REGION" \
  --project "$PROJECT_ID" >/dev/null 2>&1; then

  if gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" \
    --project "$PROJECT_ID" >/dev/null 2>&1; then

    if [ "$KEEP_DISK" != "true" ]; then
      DEMO_DISKS="$(kubectl -n "$NAMESPACE" get pv \
        -o jsonpath='{range .items[?(@.spec.claimRef.namespace=="'"$NAMESPACE"'")]}{.spec.csi.volumeHandle}{"\n"}{end}' \
        2>/dev/null | awk -F/ 'NF{print $NF"|"$(NF-2)}' || true)"
    fi

    say "helm uninstall (releases the PVC)"
    helm uninstall backstage -n "$NAMESPACE" 2>/dev/null || true
    helm uninstall release-copilot -n "$NAMESPACE" 2>/dev/null || true
    kubectl -n "$NAMESPACE" delete pvc --all --ignore-not-found 2>/dev/null || true
  fi

  say "Deleting cluster"
  gcloud container clusters delete "$CLUSTER_NAME" \
    --region "$REGION" --project "$PROJECT_ID" --quiet
else
  say "Cluster already gone"
fi

# Fleet membership outlives the cluster when the cluster was registered for mesh.
say "Unregistering fleet membership"
gcloud container fleet memberships delete "$CLUSTER_NAME" \
  --location "$REGION" --project "$PROJECT_ID" --quiet 2>/dev/null || true

# The KSA principal disappears with the namespace, but its project-level IAM
# bindings do NOT — they linger as dangling members on the policy.
say "Revoking Workload Identity IAM grants"
KSA_PRINCIPAL="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT_ID}.svc.id.goog/subject/ns/${NAMESPACE}/sa/release-copilot"
for role in roles/aiplatform.user roles/bigquery.jobUser roles/bigquery.dataEditor; do
  gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
    --member="$KSA_PRINCIPAL" --role="$role" --condition=None >/dev/null 2>&1 || true
done

# CI identity created by scripts/gke-poc-ci-setup.sh. Removed here so the PoC
# leaves no standing credential path from GitHub into the project.
say "Removing PoC CI identity (WIF provider + service account)"
CI_SA="release-copilot-poc-ci@${PROJECT_ID}.iam.gserviceaccount.com"
# artifactregistry.writer was granted on the repo, not the project, so it dies
# with the repo below; only the project-wide role needs an explicit revoke.
gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CI_SA}" --role=roles/container.developer --condition=None >/dev/null 2>&1 || true
gcloud iam workload-identity-pools providers delete poc-provider \
  --project="$PROJECT_ID" --location=global \
  --workload-identity-pool=github-actions-pool --quiet 2>/dev/null || true
gcloud iam service-accounts delete "$CI_SA" --project="$PROJECT_ID" --quiet 2>/dev/null || true

if [ "$KEEP_IMAGES" != "true" ]; then
  say "Deleting Artifact Registry repo ($AR_REPO)"
  gcloud artifacts repositories delete "$AR_REPO" --location="$REGION" \
    --project="$PROJECT_ID" --quiet 2>/dev/null || true
fi

# Belt-and-braces: any UNATTACHED pvc-* disk this demo created. Filtering on
# `users` being empty is what makes this safe to run — a disk still attached to
# something else is never touched.
say "Reclaiming orphaned PVC disks"
for entry in $DEMO_DISKS; do
  disk="${entry%%|*}"
  zone="${entry##*|}"
  [ -n "$disk" ] || continue
  gcloud compute disks delete "$disk" --zone "$zone" \
    --project "$PROJECT_ID" --quiet 2>/dev/null || true
done
gcloud compute disks list --project "$PROJECT_ID" \
  --filter="name~^pvc- AND -users:*" \
  --format="value(name,zone.basename())" 2>/dev/null |
  while read -r disk zone; do
    [ -n "$disk" ] || continue
    echo "  orphaned, unattached: $disk ($zone)"
    gcloud compute disks delete "$disk" --zone "$zone" \
      --project "$PROJECT_ID" --quiet || true
  done

say "Down. Remaining PoC footprint:"
gcloud container clusters list --project "$PROJECT_ID" --format='value(name)' | sed 's/^/  cluster: /'
gcloud artifacts repositories list --project "$PROJECT_ID" --format='value(name)' | sed 's/^/  ar-repo: /'
gcloud compute disks list --project "$PROJECT_ID" --format='value(name)' | sed 's/^/  disk:    /'
echo
echo "Next session: scripts/gke-demo-up.sh  (~15 min to a working demo)"
