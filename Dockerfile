###############################################################################
# Release Copilot image — uv-based, enterprise-friendly.
#
# External touchpoints are ONLY: (1) the base image, (2) your Python package
# index. Both are build-args so they can point at internal mirrors — there is no
# pull from ghcr.io / docker.io/uv and no BuildKit frontend image.
#
# Internal-mirror build example:
#   docker build \
#     --build-arg BASE_IMAGE=registry.internal/library/python:3.11-slim \
#     --build-arg PIP_INDEX_URL=https://artifactory.internal/api/pypi/pypi-remote/simple \
#     -t release-copilot .
###############################################################################
ARG BASE_IMAGE=python:3.11-slim

# ---- builder: install locked deps into a self-contained venv ----
FROM ${BASE_IMAGE} AS builder

ARG UV_VERSION=0.6.17
# Default to public PyPI; override with your internal mirror for air-gapped builds.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URL=

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL} \
    UV_INDEX_URL=${PIP_INDEX_URL} \
    UV_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL} \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Install uv from the (mirror-able) Python index — no external container registry.
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# Install exactly what's pinned in uv.lock (no project build — package = false).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

# ---- runtime: minimal image, just the venv + app source ----
FROM ${BASE_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Bring over the prebuilt venv and the application source only (no build tools, no uv).
COPY --from=builder /opt/venv /opt/venv
COPY src/ ./src/
COPY .env.example ./

# Run as a non-root user.
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# GitHub auth comes from the GH_TOKEN env var (set by the Helm chart's Secret);
# the in-cluster image intentionally omits the `gh` CLI fallback.
CMD ["uvicorn", "release_agent.app_fastapi:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
