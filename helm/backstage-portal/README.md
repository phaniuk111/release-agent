# backstage-portal Helm chart

Deploys the Backstage developer portal that fronts the **release-copilot** ADK
agent — the UI half of the two-pod topology:

```
Pod: backstage-portal           Pod: release-copilot (helm/release-copilot)
  UI + proxy backend     ──►     FastAPI + ADK agent
  /api/proxy/release-copilot     github.com · Vertex AI · BigQuery
```

## Build & push the image

```bash
docker build -t <registry>/backstage-release-copilot:<tag> ./backstage
docker push <registry>/backstage-release-copilot:<tag>
```

## Install

```bash
helm install backstage ./helm/backstage-portal \
  --set image.repository=<registry>/backstage-release-copilot \
  --set image.tag=<tag> \
  --set releaseCopilot.url=http://<release-copilot-svc>.<ns>.svc.cluster.local:8000 \
  --set config.app.baseUrl=https://backstage.example.internal \
  --set config.backend.baseUrl=https://backstage.example.internal
```

Order doesn't matter; only `releaseCopilot.url` couples the two charts.

## Shared-hostname exposure (GKE + Istio/ASM) with a path prefix

Serve the portal on a common host at `/idp` via a VirtualService:

```bash
helm install backstage ./helm/backstage-portal \
  --set image.repository=<registry>/backstage-release-copilot \
  --set image.tag=<tag> \
  --set releaseCopilot.url=http://release-copilot:8000 \
  --set virtualService.enabled=true \
  --set virtualService.hosts[0]=shared.internal.company.com \
  --set virtualService.gateways[0]=infra/shared-gateway \
  --set virtualService.pathPrefix=/idp \
  --set config.app.baseUrl=https://shared.internal.company.com/idp \
  --set config.backend.baseUrl=https://shared.internal.company.com/idp
```

How it works: the VS redirects `/idp` → `/idp/` and strips the prefix before
forwarding to the pod. The app still knows the prefix because `app.baseUrl`'s
pathname (`/idp`) is read at serve time — asset URLs in `index.html` are
templated with it, and the frontend router uses it as its base path. So asset
and API requests return **with** the prefix and get stripped again on the way
back in. Pod-internal routing is unchanged.

If your namespace already has a Gateway terminating the common host, keep
`gateway.enabled=false` and point `virtualService.gateways` at it. Only enable
`gateway.enabled` (and set `gateway.selector`) if this chart should create one.

## Key values

| Value | Default | Purpose |
| --- | --- | --- |
| `image.repository/tag` | `backstage-release-copilot:poc` | app image |
| `releaseCopilot.url` | `http://release-copilot:8000` | agent service the proxy forwards to |
| `config.*` | sqlite, guest auth | merged over baked-in app-config (ConfigMap → `app-config.kubernetes.yaml`) |
| `persistence.enabled` | `false` | `false` = sqlite in `emptyDir` (pod-lifetime); `true` = sqlite on a PVC |
| `ingress.*` | disabled | classic k8s Ingress (mutually exclusive with virtualService) |
| `virtualService.*` | disabled | Istio VS on a shared host; `pathPrefix=/idp` |
| `gateway.*` | disabled | only if the chart should create its own Gateway |
| `service.port` | `7007` | Backstage http port |

## Notes

- **PoC auth is guest-only** with `dangerously-allow-unauthenticated` on the
  release-copilot proxy — fine locally; add a real auth provider (and drop that
  flag) before exposing beyond localhost.
- **State**: sqlite by default — with `persistence.enabled=false` it lives in an
  `emptyDir` (survives container restarts within the same pod, resets on
  reschedule). With `persistence.enabled=true` the sqlite file is stored on a
  PVC (`backstage-data`). Single-replica only (PVC is ReadWriteOnce) — point
  `config.backend.database` at postgres for multi-replica.
- **Rolling deploys with PVC**: the chart sets `strategy: Recreate` when
  persistence is enabled — a RollingUpdate surge pod cannot attach a
  ReadWriteOnce volume held by the old pod and would hang in ContainerCreating.
- **A PVC is not a backup.** Backstage catalog/history is regenerable from
  `examples/*.yaml`, but plugin/queue state is not. Use volume snapshots
  (`VolumeSnapshotClass` + a CronJob or Velero) or schedule sqlite backups
  (`sqlite3 /app/backstage-data/backstage.sqlite ".backup ..."` from a sidecar).
  For anything beyond PoC: Postgres.
- **Catalog demo data**: the chart seeds the same example locations baked into
  the image (`examples/*.yaml`), including the mock Spring Boot
  `payment-service` and the `release-platform` release-copilot entities.
- The baked-in image default baseUrls are localhost; always set
  `config.app.baseUrl`/`config.backend.baseUrl` to the external URL — including
  the path prefix when using `virtualService.pathPrefix`.
