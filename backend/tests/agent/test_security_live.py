"""Live security checks: the scripted model drives the real agent tools against the real gateway, so the persona
policy that answers is the gateway's, not a fake's. Run with the stack up (`make test-live`); no API key needed."""
import pytest

from prism.agent.gateway_client import GatewayClient, GatewayError
from prism.agent.model import ScriptedModelClient, reply_text, reply_tools
from tests.agent.live_support import gateway_base, live_service, require_live, unique_question, user_for

pytestmark = pytest.mark.live


@pytest.fixture(scope="module", autouse=True)
def live():
    require_live()


async def run(persona, script):
    client = ScriptedModelClient(script)
    events = [e async for e in live_service(client).chat(user_for(persona), unique_question("security probe"))]
    return events, client


def tool_result(client, request_index):
    """The (single) tool result the model saw in request `request_index`."""
    return client.requests[request_index].messages[-1].content[0]


async def test_bi_analyst_is_refused_free_form_and_the_run_reports_a_refusal():
    events, client = await run("bi_analyst", [
        reply_tools(("query_source", {"source": "cashrecon", "request": {"sql": "SELECT * FROM breaks"}})),
        reply_text("I can only answer with governed metrics.")])
    result = tool_result(client, 1)
    assert result.is_error and "metrics_only" in result.content
    assert [e["type"] for e in events] == ["summary", "telemetry"]


async def test_steward_cannot_reach_cash_recon_metrics():
    events, client = await run("steward", [
        reply_tools(("run_metric", {"metric_id": "open_breaks"})), reply_text("Not available to you.")])
    result = tool_result(client, 1)
    assert result.is_error and "not_permitted" in result.content
    assert [e["type"] for e in events] == ["summary", "telemetry"]


async def test_one_users_handle_is_unreadable_by_another():
    async with GatewayClient(gateway_base(), user_for("head_data").token) as gw:
        handle = (await gw.call("run_metric", {"metric_id": "late_feeds", "dimensions": ["source_id"]}))["handle"]
        assert (await gw.call("get_rows", {"handle": handle, "offset": 0, "limit": 1}))["rows"]   # the owner can read
    async with GatewayClient(gateway_base(), user_for("head_data").token) as other:   # same persona, other sub
        with pytest.raises(GatewayError) as exc:
            await other.call("get_rows", {"handle": handle, "offset": 0, "limit": 5})
    assert exc.value.code == "unknown_handle"
