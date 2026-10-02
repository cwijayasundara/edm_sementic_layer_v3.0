# Reasoning Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record each agent run's decision trace in Neo4j through the gateway (owner-only, 7-day retention) and show it as a "Reasoning" timeline next to the context graph, with the touched nodes highlighted.

**Architecture:** The agent collects steps in `RunState` (tool, args, handle, timing, the model's note) and, before telemetry, calls a new gateway tool `record_trace`. The gateway writes `(:Trace)-[:HAS_STEP]->(:TraceStep)-[:CALLED]->(:ToolCall)` nodes (never `:Ctx`) and derives `TOUCHED` / `ANSWERED_WITH` links itself from the caller's own handles, each target passing `gate()`. `get_trace` (owner-only, re-gated) and `mark_trace_confirmed` complete the gateway surface; the agent exposes `GET /runs/{run_id}/trace`; the UI's context-graph dialog gains a Reasoning tab.

**Tech Stack:** Python 3.13, Neo4j 5.26, FastMCP gateway, FastAPI agent, pytest; Next.js 15, React 19, ECharts 5, zod, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-02-reasoning-trace-design.md`

## Global Constraints
- Trace nodes NEVER carry the `:Ctx` label; they carry `ns`. Labels: `Trace`, `TraceStep`, `ToolCall`. Relationships: `HAS_STEP`, `CALLED`, `TOUCHED`, `ANSWERED_WITH`.
- Never name a Cypher variable `s` in a statement that uses `gate()` (gate's `_scoped` uses `any(s IN ...)`; the server rejects the shadowing).
- `Trace.sub` comes from the verified token only. Readers: the owner only. Another caller's trace, an expired trace and an unknown run all answer `unknown_trace` (agent: 404 `{"detail": "not found"}`).
- `TOUCHED` / `ANSWERED_WITH` come only from the caller's own live handles (via the gateway's `_lineage_plan`), every target a `:Ctx` node in the namespace passing `gate()`; free text (`label`, `note`, `considered`, `args`) never becomes a link.
- Caps: ≤ 40 steps (more is refused `invalid_request`); `label` ≤ 200, `note` ≤ 500, `args` JSON ≤ 2000, `question` / `answer` ≤ 2000, `considered` ≤ 20 ids (longer text is truncated, not refused).
- Retention: `PRISM_TRACE_RETENTION_DAYS` (`Settings.trace_retention_days`, default 7, ≥ 1); times are integer epoch seconds.
- Run id pattern: `^[0-9a-f]{32}$` (the agent's `uuid4().hex`).
- Rate limits: `record_trace` and `mark_trace_confirmed` use the `record_answer` limiter; `get_trace` uses the `search_context` limiter and timeout.
- `record_trace`, `get_trace`, `mark_trace_confirmed` are absent from `GATEWAY_TOOL_NAMES`, `SUPERVISOR_TOOLS`, `SUBAGENT_TOOLS`.
- Audit: one row per call with counts only (the existing `rows` field = step count); never run id, question, answer, notes or args. (Spec §3.1 listed `run_id`; the audit table has no column for it and adding one is out of scope.)
- The trace write happens before the telemetry event; its failure logs `trace_write_failed` (exception type only) and never changes the answer.
- UI copy, verbatim: "How I got this", "Graph", "Reasoning", "This reasoning trace has expired or is not available.", "The reasoning trace is unavailable.", "This widget was re-run from a saved dashboard; it has no reasoning trace.", "Confirmed".
- Commits as `cwijayasundara@gmail.com` (`git -c user.email=cwijayasundara@gmail.com commit ...`), body ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; stage explicit paths only (never `frontend/e2e/zz_shots.spec.ts`).
- Frontend node commands always get `< /dev/null` (they hang otherwise in this environment).

## Review Focus
1. **A trace whose step cites another caller's (or an expired) handle** → stored without links, never an error; pinned in Task 2 (`test_record_trace_foreign_handle_gets_no_links`).
2. **A reader whose role lost a scope after the run** → their chips for now-hidden nodes disappear; pinned in Task 1 (`test_get_trace_regates_touched_nodes`).
3. **`make graph` after traces exist** → traces and their surviving links remain; pinned in Task 1 (`test_traces_survive_a_graph_load_and_stay_out_of_the_catalog`).
4. **A run that ends in an error or a refusal** → still recorded with an `error` / `refusal` final step; pinned in Task 4 (`test_failed_run_records_an_error_step`).
5. **Opening "How I got this" on a turn with no widgets** → the Reasoning tab works and the Graph tab is disabled; pinned in Task 6 (`graph tab is disabled without a handle`).

---

### Task 1: Trace storage in Neo4j (`prism/graph/traces.py`)

**Files:**
- Create: `backend/prism/graph/traces.py`
- Modify: `backend/prism/graph/schema.py` (add the trace constraint to `SCHEMA`)
- Test: `backend/tests/graph/test_traces.py`

**Interfaces:**
- Consumes: `retrieval.gate`, `retrieval.gate_params`, `retrieval.arun_read`, `retrieval.run_read`, `retrieval.GraphUnavailable`, `retrieval.GraphError`, `retrieval.DATABASE`, `lineage.KINDS`.
- Produces:
  - `TRACE_CONSTRAINT: str` (also appended to `schema.SCHEMA`)
  - `class TraceOwned(Exception)` — the run id belongs to another caller
  - `async arecord_trace(adriver, trace: dict, claims: dict, *, ns: str | None, now: int, retention_s: int, timeout_s: float) -> int` (returns the number of steps written; raises `TraceOwned`)
  - `async aget_trace(adriver, run_id: str, claims: dict, *, ns, now: int, timeout_s) -> dict | None`
  - `async amark_confirmed(adriver, run_id: str, sub: str, *, ns, now: int, timeout_s) -> bool`
  - sync twins `record_trace`, `get_trace`, `mark_confirmed` with the same arguments and a sync `driver` (tests).
  - `trace` dict shape: `{run_id, sub, question, answer, path, status, answered: [local_uid], steps: [{seq, parent, kind, label, note, considered, ms, status, error_code, tool, args_json, handle, rows, truncated, touched: [local_uid]}]}` — already capped and with `touched` already derived by the gateway.

- [ ] **Step 1: Write the failing tests** (`backend/tests/graph/test_traces.py`)

```python
"""Trace storage: owner-bound, re-gated on read, outside the catalog. Neo4j session graph in ns `prism_test`."""
import pytest

from prism.graph.catalog import load_catalog
from prism.graph.lineage import lineage
from prism.graph.retrieval import context_pack
from prism.graph.traces import TraceOwned, get_trace, mark_confirmed, record_trace
from prism.security.personas import claims_for
from tests.graph_ns import TEST_GRAPH_NS, load_test_graph

NOW, DAY = 1_790_000_000, 86_400


def trace(run_id="a" * 32, sub="steward", **kw):
    return {"run_id": run_id, "sub": sub, "question": "Which vendor drives price conflicts?", "answer": "Vendor A.",
            "path": "metric", "status": "ok", "answered": ["metric:price_conflicts"],
            "steps": [
                {"seq": 0, "parent": None, "kind": "context", "label": "Searched the context graph", "note": "Look up",
                 "considered": ["price_conflicts"], "ms": 12, "status": "ok", "error_code": None,
                 "tool": "search_context", "args_json": '{"question": "q"}', "handle": None, "rows": None,
                 "truncated": None, "touched": []},
                {"seq": 1, "parent": None, "kind": "metric", "label": "Ran metric price_conflicts by vendor_id",
                 "note": None, "considered": [], "ms": 40, "status": "ok", "error_code": None, "tool": "run_metric",
                 "args_json": '{"metric_id": "price_conflicts"}', "handle": "r_aaaaaaaaaaaa", "rows": 4,
                 "truncated": False,
                 "touched": ["metric:price_conflicts", "dim:price_conflicts.vendor_id", "source:marketmaster"]},
                {"seq": 2, "parent": None, "kind": "answer", "label": "Answered", "note": None, "considered": [],
                 "ms": None, "status": "ok", "error_code": None, "tool": None, "args_json": None, "handle": None,
                 "rows": None, "truncated": None, "touched": []},
            ], **kw}


@pytest.fixture
def db(neo4j_driver, context_graph):
    neo4j_driver.execute_query("MATCH (n) WHERE (n:Trace OR n:TraceStep OR n:ToolCall) AND n.ns = $ns DETACH DELETE n",
                               ns=TEST_GRAPH_NS)
    return neo4j_driver


def put(db, t=None, who="steward", now=NOW, retention=7 * DAY):
    return record_trace(db, t or trace(), claims_for(who), ns=TEST_GRAPH_NS, now=now, retention_s=retention,
                        timeout_s=5.0)


def get(db, run_id="a" * 32, who="steward", now=NOW):
    return get_trace(db, run_id, claims_for(who) if isinstance(who, str) else who, ns=TEST_GRAPH_NS, now=now,
                     timeout_s=5.0)


@pytest.mark.neo4j
def test_owner_round_trip_with_links(db):
    assert put(db) == 3
    t = get(db)
    assert (t["run_id"], t["question"], t["answer"], t["confirmed"]) == ("a" * 32, trace()["question"], "Vendor A.", False)
    assert [s["seq"] for s in t["steps"]] == [0, 1, 2]
    step = t["steps"][1]
    assert step["tool"] == "run_metric" and step["rows"] == 4 and step["handle"] == "r_aaaaaaaaaaaa"
    assert {x["id"] for x in step["touched"]} == {"metric:price_conflicts", "dim:price_conflicts.vendor_id",
                                                  "source:marketmaster"}
    assert all(set(x) == {"id", "kind", "label"} for x in step["touched"])
    assert t["steps"][0]["considered"] == ["price_conflicts"] and t["steps"][0]["touched"] == []


@pytest.mark.neo4j
def test_other_callers_and_expired_runs_read_as_missing(db):
    put(db)
    assert get(db, who="head_data") is None
    assert get(db, run_id="b" * 32) is None
    assert get(db, now=NOW + 8 * DAY) is None


@pytest.mark.neo4j
def test_record_refuses_another_callers_run_id_and_replaces_own(db):
    put(db)
    with pytest.raises(TraceOwned):
        put(db, trace(sub="head_data"), who="head_data")
    put(db, trace(answer="Vendor B."))
    assert get(db)["answer"] == "Vendor B."
    assert db.execute_query("MATCH (t:Trace {ns: $ns}) RETURN count(t) AS c", ns=TEST_GRAPH_NS).records[0]["c"] == 1


@pytest.mark.neo4j
def test_links_only_to_visible_catalog_nodes(db):
    t = trace(sub="cash_ops_emea")
    t["steps"][1]["touched"] = ["metric:price_conflicts", "metric:open_breaks", "metric:no_such_metric"]
    put(db, t, who="cash_ops_emea")
    touched = {x["id"] for x in get(db, who="cash_ops_emea")["steps"][1]["touched"]}
    assert touched == {"metric:open_breaks"}   # marketmaster is out of scope; unknown ids link nothing


@pytest.mark.neo4j
def test_get_trace_regates_touched_nodes(db):
    put(db)
    narrowed = {**claims_for("steward"), "scopes": ["refmaster"]}
    assert get(db, who=narrowed)["steps"][1]["touched"] == []


@pytest.mark.neo4j
def test_expired_traces_of_the_caller_are_deleted_on_write(db):
    put(db, now=NOW - 8 * DAY)
    put(db, trace(run_id="c" * 32))
    left = db.execute_query("MATCH (t:Trace {ns: $ns}) RETURN collect(t.run_id) AS ids", ns=TEST_GRAPH_NS)
    assert left.records[0]["ids"] == ["c" * 32]
    assert db.execute_query("MATCH (n:TraceStep {ns: $ns}) RETURN count(n) AS c",
                            ns=TEST_GRAPH_NS).records[0]["c"] == 3


@pytest.mark.neo4j
def test_mark_confirmed_is_owner_only(db):
    put(db)
    assert mark_confirmed(db, "a" * 32, "head_data", ns=TEST_GRAPH_NS, now=NOW, timeout_s=5.0) is False
    assert mark_confirmed(db, "a" * 32, "steward", ns=TEST_GRAPH_NS, now=NOW, timeout_s=5.0) is True
    assert get(db)["confirmed"] is True


@pytest.mark.neo4j
def test_traces_survive_a_graph_load_and_stay_out_of_the_catalog(db, graph_embedder):
    put(db)
    load_test_graph(db, graph_embedder)
    assert get(db)["steps"][1]["touched"]                       # links to still-existing nodes survive
    labels = db.execute_query("MATCH (n:Ctx {ns: $ns}) WHERE n:Trace OR n:TraceStep OR n:ToolCall RETURN count(n) AS c",
                              ns=TEST_GRAPH_NS).records[0]["c"]
    assert labels == 0
    pack = context_pack("Which vendor drives price conflicts?", claims_for("steward"), driver=db,
                        embedder=graph_embedder, ns=TEST_GRAPH_NS)
    assert "Vendor A." not in str(pack) and "TraceStep" not in str(pack)
    g = lineage(db, {"price_conflicts": ["vendor_id"]}, [], claims_for("steward"), ns=TEST_GRAPH_NS)
    assert all(not n["id"].startswith(("trace", "step")) for n in g["nodes"])
    assert "a" * 32 not in str(load_catalog(db, TEST_GRAPH_NS).__dict__)
```

If `load_test_graph(db, graph_embedder)` refuses to reload an up-to-date namespace, call it with the keyword the
loader uses to force a reload (read `backend/tests/graph_ns.py` and `prism/graph/loader.py:load`), or bump the
version the same way `tests/conftest.py` does; the assertion that matters is that trace nodes survive a load.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest -q tests/graph/test_traces.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.graph.traces'`

- [ ] **Step 3: Implement `backend/prism/graph/traces.py`**

```python
"""Decision traces: how one agent run reached its answer, stored beside (never inside) the context graph.

(:Trace {run_id, sub, ns, ...})-[:HAS_STEP]->(:TraceStep)-[:CALLED]->(:ToolCall), with (:TraceStep)-[:TOUCHED]->
and (:Trace)-[:ANSWERED_WITH]-> links to catalog (:Ctx) nodes. Trace nodes never carry :Ctx, so the loader's
supersede, gate(), retrieval, lineage and the catalog never match them. The caller (the gateway) derives every
link target from the caller's own handles; this module re-checks each target with gate() when writing and again
when reading, so a trace never shows a node its reader cannot see today. Never name a variable `s` here: gate()
uses it internally."""
from __future__ import annotations

import asyncio
import json

from neo4j import Query, WRITE_ACCESS
from neo4j.exceptions import DriverError, Neo4jError

from prism.graph.lineage import KINDS
from prism.graph.retrieval import DATABASE, GraphError, GraphUnavailable, arun_read, gate, gate_params, run_read

TRACE_CONSTRAINT = ("CREATE CONSTRAINT trace_ns_run IF NOT EXISTS FOR (t:Trace) REQUIRE (t.ns, t.run_id) IS UNIQUE")
TRACE_LABELS = ("Trace", "TraceStep", "ToolCall")


class TraceOwned(Exception):
    """The run id already belongs to another caller."""


OWNER_CYPHER = "MATCH (t:Trace {ns: $ns, run_id: $run_id}) RETURN t.sub AS sub"

PURGE_CYPHER = """
MATCH (t:Trace {ns: $ns, sub: $sub}) WHERE t.expires_at < $now OR t.run_id = $run_id
OPTIONAL MATCH (t)-[:HAS_STEP]->(step:TraceStep)
OPTIONAL MATCH (step)-[:CALLED]->(call:ToolCall)
DETACH DELETE t, step, call
"""

CREATE_CYPHER = f"""
CREATE (t:Trace {{ns: $ns, run_id: $run_id, sub: $sub, question: $question, answer: $answer, path: $path,
                  status: $status, confirmed: false, created_at: $now, expires_at: $expires_at}})
WITH t
CALL (t) {{
  UNWIND $answered AS u
  MATCH (m:Metric:Ctx {{ns: $ns, local_uid: u}}) WHERE {gate('m')}
  MERGE (t)-[:ANSWERED_WITH]->(m)
}}
WITH t
UNWIND $steps AS st
CREATE (t)-[:HAS_STEP]->(step:TraceStep {{ns: $ns, seq: st.seq, parent: st.parent, kind: st.kind, label: st.label,
        note: st.note, considered: st.considered, ms: st.ms, status: st.status, error_code: st.error_code,
        touched: st.touched}})
FOREACH (_ IN CASE WHEN st.tool IS NULL THEN [] ELSE [1] END |
  CREATE (step)-[:CALLED]->(:ToolCall {{ns: $ns, tool: st.tool, args_json: st.args_json, handle: st.handle,
                                       rows: st.rows, truncated: st.truncated}}))
WITH step, st
CALL (step, st) {{
  UNWIND st.touched AS u
  MATCH (n:Ctx {{ns: $ns, local_uid: u}}) WHERE {gate('n')}
  MERGE (step)-[:TOUCHED]->(n)
}}
RETURN count(step) AS steps
"""

GET_CYPHER = f"""
MATCH (t:Trace {{ns: $ns, run_id: $run_id, sub: $sub}}) WHERE t.expires_at >= $now
OPTIONAL MATCH (t)-[:HAS_STEP]->(step:TraceStep)
OPTIONAL MATCH (step)-[:CALLED]->(call:ToolCall)
WITH t, step, call ORDER BY step.seq
WITH t, collect(CASE WHEN step IS NULL THEN null ELSE {{
  step: step {{.seq, .parent, .kind, .label, .note, .considered, .ms, .status, .error_code}},
  call: call {{.tool, .args_json, .handle, .rows, .truncated}},
  touched: COLLECT {{ MATCH (step)-[:TOUCHED]->(n:Ctx) WHERE {gate('n')}
                     RETURN n {{.local_uid, .name, .qualified_name, kind: [l IN labels(n) WHERE l IN $kinds][0]}} }}
}} END) AS steps
RETURN t {{.run_id, .question, .answer, .path, .status, .confirmed, .created_at}} AS trace, steps
"""

CONFIRM_CYPHER = """
MATCH (t:Trace {ns: $ns, run_id: $run_id, sub: $sub}) WHERE t.expires_at >= $now
SET t.confirmed = true RETURN count(t) AS n
"""


def _gate(claims: dict, ns: str | None) -> dict:
    return gate_params([str(x) for x in claims.get("scopes") or []], bool(claims.get("metrics_only")), ns)


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, Neo4jError) and exc.code and "ClientError.Statement" in exc.code:
        return GraphError()
    return GraphUnavailable(f"context graph unavailable: {type(exc).__name__}")


def _shape(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    out = dict(rows[0]["trace"])
    steps = []
    for item in rows[0]["steps"]:
        if item is None:
            continue
        step, call = dict(item["step"]), item["call"] or {}
        step.update({"tool": call.get("tool"), "args": call.get("args_json"), "handle": call.get("handle"),
                     "rows": call.get("rows"), "truncated": call.get("truncated"),
                     "considered": step.get("considered") or [],
                     "touched": [{"id": n["local_uid"], "kind": n["kind"],
                                  "label": n.get("qualified_name") or n.get("name") or n["local_uid"]}
                                 for n in item["touched"] if n.get("kind") in KINDS]})
        steps.append(step)
    out["steps"] = steps
    return out


def _write_params(trace: dict, claims: dict, ns, now: int, retention_s: int) -> dict:
    return {"run_id": trace["run_id"], "sub": trace["sub"], "question": trace.get("question"),
            "answer": trace.get("answer"), "path": trace.get("path"), "status": trace.get("status"),
            "answered": list(trace.get("answered") or []), "steps": list(trace.get("steps") or []), "now": now,
            "expires_at": now + retention_s, **_gate(claims, ns)}


def _write_tx(tx, params: dict) -> int:
    owner = tx.run(OWNER_CYPHER, params).single()
    if owner is not None and owner["sub"] != params["sub"]:
        raise TraceOwned()
    tx.run(PURGE_CYPHER, params).consume()
    rec = tx.run(CREATE_CYPHER, params).single()
    return rec["steps"] if rec else 0


async def _awrite_tx(tx, params: dict) -> int:
    owner = await (await tx.run(OWNER_CYPHER, params)).single()
    if owner is not None and owner["sub"] != params["sub"]:
        raise TraceOwned()
    await (await tx.run(PURGE_CYPHER, params)).consume()
    rec = await (await tx.run(CREATE_CYPHER, params)).single()
    return rec["steps"] if rec else 0


def record_trace(driver, trace: dict, claims: dict, *, ns: str | None, now: int, retention_s: int,
                 timeout_s: float) -> int:
    params = _write_params(trace, claims, ns, now, retention_s)
    try:
        with driver.session(database=DATABASE, default_access_mode=WRITE_ACCESS) as session:
            return session.execute_write(_write_tx, params)
    except TraceOwned:
        raise
    except (DriverError, Neo4jError, OSError) as exc:
        err = _translate(exc)
    raise err from None


async def arecord_trace(adriver, trace: dict, claims: dict, *, ns: str | None, now: int, retention_s: int,
                        timeout_s: float) -> int:
    params = _write_params(trace, claims, ns, now, retention_s)

    async def go() -> int:
        async with adriver.session(database=DATABASE, default_access_mode=WRITE_ACCESS) as session:
            return await session.execute_write(_awrite_tx, params)

    try:
        return await asyncio.wait_for(go(), timeout_s)
    except TraceOwned:
        raise
    except TimeoutError as exc:
        raise GraphUnavailable(f"context graph did not answer within {timeout_s}s") from exc
    except (DriverError, Neo4jError, OSError) as exc:
        err = _translate(exc)
    raise err from None


def _read_params(run_id: str, claims: dict, ns, now: int) -> dict:
    return {"run_id": run_id, "sub": claims.get("sub"), "now": now, "kinds": list(KINDS), **_gate(claims, ns)}


def get_trace(driver, run_id: str, claims: dict, *, ns: str | None, now: int, timeout_s: float) -> dict | None:
    return _shape(run_read(driver, GET_CYPHER, _read_params(run_id, claims, ns, now), timeout_s))


async def aget_trace(adriver, run_id: str, claims: dict, *, ns: str | None, now: int,
                     timeout_s: float) -> dict | None:
    return _shape(await arun_read(adriver, GET_CYPHER, _read_params(run_id, claims, ns, now), timeout_s))


def mark_confirmed(driver, run_id: str, sub: str, *, ns: str | None, now: int, timeout_s: float) -> bool:
    params = {"run_id": run_id, "sub": sub, "now": now, **gate_params([], False, ns)}
    try:
        with driver.session(database=DATABASE, default_access_mode=WRITE_ACCESS) as session:
            rec = session.run(Query(CONFIRM_CYPHER, timeout=timeout_s), params).single()
            return bool(rec and rec["n"])
    except (DriverError, Neo4jError, OSError) as exc:
        err = _translate(exc)
    raise err from None


async def amark_confirmed(adriver, run_id: str, sub: str, *, ns: str | None, now: int, timeout_s: float) -> bool:
    params = {"run_id": run_id, "sub": sub, "now": now, **gate_params([], False, ns)}

    async def go() -> bool:
        async with adriver.session(database=DATABASE, default_access_mode=WRITE_ACCESS) as session:
            rec = await (await session.run(Query(CONFIRM_CYPHER, timeout=timeout_s), params)).single()
            return bool(rec and rec["n"])

    try:
        return await asyncio.wait_for(go(), timeout_s)
    except TimeoutError as exc:
        raise GraphUnavailable(f"context graph did not answer within {timeout_s}s") from exc
    except (DriverError, Neo4jError, OSError) as exc:
        err = _translate(exc)
    raise err from None


def args_json(args: object, limit: int) -> str | None:
    """Tool arguments as JSON text, capped (the gateway stores, never parses, it)."""
    if args is None:
        return None
    text = json.dumps(args, separators=(",", ":"), default=str, ensure_ascii=False)
    return text[:limit]


__all__ = ["TRACE_CONSTRAINT", "TRACE_LABELS", "TraceOwned", "aget_trace", "amark_confirmed", "args_json",
           "arecord_trace", "get_trace", "mark_confirmed", "record_trace"]
```

In `backend/prism/graph/schema.py`, append `TRACE_CONSTRAINT`'s statement text to the `SCHEMA` list (inline the same
string; do not import `traces` from `schema`, to keep `schema` dependency-free):
`"CREATE CONSTRAINT trace_ns_run IF NOT EXISTS FOR (t:Trace) REQUIRE (t.ns, t.run_id) IS UNIQUE",`.
Add a test asserting `TRACE_CONSTRAINT in SCHEMA` to `test_traces.py`.

