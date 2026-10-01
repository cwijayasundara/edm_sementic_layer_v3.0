# Plan 3 status (handoff)

Updated 2026-10-01. Plan: `2026-09-30-plan-3-context-graph-gateway.md`. Execution: subagent-driven development
(implementer + reviewer per task, independent security red-teams on Tasks 4, 5, 6, 7, 8 and the Task 9 distiller, and a
final whole-branch review, all with their fix waves applied; ledger in `.superpowers/sdd/2026-09-30-plan-3-context-graph-gateway/progress.md`). What Plan 4 consumes:
`plan-3-carry-forward.md`.

## Done
| Task | What | Commits (merged to `main` unless noted) |
|---|---|---|
| 1 | Neo4j compose service (127.0.0.1:7475/7688), deps, settings | `af55567` |
| 2 | Local embedder (bge-small, offline, LRU) | `3f575fa`, fix `b6a2694` |
| 3 | Ontology / glossary / seed history YAML + validating loader | `3d84548`, fix `b72c16e` |
| 4 | Graph schema + idempotent versioned loader, `make graph` | `94901ed`, `b670e6e`, `8fa35b7`, fix `8b9a2c3` (merge `d3d672d`) |
| 5 | Role-filtered hybrid retrieval, context pack, catalog | `fa0dc09`, `175e7b9`, `1420354` (merge `f3f62dc`) |
| 6 | `app` DB migrations, audit and query log | `4bd4b9f`, fix `6f084ea` (merge `f983327`) |
| 7 | Gateway policy, result store, DuckDB combine, downstream | `249e346`, fixes `e2c623c`, `5c1fb8b`..`9dfbf4f` (merge `51cac39`) |
| 8 | Gateway MCP server :8200, start script, CLI | `fa6a2e1`, fixes `9644630`..`dc0921c` (merge `ecc2105`) |
| 9 | Query-history distiller + `graph.cli distill`, live e2e (`make test-live`), docs | `1761bad`, `ce961c9`, `4e50dfa`, `07d6f39`; distiller-review + whole-branch fix wave `05f500e`..(last commit "fix: history poisoning limits, test isolation, Makefile and spec deltas") on branch `plan-3-history-e2e` (not merged yet) |

Suite (`cd backend && HF_HUB_OFFLINE=1 uv run pytest -q -W error`): 2069 passed, 13 deselected (the 8 live e2e checks
and 5 loader-vs-MCP checks are marked `live` and deselected by default; `make test-live` runs them against the
running stack).

## Remaining
1. Re-run `make test-live` with the full stack up (the fix wave changed no story path, but the live checks were not
   run in it), then `make distill` into the real namespace if history is wanted (needs two callers or
   `PRISM_HISTORY_MIN_CALLERS=1`).
2. Merge `plan-3-history-e2e` to `main` / finishing-a-development-branch (ask the user before merging or opening a PR).
3. Open items for Plan 4 are in `plan-3-carry-forward.md` (spec deltas, migrate_app with the admin DSN, residual
   distiller risks).

Done: the Task 9 distiller security review (D1-D5 and minors) and the final whole-branch review (W1-W4 and minors),
fixed in one wave on `plan-3-history-e2e`.

## How to resume
1. `cd` to the repo, `docker compose up -d --wait postgres neo4j` (this project's services only; never touch the
   user's other containers). Full stack: `scripts/start_backend.sh` (never `--reseed` without asking: it rebuilds every
   source database).
2. Ledger with rulings and parked findings: `.superpowers/sdd/2026-09-30-plan-3-context-graph-gateway/progress.md`;
   per-task briefs / reports / reviews sit beside it (git-ignored).
3. Use `uv run` from `backend/`. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Rulings and notes
- Loader reads repo registries directly (no `describe()` principal); a live consistency test covers it.
- Case policy: authored display name, identity `name.casefold()`.
- Plans are method only, never results: `plan_result_leaks` guards seed plans and every distilled plan; the distiller
  additionally skips questions with values (`question_leaks`) or unsafe text.
- History scopes: `allowed_scopes` is an any-of prefilter (union), and retrieval's gate requires EVERY used object
  visible; distilled executions also USE their Dimension nodes (sensitive dimensions stay hidden from metrics-only
  callers). This is the brief's "intersection" semantics; a literal set intersection would hide every cross-source
  question from everyone.
- `verified` is agent-claimed and metric-backed (every handle) until Plan 4 adds the human confirmation; distilled
  status `agent_verified`.
- History poisoning limits: `history_min_callers` distinct callers per question and plan (default 2), 20 questions
  per caller and 300 in all per run, `record_answer` 10 a minute per caller, ASCII-only question text, number-word
  and row-scope-term skips; distilled questions never add metrics and rank after seed examples.
- Tests never touch the real graph namespace (`prism_test`) and drop the databases they create.
- Graph statement errors surface as `GraphError` (fixed message), outages as `GraphUnavailable`.
- The Makefile uses tab-indented recipes (no `.RECIPEPREFIX`), so the GNU Make 3.81 that ships with macOS runs every
  target (`make -n db|models|graph|distill|test|test-live` all parse).
