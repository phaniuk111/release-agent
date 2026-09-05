#!/usr/bin/env bash
# gke-demo-up.sh — spin up the Release Copilot PoC on GKE, cheap:
#   Autopilot cluster + managed Cloud Service Mesh + Spot Pods for both
#   services + a PVC-backed SQLite for the portal.
#
# Lifecycle: run once per demo session; pair with scripts/gke-demo-down.sh.
# Cost while running is ~1c/hr (see docs/POC_GKE_DEMO.md). The cluster fee is
# the only line that accrues when nothing is being demoed — hence the pairing.
#
# SCOPE: this script owns INFRASTRUCTURE only — cluster, mesh, namespace,
# the agent's GitHub Secret and its Workload Identity grants. Images and helm
# releases are owned by .github/workflows/poc-gke-deploy.yml, which builds on
# linux/amd64 runners and deploys with keyless WIF auth. Run this first, then
# the workflow.
#
# Prereqs: gcloud authenticated (gcloud auth login), kubectl, gh.
# NOTE: docker is NOT required anywhere in this lane.
#
# Usage:
#   scripts/gke-demo-up.sh
#   gh workflow run poc-gke-deploy.yml --ref backstage_poc
# Env overrides: PROJECT_ID, REGION, CLUSTER_NAME, NAMESPACE, AR_REPO,
#                MESH=false (skip Cloud Service Mesh).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROJECT_ID="${PROJECT_ID:-flash-keel-412418}"
REGION="${REGION:-us-central1}"
CLUSTER_NAME="${CLUSTER_NAME:-release-copilot-demo}"
NAMESPACE="${NAMESPACE:-release-copilot}"
AR_REPO="${AR_REPO:-poc}"
MESH="${MESH:-true}"

# Images live in Artifact Registry in THIS project, not ghcr:
#   - the build must be linux/amd64 (Autopilot nodes) and dev laptops are arm64,
#     so a local `docker build` produces an image the cluster cannot run;
#   - pushing to ghcr needs a token with write:packages, which the release flow's
#     PAT deliberately does not carry.
# The CI runner is amd64 and reaches AR with the same keyless WIF credential
# it uses for the cluster, so one auth path covers both.
REGISTRY="${REGISTRY:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}}"
AGENT_IMAGE="${AGENT_IMAGE:-${REGISTRY}/release-copilot}"
PORTAL_IMAGE="${PORTAL_IMAGE:-${REGISTRY}/backstage-release-copilot}"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
fail() {
  printf '\n\033[1;31m✖ %s\033[0m\n' "$*" >&2
  exit 1
}

# ---------------------------------------------------------------- preflight --
say "Preflight"
command -v gcloud >/dev/null || fail "gcloud not found"
command -v kubectl >/dev/null || fail "kubectl not found"
gcloud config list project --format='value(core.project)' >/dev/null 2>&1 ||
  fail "gcloud not authenticated (gcloud auth login)"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
[ -n "$PROJECT_NUMBER" ] || fail "cannot resolve project number for $PROJECT_ID"
echo "  project  : $PROJECT_ID ($PROJECT_NUMBER)"
echo "  cluster  : $CLUSTER_NAME  ($REGION)"
echo "  namespace: $NAMESPACE"
echo "  images   : $AGENT_IMAGE · $PORTAL_IMAGE (built by CI)"

# ------------------------------------------------------------- 1. registry --
say "Artifact Registry"
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" \
  --project="$PROJECT_ID" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker --location="$REGION" --project="$PROJECT_ID" \
    --description="Release Copilot GKE PoC — delete after session"

# --------------------------------------------------- 2. cluster (Autopilot) --
# Autopilot bills pods, not nodes, so a cluster with nothing scheduled on it
# costs only the $0.10/hr management fee.
say "Cluster (Autopilot — Spot Pods come from the workloads, not the node pools)"
if gcloud container clusters describe "$CLUSTER_NAME" --region "$REGION" \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "  reusing existing cluster $CLUSTER_NAME"
else
  gcloud container clusters create-auto "$CLUSTER_NAME" \
    --project="$PROJECT_ID" --region="$REGION" \
    --release-channel=regular --quiet
