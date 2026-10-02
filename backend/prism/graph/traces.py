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
