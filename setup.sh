#!/usr/bin/env bash
# setup.sh
# Completely isolated setup for the Release Copilot project.
# Everything (Python, packages) stays inside this directory's .venv

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Setting up isolated environment in $SCRIPT_DIR/.venv"

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "Created .venv"
fi

# Activate
source .venv/bin/activate

echo "==> Upgrading pip inside venv"
pip install --upgrade pip setuptools wheel

echo "==> Installing project dependencies (isolated to .venv)"
pip install -r requirements.txt

# Optional: install in editable mode if using pyproject
if [ -f "pyproject.toml" ]; then
    pip install -e .
fi

echo ""
echo "✅ Isolated setup complete!"
echo ""
echo "To use:"
echo "  source .venv/bin/activate"
echo "  export GOOGLE_CLOUD_PROJECT=your-project-id   # or let gcloud auto-detect"
echo "  export GOOGLE_CLOUD_LOCATION=us-central1"
echo "  # gcloud auth application-default login   (uses your already installed gcloud)"
echo "  export RELEASE_AGENT_TARGET_REPO=phaniuk111/devops"
echo ""
echo "Run the app:"
echo "  python -m src.release_agent.cli"
echo "  # or"
echo "  uvicorn src.release_agent.app_fastapi:app --reload --port 8000"
echo ""
echo "To deactivate when done: deactivate"