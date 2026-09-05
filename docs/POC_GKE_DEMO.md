# Release Copilot — GKE PoC Plan

> Goal: run the full Release Copilot portal (Backstage + ADK agent) on GKE with
> managed Istio (Cloud Service Mesh), at **near-zero cost**, using Spot Pods
> and a tear-down-after-every-session lifecycle.

---

## 1. What we're deploying

Two independent Deployments (this branch, `backstage_poc`, is self-contained):

| Service | Image (Dockerfile) | Port | State |
| --- | --- | --- | --- |
| **Backstage portal** | `backstage/Dockerfile` | 7007 | SQLite on a 1Gi PVC (`/app/backstage-data/backstage.sqlite`) |
| **ADK agent** | `Dockerfile.agent` | 8000 | In-memory (chat threads); durable state in BigQuery |

Interaction: the portal's backend **proxies** `/api/proxy/release-copilot/*`
to the agent (`RELEASE_COPILOT_URL` = the agent's ClusterIP service). The
agent never calls Backstage — one-directional, so either can fail
independently without breaking the other's core function.

```
browser ──► Backstage pod (:7007) ──proxy──► agent pod (:8000) ──► GitHub / Vertex / BigQuery
                     ▲                                          (sidecars: Istio, mTLS)
              PVC (sqlite)
```

## 2. Cost model (Autopilot + Spot Pods, 2026 pricing)

| Line | Rate | Our load | Result |
| --- | --- | --- | --- |
| Cluster fee | $0.10/hr | covered by **$74.40/mo GKE free tier** (one Autopilot/zonal cluster) | $0 |
| Portal pod | Spot ($0.0133/vCPU-hr, $0.0015/GiB-hr) | 1 vCPU / 2 GiB | ~$0.016/hr |
| Agent pod | same | 1 vCPU / 1 GiB | ~$0.015/hr |
| Managed Service Mesh | included with GKE | 2 pods | $0 |
| PVC (pd-balanced) | ~$0.10/GiB/mo | 1 GiB | ~$0.10/mo |
| Artifact Registry | $0.10/GB/mo above 0.5 GB free | ~1.5 GB | ~$0.10/mo |
| GitHub Actions | free on public repos; 2,000 min/mo otherwise | ~12 min/build | $0 |

**Running cost ~3c/hour.** A 2h demo session ~6c. A month of weekly demos ~**$1-2**.

WARNING: Autopilot does NOT bill the 250m/512Mi you *request* - it raises requests
to match your *limits* (Guaranteed QoS). The charts' limits (1 vCPU, 1-2 GiB) are
what actually bills, which is why these numbers are ~4x the naive estimate.

WARNING: the one line that can scale unexpectedly is **fleet registration**. This
project's fleet already carries GKE Enterprise features (`configmanagement`,
`policycontroller`, `fleetobservability`) with zero memberships. Registering a
cluster for mesh can flip it to the Enterprise tier ($0.00822/vCPU-hr). Check
right after registering, and unregister if it flipped:

```bash
gcloud container clusters describe release-copilot-demo --region us-central1 \
  --format='value(enterpriseConfig.clusterTier)'   # want: STANDARD
```

## 3. Lifecycle (two owners, never overlapping)

Infrastructure is a human action from an authenticated laptop. Images and helm
releases belong to CI.

```bash
scripts/gke-poc-ci-setup.sh                              # ONCE: keyless CI auth (WIF)
scripts/gke-demo-up.sh                                   # ~12 min: cluster + mesh + secret + WI
gh workflow run poc-gke-deploy.yml --ref backstage_poc   # ~12 min: build both, deploy both
scripts/gke-demo-down.sh                                 # ~6 min: uninstall + delete + sweep
```

What `up` does (INFRASTRUCTURE only), in order:

1. Artifact Registry repo - skipped if it exists
2. Autopilot cluster (`create-auto`, regular channel) - skipped if it exists
3. Fleet registration + **managed Cloud Service Mesh**, then the namespace label
   `istio.io/rev=asm-managed`. Bounded 10-min wait: a mesh that does not
   converge *degrades* the demo (no mTLS/telemetry), it does not block it
4. Secret `release-copilot-secrets` (key `gh-token`, from env/gh CLI - never committed)
5. Workload Identity: `aiplatform.user` + `bigquery.{jobUser,dataEditor}` granted
   straight to the KSA principal, so there is no GCP service account to clean up

What the **workflow** does (APPLICATION): builds both images on `ubuntu-latest`
(linux/amd64, GHA layer cache), pushes them to Artifact Registry tagged with the
commit SHA, then `helm upgrade --install`s both charts pinned to Spot Pods.

What `down` does: helm uninstall -> cluster delete -> fleet unregister -> revoke
both sets of IAM grants -> delete the AR repo -> sweep unattached `pvc-*` disks.

### Why CI builds, and not a laptop

- The runner is **linux/amd64**, matching Autopilot nodes. A dev Mac is arm64 and
  silently produces an image the cluster cannot execute.
- Auth is **keyless** (GitHub OIDC -> Workload Identity Federation). No
  service-account JSON is created, downloaded, or stored as a repo secret.
- ghcr is not used: pushing there needs a token with `write:packages`, which the
  release flow's PAT deliberately does not carry. Artifact Registry in the same
  project also means GKE pulls with no `imagePullSecret` at all.

### Env vars the scripts accept

