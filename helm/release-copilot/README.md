# release-copilot Helm chart

Deploys the **Release Copilot** (ADK + FastAPI) to **GKE** behind **Anthos Service Mesh (ASM / Istio)**.

Renders:
- **Deployment** — runs the FastAPI UI (`uvicorn release_agent.app_fastapi:app` on `:8000`), `/health` probes, Workload-Identity ServiceAccount, `GH_TOKEN` from a Secret, config via ConfigMap.
- **Service** — `ClusterIP` with a named `http` port (required by Istio).
- **VirtualService** — routes your host to the Service through an ASM ingress gateway (120s route timeout so the SSE promote/PR-tracking flow isn't cut off).
- Optional: **Gateway** (`gateway.enabled`), chart-managed **Secret**, **ServiceAccount**.

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

Two things bite here, in this order:

1. **Scheme.** `httpsProxy` names the proxy used *for* https traffic, but its
   value must be `http://host:port` — the hop to the proxy is plain HTTP.
   `https://…` produces `Unable to connect to proxy … only use HTTP`.
2. **TLS inspection.** If the proxy re-signs certificates, the slim base image
   does not trust the corporate CA and every call fails with
   `certificate verify failed: self-signed certificate in certificate chain`.
   Mount the CA:

```bash
kubectl -n release create configmap corp-proxy-ca --from-file=ca.crt=/path/to/root-ca.crt
```
```yaml
proxy:
  caBundle:
    existingConfigMap: corp-proxy-ca
    key: ca.crt          # optional, defaults to ca.crt
```

The chart mounts it read-only and sets `REQUESTS_CA_BUNDLE` (requests/PyGithub),
`SSL_CERT_FILE` (OpenSSL — httpx, google clients) **and** `GIT_SSL_CAINFO`
(git, which reads none of the others and is what the release flow clones with).
Verification stays ON — never disable it instead.

**Test from THIS image, not another pod.** Corporate base images often already
trust the CA, so a probe from a neighbouring pod can report success while the
app still fails. `/api/diagnostics` checks from inside the running app.

`config.GITHUB_BASE_URL` is unrelated to the proxy: leave it empty for github.com
(the client then uses `api.github.com`); set it only for GitHub Enterprise Server
(`https://<ghe-host>/api/v3`).

## JIRA ticket verification (optional)

The ticket a developer types when queueing goes straight into the change record,
so a typo is only found by whoever audits it later. With a read-only technical
account configured, the key is resolved at queue time:

- **key does not exist** → refused, nothing queued (developer error)
- **JIRA unreachable / token rejected** → queued with a warning (an outage must
  never block a release)
- **resolved** → the ticket summary fills the change details when the developer
  left them blank, rather than asking them to retype what JIRA already knows

```yaml
config:
  JIRA_BASE_URL: "https://your-org.atlassian.net"
  JIRA_USER_EMAIL: "release-svc@your-org.com"   # the account the token belongs to
jiraToken:
  existingSecret: "release-copilot-jira"        # managed outside the chart
  existingSecretKey: "jira-api-token"
```

The token is referenced from the Secret, never templated into the chart, and
never appears in a log or an error message — including the 401/403 path, which
is pinned by a test. Leave `JIRA_BASE_URL` empty to disable the lookup entirely;
the ticket is then stored exactly as typed.

## Surviving restarts

Two different mechanisms, often confused:

**`helm upgrade` and pod replacement** are covered by the rollout strategy. With
`maxUnavailable: 0` / `maxSurge: 1` the replacement pod must pass its readiness
probe *before* the old one is killed, so an upgrade is a handover rather than a
gap — which matters precisely because `replicaCount` is 1.
`terminationGracePeriodSeconds: 60` then lets an in-flight SSE turn (a promote
can stream for a minute) finish after the pod stops taking new traffic.

**A PodDisruptionBudget covers something else entirely**: voluntary disruption —
node drains and cluster upgrades. It does nothing for crashes or for
`helm upgrade`.

At `replicaCount: 1` a PDB is a trap, not a safeguard. `minAvailable: 1` with one
pod means that pod can never be evicted, so a node drain blocks indefinitely and
cluster maintenance wedges on this app. The chart refuses that combination:

```
Error: podDisruptionBudget.enabled requires replicaCount > 1 — at 1 replica a
PDB blocks node drains instead of protecting anything.
```

Enable it once you genuinely run 2+ replicas — which needs the per-pod state
dealt with first (see the section above). `podDisruptionBudget.force=true`
bypasses the guard if you have a reason.

## Persistent chat sessions (surviving pod restarts)

By default ADK sessions are in-memory: a restart or a rollout loses every
conversation, and a second replica would answer from a different history. That
is why `replicaCount` is pinned to 1.

`ADK_SESSION_BACKEND: "vertex"` moves sessions into a **Vertex AI Agent Engine**.
You do not have to create one: the app looks for an engine whose display name
matches `VERTEX_AGENT_ENGINE_NAME` in `GOOGLE_CLOUD_LOCATION`, and creates it if
absent. The same values file therefore works in every project.

```yaml
config:
  ADK_SESSION_BACKEND: "vertex"
  VERTEX_AGENT_ENGINE_NAME: "release-copilot-sessions"
```

Two pods starting together can each create one. That is tolerated rather than
locked against: SELECTION is deterministic — every pod sorts the matches and
takes the same one — so they converge on a single session store, and a duplicate
is logged as a warning naming the extras to delete.

To use one exact engine and create nothing, pin it instead:

```yaml
config:
  VERTEX_AGENT_ENGINE_ID: "<bare id or full resource name>"   # wins over the name
```

The pod's service account needs `roles/aiplatform.user` on the project.

**What this does and does not fix.** Conversation history survives restarts and
is shared across pods. Three pieces of state are still process-local:

| still per-pod | effect |
|---|---|
| pending `CONFIRM-…` previews | a preview served by pod A cannot be confirmed on pod B |
| paused yes/no prod-op confirmations | same |
| the per-thread GitHub PAT (memory-only by design) | user must reconnect after a restart |

So this is a real improvement to restart behaviour, but **not on its own a
licence to raise `replicaCount`** — those three need addressing first.

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
| `config.ADK_CONTEXT_CACHE` | `true` | cache the static prompt prefix (root instruction + skills) |
| `config.ADK_EVENT_COMPACTION` | `true` | summarize old events on long chat sessions |
| `config.ADK_MEMORY_ENABLED` | `true` | preload memories + persist sessions (in-memory / per-pod) |
| `updateStrategy.rollingUpdate` | `maxUnavailable: 0`, `maxSurge: 1` | new pod ready before the old is killed — what makes a restart cheap at 1 replica |
| `terminationGracePeriodSeconds` | `60` | lets an in-flight SSE turn finish after traffic stops |
| `podDisruptionBudget.enabled` | `false` | **leave off at 1 replica** — see below |
| `config.ADK_SESSION_BACKEND` | `memory` | `memory` (per-pod) or `vertex` (Agent Engine — survives restarts) |
| `config.VERTEX_AGENT_ENGINE_NAME` | `release-copilot-sessions` | engine display name; found in `GOOGLE_CLOUD_LOCATION`, **created if absent** |
| `config.VERTEX_AGENT_ENGINE_ID` | `""` | pin one exact engine instead; nothing is created |
| `config.ADK_CONFIRM_PROD_OPS` | `true` | require confirmation before prod remove / PRD release |
| `config.PRL1_BRANCH` | `PRL1` | second terminal env branch (prl1_only charts never reach PRD) |
| `config.RELEASE_UPDATER_SCRIPT` | `scripts/release/update_release_files.py` | deploy repo's file-set generator (CARE/DF releases) |
| `config.ARTIFACTORY_BASE_URL` | `""` | prepended when devs give bare `name:version` |
| `config.DF_BUILD_REPO` | `""` | Dataflow images' build repo (empty = BUILD_REPO) |
| `config.DF_RELEASE_REPO` | `""` | repo a **DF release** is raised in (empty = DEPLOY_REPO). Separate from `DF_DEPLOY_REPO` |
| `config.JIRA_BASE_URL` / `.JIRA_USER_EMAIL` | `""` | read-only JIRA lookup at queue time; empty = disabled |
| `jiraToken.existingSecret` / `.existingSecretKey` | `""` / `jira-api-token` | Secret holding the technical-account API token |
| `config.DF_DEPLOY_REPO` / `.DF_DEPLOY_WORKFLOW` | `""` / `df-deploy.yml` | Dataflow workflow-dispatch deploys |
| `config.DF_DEPLOY_REF` | `""` | Branch to dispatch on; empty = repo default branch |
| `config.DF_DISPATCH_INPUTS` | `{"image","tag","environment"}` | Maps our values onto the DF workflow's declared input names |
| `config.COMPOSER_REPO` | `""` | Composer DAGs repo; empty = no DAG version bump offered |
| `config.COMPOSER_BRANCH` / `.COMPOSER_DAG_DIR_PATTERN` | `main` / `{env}` | branch the bump PR targets, and the per-env DAG folder |

### Pointing the DF deploy at your workflow

A `workflow_dispatch` is rejected with **HTTP 422 `Unexpected inputs provided`**
if it carries an input the workflow does not declare, and with a 404 if the ref
does not contain the workflow file. So both the input names and the ref are
configuration. For a DF workflow on `main` that declares:

```yaml
on:
  workflow_dispatch:
    inputs:
      module: { type: choice, options: [svc-a, svc-b] }
      binary_version: { type: string }
```

set:

```yaml
config:
  DF_DEPLOY_REPO: "your-org/your-df-app"
  DF_DEPLOY_WORKFLOW: "UAT.yaml"     # exact file name, case-sensitive
  DF_DEPLOY_REF: "main"              # branch holding that file
  DF_DISPATCH_INPUTS: '{"module": "{image}", "binary_version": "{tag}"}'
```

The **Deploy to DF UAT** form then labels itself from the workflow: the fields
read *Module* and *Binary version*, and a `choice` input renders as a dropdown of
its `options:` — so a value GitHub would refuse cannot be entered. The
confirmation preview renders the mapped inputs, so what the developer approves is
exactly what gets dispatched.

The form reads the workflow's `inputs:` block from GitHub (cached 5 min). If that
read fails it falls back to plain *Image name* / *Tag* text fields rather than
failing to open — a rejected dispatch is then reported as an error, never as a
deploy.
| `config.CONTROL_PREFIXES` | `RCTLD,RLFT,RFTL` | control step/JOB prefixes, case-insensitive. `RCTLD` is the live gate (covers `RCTLDEF…`). Adding a prefix lets that name REFUSE a queue request — only add checks the release sign-off depends on |
| `config.BQ_DATASET` / `.BQ_TABLE` / `.BQ_LOCATION` | `release_agent` / `release_intents` / `US` | must match the terraform-provisioned table; empty dataset disables |
| `config.BQ_AUTO_CREATE` | `false` | keep false in cluster (table pre-provisioned) |
| `config.RELEASE_GUARD_BRANCHES` | `""` | e.g. `SIT,UAT,PRD,PRL1` — one release at a time |
| `proxy.httpsProxy` / `.httpProxy` / `.noProxy` | `""` | corporate egress proxy (see above) |

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
with SQLite-on-PVC for a single replica, or Postgres for many).

