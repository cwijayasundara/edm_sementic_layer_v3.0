# Plan 5 status (handoff)

Updated 2026-10-02. Plan: `2026-10-01-plan-5-ui.md`; spec: `docs/superpowers/specs/2026-10-01-m5-ui-design.md`. Branch
`plan-5-ui` (from main `5480993`, not merged, never pushed). Execution: subagent-driven development (implementer plus
reviewer per task, fix rounds where a review found something). What M6 consumes: `plan-5-carry-forward.md`.

## Done
| Task | What | Commits |
|---|---|---|
| 1 | Agent records a recipe per result handle; `widget` event carries `handle_info.recipe` | `1f1fcd9` |
| 2 | Recipe validation (depth/node bounds) and replay through the gateway as the caller | `c4e5e22`, fix `ec1279a` |
| 3 | `app.saved_dashboards` migration, narrower grant, per-caller store | `8ee63b7` |
| 4 | `/dashboards` save, list, delete, run; CORS allows DELETE | `7605370` |
| 5 | Next.js scaffold, contract schemas, SSE reader, start script | `a11f381`, fix `4b34fb0` |
| 6 | Agent API client, display-only session, formatting, recipe views | `8343f78` |
| 7 | Deterministic widget to ECharts option mapper and theme | `5e97f91` |
| 8 | Chat turn and canvas reducers | `b869b5d` |
| 9 | Persona login, session provider with 401 handling, header chips, KPI strip | `bba5ede`, fix `555f5db` |
| 10 | Widget cards, canvas grid, provenance drawer with paged rows | `ba11e81`, fix `2ab1011` |
| 11 | Streaming assistant panel (plan steps, stop, telemetry line) | `af559b1`, fixes `26e0852`, `30637d5` |
| 12 | Saved-dashboards menu and the full workspace | `1a1e73f`, fix `d6a8d7f` |
| 13 | Playwright smoke (mocked agent plus live login/KPI), README UI section, these docs | this commit |

## Rulings made during the build
- Recipe argument bounds import the gateway's own limits instead of copying numbers, so the two cannot drift.
- Stream tests (`sse`, `api`) run in the node vitest environment (jsdom's `TextEncoder` is rejected by Node's
  `TextDecoderStream`).
- CORS now allows GET/POST/DELETE (browser delete of saved dashboards); the origin is still exactly
  `http://localhost:3000`.
- `next-env.d.ts` stays untracked (Next regenerates it); `frontend/.env.example` is tracked.
- Scatter and heatmap tooltips carry units, as the spec requires.
- `stop()` clears the active-run marker so a question can be asked straight after Stop (test fails without the fix).
- "Save pinned" is disabled with an explanatory title above 8 pinned widgets (backend cap, `422`).
- Task 13 selector deviations from the brief: Playwright `getByRole` name matching is a substring match, so the
  assertions use `exact: true` for "Pin" (also matched "Save pinned (0)") and "Next" (also matched the Next.js dev
  tools button). No UI change.

## Tests
- Backend default suite (`cd backend && HF_HUB_OFFLINE=1 uv run pytest -q -W error`): 2274 passed, 24 deselected
  (baseline at branch start 2237 passed, 23 deselected). `uv run ruff check prism tests` clean.
- Backend live, new test: `tests/agent/test_dashboards_live.py` 1 passed against the running stack on 2026-10-02
  (a `cash_ops_emea` dashboard with one `cashrecon` and one `refmaster` recipe replays as `ok` with EMEA rows and
  `not_permitted`).
- Frontend `make test-ui`: lint and typecheck clean, vitest 64 passed in 15 files.
- Playwright `make e2e`: 1 passed (mocked agent: login, KPIs, ask, widget, provenance paging, pin, save, reopen),
  1 skipped (live). `PRISM_E2E_LIVE=1 make e2e`: 1 passed (login as the head-of-data persona and the KPI strip against
  the real agent), 1 skipped (mocked).

## How to run
1. Backend: `docker compose up -d --wait postgres neo4j`, then `scripts/start_backend.sh` (agent :8000, gateway :8200,
   `/dev/token` on). **Never pass `--reseed`** without asking.
2. UI: `make frontend`, open http://localhost:3000, pick a persona.
3. Checks: `make test-ui`, `make e2e` (installs Chromium on first run; starts or reuses `npm run dev` on :3000),
   `PRISM_E2E_LIVE=1 make e2e` (stack up), `make test-live` for the backend live suite.
Playwright output (`frontend/test-results/`, `frontend/playwright-report/`) is git-ignored.

## What was not exercised
- No automated test calls the real `/chat` (no `ANTHROPIC_API_KEY` in tests); the e2e feeds a canned SSE body. The
  assistant panel against a real model, and its first real-API risks (see `plan-4-carry-forward.md`), are unverified.
- No visual or cross-browser check beyond Chromium at desktop width.

## Known limits and deferred minor items
See `plan-5-carry-forward.md` for the limits and the list of deferred review findings.
