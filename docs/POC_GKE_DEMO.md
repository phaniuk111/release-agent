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
| Portal pod | 0.25 vCPU + 0.5 GiB × Spot rates ($0.0133/vCPU-hr) | 250m/512Mi | ~$0.004/hr |
| Agent pod | same | 250m/512Mi | ~$0.004/hr |
| Managed Service Mesh | ~$0.50/client/mo (per pod) | 2 pods | ~$1/mo |
| PVC (pd-balanced) | ~$0.10/GiB/mo | 1 GiB | ~$0.10/mo |

**Running cost ≈ 1¢/hour.** A 2h demo session ≈ 2¢. A month of weekly demos ≈ **$2–4**.

⚠️ Autopilot floors pod requests (250m/512Mi minimum) — that's what the math uses.

## 3. Lifecycle (two commands)

```bash
scripts/gke-demo-up.sh      # ~10 min: cluster + mesh + images + helm installs
scripts/gke-demo-down.sh    # ~5 min:  helm uninstall + cluster delete + disk sweep
```

What `up` does, in order:

1. Autopilot cluster (`create-auto`, regular channel) — skipped if it exists
2. Fleet registration + **managed Cloud Service Mesh** (`gcloud container fleet mesh`)
3. Namespace labeled for sidecar injection
4. Images: build if missing locally, push to ghcr
5. Secret `release-copilot-secrets` (GH_TOKEN from env/gh CLI — never committed)
6. `helm upgrade --install` both charts, both with
   `nodeSelector: cloud.google.com/gke-spot: "true"` and
   `persistence.enabled=true` for the portal

What `down` does: helm uninstall (releases PVC) → cluster delete → orphaned-disk sweep.

### Env vars both scripts accept

`PROJECT_ID` (default flash-keel-412418), `REGION` (us-central1),
`CLUSTER_NAME` (release-copilot-demo), `NAMESPACE` (release-copilot),
`DNS_HOST` (optional external hostname), `KEEP_DISK=true` (down: preserve
the sqlite PD across sessions).

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

- [ ] `kubectl -n release-copilot get pods` — both Running, **2/2 containers** (sidecar injected)
- [ ] `kubectl -n release-copilot logs deploy/backstage | grep -i listening`
- [ ] port-forward → portal loads, catalog shows payment-service + release entities
- [ ] Release Copilot → Queue shows data (agent → BigQuery OK)
- [ ] Deploy tab → submit → preview → Confirm → PRs merged (end-to-end)
- [ ] `istioctl proxy-status` — both pods SYNCED (mesh actually routing)

## 6. Post-PoC exit path

| When | Move |
| --- | --- |
| Second team / real auth | Cloud SQL Postgres (chart values change only) |
| Multi-replica portal | Postgres required (SQLite is single-writer) |
| Production | GKE Enterprise + zonal redundancy + real OAuth + Cloud SQL |
| Cost ceiling hit | Keep cluster up but scale deployments to 0 (`kubectl scale deploy --all --replicas=0`) — pods cost nothing paused; cluster fee still applies ($0.10/hr) unless free tier covers it |

## 7. Files

- `scripts/gke-demo-up.sh` / `gke-demo-down.sh` — the lifecycle
- `helm/backstage-portal/` — portal chart (values: persistence, spot, VS, trustProxy)
- `helm/release-copilot/` — agent chart (secret, VS, spot)
- `docker-compose.backstage.yml` — local pre-flight before touching GKE