## Debugging a fresh deployment

`GET /api/diagnostics` reports what is configured and whether each backend
actually answers — no secrets (the GitHub token is a boolean):

```bash
kubectl -n release port-forward deploy/rc-release-copilot 8000:8000
curl -s localhost:8000/api/diagnostics | jq
# or in-browser: https://<host>/<prefix>/api/diagnostics
```

Reading the `vertex` result:

| Symptom | Cause |
|---|---|
| `No API key was provided` | `GOOGLE_GENAI_USE_VERTEXAI` is not `"true"` — the SDK fell back to AI Studio |
| `PermissionDenied` / 403 | Workload Identity not bound, or the GSA lacks `roles/aiplatform.user` **in the Vertex project** (cross-project setups grant it there, not in the cluster project) |
| 404 on the model | `GEMINI_MODEL` not served in `GOOGLE_CLOUD_LOCATION` |
| 429 / `RESOURCE_EXHAUSTED` | Vertex quota — request an increase |
| times out | egress blocked, or `NO_PROXY` no longer exempts `.googleapis.com` |

`github.ok false` with 404 means the repo name is wrong or the token can't see
it; `bq.ok false` with `404 Not found: Table` means the table isn't provisioned
yet (or set `BQ_DATASET: ""` to switch the feature off).

