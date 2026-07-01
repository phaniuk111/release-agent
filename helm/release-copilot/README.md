# release-copilot Helm chart

Deploys the **Release Copilot** (ADK + FastAPI) to **GKE** behind **Anthos Service Mesh (ASM / Istio)**.

Renders:
- **Deployment** — runs the FastAPI UI (`uvicorn release_agent.app_fastapi:app` on `:8000`), `/health` probes, ASM sidecar injection, Workload-Identity ServiceAccount, `GH_TOKEN` from a Secret, config via ConfigMap.
- **Service** — `ClusterIP` with a named `http` port (required by Istio).
- **VirtualService** — routes your host to the Service through an ASM ingress gateway (120s route timeout so the SSE promote/PR-tracking flow isn't cut off).
- Optional: **Gateway** (`gateway.enabled`), **DestinationRule** (`destinationRule.enabled`, session stickiness), chart-managed **Secret**, **ServiceAccount**.

## Prerequisites
- A GKE cluster with **ASM enabled** (managed or in-cluster Istio) and an ingress gateway.
- The image pushed to a registry (default `ghcr.io/phaniuk111/release-copilot`).
- A **GCP service account** with Vertex AI access, bound to this chart's KSA via **Workload Identity**.
- A **GitHub token** (PAT with `repo` + `workflow`) stored in a Secret.

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

## Key values

| Key | Default | Notes |
|-----|---------|-------|
| `image.repository` / `image.tag` | `ghcr.io/phaniuk111/release-copilot` / appVersion | container image |
| `config.GOOGLE_CLOUD_PROJECT` | `""` | **required** for Vertex AI |
| `config.GEMINI_MODEL` | `gemini-2.5-flash` | Vertex model id |
| `config.BUILD_REPO` / `config.DEPLOY_REPO` | `phaniuk111/devops` / `phaniuk111/deployment-repo` | the two repos the agent operates on (code+build / GitOps) |
| `githubToken.existingSecret` / `githubToken.value` | `""` | provide one; `existingSecret` preferred |
| `serviceAccount.annotations` | `{}` | set `iam.gke.io/gcp-service-account` for WI |
| `istioInjection` | `true` | adds `sidecar.istio.io/inject: "true"` |
| `virtualService.hosts` / `.gateways` | example.com / `istio-ingress/asm-ingressgateway` | external host + ASM gateway |
| `virtualService.timeout` | `120s` | use `0s` to fully disable for SSE |
| `gateway.enabled` | `false` | create a Gateway instead of reusing a shared one |
| `destinationRule.enabled` | `false` | enable with `replicaCount>1` for sticky sessions |

## Shared domain with a path prefix (multiple apps on one host)

To serve several apps under one domain (e.g. `app-eod-uat.com`) by path, give each a
distinct `virtualService.pathPrefix` and point them all at the **same shared gateway**
(don't create a per-app `gateway`):

```bash
helm upgrade --install rc helm/release-copilot -n release \
  --set gateway.enabled=false \
  --set virtualService.hosts="{app-eod-uat.com}" \
  --set virtualService.gateways="{istio-ingress/asm-ingressgateway}" \
  --set virtualService.pathPrefix=/release-copilot \
  --set config.GOOGLE_CLOUD_PROJECT=<PROJECT_ID> \
  --set githubToken.existingSecret=release-copilot-secrets
```

App is then served at **`https://app-eod-uat.com/release-copilot/`**. The VirtualService:
1. redirects bare `/release-copilot` → `/release-copilot/` (so relative URLs resolve), then
2. strips the prefix (`rewrite.uri: /`) before forwarding to the pod.

The UI is **prefix-relative** (its API calls derive the base from the page path), so it works
under any prefix with no app config. Requirements:
- The shared gateway must serve the host (`app-eod-uat.com`, or `*`).
- Each app picks a **unique** prefix; avoid a root `/` catch-all VS on the shared host or it
  will shadow the others.

## Scaling note
The agent keeps chat state in **in-memory ADK session/artifact services**, so it's single-pod
by default (`replicaCount: 1`). To scale out, add shared ADK storage **or** enable
`destinationRule` (source-IP stickiness) so a client's thread stays on one pod.

## Verify locally before applying
```bash
helm lint helm/release-copilot
helm template rc helm/release-copilot -n release \
  --set config.GOOGLE_CLOUD_PROJECT=p --set githubToken.existingSecret=s
```
