# syntax=docker/dockerfile:1.9
###############################################################################
# Release Copilot image — dependencies installed with uv from pyproject.toml +
# uv.lock (fully pinned, reproducible). Runs the FastAPI production UI on :8000
# (matches the Helm chart + /health probe).
###############################################################################

# ---- builder: install locked deps into a self-contained venv ----
FROM python:3.11-slim AS builder

# Pinned uv binary from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:0.6.17 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Install exactly what's pinned in uv.lock (no project build — package = false).
# Cached on pyproject.toml + uv.lock so src changes don't rebuild the deps layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: minimal image, just the venv + app source ----
FROM python:3.11-slim AS runtime

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
