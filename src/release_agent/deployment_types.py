"""Deployment-type registry — the IDP capability catalog, server-side.

Each entry describes one golden path: what the developer supplies (the form),
which envs exist, and which repo the state lives in. The UI renders its deploy
cards and forms FROM this registry (GET /api/deployment-types), so adding a new
deployment type is a registry entry + a write-path function — no new UI code.
This is deliberately the same mental model as a Backstage scaffolder template
(form schema + action), so a future portal migration is a re-skin, not a rewrite.
"""
from __future__ import annotations

from .config import settings


def deployment_types() -> dict:
    """Client-safe registry (no secrets — repo names are not secret here)."""
    return {
        "gke": {
            "label": "GKE (Helm)",
            "description": "deploy a Helm chart",
            "envs": ["uat", "prod"],
            "deploy_repo": settings.deploy_repo,
            # The GKE form is the full JSON editor over the live deployment.json
            # (pre-filled with reality; multi-chart; prod adds the change request).
            "form": {"style": "json-editor"},
        },
        "dataflow": {
            "label": "DF",
            "description": "trigger the Dataflow flex-template deploy workflow",
            "envs": ["uat"],
            "deploy_repo": settings.df_deploy_repo,
            "workflow": settings.df_deploy_workflow,
            # Deploy = workflow_dispatch with {image, tag, environment} — no state file.
            "form": {
                "style": "fields",
                "fields": [
                    {
                        "id": "image",
                        "label": "Image name",
                        "placeholder": "order-enrichment",
                        "required": True,
                    },
                    {
                        "id": "tag",
                        "label": "Tag",
                        "placeholder": "1.4.2",
                        "required": True,
                    },
                ],
            },
        },
    }
