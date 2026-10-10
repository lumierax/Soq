#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
if [[ ! -x ../.venv/bin/python ]]; then
  python3 -m venv ../.venv
  ../.venv/bin/pip install -r requirements.txt
fi
if [[ -f .env ]]; then
  exec ../.venv/bin/uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
fi
exec ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
