"""The gateway's real runtime (prepare_runtime + lifespan): local embedder, Neo4j context graph, app-DB audit writer.
Source servers are not needed: only search_context, policy refusals and record_answer are exercised here."""
import uuid

import psycopg
import pytest

from prism.config import APP_DB, Settings
from prism.gateway.server import create_app, prepare_runtime
from prism.mcp.client import mcp_client
from prism.security.personas import claims_for
from tests.gateway.test_server import body, serving, text, token
from tests.graph_ns import TEST_GRAPH_NS

pytestmark = [pytest.mark.db, pytest.mark.neo4j]

PREFIX = "testapp_"


def _rows(settings: Settings, table: str, sub: str) -> list[dict]:
    with psycopg.connect(settings.dsn(APP_DB, admin=True)) as conn:
        return [r[0] for r in conn.execute(
            f"SELECT row_to_json(t) FROM app.{table} t WHERE sub = %s ORDER BY id", (sub,)).fetchall()]


@pytest.fixture(scope="module")
def runtime_settings() -> Settings:
    settings = Settings(db_prefix=PREFIX, graph_ns=TEST_GRAPH_NS)   # the session graph, never the real namespace
    try:
        psycopg.connect(settings.dsn("postgres", admin=True), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable ({exc}). Start it with: make db", pytrace=False)
    return settings


async def test_real_runtime_serves_role_filtered_context_and_audits_every_call(runtime_settings, context_graph):
    sub = f"rt-{uuid.uuid4().hex[:10]}"
    claims = {**claims_for("cash_ops_emea"), "sub": sub}
    mcp, app = create_app(runtime_settings, runtime=prepare_runtime(runtime_settings))
    async with serving(app) as base:
        # no source servers here: stand in for a run_metric result of this caller (record_answer needs a live handle)
        handle = mcp._prism_state.gateway.store.put(sub, ["region", "value"], [["EMEA", 3]], {
            "source": "cashrecon", "metric_id": "open_breaks",
            "plan": {"metric_ids": ["open_breaks"], "dimensions": ["region"]}})
        async with mcp_client(f"{base}/mcp", token(runtime_settings, claims)) as c:
            pack = body(await c.call_tool("search_context", {"question": "open breaks by legal entity"}))
            hidden = await c.call_tool("run_metric", {"metric_id": "price_conflicts"})
            recorded = body(await c.call_tool("record_answer", {"question": "open breaks by legal entity",
                                                                "plan": "searched", "handles": [handle],
                                                                "verified": True}))
    ids = [m["id"] for m in pack["metrics"]]
    assert "open_breaks" in ids
    assert {m["source"] for m in pack["metrics"]} <= {"cashrecon", "feedhub"}
    assert hidden.is_error and "not_permitted" in text(hidden) and "marketmaster" not in text(hidden)
    assert recorded["recorded"] is True and recorded["verified"] is True
    audit = _rows(runtime_settings, "audit", sub)
    assert [(r["tool"], r["status"], r["error_code"]) for r in audit] == [
        ("search_context", "ok", None), ("run_metric", "error", "not_permitted"), ("record_answer", "ok", None)]
    assert audit[0]["question_hash"] and "open breaks" not in str(audit)
    (q,) = _rows(runtime_settings, "query_log", sub)
    assert q["verified"] is True and q["metric_ids"] == ["open_breaks"] and q["persona"] == "cash_ops_emea" and len(q["question_hash"]) == 64
