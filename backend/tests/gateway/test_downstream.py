"""Downstream: per-(sub, source) concurrency cap, busy retry with jitter, error unwrapping, token hygiene, audit."""
import asyncio
import json
import logging

import httpx2
import jwt
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent

from prism.config import Settings
from prism.gateway.downstream import Downstream, SourceAuthRejected, SourceMetricResult, SourceQueryResult, mcp_call
from prism.gateway.errors import GatewayError
from prism.gateway.policy import MetricPlan
from prism.security.personas import claims_for

SETTINGS = Settings()
PLAN = MetricPlan(source="cashrecon", tool="run_metric", metric_id="open_breaks", unit="breaks",
                  arguments={"metric_id": "open_breaks", "dimensions": ["region"], "filters": {"region": "SECRETVAL"}})


def ok_metric(source="cashrecon", metric_id="open_breaks", truncated=False):
    return CallToolResult(content=[TextContent(type="text", text="2 rows")], structured_content={
        "source": source, "metric_id": metric_id, "unit": "breaks", "dimensions": ["region"],
        "rows": [{"region": "EMEA", "value": 3}, {"region": "APAC", "value": 2}], "row_count": 2,
        "truncated": truncated, "as_of": "2026-09-30", "window": None})


def tool_error(text):
    return CallToolResult(content=[TextContent(type="text", text=f"Error executing tool run_metric: {text}")],
                          is_error=True)


class FakeAudit:
    def __init__(self):
        self.events = []

    async def write(self, event):
        self.events.append(event)


class Fake:
    """Records tokens and concurrency; replies from a script (callable or list)."""

    def __init__(self, reply=None, delay=0.0):
        self.reply = reply or (lambda **k: ok_metric())
        self.delay = delay
        self.tokens, self.calls = [], []
        self.active = self.peak = 0
        self.by_sub: dict[str, int] = {}
        self.peak_by_sub: dict[str, int] = {}

    async def __call__(self, url, token, tool, arguments):
        sub = jwt.decode(token, options={"verify_signature": False})["sub"]
        self.tokens.append(token)
        self.calls.append((url, tool, arguments))
        self.active += 1
        self.by_sub[sub] = self.by_sub.get(sub, 0) + 1
        self.peak = max(self.peak, self.active)
        self.peak_by_sub[sub] = max(self.peak_by_sub.get(sub, 0), self.by_sub[sub])
        try:
            await asyncio.sleep(self.delay)
            r = self.reply(token=token, n=len(self.calls))
            if isinstance(r, BaseException):
                raise r
            return r
        finally:
            self.active -= 1
            self.by_sub[sub] -= 1


def make(fake, audit=None, **kw):
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    d = Downstream(SETTINGS, audit, call=fake, sleep=sleep, rand=lambda: 0.5, **kw)
    d.sleeps = sleeps
    return d


def assert_no_token(exc: BaseException, tokens):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        blob = f"{exc}|{exc!r}|{exc.args!r}"
        for t in tokens:
            assert t not in blob and t.split(".")[2] not in blob
        exc = exc.__cause__ or exc.__context__


# -------------------------------------------------------------------------------------------- happy path
async def test_run_metric_result_and_token():
    fake = Fake()
    r = await make(fake).run_metric(claims_for("cash_ops_emea"), PLAN)
    assert isinstance(r, SourceMetricResult)
    assert r.columns == ["region", "value"] and r.rows == [["EMEA", 3], ["APAC", 2]] and r.truncated is False
    assert r.source == "cashrecon" and r.metric_id == "open_breaks" and r.unit == "breaks" and r.row_count == 2
    url, tool, args = fake.calls[0]
    assert url == "http://127.0.0.1:8203/mcp" and tool == "run_metric" and args == PLAN.arguments
    claims = jwt.decode(fake.tokens[0], SETTINGS.jwt_secret.get_secret_value(), algorithms=["HS256"], audience="cashrecon-mcp")
    assert claims["exp"] - claims["iat"] == 60 and claims["sub"] == "cash_ops_emea"
    assert set(claims) <= {"sub", "roles", "scopes", "rows", "metrics_only", "aud", "iat", "exp"}


async def test_truncated_is_preserved():
    r = await make(Fake(lambda **k: ok_metric(truncated=True))).run_metric(claims_for("head_data"), PLAN)
    assert r.truncated is True