`gate_params([], False, ns)` in the confirm path only supplies `$ns`; the statement uses no gate.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -q tests/graph/test_traces.py tests/graph/test_lineage.py tests/graph/test_retrieval.py && uv run ruff check prism/graph tests/graph`
Expected: all passed; `All checks passed!`. If a statement is rejected, `GraphError` hides the server text: run the
Cypher with the driver directly (`PYTHONPATH=. uv run python ...`) to read it.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/graph/traces.py backend/prism/graph/schema.py backend/tests/graph/test_traces.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(graph): owner-bound decision traces beside the context graph, re-gated on read

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Gateway tools `record_trace`, `get_trace`, `mark_trace_confirmed`

**Files:**
- Modify: `backend/prism/config.py` (`trace_retention_days: int = Field(7, ge=1)`)
- Modify: `backend/prism/gateway/service.py` (docstring "eight tools" -> "eleven tools"; `TOOLS`; args models; `TraceStore` protocol; `Gateway(..., traces=None)`; `_record_trace`, `_get_trace`, `_mark_trace_confirmed`; `_trace_touched`)
- Modify: `backend/prism/gateway/server.py` (schema functions, `_TOOL_FUNCS`, `WRITE_TOOLS`, runtime wiring, startup constraint)
- Test: `backend/tests/gateway/test_server.py`

**Interfaces:**
- Consumes: Task 1 `arecord_trace`, `aget_trace`, `amark_confirmed`, `TraceOwned`, `TRACE_CONSTRAINT`, `args_json`; existing `Gateway._lineage_plan(sub, handle)`.
- Produces: tools
  - `record_trace({run_id, question, answer, path, status, steps})` -> `{"recorded": true, "steps": int}`
  - `get_trace({run_id})` -> the `_shape` dict from Task 1 (`unknown_trace` on miss)
  - `mark_trace_confirmed({run_id})` -> `{"confirmed": true}` (`unknown_trace` on miss)
  - `TraceStore` = object with async `record(trace, claims) -> int`, `get(run_id, claims) -> dict | None`, `confirm(run_id, sub) -> bool`.

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/gateway/test_server.py`; update the tool-list test to the eleven tools, `record_trace` and `mark_trace_confirmed` in `WRITE_TOOLS` (no READ_ONLY annotation), `get_trace` READ_ONLY)

