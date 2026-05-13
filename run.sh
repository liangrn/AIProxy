#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

: "${LISTEN_HOST:=127.0.0.1}"
: "${LISTEN_PORT:=8383}"

if [[ -n "${PYTHON:-}" ]]; then
  exec "$PYTHON" -m uvicorn app.main:app --host "$LISTEN_HOST" --port "$LISTEN_PORT"
fi

if command -v python3 >/dev/null 2>&1 && python3 -c "import uvicorn" >/dev/null 2>&1; then
  exec python3 -m uvicorn app.main:app --host "$LISTEN_HOST" --port "$LISTEN_PORT"
fi

if command -v python >/dev/null 2>&1; then
  exec python -m uvicorn app.main:app --host "$LISTEN_HOST" --port "$LISTEN_PORT"
fi

if command -v conda >/dev/null 2>&1; then
  : "${CONDA_ENV:=iai}"
  exec conda run --no-capture-output -n "$CONDA_ENV" python -m uvicorn app.main:app --host "$LISTEN_HOST" --port "$LISTEN_PORT"
fi

echo "No usable Python found. Install dependencies or set PYTHON=/path/to/python." >&2
exit 127