@pytest.mark.parametrize("reply", [ok_metric(source="refmaster"), ok_metric(metric_id="other"),
                                   CallToolResult(content=[], structured_content=None),
                                   CallToolResult(content=[], structured_content={"source": "cashrecon"})])
async def test_unexpected_reply_is_refused(reply):
    with pytest.raises(GatewayError) as info:
        await make(Fake(lambda **k: reply)).run_metric(claims_for("head_data"), PLAN)
    assert info.value.code == "source_unavailable"


async def test_query_shape_and_metrics_only():
    reply = CallToolResult(content=[], structured_content={"source": "cashrecon", "columns": ["a"], "rows": [[1]],
                                                           "row_count": 1, "truncated": True})
    fake = Fake(lambda **k: reply)
    d = make(fake)
    r = await d.query(claims_for("head_data"), "cashrecon", {"sql": "SELECT 1 AS a"})
    assert isinstance(r, SourceQueryResult) and r.columns == ["a"] and r.rows == [[1]] and r.truncated is True
    assert fake.calls[-1][1:] == ("query", {"request": {"sql": "SELECT 1 AS a"}})
    for claims in (claims_for("bi_analyst"), {k: v for k, v in claims_for("head_data").items()
                                               if k != "metrics_only"}):
        with pytest.raises(GatewayError) as info:
            await d.query(claims, "cashrecon", {"sql": "SELECT 1"})
        assert info.value.code == "metrics_only"
    for bad in ("SELECT 1", None, {f"k{i}": 1 for i in range(21)}):
        with pytest.raises(GatewayError) as info:
            await d.query(claims_for("head_data"), "cashrecon", bad)
        assert info.value.code == "invalid_request"
    with pytest.raises(GatewayError) as info:
        await d.query(claims_for("head_data"), "nosuch", {"sql": "SELECT 1"})
    assert info.value.code == "not_permitted"
    assert len(fake.calls) == 1


@pytest.mark.parametrize("claims", [{"scopes": ["cashrecon"]}, {"sub": ""}, {"sub": 5}, None])
async def test_bad_claims_are_refused_before_any_call(claims):
    fake = Fake()
    with pytest.raises(GatewayError) as info:
        await make(fake).run_metric(claims, PLAN)
    assert info.value.code == "not_permitted" and not fake.calls


# ----------------------------------------------------------------------------------------------- concurrency
async def test_semaphore_never_exceeds_the_cap_under_12_parallel_calls():
    fake = Fake(delay=0.03)
    d = make(fake)
    results = await asyncio.gather(*(d.run_metric(claims_for("head_data"), PLAN) for _ in range(12)))
    assert len(results) == 12 and fake.peak == 3


async def test_cap_is_per_sub_and_source():
    fake = Fake(delay=0.03)
    d = make(fake)
    other = MetricPlan(source="feedhub", tool="run_metric", metric_id="late_feeds", unit=None,
                       arguments={"metric_id": "late_feeds", "dimensions": [], "filters": {}})
    feed = Fake(delay=0.03, reply=lambda **k: ok_metric(source="feedhub", metric_id="late_feeds"))

    async def routed(url, token, tool, arguments):
        return await (feed if "8205" in url else fake)(url, token, tool, arguments)

    d = make(routed)
    await asyncio.gather(*(d.run_metric(claims_for(p), PLAN) for p in ("head_data", "bi_analyst") for _ in range(6)),
                         *(d.run_metric(claims_for("head_data"), other) for _ in range(6)))
    assert fake.peak_by_sub == {"head_data": 3, "bi_analyst": 3} and fake.peak == 6 and feed.peak == 3


# ------------------------------------------------------------------------------------------------- retries
async def test_retry_on_too_many_concurrent_requests_with_jittered_backoff():
    fake = Fake(lambda n, **k: tool_error("too many concurrent requests for this principal") if n <= 2
                else ok_metric())
    d = make(fake)
    r = await d.run_metric(claims_for("head_data"), PLAN)
    assert r.row_count == 2 and len(fake.calls) == 3
    assert len(d.sleeps) == 2 and d.sleeps[1] > d.sleeps[0] > 0
    assert len(set(fake.tokens)) >= 1