```python
from prism.agent.prompts import SUBAGENT_TOOLS, SUPERVISOR_TOOLS
from prism.graph.traces import TraceOwned

RUN = "a" * 32
STEP = {"seq": 0, "kind": "metric", "label": "Ran metric open_breaks", "tool": "run_metric",
        "args": {"metric_id": "open_breaks"}}


class FakeTraces:
    def __init__(self):
        self.saved: dict[str, tuple[dict, dict]] = {}
        self.confirmed: set[str] = set()
        self.fail: Exception | None = None

    async def record(self, trace, claims):
        if self.fail:
            raise self.fail
        old = self.saved.get(trace["run_id"])
        if old and old[0]["sub"] != trace["sub"]:
            raise TraceOwned()
        self.saved[trace["run_id"]] = (trace, claims)
        return len(trace["steps"])

    async def get(self, run_id, claims):
        t = self.saved.get(run_id)
        return {"run_id": run_id, "steps": t[0]["steps"]} if t and t[0]["sub"] == claims["sub"] else None

    async def confirm(self, run_id, sub):
        t = self.saved.get(run_id)
        if t and t[0]["sub"] == sub:
            self.confirmed.add(run_id)
            return True
        return False


async def test_record_trace_derives_links_from_the_callers_own_handle(settings, fake_catalog):
    traces, audit = FakeTraces(), FakeAudit()
    gw = make_gateway(settings, fake_catalog, audit=audit, traces=traces)
    async with gateway_client(settings, gw, "cash_ops_emea") as c:
        h = body(await c.call_tool("run_metric", {"metric_id": "open_breaks", "dimensions": ["region"]}))["handle"]
        out = body(await c.call_tool("record_trace", {"run_id": RUN, "question": "q", "answer": "a", "path": "metric",
                                                      "status": "ok", "steps": [{**STEP, "handle": h}]}))
    assert out == {"recorded": True, "steps": 1}
    trace, claims = traces.saved[RUN]
    assert trace["sub"] == "cash_ops_emea" and claims["metrics_only"] is False
    (step,) = trace["steps"]
    assert sorted(step["touched"]) == ["dim:open_breaks.region", "metric:open_breaks"]
    assert step["rows"] == 12 and step["truncated"] is False          # from the stored result, not the agent
    assert trace["answered"] == ["metric:open_breaks"]
    assert step["args_json"] == '{"metric_id":"open_breaks"}'
    row = audit.rows[-1]
    assert (row["tool"], row["status"], row["rows"]) == ("record_trace", "ok", 1)
    assert RUN not in str(audit.events[-1]) and "Ran metric" not in str(audit.events[-1])


async def test_record_trace_foreign_handle_gets_no_links(settings, fake_catalog):
    traces = FakeTraces()
    gw = make_gateway(settings, fake_catalog, traces=traces)
    _, app = create_app(settings, gateway=gw)
    async with serving(app) as base:
        async with mcp_client(f"{base}/mcp", token(settings, "head_data")) as head:
            h = body(await head.call_tool("run_metric", {"metric_id": "open_breaks"}))["handle"]
        async with mcp_client(f"{base}/mcp", token(settings, "cash_ops_emea")) as other:
            body(await other.call_tool("record_trace", {"run_id": RUN, "question": "q", "answer": "a",
                                                        "path": "metric", "status": "ok",
                                                        "steps": [{**STEP, "handle": h, "touched": ["metric:x"]}]}))
    (step,) = traces.saved[RUN][0]["steps"]
    assert step["touched"] == [] and step["rows"] is None and traces.saved[RUN][0]["answered"] == []


async def test_record_trace_caps_text_and_refuses_too_many_steps(settings, fake_catalog):
    traces = FakeTraces()
    gw = make_gateway(settings, fake_catalog, traces=traces)
    async with gateway_client(settings, gw, "head_data") as c:
        body(await c.call_tool("record_trace", {"run_id": RUN, "question": "q" * 3000, "answer": "a", "path": "p",
                                                "status": "ok", "steps": [{**STEP, "note": "n" * 900,
                                                                           "label": "l" * 300}]}))
        many = await c.call_tool("record_trace", {"run_id": "b" * 32, "question": "q", "answer": "a", "path": "p",
                                                  "status": "ok", "steps": [STEP] * 41})
    trace = traces.saved[RUN][0]
    assert len(trace["question"]) == 2000 and len(trace["steps"][0]["note"]) == 500
    assert len(trace["steps"][0]["label"]) == 200
    assert many.is_error and "invalid_request" in text(many)


async def test_record_trace_refuses_another_callers_run_id_like_a_bad_request(settings, fake_catalog):
    traces = FakeTraces()
    gw = make_gateway(settings, fake_catalog, traces=traces)
    args = {"run_id": RUN, "question": "q", "answer": "a", "path": "p", "status": "ok", "steps": [STEP]}
    _, app = create_app(settings, gateway=gw)
    async with serving(app) as base:
        async with mcp_client(f"{base}/mcp", token(settings, "head_data")) as head:
            body(await head.call_tool("record_trace", args))
        async with mcp_client(f"{base}/mcp", token(settings, "steward")) as other:
            r = await other.call_tool("record_trace", args)
            bad = await other.call_tool("record_trace", {**args, "run_id": "not-a-run"})
    assert r.is_error and "invalid_request" in text(r) and "invalid_request" in text(bad)
    assert traces.saved[RUN][0]["sub"] == "head_data"


async def test_get_trace_and_confirm_are_owner_only(settings, fake_catalog):
    traces = FakeTraces()
    gw = make_gateway(settings, fake_catalog, traces=traces)
    args = {"run_id": RUN, "question": "q", "answer": "a", "path": "p", "status": "ok", "steps": [STEP]}
    _, app = create_app(settings, gateway=gw)
    async with serving(app) as base:
        async with mcp_client(f"{base}/mcp", token(settings, "head_data")) as head:
            body(await head.call_tool("record_trace", args))
            assert body(await head.call_tool("get_trace", {"run_id": RUN}))["run_id"] == RUN
            assert body(await head.call_tool("mark_trace_confirmed", {"run_id": RUN})) == {"confirmed": True}
        async with mcp_client(f"{base}/mcp", token(settings, "steward")) as other:
            foreign = await other.call_tool("get_trace", {"run_id": RUN})
            missing = await other.call_tool("get_trace", {"run_id": "c" * 32})
            unconf = await other.call_tool("mark_trace_confirmed", {"run_id": RUN})
    assert foreign.is_error and text(foreign) == text(missing) and "unknown_trace" in text(foreign)
    assert unconf.is_error and "unknown_trace" in text(unconf)


async def test_trace_store_outage_maps_to_context_unavailable(settings, fake_catalog):
    traces = FakeTraces()
    traces.fail = GraphUnavailable("down")
    gw = make_gateway(settings, fake_catalog, traces=traces)
    r = await call(settings, gw, "head_data", "record_trace", {"run_id": RUN, "question": "q", "answer": "a",
                                                                "path": "p", "status": "ok", "steps": [STEP]})
    assert r.is_error and "context_unavailable" in text(r) and "down" not in text(r)


def test_trace_tools_are_never_offered_to_the_llm():
    names = {t.name for t in SUPERVISOR_TOOLS + SUBAGENT_TOOLS} | set(GATEWAY_TOOL_NAMES)
    assert not names & {"record_trace", "get_trace", "mark_trace_confirmed"}
```

