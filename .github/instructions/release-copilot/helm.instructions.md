---
applyTo: "helm/release-copilot/**"
---
# Helm chart

- All runtime configuration is environment variables rendered from
  `values.yaml` → `config:` into one ConfigMap. Every key maps to a pydantic
  field in `src/release_agent/config.py`; add both together, with a comment
  saying what the setting changes.
- Secrets are never values: the GitHub token comes from
  `githubToken.existingSecret`, JIRA and Langfuse keys from
  `jiraToken.existingSecret` and `langfuse.existingSecret`.
- `replicaCount: 1` — chat sessions, connected PATs, ADK memory and the
  one-UAT-deploy-at-a-time marker are per pod. Read the comment at the top of
  `values.yaml` before raising it.
- `virtualService.enabled: true` needs Istio CRDs (the service mesh); set it to
  false on a cluster without one. Leave the PodDisruptionBudget off at one
  replica.
- The pod's CPU limit is what Autopilot bills, and Python sees the node's CPU
  count, not the limit — keep that in mind when sizing pools.
