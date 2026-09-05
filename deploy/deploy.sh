#!/usr/bin/env bash
# Runs on the VPS. Called by the GitHub Actions workflow over SSH on every
# push to main. Safe to run manually too.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "==> Pulling latest code"
git fetch origin main
git reset --hard origin/main

echo "==> Installing dependencies (only if requirements.txt changed)"
source venv/bin/activate
if git diff --name-only HEAD@{1} HEAD 2>/dev/null | grep -q requirements.txt; then
  pip install -r requirements.txt
fi

echo "==> Writing version marker"
GIT_SHA=$(git rev-parse --short HEAD)
GIT_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "untagged")
echo "${GIT_TAG} (${GIT_SHA}) — deployed $(date -u '+%Y-%m-%d %H:%M UTC')" > VERSION

echo "==> Restarting service"
sudo systemctl restart studio

echo "==> Done. Deployed ${GIT_TAG} (${GIT_SHA})"