(The `FakeSources` run_metric returns 12 rows, so `rows == 12`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest -q tests/gateway/test_server.py -k "trace or tool_list"`
Expected: FAIL (unknown tool / unexpected keyword `traces`)

- [ ] **Step 3: Implement in `service.py`**

```python
TOOLS = ("search_context", "run_metric", "query_source", "get_rows", "combine", "record_answer", "confirm_answer",
         "lineage", "record_trace", "get_trace", "mark_trace_confirmed")
MAX_TRACE_STEPS = 40
TRACE_TEXT = {"label": 200, "note": 500, "question": 2000, "answer": 2000}
TRACE_ARGS_CHARS = 2000
TRACE_CONSIDERED = 20
RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
TRACE_KINDS = ("context", "metric", "query", "combine", "delegate", "visualize", "answer", "refusal", "error")


class TraceStepArgs(_Args):
    seq: Annotated[StrictInt, Field(ge=0, le=999)]
    parent: Annotated[StrictInt | None, Field(ge=0, le=999)] = None
    kind: Annotated[str, Field(pattern="^(" + "|".join(TRACE_KINDS) + ")$")]
    label: Annotated[str, Field(max_length=4000)] = ""
    note: Annotated[str | None, Field(max_length=4000)] = None
    considered: Annotated[list[Annotated[str, Field(max_length=MAX_NAME)]], Field(max_length=100)] = []
    ms: Annotated[float | None, Field(ge=0)] = None
    status: Annotated[str | None, Field(max_length=32)] = None
    error_code: Annotated[str | None, Field(max_length=64)] = None
    tool: Annotated[str | None, Field(max_length=64)] = None
    args: Any = None
    handle: Annotated[str | None, Field(max_length=64)] = None
    touched: Any = None          # accepted and ignored: links come from handles only


class RecordTraceArgs(_Args):
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)]
    question: Annotated[str, Field(max_length=8000)]
    answer: Annotated[str, Field(max_length=8000)] = ""
    path: Annotated[str | None, Field(max_length=32)] = None
    status: Annotated[str, Field(max_length=32)]
    steps: Annotated[list[TraceStepArgs], Field(max_length=MAX_TRACE_STEPS)]


class RunIdArgs(_Args):
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)]
```

Add `"record_trace": RecordTraceArgs, "get_trace": RunIdArgs, "mark_trace_confirmed": RunIdArgs` to `ARG_MODELS`.
`_JSON_FIELDS` already handles list/object fields. Add the protocol and constructor argument:

```python
class TraceStore(Protocol):
    async def record(self, trace: dict, claims: dict) -> int: ...
    async def get(self, run_id: str, claims: dict) -> dict | None: ...
    async def confirm(self, run_id: str, sub: str) -> bool: ...
```

(`from typing import Protocol`), `Gateway.__init__(..., traces: TraceStore | None = None)` -> `self.traces = traces`.

Tools (after `_lineage`):

```python
    def _trace_touched(self, sub: str, handle: str | None) -> tuple[list[str], list[str], int | None, bool | None]:
        """(touched local uids, answered metric uids, rows, truncated) of one of the caller's own handles; nothing
        for another caller's, an expired or a made-up handle."""
        if not handle:
            return [], [], None, None
        try:
            entry = self.store.get(sub, handle)
            plans, sources, _ = self._lineage_plan(sub, handle)
        except GatewayError:
            return [], [], None, None
        metrics = [f"metric:{m}" for m in sorted(plans)]
        dims = [f"dim:{m}.{d}" for m in sorted(plans) for d in sorted(plans[m])]
        touched = metrics + dims + [f"source:{x}" for x in sources]
        return touched, metrics, len(entry.rows), bool(entry.meta.get("truncated", False))

    def _require_traces(self) -> TraceStore:
        if self.traces is None:
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly")
        return self.traces

    async def _record_trace(self, claims: dict, args: RecordTraceArgs, rec: CallRecord) -> dict:
        sub = claims["sub"]
        store = self._require_traces()
        steps, answered = [], []
        for st in args.steps:
            touched, metrics, rows, truncated = self._trace_touched(sub, st.handle)
            answered += [m for m in metrics if m not in answered]
            steps.append({"seq": st.seq, "parent": st.parent, "kind": st.kind,
                          "label": st.label[:TRACE_TEXT["label"]],
                          "note": st.note[:TRACE_TEXT["note"]] if st.note else None,
                          "considered": list(st.considered[:TRACE_CONSIDERED]), "ms": st.ms, "status": st.status,
                          "error_code": st.error_code, "tool": st.tool,
                          "args_json": args_json(st.args, TRACE_ARGS_CHARS), "handle": st.handle if rows is not None
                          else None, "rows": rows, "truncated": truncated, "touched": touched})
        trace = {"run_id": args.run_id, "sub": sub, "question": args.question[:TRACE_TEXT["question"]],
                 "answer": args.answer[:TRACE_TEXT["answer"]], "path": args.path, "status": args.status,
                 "answered": answered, "steps": steps}
        safe = {**claims, "metrics_only": is_metrics_only(claims)}
        release = await self.record_limiter.acquire(sub)
        try:
            n = await store.record(trace, safe)
        except TraceOwned:
            raise GatewayError("invalid_request", "invalid trace") from None
        except (TimeoutError, GraphUnavailable, GraphError):
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly") from None
        finally:
            release()
        rec.fields["rows"] = n
        return {"recorded": True, "steps": n}

    async def _get_trace(self, claims: dict, args: RunIdArgs, rec: CallRecord) -> dict:
        sub = claims["sub"]
        store = self._require_traces()
        safe = {**claims, "metrics_only": is_metrics_only(claims)}
        release = await self.context_limiter.acquire(sub)
        try:
            async with asyncio.timeout(self.context_timeout_s):
                trace = await store.get(args.run_id, safe)
        except (TimeoutError, GraphUnavailable, GraphError):
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly") from None
        finally:
            release()
        if trace is None:
            raise GatewayError("unknown_trace", "unknown trace")
        rec.fields["rows"] = len(trace.get("steps") or [])
        return trace

    async def _mark_trace_confirmed(self, claims: dict, args: RunIdArgs, rec: CallRecord) -> dict:
        sub = claims["sub"]
        store = self._require_traces()
        release = await self.confirm_limiter.acquire(sub)
        try:
            ok = await store.confirm(args.run_id, sub)
        except (TimeoutError, GraphUnavailable, GraphError):
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly") from None
        finally:
            release()
        if not ok:
            raise GatewayError("unknown_trace", "unknown trace")
        return {"confirmed": True}