@pytest.mark.parametrize("text", ["too many concurrent requests for this principal", "the source is busy; retry shortly"])
async def test_busy_after_three_retries_is_source_busy(text):
    fake = Fake(lambda **k: tool_error(text))
    d = make(fake)
    with pytest.raises(GatewayError) as info:
        await d.run_metric(claims_for("head_data"), PLAN)
    assert info.value.code == "source_busy" and len(fake.calls) == 4 and len(d.sleeps) == 3


async def test_jitter_uses_the_random_source():
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    for r in (0.0, 0.999):
        fake = Fake(lambda n, **k: tool_error("the source is busy; retry shortly") if n == 1 else ok_metric())
        await Downstream(SETTINGS, None, call=fake, sleep=sleep, rand=lambda r=r: r).run_metric(
            claims_for("head_data"), PLAN)
    assert 0 < sleeps[0] < sleeps[1]


@pytest.mark.parametrize("text", [
    "busy", "column busy_flag does not exist", "the source is busy", "Too many concurrent requests",
    "unknown region 'the source is busy; retry shortly'",            # a caller-supplied value echoed by the source
    "the source is busy; retry shortly (and more)", "too many concurrent requests for this principal!",
])
async def test_only_the_sources_exact_busy_replies_are_retried(text):
    fake = Fake(lambda **k: tool_error(text))
    d = make(fake)
    with pytest.raises(GatewayError) as info:
        await d.run_metric(claims_for("head_data"), PLAN)
    assert info.value.code == "source_error" and len(fake.calls) == 1 and d.sleeps == []


def test_busy_phrases_are_the_ones_the_source_servers_raise():
    from prism.gateway.downstream import BUSY
    from prism.mcp import rest_backend, sql_backend
    from prism.mcp.results import SOURCE_BUSY_MESSAGES

    assert BUSY == SOURCE_BUSY_MESSAGES == frozenset({"too many concurrent requests for this principal",
                                                      "the source is busy; retry shortly"})
    import inspect

    for mod in (sql_backend, rest_backend):
        src = inspect.getsource(mod)
        assert "too many concurrent requests" not in src and "is busy" not in src   # they use the shared constants


# --------------------------------------------------------------------------------------------- error mapping
def group(*leaves):
    return ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("unhandled errors in a TaskGroup",
                                                                             list(leaves))])


@pytest.mark.parametrize("exc, code", [
    (group(SourceAuthRejected(401)), "source_auth"),
    (group(MCPError(-32603, "Server returned an error response"), SourceAuthRejected(403)), "source_auth"),
    (group(MCPError(-32603, "Server returned an error response")), "source_unavailable"),
    (group(httpx2.ConnectError("All connection attempts failed")), "source_unavailable"),
    (group(httpx2.ReadTimeout("timed out")), "source_timeout"),
    (group(TimeoutError()), "source_timeout"),
    (MCPError(-32603, "boom"), "source_unavailable"),
    (RuntimeError("weird"), "source_unavailable"),
])
async def test_exception_groups_are_unwrapped(exc, code):
    fake = Fake(lambda **k: exc)
    with pytest.raises(GatewayError) as info:
        await make(fake).run_metric(claims_for("head_data"), PLAN)
    assert info.value.code == code and len(fake.calls) == 1
    assert info.value.__cause__ is None and info.value.__context__ is None


async def test_slow_source_times_out():
    fake = Fake(delay=5)
    t = asyncio.get_running_loop().time()
    with pytest.raises(GatewayError) as info:
        await make(fake, timeout_s=0.2).run_metric(claims_for("head_data"), PLAN)
    assert info.value.code == "source_timeout" and asyncio.get_running_loop().time() - t < 2


async def test_cancellation_propagates():
    fake = Fake(delay=5)
    task = asyncio.ensure_future(make(fake).run_metric(claims_for("head_data"), PLAN))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_tool_error_is_the_sources_own_message():
    fake = Fake(lambda **k: tool_error("not entitled to this dataset"))
    with pytest.raises(GatewayError) as info:
        await make(fake).run_metric(claims_for("head_data"), PLAN)
    assert info.value.code == "source_error" and str(info.value) == "not entitled to this dataset"


