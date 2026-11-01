#!/usr/bin/env bash
# sync-venv-dev.sh — Sync the venv-dev virtual environment for DimOS development.
#
# Usage:
#   bash scripts/sync-venv-dev.sh
#
# This script:
# 1. Creates or updates the venv-dev virtual environment.
# 2. Installs the DimOS package with all extras (sim, web, perception, etc.).
# 3. Pulls LFS files (mujoco_sim.tar.gz, etc.) if available.
#
# Prerequisites:
# - uv (https://github.com/astral-sh/uv)
# - Git LFS (https://git-lfs.com)
# - Network access to the LFS server (lfs.dimensionalos.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DEV="$REPO_ROOT/venv_dev"

echo "=== Syncing venv-dev ==="
echo "Repo root: $REPO_ROOT"
echo "Venv dev: $VENV_DEV"

# Step 1: Create or update the venv-dev virtual environment.
echo ""
echo "Step 1: Creating/updating venv-dev..."
if [[ ! -d "$VENV_DEV" ]]; then
  echo "  venv-dev does not exist. Creating..."
  uv venv "$VENV_DEV" --python 3.12
else
  echo "  venv-dev exists. Updating..."
fi

# Step 2: Install DimOS with all extras.
echo ""
echo "Step 2: Installing DimOS with all extras..."
uv pip install -p "$VENV_DEV/bin/python" -e ".[sim,web,perception,visualization,misc,dev]"

# Step 3: Pull LFS files.
echo ""
echo "Step 3: Pulling LFS files..."
if command -v git-lfs >/dev/null 2>&1; then
  echo "  Pulling LFS files (this may take a while)..."
  git -C "$REPO_ROOT" lfs pull --include="data/.lfs/*" || echo "  WARNING: LFS pull failed. Check your LFS credentials."
else
  echo "  WARNING: git-lfs not found. Skipping LFS pull."
fi

echo ""
echo "=== Sync complete ==="
echo "Activate venv-dev with: source $VENV_DEV/bin/activate"
echo "Run simulation: dimos --simulation run unitree-go2"
