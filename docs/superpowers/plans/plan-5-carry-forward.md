# Plan 5 carry-forward (for M6)

## New agent contracts
**`widget` event** gains `handle_info.recipe`: `{"tool": "run_metric|query_source|combine", "args": {...}}`, the
call that produced the handle (`run_metric` `limit` is `null` when unset; a `combine` recipe nests its input recipes).
It is what lets the UI save a widget without saving data.

**`/dashboards`** (all need the persona bearer token; errors carry no detail). Table `app.saved_dashboards`
(`id uuid, sub, title, items jsonb, created_at`); every query filters on `sub`; the app role has
`SELECT, INSERT, DELETE` on this table only.

| Route | Behaviour |
|---|---|
| `GET /dashboards` | `{dashboards: [{id, title, created_at, widget_count}]}`, newest first, caller's own |
| `POST /dashboards` | `{title (1..80), items: [{widget, recipe}] (1..8)}`; the widget's handle is dropped; returns `201 {id}`; `409` beyond 20 per caller; `422` invalid body or recipe |
| `DELETE /dashboards/{id}` | `204`; `404` if missing or another caller's |
| `POST /dashboards/{id}/run` | Replays every recipe as the caller; `{id, title, widgets: [{widget, status, handle_info?}]}` with `status` `ok | not_permitted | unavailable | invalid`; `ok` widgets carry a fresh handle and `handle_info` (incl. recipe); a vanished column turns the widget into a `table`; `404` as above, `502` when the gateway is unreachable for all of them |

CORS now allows GET/POST/DELETE from exactly `http://localhost:3000`.

## Recipe trust model
A recipe is client-supplied, so it is untrusted. Save validates it (`parse_recipe`: tool whitelist, argument bounds
imported from the gateway's own constants, depth at most 3, at most 8 nodes). Run replays it through the gateway with
the caller's own token, so the gateway re-checks roles, scopes, row filters and metrics-only rules on every replay: a
hand-crafted recipe cannot read more than the caller can. A recipe the caller may no longer run comes back
`not_permitted`; nothing is cached across callers. The replayer dedupes identical calls within one run (sequential
callers only).

## Not done (M6)
- Thumbs-up that sends `record_answer(verified=True)` (the human-confirmation gap from Plan 4 still stands).
- Dashboard sharing (dashboards are per `sub`).
- Canvas persistence across reloads (the canvas is memory only; saved dashboards are the persistence).
- Dark theme (light only); layouts below 1024 px.

## Known limits
- Chart data is capped at 200 rows (`/results?limit=200`); the provenance table pages 50 at a time.
- Handles live in the gateway process: a gateway restart expires them and the UI shows its expired message; re-running
  the question or reopening the dashboard gets fresh ones.
- At most 20 saved dashboards per caller, 8 widgets each.
- Login is by the dev token (`/dev/token`): local demo only. Real auth is M6 work.
- The session token is in `sessionStorage`; the decoded payload is display only.
- Only Chromium is covered by e2e; no automated test calls the real `/chat`.

## Deferred minor review findings worth carrying
Backend:
- A drifted stored widget fails the whole `/run` with `500` instead of one `invalid` widget.
- Raw `KeyError` on malformed gateway output in replay; cached summary returned by reference; `Replayer` dedupe is not
  safe for concurrent callers.
- Test gaps: concurrent save under the advisory lock, mixed ok plus unavailable run, factory-level gateway error to
  `502`, malformed JSON `422`.
Frontend:
- `lib/charts.ts` has a file-level `no-explicit-any` disable (narrow it to the two formatter params); `toNum` over-coerces
  booleans and blank strings and passes NaN/Infinity.
- Chat: `Unauthorized` leaves the turn streaming (the page navigates away); question text is cleared before success;
  Enter submits during IME composition; a `failed` event can overwrite a done turn.
- SSE: no `reader.cancel` on early exit; an unknown JSON `type` under a known `event:` header is dropped silently.
- API client: a `200` with non-JSON or schema mismatch throws a raw `SyntaxError`/`ZodError` (components show fixed
  text for any non-`ApiError`); the dashboards save response is not schema-validated.
- KPI strip does not slice to 4 tiles and hides stale tiles on a failed refresh.
- Workspace: a failed post-save refresh overwrites "Dashboard saved."; localStorage write inside a state updater; the
  collapsed panel flashes open on load; no Workspace tests.
- Provenance: an `ok` widget with an empty handle renders a blank body; drawer refetches when item identity changes.
- Tooling: `next lint` is deprecated (migrate to the ESLint CLI before Next 16); `npm audit` reports 5 transitive
  vulnerabilities.
- Dashboards (final review): no dashboard group header (each reopened card is tagged with the dashboard title
  instead); `canPin` does not mirror the backend recipe depth/node bounds (an over-deep combine fails at save with a
  generic message); delete has no confirmation; no body-size cap on `POST /dashboards`.
- Widget ids are per chat turn (fallback specs always use `w1`), so they are not unique across turns; the UI keys
  reopened cards by position (`dashboardId:index`).