# ------------------------------------------------------------------------------------------ token hygiene
async def test_token_never_appears_in_errors_logs_or_audit(caplog):
    caplog.set_level(logging.DEBUG)
    audit = FakeAudit()
    replies = [
        lambda token, **k: tool_error(f"bad token {token} rejected"),
        lambda token, **k: tool_error(f"header Bearer {token.split('.')[0]}.{token.split('.')[1]}.{token.split('.')[2]}"),
        lambda token, **k: group(MCPError(-32603, f"auth failed for {token}"), SourceAuthRejected(401)),
        lambda token, **k: group(RuntimeError(f"Authorization: Bearer {token}")),
        lambda token, **k: MCPError(-32603, token),
    ]
    fake_tokens = []
    for reply in replies:
        fake = Fake(reply)
        with pytest.raises(GatewayError) as info:
            await make(fake, audit).run_metric(claims_for("head_data"), PLAN)
        fake_tokens += fake.tokens
        assert_no_token(info.value, fake.tokens)
    blob = json.dumps(audit.events, default=str) + caplog.text
    for t in fake_tokens:
        assert t not in blob and t.split(".")[2] not in blob


# ------------------------------------------------------------------------------------------------- audit
async def test_audit_has_catalog_names_only():
    audit = FakeAudit()
    await make(Fake(), audit).run_metric(claims_for("cash_ops_emea"), PLAN)
    with pytest.raises(GatewayError):
        await make(Fake(lambda **k: tool_error("nope SECRETVAL")), audit).run_metric(claims_for("cash_ops_emea"), PLAN)
    ok, err = audit.events
    assert ok["tool"] == "source.run_metric" and ok["source"] == "cashrecon" and ok["metric_id"] == "open_breaks"
    assert ok["dimensions"] == ["region"] and ok["status"] == "ok" and ok["rows"] == 2 and ok["truncated"] is False
    assert ok["sub"] == ok["persona"] == "cash_ops_emea" and isinstance(ok["ms"], float)
    assert err["status"] == "error" and err["error_code"] == "source_error"
    assert "SECRETVAL" not in json.dumps(audit.events)


# --------------------------------------------------------------------------------- real MCP app, in process
async def test_real_source_app_auth_error_and_ok():
    from tests.mcp.test_base import URL, running

    async with running() as app:
        async def call(url, token, tool, arguments):
            return await mcp_call(url, token, tool, arguments, asgi_app=app)

        plan = MetricPlan(source="fake", tool="run_metric", metric_id="m", unit=None,
                          arguments={"metric_id": "m", "dimensions": [], "filters": {}})
        good = Downstream(SETTINGS, None, call=call, urls={"fake": URL})
        r = await good.run_metric(claims_for("steward"), plan)
        assert r.rows == [[100, "steward"]] and r.columns == ["value", "who"]
        denied = MetricPlan(source="fake", tool="run_metric", metric_id="denied", unit=None,
                            arguments={"metric_id": "denied", "dimensions": [], "filters": {}})
        with pytest.raises(GatewayError) as info:
            await good.run_metric(claims_for("steward"), denied)
        assert info.value.code == "source_error" and str(info.value) == "not entitled to this dataset"
        wrong = Settings(jwt_secret="x" * 40)
        with pytest.raises(GatewayError) as info:
            await Downstream(wrong, None, call=call, urls={"fake": URL}).run_metric(claims_for("steward"), plan)
        assert info.value.code == "source_auth"


async def test_audit_status_is_never_ok_for_a_failed_or_cancelled_call():
    audit = FakeAudit()
    task = asyncio.ensure_future(make(Fake(delay=5), audit).run_metric(claims_for("head_data"), PLAN))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    bad = CallToolResult(content=[], structured_content={"source": "cashrecon", "metric_id": "open_breaks",
                                                         "dimensions": ["region"], "rows": [{"region": "x"}],
                                                         "truncated": False})
    d = make(Fake(lambda **k: bad), audit)

    def boom(*a, **k):
        raise ValueError("unexpected")

    import prism.gateway.downstream as ds
    orig = ds.SourceMetricResult
    ds.SourceMetricResult = boom
    try:
        with pytest.raises(ValueError):
            await d.run_metric(claims_for("head_data"), PLAN)
    finally:
        ds.SourceMetricResult = orig
    cancelled, crashed = audit.events
    assert cancelled["status"] == "cancelled" and crashed["status"] == "error"
    assert crashed["error_code"] == "internal_error" and crashed["rows"] is None
