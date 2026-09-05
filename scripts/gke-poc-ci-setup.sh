#!/usr/bin/env bash
# gke-poc-ci-setup.sh — one-time, keyless auth so .github/workflows/poc-gke-deploy.yml
# can push images to Artifact Registry and helm-deploy to the demo cluster.
#
# Keyless on purpose: GitHub mints a short-lived OIDC token, Workload Identity
# Federation exchanges it for a Google credential. No service-account JSON key is
# ever created, downloaded, or stored as a repo secret.
#
# Everything here is ADDITIVE — a NEW provider in the existing pool and a NEW
# service account — so the pool's existing providers (e.g. the one scoped to
# phaniuk111/liquidbase-bq) keep working untouched.
#
# Undo: scripts/gke-demo-down.sh removes both.
#
# Usage:  scripts/gke-poc-ci-setup.sh

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-flash-keel-412418}"
GITHUB_REPO="${GITHUB_REPO:-phaniuk111/release-agent}"
POOL="${POOL:-github-actions-pool}"
PROVIDER="${PROVIDER:-poc-provider}"
SA_NAME="${SA_NAME:-release-copilot-poc-ci}"
# The credential can be minted ONLY from this branch (see attribute-condition).
GITHUB_BRANCH="${GITHUB_BRANCH:-backstage_poc}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-poc}"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

say "CI service account"
gcloud iam service-accounts describe "$SA" --project="$PROJECT_ID" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT_ID" \
    --display-name="Release Copilot PoC CI (delete after PoC)"

# Least privilege for what the workflow actually does: push images, and talk to
# the cluster's API. Deliberately NOT container.admin — CI never creates or
# deletes clusters; scripts/gke-demo-up.sh does, from a human's credentials.
say "Roles"

# artifactregistry.writer is scoped to the ONE repo, not the whole project, so a
# stolen credential cannot write to any other registry here. The repo is created
# here (idempotently) rather than in gke-demo-up.sh purely so this grant has
# something to attach to.
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" \
  --project="$PROJECT_ID" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker --location="$REGION" --project="$PROJECT_ID" \
    --description="Release Copilot GKE PoC — delete after session"
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
  --location="$REGION" --project="$PROJECT_ID" \
  --member="serviceAccount:${SA}" --role=roles/artifactregistry.writer >/dev/null
echo "  granted artifactregistry.writer on repo ${AR_REPO} only"

# container.developer has no cluster-scoped equivalent, so this one is
# project-wide. It is the PoC's widest grant; gke-demo-down.sh revokes it.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" --role=roles/container.developer --condition=None >/dev/null
echo "  granted container.developer (project-wide — no cluster-scoped role exists)"

say "OIDC provider (scoped to ${GITHUB_REPO})"
if gcloud iam workload-identity-pools providers describe "$PROVIDER" \
  --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" \
  >/dev/null 2>&1; then
  echo "  provider $PROVIDER already exists"
else
  # attribute-condition is THE security boundary. Without it, any GitHub repo in
  # the world could mint a token this provider accepts. Two clauses, because the
  # repo is PUBLIC:
  #   repository — only this repo, so no other project's workflow qualifies;
  #   ref        — only this branch, so a workflow added on any other branch
  #                (or a tag) cannot reach the project even from inside the repo.
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" \
    --display-name="Release Copilot PoC" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository=='${GITHUB_REPO}' && assertion.ref=='refs/heads/${GITHUB_BRANCH}'" \
    --issuer-uri="https://token.actions.githubusercontent.com"
fi

say "Let ${GITHUB_REPO} impersonate ${SA}"
gcloud iam service-accounts add-iam-policy-binding "$SA" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

cat <<EOF

Done. The workflow's env block should read:

  WIF_PROVIDER: projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}
  CI_SA: ${SA}

Then run it (the --ref MUST be ${GITHUB_BRANCH} — the provider rejects any other):
  gh workflow run poc-gke-deploy.yml --ref ${GITHUB_BRANCH}
EOF
