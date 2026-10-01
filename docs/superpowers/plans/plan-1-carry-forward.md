# Plan 1 — outcome and carry-forward for Plan 2

Plan 1 (simulated platforms) is complete on branch `mvp-plan-1`: 14 tasks, reviewed per task, plus a whole-branch review and one fix wave. 116 tests pass.

## What exists
- Deterministic simulation (seed 42, as-of 2026-09-30) of five platforms with four planted, verified demo stories.
- Five Postgres databases (host port 5434, localhost only) with forced row-level security, a signed-context verifier that fails closed, a masking view, and five demo personas.
- Two mock REST APIs (RefMaster :8101, MarketMaster :8102) with audience-bound JWTs.
- `scripts/start_backend.sh` (seeds on first run; re-seeds only on an unambiguous "not seeded" signal).

## Decisions worth remembering
- Host Postgres port is **5434** (5432/5433 are used by other stacks on the dev machine).
- Console scripts cannot import `prism` on this Mac (macOS re-applies a `hidden` flag to the venv `.pth`; Python 3.13 skips hidden `.pth` files). Use `uv run python -m prism.sim.cli` and `uv run python -m prism.security.cli` from `backend/`. pytest uses `pythonpath = ["."]`.
- The RBAC matrix in `backend/tests/db/test_rbac.py` (`expected()`) is the access specification; never edit it to fit the code.
- The story tests are statistical; the Vendor A story is deterministic in shape (background conflicts replay week over week) and verified across 8 seeds on both profiles.

## Carry-forward (Plan 2: source MCP servers, context graph, Semantic Gateway)
1. `metrics_only` (BI Analyst) is NOT enforced by the database or the mock APIs. The Semantic Gateway must enforce it (no `query_source`/free SQL); decide whether platform APIs should also refuse row-level endpoints for such tokens.
2. `app` database: it is dropped by `recreate_databases` today. Before `app.query_log` / `app.agent_runs` hold durable data, make its migrations idempotent and exclude it from resets.
3. Glossary/metrics: "this week" = trailing 5 business days ending at the as-of date (ISO `date_trunc('week')` inverts the story). Pin the WoW definition: total conflicts +20%, corp-bond-only +32%, Vendor A x Corp bond = 66.7% of all conflicts.
4. Vocabulary drift to record in the graph: recon_type `transaction` (spec says `txn`), no `price` recon type, no `fund_admin` sources, extra ticket category `missing_file`, denormalised scope columns (`asset_class`, `region`, `fund_group`, `source_type`). "Aged break" = `status <> 'closed' AND age_days > 5`.
5. Evals must assert story numbers on the FULL profile, not only the small one.
6. Refuse the in-source dev defaults for `jwt_secret` / `ctx_hmac_key` outside tests; add a minimum `jwt_secret` length; cap the `prism-token` TTL.
7. Add a Python-vs-SQL `can()` agreement test once the gateway adds a third copy of the policy (graph `CAN_READ`); consider a dedicated non-superuser owner for the `prism_sec` functions.
8. Bind any new service in `docker-compose.yml` (Neo4j) to 127.0.0.1.

## Known backlog (minor, from reviews)
- Weekend dates from calendar-day arithmetic (settle/pay/sla_due/actions); FX instruments have implausible prices and repeated names; dividends occur on non-equities; matured securities have no `valid_to`; source-name collisions ("Aster Custody"/"Aster Bank"); `_make_entities` loops forever above ~494 entities; late-portfolio transaction recon does not reflect the omitted custodian transactions; `.env.XXXXXX` temp file has no `trap` cleanup in the start script; pagination test for corporate actions is weak on the small profile; `golden-copy` returns `quotes: []` both when there are none and when the persona cannot read vendor prices.
