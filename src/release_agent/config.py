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
    # README / .env.example / shell exports (e.g. BUILD_REPO, DEPLOY_REPO,
    # GOOGLE_CLOUD_PROJECT) are all honored — previously only the
    # RELEASE_-prefixed names worked, so exports were silently ignored.
    #
    # BUILD_REPO: code + config + build repo.  Holds image-workflows.json, the
    # git tags GitHub Actions create, build runs, and RLFT/RFTL control steps.
    # Legacy env spellings are accepted as backward-compatible aliases so
    # existing deployments don't silently misroute.
    # Repos are environment-specific and MUST be supplied by configuration — the
    # local .env file, real env vars, or the Helm ConfigMap (values.yaml -> config).
    # No org/account default is hardcoded here.
    build_repo: str = Field(
        default="",  # code + image catalog + builds/tags + RLFT controls
        validation_alias=AliasChoices(
            "BUILD_REPO",
            "RELEASE_BUILD_REPO",
            "RELEASE_AGENT_TARGET_REPO",
            "RELEASE_TARGET_REPO",
            "TARGET_REPO",
        ),
    )
    deploy_repo: str = Field(
        # Holds the SIT/UAT/PRD protected branches + configs/images.json the promote
        # PR chain edits — distinct from the build/source repo above.
        default="",
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
    # --- Branch-based promotion in DEPLOY_REPO (SIT -> UAT -> PRD) ---
    # During the day, images accumulate on UAT. Only AFTER the daily cutoff is a
    # single UAT -> PRD PR raised (that PR locks the day's release).
    sit_branch: str = Field(
        default="SIT",
        validation_alias=AliasChoices("SIT_BRANCH", "RELEASE_SIT_BRANCH"),
    )
    uat_branch: str = Field(
        default="UAT",
        validation_alias=AliasChoices("UAT_BRANCH", "RELEASE_UAT_BRANCH"),
    )
    prd_branch: str = Field(
        default="PRD",
        validation_alias=AliasChoices("PRD_BRANCH", "PROD_BRANCH", "RELEASE_PRD_BRANCH"),
    )
    # JSON config the promotion updates (same path on each env branch). [legacy]
    env_config_path: str = Field(
        default="configs/images.json",
        validation_alias=AliasChoices("ENV_CONFIG_PATH", "RELEASE_ENV_CONFIG_PATH"),
    )
    # --- Helm-chart deployment model -------------------------------------------
    # The deploy repo carries an env-pathed deployment JSON per environment, shaped
    # {"include": [entry, ...]}. {env} is uat or prd -> uat/deployment.json, prd/deployment.json.
    deployment_path_pattern: str = Field(
        default="{env}/deployment.json",
        validation_alias=AliasChoices("DEPLOYMENT_PATH_PATTERN", "RELEASE_DEPLOYMENT_PATH_PATTERN"),
    )
    # Constant filled into every entry's helm_chart_dir ("comes from the helm chart").
    helm_chart_dir: str = Field(
        default="hlm-all/com/db/eod-ds",
        validation_alias=AliasChoices("HELM_CHART_DIR", "RELEASE_HELM_CHART_DIR"),
    )
    # Env-specific values file: {env} -> uat/values_uat.yaml, prd/values_prd.yaml.
    helm_values_pattern: str = Field(
        default="{env}/values_{env}.yaml",
        validation_alias=AliasChoices("HELM_VALUES_PATTERN", "RELEASE_HELM_VALUES_PATTERN"),
    )
    # Default GKE namespace per environment (a deploy request may override).
    uat_namespace: str = Field(
        default="eod1",
        validation_alias=AliasChoices("UAT_NAMESPACE", "RELEASE_UAT_NAMESPACE"),
    )
    prd_namespace: str = Field(
        default="eod1",
        validation_alias=AliasChoices("PRD_NAMESPACE", "PROD_NAMESPACE", "RELEASE_PRD_NAMESPACE"),
    )
    # Change-request template the pasted JSON updates; the CHG is created from it
    # when the UAT->PRD PR is raised.
    change_request_path: str = Field(
        default="change-request.json",
        validation_alias=AliasChoices("CHANGE_REQUEST_PATH", "RELEASE_CHANGE_REQUEST_PATH"),
    )
    # PRD release policy: at most one PRD PR per day, created before this UTC hour.
    prd_cutoff_hour_utc: int = Field(
        default=16,
        validation_alias=AliasChoices("PRD_CUTOFF_HOUR_UTC", "RELEASE_PRD_CUTOFF_HOUR_UTC"),
    )
    prd_once_per_day: bool = Field(
        default=True,
        validation_alias=AliasChoices("PRD_ONCE_PER_DAY", "RELEASE_PRD_ONCE_PER_DAY"),
    )
    # Minimum lead time (days) between raising the UAT->PRD release PR and the
    # change's start_date. 1 = the start date must be tomorrow or later.
    prd_lead_time_days: int = Field(
        default=1,
        validation_alias=AliasChoices("PRD_LEAD_TIME_DAYS", "RELEASE_PRD_LEAD_TIME_DAYS"),
    )
    # Max tool-call turns in the free-form ReAct lane before stopping gracefully
    # (guards against runaway llm<->tools loops, well under recursion_limit=25).
    react_max_tool_turns: int = Field(
        default=8,
        validation_alias=AliasChoices("REACT_MAX_TOOL_TURNS", "RELEASE_REACT_MAX_TOOL_TURNS"),
    )
    # Step-name prefixes that mark release controls in the build pipeline
    # (e.g. "RLFT approval gate", "RFTL0001 ..."). Comma-separated.
    control_prefixes: Annotated[list[str], NoDecode] = Field(
        default=["RLFT", "RFTL"],
        validation_alias=AliasChoices("CONTROL_PREFIXES", "RELEASE_CONTROL_PREFIXES"),
    )
    # Block a PRD release when any build control failed (fail-closed). When a build
    # run can't be located we don't hard-block; the agent asks for the run id.
    prd_require_controls: bool = Field(
        default=True,
        validation_alias=AliasChoices("PRD_REQUIRE_CONTROLS", "RELEASE_PRD_REQUIRE_CONTROLS"),
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
        validation_alias=AliasChoices("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "RELEASE_GCP_PROJECT"),
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
        validation_alias=AliasChoices("GEMINI_MODEL", "VERTEX_MODEL", "RELEASE_GEMINI_MODEL"),
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

    @field_validator("allowed_workflows", "control_prefixes", mode="before")
    @classmethod
    def _split_allowed_workflows(cls, v):
        """Accept a comma-separated string (env) or a JSON array for list settings
        (dispatch allow-list, control prefixes)."""
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
        # Local/dev config file (gitignored). In-cluster, the Helm ConfigMap supplies
        # the same keys as real env vars, which take precedence over the file.
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()
