# release-copilot Helm chart

Deploys the **Release Copilot** (ADK + FastAPI) to **GKE** behind **Anthos Service Mesh (ASM / Istio)**.

Renders:
- **Deployment** — runs the FastAPI UI (`uvicorn release_agent.app_fastapi:app` on `:8000`), `/health` probes, Workload-Identity ServiceAccount, `GH_TOKEN` from a Secret, config via ConfigMap.
- **Service** — `ClusterIP` with a named `http` port (required by Istio).
- **VirtualService** — routes your host to the Service through an ASM ingress gateway (120s route timeout so the SSE promote/PR-tracking flow isn't cut off).
- Optional: **Gateway** (`gateway.enabled`), **DestinationRule** (`destinationRule.enabled`, session stickiness), chart-managed **Secret**, **ServiceAccount**.

## Prerequisites
- A GKE cluster with **ASM enabled** (managed or in-cluster Istio) and an ingress gateway.
- The image pushed to a registry (default `ghcr.io/phaniuk111/release-copilot`). The image
  must include `git` (the release flow clones the deploy repo — already in the Dockerfile).
- A **GCP service account** with Vertex AI access, bound to this chart's KSA via
  **Workload Identity**. For the optional release queue/analytics, the same GSA also needs
  BigQuery access (see below).
- A **GitHub token** (PAT with `repo` + `workflow`) stored in a Secret.
- Optional: the **BigQuery event table** provisioned separately — terraform module +
  JSON schema in [`bigquery/`](../../bigquery/). Empty `config.BQ_DATASET` disables the
  feature cleanly.

## Install

```bash
# 1) Namespace + ASM injection (managed ASM uses the revision label)
kubectl create namespace release
kubectl label namespace release istio.io/rev=asm-managed --overwrite
#   (in-cluster Istio: kubectl label namespace release istio-injection=enabled)

# 2) GitHub token secret (recommended over chart-managed)
kubectl -n release create secret generic release-copilot-secrets \
  --from-literal=gh-token=ghp_xxxxxxxxxxxx

# 3) Install
helm upgrade --install rc helm/release-copilot -n release \
  --set config.GOOGLE_CLOUD_PROJECT=<PROJECT_ID> \
  --set config.BUILD_REPO=<org>/<build-repo> \
  --set config.DEPLOY_REPO=<org>/<deploy-repo> \
  --set githubToken.existingSecret=release-copilot-secrets \
  --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"=release-copilot@<PROJECT_ID>.iam.gserviceaccount.com \
  --set virtualService.hosts="{release-copilot.your-domain.com}" \
  --set virtualService.gateways="{istio-ingress/asm-ingressgateway}"
```

Bind Workload Identity so the pod can call Vertex AI:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  release-copilot@<PROJECT_ID>.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:<PROJECT_ID>.svc.id.goog[release/rc-release-copilot]"
```

For the release queue / analytics (optional), grant the same GSA least-privilege
BigQuery access — or let the terraform module in [`bigquery/`](../../bigquery/) do it:
`roles/bigquery.dataEditor` on the dataset + `roles/bigquery.jobUser` on the project.

## Corporate egress proxy

GitHub traffic from GKE usually goes through a corporate proxy. This is pure
configuration — every client in the stack (requests/PyGithub, httpx/Vertex, the
`git` binary) honors the standard env vars the chart sets:

```yaml
proxy:
  httpsProxy: "http://proxy.corp.internal:3128"
  httpProxy:  "http://proxy.corp.internal:3128"
  # noProxy default keeps Google traffic direct (Workload Identity metadata +
  # *.googleapis.com); override only if Vertex/BQ must also use the proxy.
```

**Do you need `proxy.caBundle`?** Only if the proxy does TLS inspection. Decision
test — from a pod in the target cluster/namespace:

```bash
kubectl run tlscheck --rm -it --image=python:3.11-slim \
  --env=HTTPS_PROXY=http://proxy.corp.internal:3128 -- \
  python -c "import requests; print(requests.get('https://api.github.com').status_code)"
```

- **200** → the proxy tunnels TLS (or bypasses inspection for GitHub): leave
  `caBundle` unset — no volume, no cert env vars are rendered.
- **SSLError: certificate verify failed** → the proxy intercepts TLS: put the
  corporate root CA in a ConfigMap and set `proxy.caBundle.existingConfigMap`.
  The chart mounts it and points `REQUESTS_CA_BUNDLE` + `SSL_CERT_FILE` at it
  (verification stays ON — never disable TLS verification instead).

If the proxy team later enables inspection, the symptom is that exact SSLError in
pod logs and the fix is this values-only change — no image rebuild.

`config.GITHUB_BASE_URL` is unrelated to the proxy: leave it empty for github.com
(the client then uses `api.github.com`); set it only for GitHub Enterprise Server
(`https://<ghe-host>/api/v3`).

## Key values

| Key | Default | Notes |
|-----|---------|-------|
| `image.repository` / `image.tag` | `ghcr.io/phaniuk111/release-copilot` / appVersion | container image |
| `config.GOOGLE_CLOUD_PROJECT` | `""` | **required** for Vertex AI |
| `config.GEMINI_MODEL` | `gemini-2.5-flash` | Vertex model id |
| `config.BUILD_REPO` / `config.DEPLOY_REPO` | `""` | **required** — owner/repo of the build repo and the GitOps deploy repo |
| `githubToken.existingSecret` / `githubToken.value` | `""` | provide one; `existingSecret` preferred |
| `serviceAccount.annotations` | `{}` | set `iam.gke.io/gcp-service-account` for WI |
| `virtualService.hosts` / `.gateways` | example.com / `istio-ingress/asm-ingressgateway` | external host + ASM gateway |
| `virtualService.timeout` | `120s` | use `0s` to fully disable for SSE |
| `gateway.enabled` | `false` | create a Gateway instead of reusing a shared one |
| `destinationRule.enabled` | `false` | enable with `replicaCount>1` for sticky sessions |
| `config.ADK_CONTEXT_CACHE` | `true` | cache the static prompt prefix (root instruction + skills) |
| `config.ADK_EVENT_COMPACTION` | `true` | summarize old events on long chat sessions |
| `config.ADK_MEMORY_ENABLED` | `true` | preload memories + persist sessions (in-memory / per-pod) |
| `config.ADK_CONFIRM_PROD_OPS` | `true` | require confirmation before prod remove / PRD release |
| `config.PRL1_BRANCH` | `PRL1` | second terminal env branch (prl1_only charts never reach PRD) |
| `config.RELEASE_UPDATER_SCRIPT` | `scripts/release/update_release_files.py` | deploy repo's file-set generator (CARE/DF releases) |
| `config.ARTIFACTORY_BASE_URL` | `""` | prepended when devs give bare `name:version` |
| `config.DF_DEPLOY_REPO` / `.DF_DEPLOY_WORKFLOW` | `""` / `df-deploy.yml` | Dataflow workflow-dispatch deploys |
| `config.CONTROL_PREFIXES` | `RLFT,RFTL,RCTLD,xSecurity-Gatekeeper` | control step/JOB prefixes, case-insensitive; add `Xray and Prisma,CodeQL` to gate on scans |
| `config.BQ_DATASET` / `.BQ_TABLE` / `.BQ_LOCATION` | `release_agent` / `release_intents` / `US` | must match the terraform-provisioned table; empty dataset disables |
| `config.BQ_AUTO_CREATE` | `false` | keep false in cluster (table pre-provisioned) |
| `config.RELEASE_GUARD_BRANCHES` | `""` | e.g. `SIT,UAT,PRD,PRL1` — one release at a time |
| `proxy.httpsProxy` / `.httpProxy` / `.noProxy` | `""` | corporate egress proxy (see above) |
| `proxy.caBundle.existingConfigMap` | unset | ONLY for TLS-inspecting proxies (see decision test) |

## Shared domain with a path prefix (multiple apps on one host)

To serve several apps under one domain (e.g. `app.example.com`) by path, give each a
distinct `virtualService.pathPrefix` and point them all at the **same shared gateway**
(don't create a per-app `gateway`):

```bash
helm upgrade --install rc helm/release-copilot -n release \
  --set gateway.enabled=false \
  --set virtualService.hosts="{app.example.com}" \
  --set virtualService.gateways="{istio-ingress/asm-ingressgateway}" \
  --set virtualService.pathPrefix=/release-copilot \
  --set config.GOOGLE_CLOUD_PROJECT=<PROJECT_ID> \
  --set githubToken.existingSecret=release-copilot-secrets
```

App is then served at **`https://app.example.com/release-copilot/`**. The VirtualService:
1. redirects bare `/release-copilot` → `/release-copilot/` (so relative URLs resolve), then
2. strips the prefix (`rewrite.uri: /`) before forwarding to the pod.

The UI is **prefix-relative** (its API calls derive the base from the page path), so it works
under any prefix with no app config. Requirements:
- The shared gateway must serve the host (`app.example.com`, or `*`).
- Each app picks a **unique** prefix; avoid a root `/` catch-all VS on the shared host or it
  will shadow the others.

## Scaling note
The agent keeps chat state, sessions, and long-term memory in **in-memory ADK services**
(session / artifact / memory), so it's single-pod by default (`replicaCount: 1`). These are
per-pod and ephemeral — a **PVC does not persist them** (the memory service is pure RAM).
To scale out or survive restarts, use a shared/durable backend (ADK `DatabaseSessionService`
with SQLite-on-PVC for a single replica, or Postgres for many) **or** enable
`destinationRule` (source-IP stickiness) so a client's thread stays on one pod.

## Verify locally before applying
```bash
helm lint helm/release-copilot
helm template rc helm/release-copilot -n release \
  --set config.GOOGLE_CLOUD_PROJECT=p --set githubToken.existingSecret=s
```