`PROJECT_ID` (default flash-keel-412418), `REGION` (us-central1),
`CLUSTER_NAME` (release-copilot-demo), `NAMESPACE` (release-copilot),
`AR_REPO` (poc), `MESH=false` (skip the mesh), and on `down`:
`KEEP_DISK=true`, `KEEP_IMAGES=true`.

## 4. Why this design (decisions & trade-offs)

- **Autopilot, not Standard**: pod-based billing means no idle-node cost for
  a 2-pod demo; Standard would bill two e2 nodes 24/7 unless we tuned them.
- **Spot Pods, not Spot node pools**: Autopilot provisions spot capacity per
  workload via `cloud.google.com/gke-spot`. Evictions cost nothing here —
  the demo cluster is deleted after each session anyway.
- **Delete cluster per session**: the $0.10/hr cluster fee is the *only*
  always-on line; deleting after each demo means the free tier absorbs it.
  Data that must survive (release events) lives in **BigQuery** — external
  to the cluster by design.
- **Managed CSM over in-cluster Istio**: zero istioctl, zero upgrade work;
  the fleet API handles it. Note: Autopilot only supports *managed* CSM.
- **SQLite over Postgres for now**: single replica, disposable data. Switch
  to Cloud SQL when the portal outlives the PoC (see helm README).

### Known trade-offs (accepted)

- Spot eviction can kill an in-flight agent chat thread (sessions are
  in-memory). Retry the message; the queue/ledger in BQ is durable.
- Cluster delete resets: portal starred entities, visit tracking, sqlite.
- Recreate-from-scratch takes ~10 min/session (cluster + mesh + pulls).

## 5. Verification checklist (per session)

- [ ] `gh run watch` — build + deploy both green
- [ ] `kubectl -n release-copilot get pods` — both Running, **2/2 containers** (sidecar injected)
- [ ] `kubectl -n release-copilot logs deploy/backstage | grep -i listening`
- [ ] port-forward → portal loads, catalog shows payment-service + release entities
- [ ] Release Copilot → Queue shows data (agent → BigQuery OK, proves Workload Identity)
- [ ] Deploy tab → submit → preview → Confirm → PRs merged (end-to-end)
- [ ] `kubectl -n release-copilot get pods -o jsonpath='{..containers[*].name}'` —
      an `istio-proxy` alongside each app container (mesh actually injected;
      `istioctl` is not needed and is not installed by these scripts)

## 6. Security posture (this repo is PUBLIC)

Every workflow log, step summary and run artifact on `phaniuk111/release-agent`
is world-readable, and the deploy workflow can mint a real Google credential.
What that forces:

| Control | Why |
| --- | --- |
| **No long-lived keys** | Auth is GitHub OIDC → WIF. No service-account JSON is created, downloaded, or stored as a repo secret. |
| **Provider pinned to repo AND branch** | `assertion.repository=='phaniuk111/release-agent' && assertion.ref=='refs/heads/backstage_poc'`. Without the repo clause any repo on GitHub could mint a token; without the ref clause a workflow added on any other branch could. |
| **Actions pinned to commit SHAs** | A retagged upstream action is a direct path to exfiltrating the OIDC token. Tags are recorded in trailing comments. |
| **`artifactregistry.writer` scoped to one repo** | Not project-wide, so a stolen credential cannot write to any other registry in the project. |
| **`permissions:` minimal** | `contents: read` + `id-token: write`. Nothing else. |
| **Step summary carries no topology** | `kubectl get pods` without `-o wide` — pod IPs and node names do not belong on a public page. |
| **Manual trigger only** | No `pull_request`/`pull_request_target`, so a fork PR has no path to the credential. |

The agent's GitHub PAT never passes through CI by default: it lives in the
cluster Secret `release-copilot-secrets` (key `gh-token`) that `gke-demo-up.sh`
creates from your local `gh` session. Set `RELEASE_COPILOT_GH_TOKEN` only if you
want CI to manage it — and note it reaches OTHER repos (`BUILD_REPO`,
`DEPLOY_REPO`), so the workflow's own `GITHUB_TOKEN` cannot substitute for it.

### Residual risks (accepted for a PoC)

- **`container.developer` is project-wide.** GKE has no cluster-scoped
  equivalent, so the CI identity can reach any cluster in the project. It is the
  widest grant here; `gke-demo-down.sh` revokes it at teardown.
- **Anyone with write access to `backstage_poc` can reach the project.** The
  branch pin narrows *which* ref, not *who* can push to it. Protect the branch
  if that matters.

## 7. Post-PoC exit path

| When | Move |
| --- | --- |
| Second team / real auth | Cloud SQL Postgres (chart values change only) |
| Multi-replica portal | Postgres required (SQLite is single-writer) |
| Production | GKE Enterprise + zonal redundancy + real OAuth + Cloud SQL |
| Cost ceiling hit | Keep cluster up but scale deployments to 0 (`kubectl scale deploy --all --replicas=0`) — pods cost nothing paused; cluster fee still applies ($0.10/hr) unless free tier covers it |

## 8. Files

- `scripts/gke-demo-up.sh` / `gke-demo-down.sh` — the infrastructure lifecycle
- `scripts/gke-poc-ci-setup.sh` — one-time keyless CI auth (WIF, no keys)
- `.github/workflows/poc-gke-deploy.yml` — the build + deploy lane
- `helm/backstage-portal/` — portal chart (values: persistence, spot, VS, trustProxy)
- `helm/release-copilot/` — agent chart (secret, VS, spot)
- `docker-compose.backstage.yml` — local pre-flight before touching GKE
