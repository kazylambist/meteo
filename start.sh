#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"  # se place dans le dossier du projet

# venv si absent (ne fait rien s'il existe déjà)
if [ ! -d ".venv" ]; then
  /usr/bin/env python3 -m venv .venv
fi
source .venv/bin/activate

# DB par défaut si non fournie
export DATABASE_URL="${DATABASE_URL:-sqlite:///$PWD/instance/moodspec.db}"
mkdir -p instance

# log utile
echo "[run] PWD=$PWD"
echo "[run] DATABASE_URL=$DATABASE_URL"

# lance l'app
python3 mood-speculator-v2.py