```

Imports: `from prism.graph.traces import TraceOwned, args_json`. Do not change the audit sanitiser: the tools only
set the existing `rows` field.

- [ ] **Step 4: Implement in `server.py` and `config.py`**

`config.py`: `trace_retention_days: int = Field(7, ge=1)` next to `history_min_callers`.

`server.py`: schema functions

```python
async def record_trace(run_id: str, question: str, answer: str, path: str, status: str,
                       steps: list) -> CallToolResult:
    """Store how one of your own runs reached its answer (the agent calls this; never needed to answer)."""
    raise NotImplementedError


async def get_trace(run_id: str) -> CallToolResult:
    """One of your own recorded run traces (UI only)."""
    raise NotImplementedError


async def mark_trace_confirmed(run_id: str) -> CallToolResult:
    """Mark one of your own run traces as confirmed by you (UI only)."""
    raise NotImplementedError
```

Add them to `_TOOL_FUNCS`; `WRITE_TOOLS = frozenset({"record_answer", "confirm_answer", "record_trace",
"mark_trace_confirmed"})`. In `prepare_runtime`, after `verify_connectivity()` succeeds, run
`driver.execute_query(TRACE_CONSTRAINT)` (import from `prism.graph.traces`; wrap failures like the other startup
graph calls into `GatewayStartupError("could not prepare the trace store ...: run `make db`")`). In `start()`:

```python
        retention = settings.trace_retention_days * 86_400

        class _Traces:
            async def record(self, trace, claims):
                return await arecord_trace(adriver, trace, claims, ns=settings.graph_ns, now=int(time.time()),
                                           retention_s=retention, timeout_s=timeout)

            async def get(self, run_id, claims):
                return await aget_trace(adriver, run_id, claims, ns=settings.graph_ns, now=int(time.time()),
                                        timeout_s=timeout)

            async def confirm(self, run_id, sub):
                return await amark_confirmed(adriver, run_id, sub, ns=settings.graph_ns, now=int(time.time()),
                                             timeout_s=timeout)
```

and pass `traces=_Traces()` to `Gateway(...)`. Update both module docstrings ("eight" -> "eleven").

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest -q tests/gateway && uv run ruff check prism tests/gateway`
Expected: all passed; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add backend/prism/config.py backend/prism/gateway/service.py backend/prism/gateway/server.py backend/tests/gateway/test_server.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(gateway): record_trace, get_trace, mark_trace_confirmed (owner-bound, links from handles)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 3: Agent step collection (runner hook, RunState, ToolBox)

**Files:**
- Modify: `backend/prism/agent/runner.py` (`observe` hook), `backend/prism/agent/state.py` (`steps`, `pending_note`, `add_step`), `backend/prism/agent/tools.py` (record steps), `backend/prism/agent/service.py` (pass `observe` to the supervisor runner only)
- Test: `backend/tests/agent/test_runner.py`, `backend/tests/agent/test_tools.py`

**Interfaces:**
- Produces:
  - `MessagesRunner(..., observe: Callable[[str], None] | None = None)`: called with the joined `TextBlock` text of an assistant response that also contains tool uses (only when non-empty), before those tools run.
  - `RunState.steps: list[dict]`, `RunState.pending_note: str | None`, `RunState.add_step(**fields) -> int` (assigns `seq = len(steps)`, attaches and clears `pending_note` as `note`, returns seq).
  - Step dict keys: `seq, parent, kind, label, note, considered, ms, status, error_code, tool, args, handle`.
  - `step_label(name: str, args: dict) -> str` and `STEP_KIND: dict[str, str]` in `tools.py`.

- [ ] **Step 1: Write the failing tests**

