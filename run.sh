#!/bin/sh
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

for command in uv node npm; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Missing $command. Install uv and Node.js 22 or newer, then run ./run.sh again." >&2
        exit 1
    fi
done

uv sync --locked
npm --prefix web ci --no-audit --no-fund
PUBLIC_API_BASE= npm --prefix web run build

echo "Open http://127.0.0.1:${PORT:-8001} — press Ctrl+C to stop."
exec uv run --no-sync uvicorn api.main:app --host 127.0.0.1 --port "${PORT:-8001}"
