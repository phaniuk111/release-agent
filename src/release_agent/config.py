"""Runtime configuration helpers."""

import os
import subprocess
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _use_system_ca_for_requests() -> None:
    """Make `requests` trust the image's system CA store.

    requests/PyGithub verify against certifi's PRIVATE bundle and deliberately
    ignore the OS trust store. Behind a TLS-inspecting corporate proxy the
    corporate root CA is installed system-wide — so curl, git and urllib all
    work — while requests alone fails with
    "CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain".
    That split is confusing: a urllib probe in the very same pod succeeds.

    ASK OpenSSL where its trust store is rather than guessing distro paths —
    the location varies (/etc/ssl/certs/ca-certificates.crt on Debian,
    /etc/pki/tls/certs/ca-bundle.crt on RHEL, /etc/ssl/cert.pem on Alpine and
    some corporate images), and this is exactly the file urllib/curl/git use.

    An explicit REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE always wins, and nothing
    happens when no system bundle exists (verification stays ON via certifi).
    """
    if os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE"):
        return
    try:
        import ssl

        cafile = ssl.get_default_verify_paths().cafile
    except Exception:
        return
    if cafile and os.path.exists(cafile):
        os.environ["REQUESTS_CA_BUNDLE"] = cafile


_use_system_ca_for_requests()


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
    # Dataflow images are BUILT in a separate repo from the GKE services' build
    # repo. Empty = fall back to BUILD_REPO. Used when verifying DF image tags /
    # controls by image+tag (run-URL lookups carry their own repo).
    df_build_repo: str = Field(
        default="",
        validation_alias=AliasChoices("DF_BUILD_REPO", "DATAFLOW_BUILD_REPO"),
    )
    # GitOps repo a DATAFLOW RELEASE file-set is raised in. Distinct from
    # df_deploy_repo (which only hosts the deploy workflow) and from deploy_repo
    # (the CARE release repo): DF releases are raised separately, so they get
    # their own repo, their own release branch chain and their own in-flight
    # guard. Empty = fall back to DEPLOY_REPO, i.e. one repo for both.
    df_release_repo: str = Field(
        default="",
        validation_alias=AliasChoices("DF_RELEASE_REPO", "DATAFLOW_RELEASE_REPO"),
    )
    # Composer DAGs repo. A DF deploy builds a flex template under a VERSION path
    # in a bucket; the DAGs that launch it carry that version as the fallback of
    # `dag_run.conf['version'] | default('…')`, so the deploy is only half-done
    # until they are bumped. Empty = the DAG bump is not offered at all.
    composer_repo: str = Field(
        default="",
        validation_alias=AliasChoices("COMPOSER_REPO", "COMPOSER_DAGS_REPO"),
    )
    # Branch the DAGs live on and the bump PR targets.
    composer_branch: str = Field(
        default="main",
        validation_alias=AliasChoices("COMPOSER_BRANCH", "COMPOSER_DAGS_BRANCH"),
    )
    # Folder holding an environment's DAGs, e.g. "uat" -> uat/<dag>.py.
    composer_dag_dir_pattern: str = Field(
        default="{env}",
        validation_alias=AliasChoices("COMPOSER_DAG_DIR_PATTERN", "COMPOSER_DAG_DIR"),
    )
    # Dataflow flex-template deploys: repo hosting the DF deploy workflow. Deploying
    # means workflow_dispatch of df_deploy_workflow with {image, tag, environment}.
    df_deploy_repo: str = Field(
        default="",
        validation_alias=AliasChoices("DF_DEPLOY_REPO", "DATAFLOW_DEPLOY_REPO"),
    )
    # File name as it appears under .github/workflows/ — matched exactly, incl. case
    # and the .yml/.yaml suffix.
    df_deploy_workflow: str = Field(
        default="df-deploy.yml",
        validation_alias=AliasChoices("DF_DEPLOY_WORKFLOW", "DATAFLOW_DEPLOY_WORKFLOW"),
    )
    # Branch the dispatch runs on. GitHub only accepts a workflow_dispatch for a ref
    # that ALREADY contains the workflow file, so this is not always the repo's
    # default branch (DF repos keep the deploy workflow on main while the default
    # branch may be a env branch). Empty = the repo's default branch.
    df_deploy_ref: str = Field(
        default="",
        validation_alias=AliasChoices("DF_DEPLOY_REF", "DATAFLOW_DEPLOY_REF"),
    )
    # workflow_dispatch input names differ per team, and GitHub REJECTS a
    # dispatch carrying inputs the workflow does not declare. Map our values
    # onto the target workflow's input names with a JSON template; the
    # placeholders {image}, {tag} and {environment} are substituted. Omit a key
    # to not send it at all (e.g. a workflow with no environment input).
    #   e.g. '{"module": "{image}", "binary_version": "{tag}"}'
    df_dispatch_inputs: str = Field(
        default='{"image": "{image}", "tag": "{tag}", "environment": "{environment}"}',
        validation_alias=AliasChoices("DF_DISPATCH_INPUTS", "DATAFLOW_DISPATCH_INPUTS"),
    )
    # JIRA (read-only) — a technical account resolves the ticket a developer
    # types at queue time, so a typo'd key cannot reach the change record and the
    # ticket summary can be reused in the CHG draft. Leave the base URL empty to
    # disable the lookup entirely. The token comes from a Secret and is never
    # logged or returned to a client.
    jira_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("JIRA_BASE_URL", "JIRA_URL"),
    )
    jira_user_email: str = Field(
        default="",
        validation_alias=AliasChoices("JIRA_USER_EMAIL", "JIRA_EMAIL"),
    )
    jira_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("JIRA_API_TOKEN", "JIRA_TOKEN"),
    )
    jira_timeout_seconds: float = Field(
        default=8.0,
        validation_alias=AliasChoices("JIRA_TIMEOUT_SECONDS",),
    )
    # GitHub Enterprise Server API root (e.g. https://github.corp.internal/api/v3).
    # Empty = public github.com. Egress proxies need no setting here — PyGithub
    # rides on requests, which honors HTTPS_PROXY/NO_PROXY/REQUESTS_CA_BUNDLE.
    github_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("GITHUB_BASE_URL", "GH_BASE_URL"),
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
    # Second terminal environment branch alongside PRD (releases may target PRD,
    # PRL1, or both; prl1_only services stop at UAT/PRL1 and never reach PRD).
    prl1_branch: str = Field(
        default="PRL1",
        validation_alias=AliasChoices("PRL1_BRANCH", "RELEASE_PRL1_BRANCH"),
    )
    # --- Live release file-set model (release_details.json -> updater script) ----
    # The deployment repo carries the release as a FILE-SET present on every branch:
    # artefact-provider/artefact.json, sdlc-governance/{releaseTemplate,
    # changeRequestTemplate,RCTLDEF0000104}.json and the per-env governed deploy
    # workflows. The repo's own updater script generates them from a TRANSIENT
    # release_details.json (never committed).
    release_updater_script: str = Field(
        default="scripts/release/update_release_files.py",
        validation_alias=AliasChoices("RELEASE_UPDATER_SCRIPT"),
    )
    # Base path prepended when a developer supplies bare name:version instead of a
    # full artifactory URL (e.g. https://artifactory.../com/db/acme-ds/).
    artifactory_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("ARTIFACTORY_BASE_URL"),
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
    # Generic default; set HELM_CHART_DIR (env / private .env / Helm) to your real path.
    helm_chart_dir: str = Field(
        default="charts",
        validation_alias=AliasChoices("HELM_CHART_DIR", "RELEASE_HELM_CHART_DIR"),
    )
    # Env-specific values file: {env} -> uat/values_uat.yaml, prd/values_prd.yaml.
    helm_values_pattern: str = Field(
        default="{env}/values_{env}.yaml",
        validation_alias=AliasChoices("HELM_VALUES_PATTERN", "RELEASE_HELM_VALUES_PATTERN"),
    )
    # Default GKE namespace per environment (a deploy request may override).
    # Generic default; set UAT_NAMESPACE / PRD_NAMESPACE to your real namespaces.
    uat_namespace: str = Field(
        default="default",
        validation_alias=AliasChoices("UAT_NAMESPACE", "RELEASE_UAT_NAMESPACE"),
    )
    prd_namespace: str = Field(
        default="default",
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
    # Branches that count as "a release in flight": while any OPEN PR targets one
    # of these, add-to-release is blocked (one release at a time). Empty = just
    # the PRD branch. Comma-separated in env, e.g. RELEASE_GUARD_BRANCHES="PRD,PRL1".
    release_guard_branches: Annotated[list[str], NoDecode] = Field(
        default=[],
        validation_alias=AliasChoices("RELEASE_GUARD_BRANCHES", "PRD_GUARD_BRANCHES"),
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
    # Step/job-name prefixes that mark release controls in the build pipeline —
    # matched case-insensitively against step AND job names. The live gate is
    # RCTLD, which covers the RCTLDEF0001691-style SDLC controls; RLFT/RFTL are
    # the demo-scaffolding names. xSecurity-Gatekeeper is deliberately NOT here:
    # gating on a scanner that is not part of the release sign-off would refuse
    # builds the release process considers eligible. Comma-separated env override.
    control_prefixes: Annotated[list[str], NoDecode] = Field(
        default=["RCTLD", "RLFT", "RFTL"],
        validation_alias=AliasChoices("CONTROL_PREFIXES", "RELEASE_CONTROL_PREFIXES"),
    )
    # Block a PRD release when any build control failed (fail-closed). When a build
    # run can't be located we don't hard-block; the agent asks for the run id.
    prd_require_controls: bool = Field(
        default=True,
        validation_alias=AliasChoices("PRD_REQUIRE_CONTROLS", "RELEASE_PRD_REQUIRE_CONTROLS"),
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

    # --- ADK 2.x runtime features (all env-overridable, safe defaults) ----------
    # Context caching: cache the large static prefix (root instruction + skill
    # catalog) to cut latency/cost. Only caches context above min_tokens.
    adk_context_cache: bool = Field(
        default=True,
        validation_alias=AliasChoices("ADK_CONTEXT_CACHE", "RELEASE_ADK_CONTEXT_CACHE"),
    )
    # Gemini 2.5 Flash caches from 1024 tokens; the old 2048 default sat ABOVE
    # this app's prompt prefix (~1.1k tokens), so nothing was ever cached
    # ("Previous request too small for caching (1079 < 2048)"). 1024 = cache
    # from the smallest size the model supports.
    adk_context_cache_min_tokens: int = Field(
        default=1024,
        validation_alias=AliasChoices(
            "ADK_CONTEXT_CACHE_MIN_TOKENS", "RELEASE_ADK_CONTEXT_CACHE_MIN_TOKENS"
        ),
    )
    adk_context_cache_ttl_seconds: int = Field(
        default=1800,
        validation_alias=AliasChoices(
            "ADK_CONTEXT_CACHE_TTL_SECONDS", "RELEASE_ADK_CONTEXT_CACHE_TTL_SECONDS"
        ),
    )
    # Event compaction: summarize older events on long chat sessions to avoid
    # context overflow. compaction_interval/overlap_size are required by ADK.
    adk_event_compaction: bool = Field(
        default=True,
        validation_alias=AliasChoices("ADK_EVENT_COMPACTION", "RELEASE_ADK_EVENT_COMPACTION"),
    )
    adk_compaction_interval: int = Field(
        default=20,
        validation_alias=AliasChoices("ADK_COMPACTION_INTERVAL", "RELEASE_ADK_COMPACTION_INTERVAL"),
    )
    adk_compaction_overlap: int = Field(
        default=3,
        validation_alias=AliasChoices("ADK_COMPACTION_OVERLAP", "RELEASE_ADK_COMPACTION_OVERLAP"),
    )
    # Long-term memory: preload relevant memories each turn and persist finished
    # chat sessions to the (in-memory) memory service.
    adk_memory_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("ADK_MEMORY_ENABLED", "RELEASE_ADK_MEMORY_ENABLED"),
    )
    # Where ADK chat sessions live. "memory" is per-pod: a restart loses the
    # conversation and a second replica sees a different history, which is why
    # the chart pins replicaCount: 1. "vertex" stores them in a Vertex AI Agent
    # Engine, so history survives restarts and is shared across pods.
    #
    # NOTE this covers CONVERSATION HISTORY ONLY. The pending-CONFIRM maps and
    # the per-thread GitHub PAT are still process-local, so switching this alone
    # does NOT make the app safe to run with replicaCount > 1.
    adk_session_backend: str = Field(
        default="memory",
        validation_alias=AliasChoices("ADK_SESSION_BACKEND", "RELEASE_ADK_SESSION_BACKEND"),
    )
    # Agent Engine (reasoning engine) holding the sessions. Pin an exact one with
    # VERTEX_AGENT_ENGINE_ID (bare id or full resource name) — otherwise the
    # engine is resolved BY DISPLAY NAME in gcp_location and created if absent,
    # so the same values file works in every project without anyone pasting an
    # opaque numeric id.
    vertex_agent_engine_id: str = Field(
        default="",
        validation_alias=AliasChoices("VERTEX_AGENT_ENGINE_ID", "AGENT_ENGINE_ID"),
    )
    vertex_agent_engine_name: str = Field(
        default="release-copilot-sessions",
        validation_alias=AliasChoices("VERTEX_AGENT_ENGINE_NAME", "AGENT_ENGINE_NAME"),
    )
    # Require human confirmation before high-impact prod ops (prod remove / merge
    # PRD release) via ADK tool confirmation.
    adk_confirm_prod_ops: bool = Field(
        default=True,
        validation_alias=AliasChoices("ADK_CONFIRM_PROD_OPS", "RELEASE_ADK_CONFIRM_PROD_OPS"),
    )

    # --- Release intake queue (BigQuery) -----------------------------------------
    # Devs register "put me in the next release" any day of the week; DevOps sees
    # the accumulated list when creating the release. Stored as an APPEND-ONLY
    # event table in BigQuery (no Cloud SQL needed at this volume). Empty dataset
    # name disables the feature entirely (all queue endpoints report disabled).
    # The table is provisioned SEPARATELY (terraform module + JSON schema in
    # bigquery/); dataset + table names arrive via env / Helm values.
    # BQ can live in a DIFFERENT GCP project than Vertex (GOOGLE_CLOUD_PROJECT)
    # — e.g. GKE in project A, Vertex in B, the data warehouse in C. Empty =
    # same project as Vertex. The GSA needs BQ roles in whichever project this is.
    bq_project: str = Field(
        default="",
        validation_alias=AliasChoices("BQ_PROJECT", "RELEASE_BQ_PROJECT"),
    )
    bq_dataset: str = Field(
        default="release_agent",
        validation_alias=AliasChoices("BQ_DATASET", "RELEASE_BQ_DATASET"),
    )
    bq_table: str = Field(
        default="release_intents",
        validation_alias=AliasChoices("BQ_TABLE", "RELEASE_BQ_TABLE"),
    )
    bq_location: str = Field(
        default="US",
        validation_alias=AliasChoices("BQ_LOCATION", "RELEASE_BQ_LOCATION"),
    )
    # Dev convenience only: create the dataset/table on first touch. Keep FALSE in
    # cluster deployments — there the table is created separately (DDL/terraform)
    # and the runtime service account needs no schema-mutation permissions.
    bq_auto_create: bool = Field(
        default=False,
        validation_alias=AliasChoices("BQ_AUTO_CREATE", "RELEASE_BQ_AUTO_CREATE"),
    )

    # --- Console links (read-only deep links shown in the UI) --------------------
    # Where a human goes to LOOK at what a release produced: the GKE workload view
    # and the Grafana dashboards, PER ENVIRONMENT. The portal never calls these —
    # it only renders them.
    #
    # JSON, environment name -> ordered list of links (same convention as
    # DF_DISPATCH_INPUTS). A per-env LIST rather than one field per link because
    # each environment carries its own handful of dashboards, and that set grows
    # without a code change:
    #   {"UAT": [{"label": "GKE", "url": "https://..."},
    #            {"label": "Latency", "url": "https://..."}],
    #    "PRD": [...]}
    # Key order is the tab order. Empty = the placeholder set below, so the strip
    # still renders and names what is left to fill in. Parsed lazily by the
    # endpoint, never at import: a typo here must not stop the app from booting.
    console_links: str = Field(
        default="",
        validation_alias=AliasChoices("CONSOLE_LINKS", "RELEASE_CONSOLE_LINKS"),
    )

    # App metadata (used by FastAPI)
    app_title: str = "Dev Portal"
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

    @field_validator("control_prefixes", "release_guard_branches", mode="before")
    @classmethod
    def _split_list_setting(cls, v):
        """Accept a comma-separated string (env) or a JSON array for list settings
        (control prefixes, release-guard branches)."""
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