`tests/agent/test_runner.py` (use the file's existing scripted-model helpers; read the top of the file):

```python
async def test_observe_receives_text_written_before_tool_calls():
    seen = []
    # scripted responses: [TextBlock("I will look up the metric."), ToolUse(...)] then a final text
    ...  # build exactly like test_loop_executes_tools_in_parallel_and_returns_final_text, with observe=seen.append
    assert seen == ["I will look up the metric."]
```

Write the body by copying `test_loop_executes_tools_in_parallel_and_returns_final_text`'s setup, changing the first
scripted response to contain a `TextBlock` before its tool use, passing `observe=seen.append`, and asserting `seen`.
Also assert `observe` is not called for the final text-only response.

`tests/agent/test_tools.py`:

```python
def test_add_step_numbers_steps_and_consumes_the_pending_note():
    state = make_state()          # use the file's existing RunState factory/fixture
    state.pending_note = "Checking the catalog first."
    assert state.add_step(kind="context", label="Searched the context graph", tool="search_context") == 0
    assert state.add_step(kind="metric", label="Ran metric x", tool="run_metric") == 1
    assert state.steps[0]["note"] == "Checking the catalog first." and state.steps[1]["note"] is None


async def test_toolbox_records_a_step_per_gateway_call_with_handle_and_timing():
    # FakeGateway: search_context -> {"metrics": [{"id": "open_breaks"}], ...}; run_metric -> summary()
    ...
    kinds = [(s["kind"], s["tool"]) for s in state.steps]
    assert kinds == [("context", "search_context"), ("metric", "run_metric")]
    assert state.steps[0]["considered"] == ["open_breaks"]
    assert state.steps[1]["handle"] == "r_aaaaaaaaaaaa" and state.steps[1]["status"] == "ok"
    assert state.steps[1]["label"] == "Ran metric open_breaks by region"
    assert state.steps[1]["ms"] >= 0


async def test_toolbox_records_a_failed_call_as_an_error_step():
    # FakeGateway run_metric -> GatewayError("not_permitted", "no")
    ...
    assert state.steps[-1]["status"] == "error" and state.steps[-1]["error_code"] == "not_permitted"


async def test_delegate_nests_subagent_steps_under_the_delegate_step():
    # reuse test_delegate_runs_a_subagent_with_only_gateway_tools's setup
    ...
    delegate = next(s for s in state.steps if s["kind"] == "delegate")
    children = [s for s in state.steps if s["parent"] == delegate["seq"]]
    assert children and all(s["tool"] in ("search_context", "run_metric", "query_source") for s in children)
```

Fill each `...` from the closest existing test in the same file (they already construct `ToolBox`, `FakeGateway`
and scripted models); keep the assertions exactly as written.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest -q tests/agent/test_runner.py tests/agent/test_tools.py`
Expected: FAIL (`observe` unexpected keyword; `add_step` missing)

- [ ] **Step 3: Implement**

`state.py` (RunState fields and method):

```python
    steps: list[dict] = field(default_factory=list)
    pending_note: str | None = None

    def add_step(self, **fields) -> int:
        seq = len(self.steps)
        step = {"seq": seq, "parent": None, "kind": "error", "label": "", "note": None, "considered": [],
                "ms": None, "status": "ok", "error_code": None, "tool": None, "args": None, "handle": None}
        step.update(fields)
        if self.pending_note and step.get("note") is None:
            step["note"] = self.pending_note[:500]
        self.pending_note = None
        self.steps.append(step)
        return seq
```

`runner.py`: add `observe: Callable[[str], None] | None = None` to `__init__` (store as `self._observe`), and in
`_loop` right after `uses = [...]`:

```python
            if uses and self._observe is not None:
                note = "".join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
                if note:
                    self._observe(note)
```

`tools.py`:

```python
STEP_KIND = {"search_context": "context", "run_metric": "metric", "query_source": "query", "combine": "combine",
             "delegate": "delegate", "visualize": "visualize"}


def step_label(name: str, args: dict) -> str:
    if name == "search_context":
        return "Searched the context graph"
    if name == "run_metric":
        dims = ", ".join(args.get("dimensions") or [])
        return f"Ran metric {args.get('metric_id')}" + (f" by {dims}" if dims else "")
    if name == "query_source":
        return f"Queried {args.get('source')}"
    if name == "combine":
        return f"Combined {len(args.get('handles') or {})} results"
    if name == "delegate":
        return f"Delegated to {args.get('source')}: {str(args.get('sub_question') or '')[:120]}"
    if name == "visualize":
        return "Built the dashboard"
    return name
```

In `_gateway_tool(self, name, args, created, parent=None)`: time the call with `time.monotonic()`; on
`GatewayError` call `self._state.add_step(kind=STEP_KIND.get(name, "error"), label=step_label(name, args),
tool=name, args=args, parent=parent, ms=..., status="error", error_code=exc.code)` before returning the error
outcome; on success add a step with `handle` (None for search_context) and, for search_context,
`considered=[m.get("id") for m in out.get("metrics", []) if isinstance(m, dict) and m.get("id")][:20]`.
`subagent_handler(..., _created, parent=None)` passes `parent` through. In `_delegate`, first
`seq = self._state.add_step(kind="delegate", label=step_label("delegate", args), tool="delegate",
args={"source": source, "sub_question": sub_question})`, then the inner handler calls
`self.subagent_handler(name, a, created, parent=seq)`; after the run set `self._state.steps[seq]["ms"]` and, on
`RunLimitExceeded`, `status="error", error_code="subagent_limit"`. In `_visualize`, add a `visualize` step (with
`handle=handles[0]`) after the spec is built. `import time` at the top.

`service.py` `_supervise`: pass `observe=lambda text: setattr(state, "pending_note", text)` to the supervisor
`MessagesRunner` (subagent runners get no observer).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -q tests/agent && uv run ruff check prism/agent tests/agent`
Expected: all passed except the known `.env`-dependent `test_agent_settings_defaults`; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add backend/prism/agent/runner.py backend/prism/agent/state.py backend/prism/agent/tools.py backend/prism/agent/service.py backend/tests/agent/test_runner.py backend/tests/agent/test_tools.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(agent): collect a decision trace per run (steps, notes, nested delegate steps)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Agent writes the trace and serves it

**Files:**
- Modify: `backend/prism/agent/service.py` (`_record_trace` before telemetry), `backend/prism/agent/api.py` (`GET /runs/{run_id}/trace`; confirm with `run_id`)
- Test: `backend/tests/agent/test_service.py`, `backend/tests/agent/test_api.py`

**Interfaces:**
- Consumes: gateway tools `record_trace`, `get_trace`, `mark_trace_confirmed` (Task 2); `RunState.steps` (Task 3).
- Produces: `GET /runs/{run_id}/trace` (404 / 429 / 502 like `/lineage`); `POST /answers/{record_id}/confirm?run_id=<32 hex>`.

- [ ] **Step 1: Write the failing tests**

Service tests (use the existing scripted-model + FakeGateway helpers of the chat tests):

```python
async def test_trace_is_recorded_before_telemetry():
    # scripted: one run_metric then a final text; FakeGateway answers run_metric, record_answer, record_trace
    events, gw = ...   # run AgentService.chat and collect events; keep the FakeGateway
    names = [c[0] for c in gw.calls]
    assert names.index("record_trace") > names.index("run_metric")
    assert events[-1]["type"] == "telemetry"
    args = dict(gw.calls)["record_trace"]
    assert args["run_id"] == events[-1]["run_id"] and args["status"] == "ok"
    assert [s["kind"] for s in args["steps"]][-1] == "answer"
    assert args["question"] and "sub" not in args


async def test_failed_run_records_an_error_step():
    # scripted model raises ModelError (not retryable)
    ...
    args = dict(gw.calls)["record_trace"]
    assert args["status"] == "error" and args["steps"][-1]["kind"] == "error"


async def test_trace_write_failure_never_changes_the_answer(caplog):
    # FakeGateway record_trace -> GatewayError("context_unavailable", "down")
    ...
    assert [e["type"] for e in events][-2:] == ["answer", "telemetry"] or events[-1]["type"] == "telemetry"
    assert "trace_write_failed" in caplog.text and "down" not in caplog.text
```

API tests:

```python
TRACE = {"run_id": "a" * 32, "question": "q", "answer": "a", "path": "metric", "status": "ok", "confirmed": False,
         "created_at": 1, "steps": []}


def test_trace_passthrough_and_error_mapping():
    h = {"Authorization": f"Bearer {token()}"}
    gw = FakeGateway({"get_trace": TRACE})
    c = app_with(gw)
    assert c.get(f"/runs/{'a' * 32}/trace", headers=h).json() == TRACE
    assert gw.calls == [("get_trace", {"run_id": "a" * 32})]
    assert c.get("/runs/nope/trace", headers=h).status_code == 404 and len(gw.calls) == 1
    assert c.get(f"/runs/{'a' * 32}/trace").status_code == 401
    for code, status in (("unknown_trace", 404), ("rate_limited", 429), ("context_unavailable", 502)):
        r = app_with(FakeGateway({"get_trace": GatewayError(code, "secret")})).get(f"/runs/{'a' * 32}/trace", headers=h)
        assert r.status_code == status and "secret" not in r.text


def test_confirm_with_run_id_marks_the_trace_and_ignores_its_failure():
    h = {"Authorization": f"Bearer {token()}"}
    rid = "11111111-1111-4111-8111-111111111111"
    gw = FakeGateway({"confirm_answer": {"confirmed": True}, "mark_trace_confirmed": GatewayError("unknown_trace", "x")})
    r = app_with(gw).post(f"/answers/{rid}/confirm?run_id={'a' * 32}", headers=h)
    assert r.status_code == 204
    assert [c[0] for c in gw.calls] == ["confirm_answer", "mark_trace_confirmed"]
    gw2 = FakeGateway({"confirm_answer": {"confirmed": True}})
    assert app_with(gw2).post(f"/answers/{rid}/confirm?run_id=bad", headers=h).status_code == 204
    assert [c[0] for c in gw2.calls] == ["confirm_answer"]
```

Fill the service-test `...` from the existing chat tests in the same file; keep the assertions.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest -q tests/agent -k "trace"`
Expected: FAIL (no record_trace call; route missing)

- [ ] **Step 3: Implement**

`service.py`:

```python
RUN_ID = re.compile(r"^[0-9a-f]{32}$")


    async def _record_trace(self, gateway: GatewayPort, state: RunState, status: str, text: str) -> None:
        """Best effort, before telemetry; never raises and never changes the answer."""
        kind = "error" if status in ("error", "limit") else "refusal" if status == "refused" else "answer"
        label = {"answer": "Answered", "refusal": "Declined", "error": "Stopped with an error"}[kind]
        state.add_step(kind=kind, label=label, status="ok" if kind == "answer" else kind,
                       error_code=state.error_code)
        try:
            await gateway.call("record_trace", {
                "run_id": state.run_id, "question": state.question, "answer": (text or "")[:2000],
                "path": self._path(state), "status": status, "steps": state.steps[:40]})
        except Exception as exc:  # noqa: BLE001 - the trace is a side record
            log.warning("trace_write_failed: %s", getattr(exc, "code", type(exc).__name__))
```

Call it at the end of `_converse` on every path: after the `failure` branch's error event
(`status="limit" if failure == "run_limit" else "error"`), after the `gateway_unavailable` branch (`"error"`), and
after the answer/record_answer path with `run.status` and the summary text (the `summary` event's text). Use
`await self._record_trace(gateway, state, run.status, summary_text)` so it completes before `chat()` yields
telemetry.

`api.py`:

```python
RUN_ID = re.compile(r"^[0-9a-f]{32}$")


    @app.get("/runs/{run_id}/trace")
    async def run_trace(run_id: str, user: UserContext = Depends(current_user)) -> dict:
        if not RUN_ID.match(run_id):
            raise HTTPException(404, "not found")
        try:
            async with factory(user) as gateway:
                return await gateway.call("get_trace", {"run_id": run_id})
        except GatewayError as exc:
            if exc.code in ("unknown_trace", "not_permitted"):
                raise HTTPException(404, "not found") from None
            if exc.code == "rate_limited":
                raise HTTPException(429, "too many requests") from None
            raise HTTPException(502, "data service unavailable") from None
```

`confirm_answer(record_id, run_id: str | None = Query(None), ...)`: after the successful `confirm_answer` call and
inside the same `async with`, `if run_id and RUN_ID.match(run_id):` call `mark_trace_confirmed` in its own
`try/except GatewayError: pass`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -q tests/agent && uv run ruff check prism/agent tests/agent`
Expected: all passed except the known `test_agent_settings_defaults`; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add backend/prism/agent/service.py backend/prism/agent/api.py backend/tests/agent
git -c user.email=cwijayasundara@gmail.com commit -m "feat(agent): record the run's trace before telemetry; GET /runs/{run_id}/trace; confirm marks it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Frontend data layer (schema, API, timeline mapper, highlight, run ids)

**Files:**
- Modify: `frontend/lib/schemas.ts`, `frontend/lib/api.ts`, `frontend/lib/graph.ts`, `frontend/lib/canvas.ts`
- Create: `frontend/lib/trace.ts`
- Test: `frontend/tests/trace.test.ts`, `frontend/tests/graph.test.ts`, `frontend/tests/canvas.test.ts`

**Interfaces:**
- Produces:
  - zod `Trace`, `TraceStep`, `TraceNode`; `type Trace`, `type TraceStep`.
  - `api.trace(token, runId): Promise<Trace>`; `api.confirm(token, recordId, runId?)` (adds `?run_id=` when given).
  - `lib/trace.ts`: `STEP_KIND_LABEL: Record<string, string>`, `formatMs(ms: number | null | undefined): string`, `touchedIds(t: Trace): Set<string>`, `stepRows(t: Trace): { step: TraceStep; depth: number }[]` (children right after their parent, `depth` 1).
  - `toGraphOption(g, { highlight?: Set<string> } = {})`: highlighted nodes get `itemStyle.borderColor = "#e0a526"` and `borderWidth = 3`.
  - `CanvasItem.runId?: string`; `CanvasAction` `{ type: "setRun"; turnId: string; runId: string }` sets `runId` on items whose key starts with `${turnId}:`.

- [ ] **Step 1: Write the failing tests**

`tests/trace.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { Trace } from "@/lib/schemas";
import { formatMs, stepRows, touchedIds } from "@/lib/trace";

const T = Trace.parse({ run_id: "a".repeat(32), question: "q", answer: "a", path: "metric", status: "ok",
  confirmed: true, created_at: 1, steps: [
    { seq: 0, parent: null, kind: "delegate", label: "Delegated to cashrecon: x", note: null, considered: [], ms: 900,
      status: "ok", error_code: null, tool: "delegate", args: null, handle: null, rows: null, truncated: null, touched: [] },
    { seq: 1, parent: 0, kind: "metric", label: "Ran metric open_breaks", note: "first", considered: [], ms: 40,
      status: "ok", error_code: null, tool: "run_metric", args: "{\"metric_id\":\"open_breaks\"}",
      handle: "r_aaaaaaaaaaaa", rows: 12, truncated: false,
      touched: [{ id: "metric:open_breaks", kind: "Metric", label: "open_breaks" }] },
    { seq: 2, parent: null, kind: "answer", label: "Answered", note: null, considered: [], ms: null, status: "ok",
      error_code: null, tool: null, args: null, handle: null, rows: null, truncated: null, touched: [] },
  ] });

describe("trace helpers", () => {
  it("orders children under their delegate step", () => {
    expect(stepRows(T).map((r) => [r.step.seq, r.depth])).toEqual([[0, 0], [1, 1], [2, 0]]);
  });
  it("collects touched node ids", () => {
    expect([...touchedIds(T)]).toEqual(["metric:open_breaks"]);
  });
  it("formats durations", () => {
    expect([formatMs(40), formatMs(1250), formatMs(null)]).toEqual(["40 ms", "1.3 s", ""]);
  });
});
```

`tests/graph.test.ts` (append):

```ts
  it("rings highlighted nodes", () => {
    const s = (toGraphOption(g, { highlight: new Set(["metric:open_breaks"]) }) as any).series[0];
    expect(s.data[0].itemStyle.borderWidth).toBe(3);
    expect(s.data[1].itemStyle.borderWidth).toBeUndefined();
  });
```

Canvas reducer test (append to the existing canvas test file):

```ts
  it("setRun attaches the run id to the turn's items only", () => {
    let items = canvasReducer([], { type: "add", item: { key: "t1:w1", widget: W, info: null, origin: "q", status: "ok" } });
    items = canvasReducer(items, { type: "add", item: { key: "t2:w1", widget: W, info: null, origin: "q", status: "ok" } });
    items = canvasReducer(items, { type: "setRun", turnId: "t1", runId: "a".repeat(32) });
    expect(items.find((i) => i.key === "t1:w1")!.runId).toBe("a".repeat(32));
    expect(items.find((i) => i.key === "t2:w1")!.runId).toBeUndefined();
  });
```

(`W` = any widget fixture already in that file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run tests/trace.test.ts tests/graph.test.ts < /dev/null`
Expected: FAIL (cannot resolve `@/lib/trace`; no `Trace` export)

- [ ] **Step 3: Implement**

`lib/schemas.ts`:

```ts
export const TraceNode = z.object({ id: z.string(), kind: z.enum(LINEAGE_KINDS), label: z.string() });
export const TraceStep = z.object({
  seq: z.number(), parent: z.number().nullable(), kind: z.string(), label: z.string(), note: z.string().nullable(),
  considered: z.array(z.string()).default([]), ms: z.number().nullable(), status: z.string().nullable(),
  error_code: z.string().nullable(), tool: z.string().nullable(), args: z.string().nullable(),
  handle: z.string().nullable(), rows: z.number().nullable(), truncated: z.boolean().nullable(),
  touched: z.array(TraceNode).default([]),
});
export type TraceStep = z.infer<typeof TraceStep>;
export const Trace = z.object({ run_id: z.string(), question: z.string().nullable(), answer: z.string().nullable(),
  path: z.string().nullable(), status: z.string().nullable(), confirmed: z.boolean(), created_at: z.number(),
  steps: z.array(TraceStep) });
export type Trace = z.infer<typeof Trace>;
```

`lib/api.ts`:

```ts
  async trace(token: string, runId: string): Promise<Trace> {
    return Trace.parse(await json(await request(`/runs/${encodeURIComponent(runId)}/trace`, { token })));
  },
  async confirm(token: string, recordId: string, runId?: string): Promise<void> {
    const q = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
    await request(`/answers/${encodeURIComponent(recordId)}/confirm${q}`, { token, method: "POST" });
  },
```

`lib/trace.ts`:

```ts
import type { Trace, TraceStep } from "@/lib/schemas";

export const STEP_KIND_LABEL: Record<string, string> = {
  context: "Context", metric: "Metric", query: "Query", combine: "Combine", delegate: "Delegate",
  visualize: "Dashboard", answer: "Answer", refusal: "Declined", error: "Error",
};

export function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function touchedIds(t: Trace): Set<string> {
  return new Set(t.steps.flatMap((s) => s.touched.map((n) => n.id)));
}

export function stepRows(t: Trace): { step: TraceStep; depth: number }[] {
  const top = t.steps.filter((s) => s.parent === null);
  return top.flatMap((s) => [{ step: s, depth: 0 },
    ...t.steps.filter((c) => c.parent === s.seq).map((c) => ({ step: c, depth: 1 }))]);
}
```

`lib/graph.ts`: change the signature to `toGraphOption(g: Lineage, opts: { highlight?: Set<string> } = {})` and the
node `itemStyle` to `{ color: nodeColor(n), ...(opts.highlight?.has(n.id) ? { borderColor: "#e0a526", borderWidth: 3 } : {}) }`.

`lib/canvas.ts`: `runId?: string` on `CanvasItem`; the `setRun` action:

```ts
    case "setRun":
      return items.map((i) => (i.key.startsWith(`${action.turnId}:`) ? { ...i, runId: action.runId } : i));
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run < /dev/null && ./node_modules/.bin/tsc --noEmit < /dev/null && npx eslint app components lib < /dev/null`
Expected: all pass; no type or lint errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/schemas.ts frontend/lib/api.ts frontend/lib/graph.ts frontend/lib/canvas.ts frontend/lib/trace.ts frontend/tests/trace.test.ts frontend/tests/graph.test.ts frontend/tests/canvas.test.ts
git -c user.email=cwijayasundara@gmail.com commit -m "feat(ui): trace schema and API, timeline helpers, graph highlight, canvas run ids

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

### Task 6: Reasoning tab, "How I got this", wiring

**Files:**
- Create: `frontend/components/ReasoningTimeline.tsx`
- Modify: `frontend/components/ContextGraphDialog.tsx` (tabs; optional `handle`; `runId`; `initialTab`; highlight), `frontend/components/ContextGraph.tsx` (`highlight` prop), `frontend/components/WidgetCard.tsx` (pass `runId`), `frontend/components/AssistantPanel.tsx` ("How I got this", `onRun`, `onExplain`, confirm with run id), `frontend/components/Workspace.tsx` (dispatch `setRun`; explain dialog)
- Test: `frontend/tests/reasoning.test.tsx`, `frontend/tests/contextgraph.test.tsx`, `frontend/tests/assistant.test.tsx`

**Interfaces:**
- Consumes: Task 5 `api.trace`, `stepRows`, `formatMs`, `touchedIds`, `STEP_KIND_LABEL`, `toGraphOption(g, {highlight})`, `CanvasItem.runId`, `setRun`.
- Produces:
  - `ReasoningTimeline({ trace, onNode })` — pure view of a loaded trace; `onNode(id)` on chip click.
  - `ContextGraphDialog({ open, onOpenChange, title, handle?, runId?, initialTab? = "graph" })`; tabs "Graph" | "Reasoning"; Graph disabled without `handle`; Reasoning loads `api.trace` when shown and `runId` is set; copy constants `TRACE_MISSING`, `TRACE_UNAVAILABLE`, `TRACE_NONE` (the three verbatim strings from Global Constraints).
  - `AssistantPanel({ onWidget, onRun?, onExplain?, examples })`: `onRun(turnId, runId)` when a turn's telemetry arrives; "How I got this" button on a turn with telemetry calls `onExplain({ runId, handle?, title })` (`handle` = the turn's first widget handle if any, `title` = the question).

- [ ] **Step 1: Write the failing tests**

`tests/reasoning.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ReasoningTimeline } from "@/components/ReasoningTimeline";
import { Trace } from "@/lib/schemas";

const T = Trace.parse({ run_id: "a".repeat(32), question: "q", answer: "Vendor A.", path: "metric", status: "ok",
  confirmed: true, created_at: 1, steps: [
    { seq: 0, parent: null, kind: "metric", label: "Ran metric price_conflicts by vendor_id", note: "Check conflicts",
      considered: [], ms: 1250, status: "ok", error_code: null, tool: "run_metric",
      args: "{\"metric_id\":\"price_conflicts\"}", handle: "r_aaaaaaaaaaaa", rows: 4, truncated: false,
      touched: [{ id: "metric:price_conflicts", kind: "Metric", label: "price_conflicts" }] },
    { seq: 1, parent: null, kind: "error", label: "Queried cashrecon", note: null, considered: [], ms: 10,
      status: "error", error_code: "not_permitted", tool: "query_source", args: null, handle: null, rows: null,
      truncated: null, touched: [] },
    { seq: 2, parent: null, kind: "answer", label: "Answered", note: null, considered: [], ms: null, status: "ok",
      error_code: null, tool: null, args: null, handle: null, rows: null, truncated: null, touched: [] },
  ] });

describe("ReasoningTimeline", () => {
  it("lists steps with rows, duration, note, args and badges", async () => {
    render(<ReasoningTimeline trace={T} onNode={vi.fn()} />);
    expect(screen.getByText("Ran metric price_conflicts by vendor_id")).toBeInTheDocument();
    expect(screen.getByText(/4 rows · 1\.3 s/)).toBeInTheDocument();
    expect(screen.getByText("Check conflicts")).toBeInTheDocument();
    expect(screen.getByText("not_permitted")).toBeInTheDocument();
    expect(screen.getByText("Confirmed")).toBeInTheDocument();
    await userEvent.click(screen.getAllByText("Arguments")[0]);
    expect(screen.getByText(/"metric_id":"price_conflicts"/)).toBeInTheDocument();
  });

  it("a touched-node chip reports its id", async () => {
    const onNode = vi.fn();
    render(<ReasoningTimeline trace={T} onNode={onNode} />);
    await userEvent.click(screen.getByRole("button", { name: "price_conflicts" }));
    expect(onNode).toHaveBeenCalledWith("metric:price_conflicts");
  });
});
```

`tests/contextgraph.test.tsx` (append; reuse the file's mocks, and add `trace` to the mocked `api`):

```tsx
  it("opens on Reasoning, and graph tab is disabled without a handle", async () => {
    trace.mockResolvedValueOnce(T_MIN);   // a minimal Trace with one metric step touching metric:open_breaks
    render(<ContextGraphDialog open onOpenChange={vi.fn()} title="q" runId={"a".repeat(32)} initialTab="reasoning" />);
    expect(await screen.findByText("Answered")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Graph" })).toBeDisabled();
  });

  it("missing trace and no run id show their notices", async () => {
    trace.mockRejectedValueOnce(new ApiError(404));
    const { unmount } = render(<ContextGraphDialog open onOpenChange={vi.fn()} title="q" handle="r_aaaaaaaaaaaa"
      runId={"a".repeat(32)} initialTab="reasoning" />);
    expect(await screen.findByText(TRACE_MISSING)).toBeInTheDocument();
    unmount();
    render(<ContextGraphDialog open onOpenChange={vi.fn()} title="q" handle="r_aaaaaaaaaaaa" initialTab="reasoning" />);
    expect(screen.getByText(TRACE_NONE)).toBeInTheDocument();
  });

  it("a chip switches to the graph tab with that node selected", async () => {
    lineage.mockResolvedValue(G);
    trace.mockResolvedValueOnce(T_MIN);
    render(<ContextGraphDialog open onOpenChange={vi.fn()} title="q" handle="r_aaaaaaaaaaaa" runId={"a".repeat(32)}
      initialTab="reasoning" />);
    await userEvent.click(await screen.findByRole("button", { name: "open_breaks" }));
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("Open cash breaks")).toBeInTheDocument();   // node detail panel
  });
```

`tests/assistant.test.tsx` (append):

```tsx
  it("offers How I got this once telemetry arrives and reports the run id", async () => {
    chat.mockImplementation(async function* () {
      yield widget;
      yield { type: "summary", text: "Done." };
      yield { type: "telemetry", run_id: "a".repeat(32), path: "metric", models: ["m"], input_tokens: 1,
        output_tokens: 1, cache_read_input_tokens: 0, llm_turns: 1, tool_calls: 1, tool_latency_ms: 1, cost_usd: 0 };
    });
    const onRun = vi.fn(), onExplain = vi.fn();
    render(<AssistantPanel onWidget={vi.fn()} onRun={onRun} onExplain={onExplain} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "Q?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await userEvent.click(await screen.findByRole("button", { name: "How I got this" }));
    expect(onRun).toHaveBeenCalledWith(expect.any(String), "a".repeat(32));
    expect(onExplain).toHaveBeenCalledWith({ runId: "a".repeat(32), handle: "r_aaaaaaaaaaaa", title: "Q?" });
  });
```

Also update the existing confirm test expectation to `confirm` being called with `("T", recordId, runId)` when the
turn has telemetry (or `undefined` when not).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run tests/reasoning.test.tsx tests/contextgraph.test.tsx tests/assistant.test.tsx < /dev/null`
Expected: FAIL

- [ ] **Step 3: Implement `components/ReasoningTimeline.tsx`**

```tsx
"use client";
import { AlertCircle, CircleCheck } from "lucide-react";
import type { Trace } from "@/lib/schemas";
import { STEP_KIND_LABEL, formatMs, stepRows } from "@/lib/trace";

export function ReasoningTimeline({ trace, onNode }: { trace: Trace; onNode: (id: string) => void }) {
  return (
    <ol className="space-y-3 text-sm" aria-label="Reasoning steps">
      {stepRows(trace).map(({ step, depth }) => {
        const facts = [step.rows != null ? `${step.rows} rows` : "", formatMs(step.ms)].filter(Boolean).join(" · ");
        const failed = step.status === "error" || step.kind === "error" || step.kind === "refusal";
        return (
          <li key={step.seq} className={`rounded-lg border bg-white px-3 py-2 ${depth ? "ml-6" : ""}`}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded bg-[var(--prism-paper)] px-1.5 py-0.5 text-[11px] font-medium text-[var(--prism-muted)]">
                {STEP_KIND_LABEL[step.kind] ?? step.kind}</span>
              <span className="font-medium text-[var(--prism-ink)]">{step.label}</span>
              {facts && <span className="text-xs text-muted-foreground">{facts}</span>}
              {failed && step.error_code && <span className="inline-flex items-center gap-1 rounded bg-[#fbecef] px-1.5 py-0.5 text-[11px] text-[var(--prism-crimson)]">
                <AlertCircle className="size-3" aria-hidden />{step.error_code}</span>}
              {step.kind === "answer" && trace.confirmed && <span className="inline-flex items-center gap-1 text-xs font-medium text-[var(--src-cashrecon)]">
                <CircleCheck className="size-3.5" aria-hidden />Confirmed</span>}
            </div>
            {step.note && <p className="mt-1 text-xs text-muted-foreground italic">{step.note}</p>}
            {step.kind === "answer" && trace.answer && <p className="mt-1 text-[var(--prism-ink)]">{trace.answer}</p>}
            {step.considered.length > 0 && <p className="mt-1 text-xs text-muted-foreground">Considered: {step.considered.join(", ")}</p>}
            {step.touched.length > 0 && <div className="mt-2 flex flex-wrap gap-1.5">
              {step.touched.map((n) => <button key={n.id} type="button" onClick={() => onNode(n.id)}
                className="rounded-full border px-2 py-0.5 text-xs hover:bg-[var(--prism-paper)]">{n.label}</button>)}
            </div>}
            {step.args && <details className="mt-2 text-xs">
              <summary className="cursor-pointer text-muted-foreground">Arguments</summary>
              <pre className="mt-1 overflow-x-auto rounded bg-[var(--prism-paper)] p-2 whitespace-pre-wrap break-all">{step.args}</pre>
            </details>}
          </li>
        );
      })}
    </ol>
  );
}
```

- [ ] **Step 4: Implement the dialog tabs (`ContextGraphDialog.tsx`)**

- Props: `{ open, onOpenChange, title, handle?: string, runId?: string, initialTab?: "graph" | "reasoning" }`.
- State: `tab` (reset to `initialTab ?? (handle ? "graph" : "reasoning")` when `open` becomes true), the existing
  lineage `load` (fetched only when `open && handle`), and `traceLoad: {kind: "idle"|"loading"} | {kind: "ok"; trace}
  | {kind: "missing"} | {kind: "error"}` fetched when `open && runId` (same `cancelled` guard, 404 -> missing,
  `Unauthorized` ignored, Retry bumps a nonce).
- Tabs: a `role="tablist"` with two `role="tab"` buttons ("Graph", "Reasoning"), `aria-selected`, Graph `disabled`
  when `!handle`; panels with `role="tabpanel"`.
- Reasoning panel: `!runId` -> `<Notice>{TRACE_NONE}</Notice>`; loading -> skeleton; missing -> `TRACE_MISSING`;
  error -> `TRACE_UNAVAILABLE` + Retry; ok -> `<ReasoningTimeline trace={...} onNode={(id) => { setSelected(id);
  setTab("graph"); }} />`.
- Graph panel: existing `Body`, passing `highlight={traceLoad.kind === "ok" ? touchedIds(traceLoad.trace) : undefined}`
  through to `ContextGraph` (new optional `highlight?: Set<string>` prop forwarded to `toGraphOption`).
- Export `TRACE_MISSING = "This reasoning trace has expired or is not available."`,
  `TRACE_UNAVAILABLE = "The reasoning trace is unavailable."`,
  `TRACE_NONE = "This widget was re-run from a saved dashboard; it has no reasoning trace."`.
- Keep `selected` in the dialog (lifted out of `Body`) so the chip click survives the tab switch.

- [ ] **Step 5: Wire the panel and the workspace**

- `WidgetCard`: pass `runId={item.runId}` to `ContextGraphDialog`.
- `AssistantPanel`: new optional props `onRun?: (turnId: string, runId: string) => void` and
  `onExplain?: (x: { runId: string; handle?: string; title: string }) => void`. In `ask()`, when an event of type
  `telemetry` arrives call `onRun?.(id, event.run_id)`. Remember each turn's first widget handle (a `useRef` map
  `turnId -> handle` filled in the `widget` branch). `TurnView` gets `onExplain?: () => void` and renders, when
  `turn.telemetry`, a small link button "How I got this" (lucide `Route` icon) next to "Run details" that calls it.
  `confirmTurn` calls `api.confirm(token, a.recordId, turn.telemetry?.run_id)`.
- `Workspace`: `const onRun = useCallback((turnId, runId) => dispatch({ type: "setRun", turnId, runId }), [])`;
  `const [explain, setExplain] = useState<{ runId: string; handle?: string; title: string } | null>(null)`;
  pass `onRun` and `onExplain={setExplain}` to `AssistantPanel`; render
  `{explain && <ContextGraphDialog open onOpenChange={(o) => { if (!o) setExplain(null); }} title={explain.title}
  handle={explain.handle} runId={explain.runId} initialTab="reasoning" />}`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd frontend && npx vitest run < /dev/null && ./node_modules/.bin/tsc --noEmit < /dev/null && npx eslint app components lib < /dev/null`
Expected: all pass; no type or lint errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/components/ReasoningTimeline.tsx frontend/components/ContextGraphDialog.tsx frontend/components/ContextGraph.tsx frontend/components/WidgetCard.tsx frontend/components/AssistantPanel.tsx frontend/components/Workspace.tsx frontend/tests/reasoning.test.tsx frontend/tests/contextgraph.test.tsx frontend/tests/assistant.test.tsx
git -c user.email=cwijayasundara@gmail.com commit -m "feat(ui): Reasoning tab and How I got this: decision timeline with links into the context graph

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Smoke test, docs, live check

**Files:**
- Modify: `frontend/e2e/smoke.spec.ts`, `frontend/e2e/fixtures.ts`, `README.md`

- [ ] **Step 1: Extend the mocked smoke test**

`fixtures.ts`: add `export const TRACE = {...}` (run_id = the telemetry `run_id` of `chatBody`, i.e. `"r1"` today —
change the fixture's telemetry `run_id` to `"a".repeat(32)` and use the same value; one `metric` step touching
`metric:open_breaks`, one `answer` step). `smoke.spec.ts`: route `/runs/` -> `json(TRACE)`; after the answer:

```ts
    await page.getByRole("button", { name: "How I got this" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("tab", { name: "Reasoning" })).toHaveAttribute("aria-selected", "true");
    await expect(dialog.getByText("Ran metric open_breaks by region")).toBeVisible();
    await dialog.getByRole("button", { name: "open_breaks" }).click();
    await expect(dialog.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("Escape");
```

- [ ] **Step 2: Run the smoke test**

Run: `cd frontend && npx playwright test e2e/smoke.spec.ts < /dev/null`
Expected: `1 passed, 1 skipped`

- [ ] **Step 3: README**

Gateway section: "eight tools" -> "eleven tools"; table rows for `record_trace(run_id, question, answer, path,
status, steps)` ("the agent stores how one of your runs reached its answer; links come from your own handles"),
`get_trace(run_id)` ("one of your own traces, re-checked against your role; UI only"), `mark_trace_confirmed(run_id)`;
a "Reasoning traces" subsection under "Context graph": the node model, owner-only, `PRISM_TRACE_RETENTION_DAYS`
(default 7), survives `make graph`, never offered to the model. "Use it" step 3: "**How I got this** on an answer
shows the steps the agent took and the graph nodes each step used."

- [ ] **Step 4: Full checks**

Run: `cd backend && uv run pytest -q && uv run ruff check . ; cd ../frontend && npx vitest run < /dev/null && ./node_modules/.bin/tsc --noEmit < /dev/null`
Expected: green apart from the known `.env`-dependent `test_agent_settings_defaults`.

- [ ] **Step 5: Live check** (only with the user's go-ahead to restart, or after they restart: `scripts/start_backend.sh`, `scripts/start_frontend.sh`)

As `steward`, ask "Which price vendor drives the most corporate bond price conflicts?", press **How I got this**:
the timeline lists a context step, a `price_conflicts` metric step with rows and chips, and the answer; a chip opens
the Graph tab with that node ringed. Screenshot both tabs.

- [ ] **Step 6: Commit**

```bash
git add frontend/e2e/smoke.spec.ts frontend/e2e/fixtures.ts README.md
git -c user.email=cwijayasundara@gmail.com commit -m "test(ui): smoke covers the reasoning tab; docs: decision traces

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
