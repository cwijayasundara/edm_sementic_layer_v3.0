import pytest

from prism.agent.gateway_client import GatewayClient, GatewayError, parse_tool_error


@pytest.mark.parametrize("text,code,msg", [
    ("Error executing tool run_metric: not_permitted: no such metric", "not_permitted", "no such metric"),
    ("Error executing tool combine: invalid_sql: only SELECT", "invalid_sql", "only SELECT"),
    ("Error executing tool x: internal_error (ref ab12): boom", "internal_error", "boom"),
    ("something unexpected", "tool_error", "something unexpected"),
])
def test_parse_tool_error(text, code, msg):
    e = parse_tool_error(text)
    assert (e.code, e.message) == (code, msg)


def test_error_classes():
    assert GatewayError("not_permitted", "x").final and GatewayError("metrics_only", "x").final
    assert GatewayError("invalid_sql", "x").caller_fixable and GatewayError("grain_too_fine", "x").caller_fixable
    assert GatewayError("rate_limited", "x").retry_later and GatewayError("source_timeout", "x").retry_later
    other = GatewayError("source_error", "x")
    assert not (other.final or other.caller_fixable or other.retry_later)


def test_message_is_truncated():
    assert len(parse_tool_error("Error executing tool a: invalid_sql: " + "x" * 5000).message) <= 500


@pytest.mark.parametrize("url", ["http://gw:8000", "http://gw:8000/", "http://gw:8000/mcp", "http://gw:8000/mcp/"])
def test_endpoint_is_built_without_doubling_mcp(url):
    assert GatewayClient(url, "t")._url == "http://gw:8000/mcp"


async def test_non_text_error_block_yields_a_gateway_error():
    class Block:  # no .text attribute
        pass

    class Result:
        is_error, content, structured_content = True, [Block()], None

    class Fake:
        async def call_tool(self, tool, arguments):
            return Result()

    gw = GatewayClient("http://gw:8000", "t")
    gw._client = Fake()
    with pytest.raises(GatewayError) as e:
        await gw.call("run_metric", {})
    assert e.value.code == "tool_error"
