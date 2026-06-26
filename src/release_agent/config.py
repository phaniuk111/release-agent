"""Runtime configuration helpers."""

import os
import subprocess
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _get_gcp_project() -> str:
    """Get GCP project from env or gcloud (ADC / installed gcloud)."""
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
    if project:
        return project

    # Try to auto-detect from gcloud (user has gcloud installed)
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            proj = result.stdout.strip()
            if proj and proj != "(unset)":
                return proj
    except Exception:
        pass

    return ""


class Settings(BaseSettings):
    # Each field accepts several env var spellings so the names used in the
    # README / .env.example / shell exports (e.g. RELEASE_AGENT_TARGET_REPO,
    # DEPLOY_REPO, GOOGLE_CLOUD_PROJECT) are all honored — previously only the
    # RELEASE_-prefixed names worked, so exports were silently ignored.
    target_repo: str = Field(
        default="phaniuk111/devops",
        validation_alias=AliasChoices(
            "RELEASE_AGENT_TARGET_REPO", "RELEASE_TARGET_REPO", "TARGET_REPO"
        ),
    )
    deploy_repo: str = Field(
        default="phaniuk111/devops",  # may differ if the workflow opens the PR elsewhere
        validation_alias=AliasChoices(
            "DEPLOY_REPO", "RELEASE_DEPLOY_REPO", "RELEASE_AGENT_DEPLOY_REPO"
        ),
    )
    default_workflow: str = Field(
        default="image-tag-step-report.yml",
        validation_alias=AliasChoices("DEFAULT_WORKFLOW", "RELEASE_DEFAULT_WORKFLOW"),
    )
    # Workflow dispatched in DEPLOY_REPO to (re)run the deployment simulation.
    on_merge_workflow: str = Field(
        default="on-merge-deploy.yml",
        validation_alias=AliasChoices("ON_MERGE_WORKFLOW", "RELEASE_ON_MERGE_WORKFLOW"),
    )
    # --- Branch-based promotion in DEPLOY_REPO ---
    # Promote to UAT  = PR into the UAT branch.
    # Promote to PROD = PR from the UAT branch into the PRD branch.
    uat_branch: str = Field(
        default="UAT",
        validation_alias=AliasChoices("UAT_BRANCH", "RELEASE_UAT_BRANCH"),
    )
    prd_branch: str = Field(
        default="PRD",
        validation_alias=AliasChoices("PRD_BRANCH", "PROD_BRANCH", "RELEASE_PRD_BRANCH"),
    )
    # JSON config the promotion updates (same path on each env branch).
    env_config_path: str = Field(
        default="configs/images.json",
        validation_alias=AliasChoices("ENV_CONFIG_PATH", "RELEASE_ENV_CONFIG_PATH"),
    )
    # Change-request template the pasted JSON updates; the CHG is created from it
    # when the UAT->PRD PR is raised.
    change_request_path: str = Field(
        default="change-request.json",
        validation_alias=AliasChoices("CHANGE_REQUEST_PATH", "RELEASE_CHANGE_REQUEST_PATH"),
    )
    # Allow-list of workflows the agent may dispatch (enforced). Comma-separated
    # in env, e.g. ALLOWED_WORKFLOWS="image-tag-step-report.yml,release-promote.yml".
    allowed_workflows: Annotated[list[str], NoDecode] = Field(
        default=[
            "image-tag-step-report.yml",
            "build-payments-api.yml",
            "build-orders-api.yml",
            "release-promote.yml",
            "create-deployment-pr.yml",
        ],
        validation_alias=AliasChoices("ALLOWED_WORKFLOWS", "RELEASE_ALLOWED_WORKFLOWS"),
    )
    manifest_path: str = Field(
        default="release-manifest.json",
        validation_alias=AliasChoices("MANIFEST_PATH", "RELEASE_MANIFEST_PATH"),
    )
    config_path: str = Field(
        default="image-workflows.json",
        validation_alias=AliasChoices("CONFIG_PATH", "RELEASE_CONFIG_PATH"),
    )

    # Vertex AI Gen AI project — resolved at runtime (env or gcloud), never hardcoded in source code
    gcp_project: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "RELEASE_GCP_PROJECT"
        ),
    )
    gcp_location: str = Field(
        default="us-central1",
        validation_alias=AliasChoices(
            "GOOGLE_CLOUD_LOCATION", "GCP_LOCATION", "RELEASE_GCP_LOCATION"
        ),
    )
    # Vertex Gemini model id. Default to a currently-available model
    # (gemini-2.0-flash was retired); override per-project/region if needed.
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        validation_alias=AliasChoices(
            "GEMINI_MODEL", "VERTEX_MODEL", "RELEASE_GEMINI_MODEL"
        ),
    )

    # App metadata (used by FastAPI)
    app_title: str = "Release Copilot"
    # NoDecode: skip pydantic-settings' built-in JSON decoding so the validator
    # below receives the raw env string and can accept comma-separated values.
    cors_origins: Annotated[list[str], NoDecode] = ["*"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, v):
        """Accept a comma-separated string (the natural shell syntax) in addition
        to a JSON array, so `RELEASE_CORS_ORIGINS=https://a,https://b` doesn't
        crash the app at import time with a JSON parse error."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return ["*"]
            if v.startswith("["):
                import json
                return json.loads(v)  # explicit, since NoDecode disabled auto-parse
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("allowed_workflows", mode="before")
    @classmethod
    def _split_allowed_workflows(cls, v):
        """Accept a comma-separated string (env) or a JSON array for the dispatch
        allow-list."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                import json
                return json.loads(v)
            return [w.strip() for w in v.split(",") if w.strip()]
        return v

    def model_post_init(self, __context):
        if not self.gcp_project:
            self.gcp_project = _get_gcp_project()

    model_config = SettingsConfigDict(
        env_prefix="RELEASE_",
        populate_by_name=True,
        extra="ignore",
        case_sensitive=False,
    )


settings = Settings()
