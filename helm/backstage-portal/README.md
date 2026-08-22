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

## Key values

| Value | Default | Purpose |
| --- | --- | --- |
| `image.repository/tag` | `backstage-release-copilot:poc` | app image |
| `releaseCopilot.url` | `http://release-copilot:8000` | agent service the proxy forwards to |
| `config.*` | sqlite in-memory, guest auth | merged over baked-in app-config (ConfigMap → `app-config.kubernetes.yaml`) |
| `ingress.*` | disabled | enable + host for real access |
| `service.port` | `7007` | Backstage http port |

## Notes

- **PoC auth is guest-only** with `dangerously-allow-unauthenticated` on the
  release-copilot proxy — fine locally; add a real auth provider (and drop that
  flag) before exposing beyond localhost.
- **State**: default better-sqlite3 in-memory — plugin/queue state resets on
  restart. Point `config.backend.database` at postgres for persistence.
- The baked-in image default baseUrls are localhost; always set
  `config.app.baseUrl`/`config.backend.baseUrl` to the external URL.
