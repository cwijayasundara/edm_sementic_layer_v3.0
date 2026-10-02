#!/usr/bin/env bash
# Prism UI: check Node >= 20, install dependencies when needed, run next dev on :3000.
set -euo pipefail
cd "$(dirname "$0")/../frontend"

if ! command -v node >/dev/null 2>&1; then echo "Node.js >= 20 is required" >&2; exit 1; fi
major=$(node -p 'process.versions.node.split(".")[0]')
if [ "$major" -lt 20 ]; then echo "Node.js >= 20 is required (found $(node -v))" >&2; exit 1; fi

if [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then npm ci; fi

agent="${NEXT_PUBLIC_AGENT_URL:-http://localhost:8000}"
if ! curl -fsS --max-time 2 "$agent/healthz" >/dev/null 2>&1; then
  echo "warning: the agent is not answering at $agent (start it with scripts/start_backend.sh)" >&2
fi
exec npm run dev
