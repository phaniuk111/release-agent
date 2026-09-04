#!/usr/bin/env bash
# gke-demo-down.sh — tear down the demo cluster. This DESTROYS all data:
# the PVC (sqlite), queue events (BigQuery survive — they're external),
# and any in-memory agent sessions.
#
# Usage:
#   scripts/gke-demo-down.sh            # interactive (asks to confirm)
#   scripts/gke-demo-down.sh --yes      # skip confirmation
# Env: PROJECT_ID, REGION, CLUSTER_NAME, NAMESPACE (same as gke-demo-up.sh)

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-flash-keel-412418}"
REGION="${REGION:-us-central1}"
CLUSTER_NAME="${CLUSTER_NAME:-release-copilot-demo}"
NAMESPACE="${NAMESPACE:-release-copilot}"
KEEP_DISK="${KEEP_DISK:-false}"   # KEEP_DISK=true keeps the sqlite PD for next session

say()  { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }

# Graceful first: uninstall helm releases so the PVC is released and any
# finalizers run cleanly. If the cluster is already gone, skip.
if gcloud container clusters describe "$CLUSTER_NAME" --region "$REGION" \
     --project "$PROJECT_ID" >/dev/null 2>&1; then

  if gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" \
       --project "$PROJECT_ID" >/dev/null 2>&1; then
    say "helm uninstall (releases the PVC)"
    helm uninstall backstage -n "$NAMESPACE" 2>/dev/null || true
    helm uninstall release-copilot -n "$NAMESPACE" 2>/dev/null || true
    kubectl -n "$NAMESPACE" delete pvc backstage-data --ignore-not-found
  fi

  # Optional: keep the sqlite PD for the next session (survives cluster delete
  # only if we reclaim it first — default is delete with the cluster).
  if [ "$KEEP_DISK" = "true" ]; then
    say "Preserving sqlite disk"
    kubectl -n "$NAMESPACE" patch pvc backstage-data \
      -p '{"metadata":{"finalizers":null}}' --type=merge 2>/dev/null || true
  fi

  say "Deleting cluster"
  gcloud container clusters delete "$CLUSTER_NAME" \
    --region "$REGION" --project "$PROJECT_ID" --quiet
else
  say "Cluster already gone"
fi

# Reclaim any orphaned PDs (PVC-backed disks outlive the cluster if they were
# bound to a retained PVC — belt-and-braces sweep).
say "Reclaiming orphaned GCE disks (release-copilot-demo-*)"
for disk in $(gcloud compute disks list --project "$PROJECT_ID" \
    --filter="name~^${CLUSTER_NAME}" --format="value(name)" 2>/dev/null); do
  gcloud compute disks delete "$disk" --zone "$(gcloud compute disks list \
    --project "$PROJECT_ID" --filter="name=$disk" \
    --format="value(zone)")" --project "$PROJECT_ID" --quiet || true
done

say "Down. Total session cost was ~1c/hr — check:"
echo "  gcloud billing accounts list  # your 74.40 USD/mo free tier absorbs the cluster fee"
echo
echo "Next session: scripts/gke-demo-up.sh  (~10 min to a working demo)"