fi
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" \
  --project "$PROJECT_ID"

# -------------------------------------------------- 3. Cloud Service Mesh ----
# Managed CSM via the fleet API (Istio with no cluster-side install work).
# Autopilot supports the MANAGED control plane only, and managed CSM injects on
# the `istio.io/rev=asm-managed` label — NOT `istio-injection=enabled`, which is
# the in-cluster-Istio label and silently injects nothing here.
kubectl create ns "$NAMESPACE" 2>/dev/null || true
MESH_READY=false
if [ "$MESH" = "true" ]; then
  say "Managed Cloud Service Mesh (fleet API)"
  gcloud services enable mesh.googleapis.com --project "$PROJECT_ID"
  gcloud container fleet mesh enable --project "$PROJECT_ID"
  gcloud container clusters update "$CLUSTER_NAME" \
    --region "$REGION" --project "$PROJECT_ID" --fleet-project "$PROJECT_ID"
  gcloud container fleet mesh update --management automatic \
    --memberships "$CLUSTER_NAME" --location "$REGION" --project "$PROJECT_ID"

  # Bounded wait: the managed control plane takes a few minutes to provision.
  # The PoC's portal→agent hop is a plain ClusterIP call, so a mesh that does
  # not converge degrades the demo (no mTLS/telemetry) instead of blocking it.
  for _ in $(seq 1 30); do
    if kubectl get controlplanerevision -n istio-system 2>/dev/null |
      grep -q asm-managed; then
      MESH_READY=true
      break
    fi
    sleep 20
  done
  if [ "$MESH_READY" = "true" ]; then
    kubectl label namespace "$NAMESPACE" istio.io/rev=asm-managed --overwrite
    echo "  mesh ready — sidecars will be injected (pods become 2/2)"
  else
    echo "  ⚠ mesh did not converge in 10min; continuing WITHOUT sidecars"
  fi
fi

# ---------------------------------------------------- 4. secret + identity ----
say "Secret + Workload Identity"
# GitHub token: env or gh CLI. Key name must match githubToken.existingSecretKey.
GH_TOKEN="${GH_TOKEN:-$(gh auth token 2>/dev/null || true)}"
[ -n "$GH_TOKEN" ] || fail "no GH_TOKEN (env) and gh CLI not authenticated"
kubectl -n "$NAMESPACE" create secret generic release-copilot-secrets \
  --from-literal=gh-token="$GH_TOKEN" --dry-run=client -o yaml | kubectl apply -f -

# Autopilot has Workload Identity on by default: grant the roles straight to the
# KSA principal, so there is no GCP service account to create or clean up.
KSA_PRINCIPAL="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT_ID}.svc.id.goog/subject/ns/${NAMESPACE}/sa/release-copilot"
for role in roles/aiplatform.user roles/bigquery.jobUser roles/bigquery.dataEditor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="$KSA_PRINCIPAL" --role="$role" --condition=None >/dev/null
done
echo "  granted aiplatform.user + bigquery.{jobUser,dataEditor} to the KSA"

# -------------------------------------------------------------- 5. done ----
say "Infrastructure ready — CI builds and deploys from here"
cat <<EOF

  gh workflow run poc-gke-deploy.yml --ref backstage_poc
  gh run watch

Then:
  kubectl -n $NAMESPACE port-forward svc/backstage 7007:7007
  # → http://localhost:7007  (guest sign-in; Release Copilot page at /release-copilot)

When done:  scripts/gke-demo-down.sh
Cost note:  spot pods ≈3c/hr while running; the cluster fee is the only line
            that accrues when nothing is being demoed — hence tearing down.
EOF
