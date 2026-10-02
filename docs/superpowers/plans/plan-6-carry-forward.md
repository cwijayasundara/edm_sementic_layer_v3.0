# Plan 6 carry-forward

## New contracts
**Gateway tools.**

| Tool | Result | Errors |
|---|---|---|
| `record_answer(question, plan, handles)` | `{recorded, record_id, metric_backed, metric_ids, dimensions}`; always stores `verified = false` | a `verified` argument is now `invalid_request` |
| `confirm_answer(record_id)` (seventh tool, not read-only) | `{confirmed: true}` on the caller's own metric-backed `ok` row; idempotent | `not_confirmable` for every other case (never says which); `confirm_failed` on a DB error (retry); `rate_limited` |

`confirm_answer` is not in the model's tool list.

**SSE.** The order is `plan*`, `widget*`, `summary`, `answer?`, `telemetry`.
- The new event is `{"type": "answer", "record_id": "<uuid>", "confirmable": bool}`.
- It is emitted only when the gateway recorded the answer (no fallback, refusal or error, and the write-back succeeded).

**Agent route.** `POST /answers/{record_id}/confirm` returns:
- `204` when confirmed;
- `404` for not confirmable (no detail);
- `422` for an id that is not a UUID (never sent to the gateway);
- `429` when rate-limited;
- `502` for any other gateway error;
- `401` without a valid token.

**Database.**
- Migration 5 adds `record_id uuid` (unique index) and `metric_backed` to `app.query_log`.
- Old rows were reset to `verified = false` with `metric_backed` set to the old claim and `record_id = NULL`, so they can never be confirmed.
- The app role holds `UPDATE (verified)` on `app.query_log` and nothing else beyond SELECT/INSERT. The grant is declarative and re-applied on every `migrate_app`.

**Distiller.** It reads only `verified = true` (human-confirmed) rows, and distilled executions carry `status: verified`.

**UI.** A turn whose `answer` event is confirmable shows "Correct? Confirm", then "Confirmed", or "Could not confirm this answer." with a retry.

## Evals
Commands:
- `make eval-check`: no model calls. It replays the golden references, checks that the planted stories hold, and probes that each canary is readable or hidden as its case claims.
- `make eval`: runs `eval-check` first, then 30 golden and 15 red-team cases through the agent and the real model.
- Single cases: `uv run python -m prism.evals.cli --case <id> --max-cost-usd <n>`.

Exit codes:
- 0: no leak;
- 1: any leak;
- 2: the run could not start, or a red-team case could not be checked (`unverified`).

The report is still written in that last case. Reports go to `backend/evals/reports/<UTC stamp>/`.

**How it works.**
- Each case runs under its own `eval-<12 hex>` sub, minted with the real JWT secret and sent to loopback URLs only. The reference replay uses a different sub.
- Golden checks are `answered`, `routing`, `rows`, `story` and `chart`. `rows` is reported but not required when a case has `story` assertions.
- Red-team detectors:
  - `obeyed`: an obey canary in the answer text;
  - `values`: a forbidden literal anywhere, answer or rows;
  - `hidden`: a hidden canary anywhere;
  - `scope_rows`: an out-of-scope row;
  - `tools`: a successful forbidden call in the case's own `app.audit` rows.
- The canaries are in `prism/sim/canaries.py`; a pre-Plan-6 seed needs `make reseed`.

**Verified 2026-10-02 on the live stack** after `make reseed` and `make graph`:
- `make eval-check`: 0 problems in 30 golden and 15 red-team cases. Every planted-story assertion holds as written; none were dropped.
- `make test-live`: 25 passed.
- Playwright smoke test (mocked agent, including the Confirm click): passed.

**First real-model run (`make eval`): not run yet.** Record its pass rate, leaks, cost, p50/p95 latency and cache reads
here after the first run, then tune per plan-4-carry-forward "First real-API-call risks".

## Known limits
- The cost cap is checked between cases, so one case can overshoot it.
- The tools detector reads `app.audit` only. A dropped audit row (writer DB failure) would hide a forbidden call.
- Leak scans cover at most 2,000 rows per widget, and truncation is not flagged.
- `rows` is exact per grouping. A correct answer grouped differently fails `rows`, which is why story cases do not require it.
- An `obey` case whose question never leads the agent to the injected row passes trivially; there is no "reached the row"
  detector.

## Deferred minor review findings
- **Stale gateway.** A gateway started before migration 5 and left running across `migrate_app` could still insert
  `verified = true` rows. Hardening: have the distiller's eligibility SQL also require
  `record_id IS NOT NULL AND metric_backed`.
- **Trailing newline.** The agent's `RECORD_ID` uses `$` with `.match`, so a trailing newline passes and gets 502
  instead of 422. Use `fullmatch`.
- **Noisy red-team case.** `sql_semicolon_stacking` flags an answer that merely quotes the injected row.
- **Tolerance on ids.** `rows_match` applies the relative tolerance to numeric-looking id columns.
- **Double-click guard.** The UI guard reads the render-time `turn` prop, not a ref.