It is intentionally NOT part of `/health` — it makes live calls, so probes must
not hit it.

### Two probes for TLS / proxy problems

Run these **in the app pod**, not a neighbouring one: corporate base images often
already trust the corporate CA, so a probe from another pod can report success
while the app still fails. Both read the proxy from the pod's own environment,
so there is nothing to edit.

**1. Which CA file does this image actually use?**

```bash
kubectl -n <namespace> exec deploy/<release>-release-copilot -- \
  python -c "import ssl; p=ssl.get_default_verify_paths(); print('cafile:', p.cafile); print('capath:', p.capath)"
```

Whatever `cafile` prints is the store that `curl`, `git` and `urllib` already
trust — and therefore the correct value for `REQUESTS_CA_BUNDLE`. Debian prints
`/etc/ssl/certs/ca-certificates.crt`, RHEL/UBI `/etc/pki/tls/certs/ca-bundle.crt`.
Read it; don't assume.

**2. Which client, and which host, actually fails?**

```bash
kubectl -n <namespace> exec -i deploy/<release>-release-copilot -- python - <<'PROBE'
import os, ssl, urllib.request, requests, certifi
px = os.environ.get("HTTPS_PROXY", "")
o = urllib.request.build_opener(urllib.request.ProxyHandler({"http": px, "https": px}))
for host in ("https://github.com", "https://api.github.com"):
    for name, call in (("urllib  ", lambda h=host: o.open(h, timeout=10).status),
                       ("requests", lambda h=host: requests.get(h, timeout=10).status_code)):
        try:
            print(host, name, "->", call())
        except Exception as e:
            print(host, name, "-> FAIL", type(e).__name__)
print("system CA:", ssl.get_default_verify_paths().cafile)
print("certifi  :", certifi.where())
PROBE
```

| Result | Meaning | Fix |
|---|---|---|
| `urllib` OK, `requests` FAIL | the CA is in the **system** store; only `requests` (hence PyGithub) can't see it — it verifies against certifi | set `REQUESTS_CA_BUNDLE` to probe 1's `cafile`, or rebuild: the app now defaults to it |
| both FAIL with a cert error | the image doesn't trust the CA at all | mount it — `proxy.caBundle.existingConfigMap` |
| `github.com` OK, `api.github.com` FAIL | the proxy treats the hosts differently | proxy allow-list change (network team). The app needs **both**: REST on `api.github.com`, git clone on `github.com` |
| both FAIL, `ProxyError … only use HTTP` | proxy URL has the wrong scheme | the value must be `http://host:port` |

## Verify locally before applying
```bash
helm lint helm/release-copilot
helm template rc helm/release-copilot -n release \
  --set config.GOOGLE_CLOUD_PROJECT=p --set githubToken.existingSecret=s
```
