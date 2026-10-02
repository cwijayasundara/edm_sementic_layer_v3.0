.PHONY: test test-fast test-mcp test-graph test-gateway test-live seed reseed db models graph distill gateway agent clean-test-dbs frontend test-ui e2e eval eval-check

# Databases the test suite creates under its own prefixes (backend/tests/db_cleanup.py); never test_* or real ones.
TEST_DB_REGEX = ^(testapp|testappnew|testmig|testhist|testhistcli)_

db:
	docker compose up -d --wait postgres neo4j

test: db
	cd backend && uv run pytest -q

test-fast:
	cd backend && uv run pytest -q tests/sim tests/test_config.py tests/security

test-mcp: db
	cd backend && uv run pytest -q tests/mcp

test-graph: db
	cd backend && uv run pytest -q -m neo4j

test-gateway: db
	cd backend && HF_HUB_OFFLINE=1 uv run pytest -q tests/gateway

# Live end-to-end checks through the running gateway (start it first: scripts/start_backend.sh; full profile).
test-live:
	cd backend && HF_HUB_OFFLINE=1 uv run pytest -q -m live -rs

seed: db
	cd backend && uv run python -m prism.sim.cli

reseed: db
	cd backend && uv run python -m prism.sim.cli --reset

models:
	cd backend && uv run python -m prism.graph.embedder download --cache-dir .models

graph: db
	cd backend && HF_HUB_OFFLINE=1 uv run python -m prism.graph.cli load

# Query history -> context graph (verified, metric-backed app.query_log rows). Re-run after every `make graph`.
distill: db
	cd backend && HF_HUB_OFFLINE=1 uv run python -m prism.graph.cli distill

# The gateway alone (scripts/start_backend.sh starts it with everything else); needs `make models` and `make graph`.
gateway: db
	cd backend && uv run uvicorn prism.gateway.server:create_app_from_env --factory --host 127.0.0.1 --port $${PRISM_GATEWAY_PORT:-8200}

# The agent service alone (HTTP: /chat SSE, /kpis, /results); needs the gateway running (scripts/start_backend.sh).
# /dev/token stays off unless PRISM_AGENT_DEV_TOKEN_ENABLED=true; /chat answers 503 without ANTHROPIC_API_KEY.
agent: db
	cd backend && uv run uvicorn prism.agent.api:create_app_from_env --factory --host 127.0.0.1 --port $${PRISM_AGENT_PORT:-8000}

# Drop leftover test databases (only names matching TEST_DB_REGEX) in this project's compose Postgres.
clean-test-dbs: db
	@docker compose exec -T postgres psql -U postgres -Atc "SELECT datname FROM pg_database WHERE datname ~ '$(TEST_DB_REGEX)' ORDER BY 1" | \
	while read -r name; do \
	  echo "dropping $$name"; \
	  docker compose exec -T postgres psql -U postgres -q -c "DROP DATABASE IF EXISTS \"$$name\" WITH (FORCE)" < /dev/null || exit 1; \
	done

# The UI on :3000 (scripts/start_frontend.sh); talks to the agent at NEXT_PUBLIC_AGENT_URL (default :8000).
frontend:
	scripts/start_frontend.sh

test-ui:
	cd frontend && npm run lint && npm run typecheck && npm test

# Playwright smoke against a mocked agent (no stack, no API key). PRISM_E2E_LIVE=1 adds the live login + KPI check.
e2e:
	cd frontend && npx playwright install chromium && npm run e2e

# Live evals: golden + red-team questions through the running agent and the real model (costs API money; needs
# scripts/start_backend.sh and ANTHROPIC_API_KEY). Reports in backend/evals/reports/. Exit 1 on any red-team leak.
eval:
	cd backend && HF_HUB_OFFLINE=1 uv run python -m prism.evals.cli --suite all

# The eval case files against the live seed, without the model: references replay, stories hold, canaries are placed.
eval-check:
	cd backend && HF_HUB_OFFLINE=1 uv run python -m prism.evals.cli --check-references
