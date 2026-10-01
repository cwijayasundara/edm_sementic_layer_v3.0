#!/usr/bin/env bash
# Start the Prism backend: Postgres + Neo4j (Docker), seed on first run (or when keys changed), the embedding model
# and the context graph when missing or stale, then all services (APIs, source MCP servers, the gateway).
# Usage: scripts/start_backend.sh [--reseed]
# Never resets the context graph: it is loaded only when `prism.graph.cli check` says it is empty or was written by an
# older loader (exit 1); "could not tell" (exit 2) stops the script instead.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RESEED=0
for arg in "$@"; do
  case "$arg" in
    --reseed) RESEED=1 ;;
    *) echo "Unknown option: $arg (supported: --reseed)" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
docker info >/dev/null 2>&1 || die "the Docker daemon is not running - start Docker Desktop and retry"
command -v uv >/dev/null 2>&1 || die "uv is not installed - see https://docs.astral.sh/uv/getting-started/installation/"
command -v openssl >/dev/null 2>&1 || die "openssl is required to generate local secrets"

# The value of NAME in .env ("" when absent or empty): `export NAME=...`, quotes and surrounding blanks allowed.
env_value() {
  local name="$1" line value
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" .env 2>/dev/null | tail -n 1)" || true
  value="${line#*=}"
  value="${value#"${value%%[![:space:]]*}"}"   # trim leading blanks
  value="${value%"${value##*[![:space:]]}"}"   # trim trailing blanks
  if [[ ${#value} -ge 2 && ( ${value:0:1} == '"' && ${value: -1} == '"' || ${value:0:1} == "'" && ${value: -1} == "'" ) ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

if [[ ! -f .env ]]; then
  (umask 077 && cp .env.example .env)
  echo "Created .env from .env.example"
fi
chmod 600 .env  # holds local secrets: owner read/write only

# The gateway port: the environment, else .env, else 8200 (honcho's Procfile reads the exported value).
PRISM_GATEWAY_PORT="${PRISM_GATEWAY_PORT:-$(env_value PRISM_GATEWAY_PORT)}"
PRISM_GATEWAY_PORT="${PRISM_GATEWAY_PORT:-8200}"
[[ "$PRISM_GATEWAY_PORT" =~ ^[0-9]{1,5}$ ]] || die "PRISM_GATEWAY_PORT must be a port number"
export PRISM_GATEWAY_PORT

# The agent service port: the environment, else .env, else 8000.
PRISM_AGENT_PORT="${PRISM_AGENT_PORT:-$(env_value PRISM_AGENT_PORT)}"
PRISM_AGENT_PORT="${PRISM_AGENT_PORT:-8000}"
[[ "$PRISM_AGENT_PORT" =~ ^[0-9]{1,5}$ ]] || die "PRISM_AGENT_PORT must be a port number"
(( 10#$PRISM_AGENT_PORT >= 1 && 10#$PRISM_AGENT_PORT <= 65535 )) || die "PRISM_AGENT_PORT must be between 1 and 65535"
(( 10#$PRISM_AGENT_PORT != 10#$PRISM_GATEWAY_PORT )) || die "PRISM_AGENT_PORT must differ from PRISM_GATEWAY_PORT ($PRISM_GATEWAY_PORT)"
export PRISM_AGENT_PORT
# POST /dev/token (persona token mint): the environment, else .env, else on (this launcher binds 127.0.0.1 only);
# an explicit `false` in either place is honoured.
PRISM_AGENT_DEV_TOKEN_ENABLED="${PRISM_AGENT_DEV_TOKEN_ENABLED:-$(env_value PRISM_AGENT_DEV_TOKEN_ENABLED)}"
# env_value keeps a trailing `# comment`; strip it, blanks and quotes, then accept only a plain boolean.
flag="${PRISM_AGENT_DEV_TOKEN_ENABLED%%#*}"
flag="${flag//[[:space:]\"\']/}"
flag="$(printf '%s' "$flag" | tr '[:upper:]' '[:lower:]')"
case "$flag" in
  "") flag=true ;;
  true|1|yes) flag=true ;;
  false|0|no) flag=false ;;
  *) die "PRISM_AGENT_DEV_TOKEN_ENABLED must be true or false (got: ${PRISM_AGENT_DEV_TOKEN_ENABLED})" ;;
esac
export PRISM_AGENT_DEV_TOKEN_ENABLED="$flag"

for port in 8101 8102 "$PRISM_GATEWAY_PORT" "$PRISM_AGENT_PORT" 8201 8202 8203 8204 8205; do
  if lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $port is already in use - is the backend already running?"
  fi
done

ensure_secret() {
  # Set = a non-empty value after stripping `export`, blanks and quotes: `KEY=`, `KEY=""` and `export KEY=` are empty.
  local name="$1" tmp
  if [[ -n "$(env_value "$name")" ]]; then
    return
  fi
  if grep -qE "^[[:space:]]*(export[[:space:]]+)?${name}=" .env; then
    # Present but empty: fill it in place (every such line, so a later empty one cannot win). The secret is passed
    # via the environment, never on a command line.
    tmp="$(umask 077 && mktemp .env.XXXXXX)"
    PRISM_NEW_SECRET="$(openssl rand -hex 32)" awk -v name="$name" '
      BEGIN { v = ENVIRON["PRISM_NEW_SECRET"]; re = "^[[:space:]]*(export[[:space:]]+)?" name "=" }
      $0 ~ re { prefix = ($0 ~ /^[[:space:]]*export[[:space:]]/) ? "export " : ""; print prefix name "=" v; next }
      { print }' .env > "$tmp"
    mv "$tmp" .env
  else
    [[ -s .env && -n "$(tail -c1 .env)" ]] && echo >> .env  # keep the new line off the end of an unterminated one
    echo "${name}=$(openssl rand -hex 32)" >> .env
  fi
  echo "Generated ${name} in .env"
}
ensure_secret PRISM_CTX_HMAC_KEY
ensure_secret PRISM_JWT_SECRET
ensure_secret PRISM_AUDIT_HMAC_KEY

echo "Starting Postgres and Neo4j..."
docker compose up -d --wait postgres neo4j \
  || die "Postgres/Neo4j did not become healthy (see: docker compose logs postgres neo4j)"

cd backend
uv sync --quiet
if [[ $RESEED -eq 1 ]]; then
  uv run python -m prism.sim.cli --reset
else
  # --check: 0 = seeded, 1 = definitely not seeded (or key changed), 2/other = could not tell.
  # Only a definite "not seeded" may trigger the destructive re-seed.
  set +e
  uv run python -m prism.sim.cli --check
  rc=$?
  set -e
  case $rc in
    0) ;;
    1)
      echo "Seeding simulated platforms (first run, or security key changed)..."
      uv run python -m prism.sim.cli --reset
      ;;
    *)
      die "could not determine whether the databases are seeded (--check exit code $rc, see the error above); not re-seeding. Fix the problem or run: scripts/start_backend.sh --reseed"
      ;;
  esac
fi

# The container healthcheck runs inside the container; poll the published bolt port the gateway uses.
echo "Waiting for Neo4j (bolt)..."
BOLT_WAIT_S="${PRISM_BOLT_WAIT_S:-60}"
deadline=$((SECONDS + BOLT_WAIT_S))
until uv run python -m prism.graph.cli ping >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    # one last attempt with its output visible: an auth failure (wrong PRISM_NEO4J_PASSWORD) is not "not up yet",
    # and retrying a bad password can lock the account
    uv run python -m prism.graph.cli ping || true
    die "Neo4j did not answer on bolt within ${BOLT_WAIT_S} s (see the error above and: docker compose logs neo4j)"
  fi
  sleep 2
done

# The real model file, not just a non-empty directory: an interrupted download leaves metadata without the weights.
if [[ -z "$(find -L .models -name model_optimized.onnx -type f -size +0 2>/dev/null | head -n 1)" ]]; then
  echo "Downloading the embedding model into backend/.models (first run or incomplete cache, needs network)..."
  uv run python -m prism.graph.embedder download --cache-dir .models || die "model download failed; retry with: make models"
fi

# check: 0 = current, 1 = empty or older than GRAPH_SCHEMA_VERSION (load it), 2/other = could not tell (stop).
set +e
HF_HUB_OFFLINE=1 uv run python -m prism.graph.cli check
rc=$?
set -e
case $rc in
  0) ;;
  1)
    echo "Loading the context graph..."
    HF_HUB_OFFLINE=1 uv run python -m prism.graph.cli load >/dev/null || die "context graph load failed (see the error above)"
    ;;
  *)
    die "could not determine the context graph state (check exit code $rc, see the error above); not loading. Fix the problem or run: make graph"
    ;;
esac

echo "Starting services (Ctrl-C to stop):"
echo "  RefMaster API      http://127.0.0.1:8101/docs"
echo "  MarketMaster API   http://127.0.0.1:8102/docs"
echo "  MCP servers        http://127.0.0.1:8201..8205/mcp  (refmaster, marketmaster, cashrecon, assetrecon, feedhub)"
echo "  Semantic gateway   http://127.0.0.1:${PRISM_GATEWAY_PORT}/mcp"
echo "  Agent service      http://127.0.0.1:${PRISM_AGENT_PORT}/healthz  (/chat, /kpis, /results; /dev/token enabled=${PRISM_AGENT_DEV_TOKEN_ENABLED}, local only)"
echo "  smoke test:        (cd backend && uv run python -m prism.gateway.cli list --as head_data)"
exec uv run honcho start -f Procfile
