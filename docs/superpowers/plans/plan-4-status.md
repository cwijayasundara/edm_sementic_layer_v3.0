# Plan 4 status (handoff)

Updated 2026-10-01. Plan: `2026-10-01-plan-4-agent-service.md`. Branch `plan-4-agent-service-v2` (not merged, never
pushed). Execution: subagent-driven development (implementer plus reviewer per task), then one whole-branch review and
one final fix wave. What M5 (UI) and M6 consume: `plan-4-carry-forward.md`.

## Done
| Task | What | Commits |
|---|---|---|
| 1 | Agent settings, `anthropic` dependency, `app.agent_runs` migration | `a40e01d` |
| 2 | `ModelClient` protocol, scripted fake, Anthropic client (cache breakpoints) | `ca580c9` |
| 3 | Gateway MCP client, typed error classes, `FakeGateway` | `8382153`, fix `e60bf5c` |
| 4 | `DashboardSpec`, validation against seen handles, table fallback | `9494371` |
| 5 | `MessagesRunner` loop (caps, parallel tools, metering) | `caea609`, fix `aae73d7` |
| 6 | Persona JWT auth, `agent_runs` telemetry writer | `0e468d7` |
| 7 | Tool handlers, delegate and visualize subagents, frozen prompts | `4713ba5` |
| 8 | `AgentService.chat` (fast path, delegation, one-step escalation, fallback, telemetry) | `7e94ccd`, fix `0c255e2` |
| 9 | FastAPI app: `/chat` SSE, `/kpis`, `/results`, `/dev/token` | `e061109`, fix `7e955c6` |
| 10 | Live checks: security matrix, planted stories through `/chat`, KPI tiles | `6ceae65` |
| 11 | Wiring: Procfile, launcher, `make agent`, README | `44ea5e7` |
| fix wave | Lifecycle, KPI/launcher, model hardening, prompts/tools, docs | `fabf8e0`, `1203b6a`, `d849467`, `0de47da`, then the TrustedHost + docs commit |

Fix wave in one line each: the supervisor task is cancelled before the gateway session closes and the run is written
(`contextlib.aclosing`); the summary is the supervisor's text; `record_answer` sends only the metric handles the widgets
rest on and is skipped for fallback/refusal/error runs; KPI tiles scale fractions to percent and pin the USD total, the
KPI cache is keyed on a scope digest; the launcher honours `false` from `.env` and rejects bad agent ports;
`ModelError` keeps status/type/detail for the log, 408/409/504 retry, back-off 0.5 s then 1 s honouring `retry-after`;
`max_tokens` 16000 with `model_truncated` and `refusal` handling; thinking blocks round-trip and empty text blocks are
dropped; a third cache breakpoint on the newest message; "Data as of" in the run context; per-caller gateway
concurrency gates; whole 20,000-char context packs; TrustedHost guard.

## Tests
- Default suite (`cd backend && HF_HUB_OFFLINE=1 uv run pytest -q -W error`): 2231 passed, 23 deselected at the
  last code commit (baseline before the fix wave 2186). `uv run ruff check prism tests` clean.
- Live (`make test-live`, stack up, deselected by default): 23 live-marked tests deselected overall, of which 10 are
  `tests/agent` (security matrix and handle isolation, the two planted stories through `/chat` with a scripted model,
  one KPI-tile check per persona). The fix wave changed the KPI live assertions (units, 0..100 for percent tiles) and
  the host used by the live ASGI client (`http://localhost`); they were NOT run in the fix wave (stack-free).
- No test calls the real Anthropic API; every model response is scripted.

## How to run
1. Stack: `docker compose up -d --wait postgres neo4j` (this project's services only), then `scripts/start_backend.sh`
   starts everything including the agent on `PRISM_AGENT_PORT` (default 8000). It binds 127.0.0.1 and turns
   `/dev/token` on unless the environment or `.env` says `PRISM_AGENT_DEV_TOKEN_ENABLED=false`.
   **Never pass `--reseed`** (it rebuilds every source database) without asking.
2. Agent alone: `make agent` (needs the gateway running; `/dev/token` stays off unless the flag is set;
   `/chat` answers 503 without `ANTHROPIC_API_KEY`).
3. Try it:
```bash
T=$(curl -s -XPOST localhost:8000/dev/token -H 'content-type: application/json' -d '{"persona_id":"head_data"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s localhost:8000/kpis -H "Authorization: Bearer $T"
curl -sN -XPOST localhost:8000/chat -H "Authorization: Bearer $T" -H 'content-type: application/json' \
  -d '{"question":"How many open breaks are there by region?"}'
curl -s "localhost:8000/results/r_0123456789ab?offset=0&limit=20" -H "Authorization: Bearer $T"
```
4. Live suite: `make test-live`. It writes `agent-live-*` rows into the real app database
   (`app.audit_log`, `app.agent_runs`, `app.query_log`); that is expected.
