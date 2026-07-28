#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/var/www/api_sensores/venv/bin/python}"

cd "$REPO_DIR"
"$PYTHON_BIN" scripts/export_monthly_csv.py

if [[ -z "$(git status --porcelain -- data)" ]]; then
  exit 0
fi

git add data
git commit -m "datos: actualización horaria"
git push origin main
