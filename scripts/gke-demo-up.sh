#!/usr/bin/env bash
# gke-demo-up.sh — spin up the Release Copilot PoC on GKE, cheap:
#   Autopilot cluster + managed Cloud Service Mesh (ASM) + Spot Pods for both
#   services + a PVC-backed SQLite for the portal.
#
# Lifecycle: run once per demo session; pair with scripts/gke-demo-down.sh.
# Cost (~1c/hr while running, see docs/POC_GKE_DEMO.md): cluster fee is
# covered by the GKE free tier (one zonal/Autopilot cluster per billing
# account).
#
# Prereqs: gcloud authenticated (gcloud auth login), helm, kubectl, docker.
#
# Usage:
#   scripts/gke-demo-up.sh
# Env overrides: CLUSTER_NAME, REGION, NAMESPACE, PROJECT_ID, DNS_HOST.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROJECT_ID="${PROJECT_ID:-flash-keel-412418}"
REGION="${REGION:-us-central1}"
CLUSTER_NAME="${CLUSTER_NAME:-release-copilot-demo}"
NAMESPACE="${NAMESPACE:-release-copilot}"
DNS_HOST="${DNS_HOST:-}"    # optional: set for external hostname, else port-forward

# Image repos — point at YOUR registry (ghcr by default to match CI).
AGENT_IMAGE="${AGENT_IMAGE:-ghcr.io/${GITHUB_REPO_OWNER:-phaniuk111}/release-copilot}"
PORTAL_IMAGE="${PORTAL_IMAGE:-ghcr.io/${GITHUB_REPO_OWNER:-phaniuk111}/backstage-release-copilot}"
TAG="${TAG:-poc}"

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight --
say "Preflight"
command -v gcloud  >/dev/null || fail "gcloud not found"
command -v helm    >/dev/null || fail "helm not found"
command -v kubectl >/dev/null || fail "kubectl not found"
command -v docker  >/dev/null || fail "docker not found"
gcloud config list project --format='value(core.project)' >/dev/null 2>&1 \
  || fail "gcloud not authenticated (gcloud auth login)"

PROJECT_ID="$(gcloud config get-value project 2>/dev/null || echo "$PROJECT_ID")"
[ -n "$PROJECT_ID" ] || fail "no GCP project set (PROJECT_ID env or gcloud config)"
echo "  project  : $PROJECT_ID"
echo "  cluster  : $CLUSTER_NAME  ($REGION)"
echo "  namespace: $NAMESPACE"
echo "  images   : $AGENT_IMAGE:$TAG · $PORTAL_IMAGE:$TAG"

# ---------------------------------------------------- 1. cluster (Autopilot) --
say "Cluster (Autopilot — Spot Pods enabled by the workloads, not the node pools)"
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

# -------------------------------------------------- 2. Cloud Service Mesh ----
# Managed ASM via the fleet API (Istio without any cluster-side install work).
# Fleet registration + automatic management = the whole ASM story for this PoC.
say "Managed Cloud Service Mesh (fleet API)"
gcloud container fleet mesh enable --project "$PROJECT_ID" >/dev/null
gcloud container clusters update "$CLUSTER_NAME" \
  --region "$REGION" --project "$PROJECT_ID" \
  --fleet-project "$PROJECT_ID" >/dev/null
kubectl label namespace "$NAMESPACE" istio-injection=enabled --overwrite 2>/dev/null \
  || true   # namespace may not exist yet

# ------------------------------------------------------------- 3. images ----
say "Images (build once, reuse per session via ghcr cache)"
for svc in agent portal; do
  case "$svc" in
    agent)
      ctx=f; df=Dockerfile.agent; img="$AGENT_IMAGE:$TAG" ;;
    portal)
      ctx=backstage; df=backstage/Dockerfile; img="$PORTAL_IMAGE:$TAG" ;;
  esac
  if docker image inspect "$img" >/dev/null 2>&1; then
    echo "  using cached $img"
  else
    docker build -f "$df" -t "$img" "$ctx"
  fi
  docker push "$img"
done

# ---------------------------------------------------------- 4. namespace ----
say "Namespace"
kubectl create ns "$NAMESPACE" 2>/dev/null || true
kubectl label namespace "$NAMESPACE" istio-injection=enabled --overwrite

# Secrets — GitHub token for the agent + (optionally) the portal.
# Source: GH_TOKEN env or gh CLI. Never hardcode in files.
GH_TOKEN="${GH_TOKEN:-$(gh auth token 2>/dev/null || true)}"
[ -n "$GH_TOKEN" ] || fail "no GH_TOKEN (env) and gh CLI not authenticated"
kubectl -n "$NAMESPACE" create secret generic release-copilot-secrets \
  --from-literal=GH_TOKEN="$GH_TOKEN" 2>/dev/null || true

# ------------------------------------------------------------ 5. deploy ----
# Spot pods on both services (pod-level: cloud.google.com/gke-spot selector).
cat > /tmp/poc-spot.yaml <<'YAML'
nodeSelector:
  cloud.google.com/gke-spot: "true"
YAML

say "helm install release-copilot (agent)"
helm upgrade --install release-copilot ./helm/release-copilot \
  --namespace "$NAMESPACE" \
  --set image.repository="$AGENT_IMAGE" --set image.tag="$TAG" \
  -f /tmp/poc-spot.yaml

say "helm install backstage (portal)"
helm upgrade --install backstage ./helm/backstage-portal \
  --namespace "$NAMESPACE" \
  --set image.repository="$PORTAL_IMAGE" --set image.tag="$TAG" \
  --set releaseCopilot.url="http://release-copilot.${NAMESPACE}.svc.cluster.local:8000" \
  --set persistence.enabled=true \
  -f /tmp/poc-spot.yaml \
  --set config.app.baseUrl="http://localhost:7007" \
  --set config.backend.baseUrl="http://localhost:7007"

# -------------------------------------------------------------- 6. done ----
say "Done"
cat <<EOF

Next:
  kubectl -n $NAMESPACE rollout status deploy/release-copilot deploy/backstage --timeout=300s
  kubectl -n $NAMESPACE port-forward svc/backstage 7007:7007
  # → http://localhost:7007  (guest sign-in; Release Copilot page at .../release-copilot)

When done:  scripts/gke-demo-down.sh
Cost note:  spot pods ≈1c/hr while running; cluster fee covered by free tier.
EOF
