"""Live end-to-end checks against the RUNNING stack (scripts/start_backend.sh), through the Semantic Gateway only.

`make test-live` (`pytest -m live`); deselected from the default run (pyproject addopts `-m "not live"`). Skipped
unless the gateway (PRISM_GATEWAY_URL, loopback only: the probe and every call use that one URL) and the five source
MCP servers, Neo4j and Postgres all answer, and unless the databases hold the FULL simulation profile: the small profile
does not carry the planted stories (Plan 1 carry-forward #5), and the seed / as-of the figures are pinned for. Re-seed with `scripts/start_backend.sh --reseed` (the
default profile is full) only when you mean to: it rebuilds every source database.

Every figure below comes back through gateway tools (run_metric, get_rows, combine); nothing reads a source database.
The only direct database read is `seed_info.profile`, to decide whether to skip: one SELECT as the admin role,
because only the admin owns public.seed_info (granting it to the app role would widen that role's exact grants). The calls write audit rows into the
real `app` database under subs `e2e-live-<random>`; record_answer is never called, so app.query_log is untouched.
"""
import asyncio
import socket
import uuid
from collections import Counter
from datetime import date
from urllib.parse import urlparse

import psycopg
import pytest

from prism.config import APP_DB, Settings
from prism.gateway.server import GATEWAY_AUDIENCE
from prism.mcp.client import mcp_client
from prism.mcp.servers import MCP_PORTS
from prism.security.personas import claims_for
from prism.security.tokens import mint

pytestmark = pytest.mark.live

SETTINGS = Settings()
AS_OF = SETTINGS.as_of
STORY_SEED = 42      # the pinned figures (90/75, 60, 64 of 79) hold for this seed and as-of 2026-09-30


def _open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def gateway_target(url: str) -> tuple[str, str, int]:
    """(MCP endpoint URL, host, port) of the gateway: the probe and every call use this one URL. Live tokens are minted
    with the real JWT secret, so anything but an http(s) loopback URL is refused (ValueError)."""
    u = urlparse(str(url))
    if u.scheme not in ("http", "https") or u.hostname not in LOOPBACK:
        raise ValueError(f"refusing a non-loopback gateway URL ({u.scheme or '?'}://{u.hostname or '?'}): the live "
                         "tests send tokens minted with the real JWT secret")
    port = u.port or (443 if u.scheme == "https" else 80)
    return f"{str(url).rstrip('/')}/mcp", u.hostname, port


def _why_not_live() -> str | None:
    bolt = urlparse(SETTINGS.neo4j_uri)
    _, gw_host, gw_port = gateway_target(SETTINGS.gateway_url)
    ports = {"gateway": (gw_host, gw_port),
             **{f"{s}-mcp": ("127.0.0.1", p) for s, p in MCP_PORTS.items()},
             "neo4j": (bolt.hostname or "127.0.0.1", bolt.port or 7687),
             "postgres": (SETTINGS.pg_host, SETTINGS.pg_port)}
    closed = [f"{name} {host}:{port}" for name, (host, port) in ports.items() if not _open(host, port)]
    if closed:
        return f"the backend stack is not running (closed: {', '.join(closed)}); start it with scripts/start_backend.sh"
    try:
        with psycopg.connect(SETTINGS.dsn(APP_DB, admin=True), connect_timeout=3) as conn:
            row = conn.execute("SELECT profile, seed, as_of FROM public.seed_info").fetchone()
    except psycopg.Error as exc:
        return f"could not read the seeded profile ({type(exc).__name__})"
    if not row or row[0] != "full":
        return (f"the databases hold the {row[0] if row else 'unknown'!r} profile; the stories need the FULL profile "
                "(scripts/start_backend.sh --reseed rebuilds every source database: run it only if you mean to)")
    if (row[1], row[2]) != (STORY_SEED, AS_OF):
        return (f"the story figures are pinned for seed {STORY_SEED} as of {AS_OF}; the databases hold seed {row[1]} "
                f"as of {row[2]}")
    return None


@pytest.fixture(scope="module")
def live():
    try:
        reason = _why_not_live()
    except ValueError as exc:            # a non-loopback gateway URL: refuse loudly, never send a token there
        pytest.fail(str(exc), pytrace=False)
    if reason:
        pytest.skip(reason)


class Gw:
    """One caller's MCP session with the gateway."""

    def __init__(self, client):
        self.c = client

    async def call(self, tool: str, args: dict):
        return await self.c.call_tool(tool, args)

    async def ok(self, tool: str, args: dict) -> dict:
        r = await self.call(tool, args)
        assert not r.is_error, r.content[0].text if r.content else r
        return r.structured_content

    async def metric_rows(self, metric_id: str, dims: list[str], **kw) -> tuple[str, list[str], list[list]]:
        """(handle, columns, all rows paged through get_rows)."""
        out = await self.ok("run_metric", {"metric_id": metric_id, "dimensions": dims, "limit": 1000, **kw})
        handle, summary = out["handle"], out["summary"]
        assert summary["truncated"] is False, (metric_id, dims)
        rows: list[list] = []
        while len(rows) < summary["row_count"]:
            page = await self.ok("get_rows", {"handle": handle, "offset": len(rows), "limit": 200})
            assert page["rows"], "get_rows returned an empty page before the end"
            rows += page["rows"]
        return handle, summary["columns"], rows

    @staticmethod
    def error(result) -> str:
        assert result.is_error, result.structured_content
        return result.content[0].text


def _session(persona: str, sub: str | None = None):
    claims = {**claims_for(persona, ttl_s=600), "sub": sub or f"e2e-live-{uuid.uuid4().hex[:10]}"}
    token = mint(claims, GATEWAY_AUDIENCE, SETTINGS.jwt_secret.get_secret_value(), ttl_s=600)
    return mcp_client(gateway_target(SETTINGS.gateway_url)[0], token, timeout_s=10, read_timeout_s=120)


def _business_days(values) -> list[str]:
    """Distinct dates up to the as-of date, ascending (ISO strings sort as dates)."""
    return sorted({str(v) for v in values if date.fromisoformat(str(v)) <= AS_OF})


# ------------------------------------------------------------------------------------------- the four stories
async def test_story_vendor_a_corp_bond_price_conflicts(live):
    """Vendor A x Corp bond drives this week's conflicts: 60 of 90 this week; total +20% week over week (75 -> 90).
    "This week" = the trailing 5 business days ending at the as-of date (Plan 1 carry-forward #3)."""
    async with _session("steward") as c:
        gw = Gw(c)
        _, _, by_day = await gw.metric_rows("price_conflicts", ["price_date"])
        days = _business_days(d for d, _ in by_day)
        this_week, prev_week = days[-5:], days[-10:-5]
        total = {d: v for d, v in by_day}
        this, prev = sum(total[d] for d in this_week), sum(total[d] for d in prev_week)
        assert (this, prev) == (90, 75) and round((this - prev) / prev * 100) == 20
        handle, _, cells = await gw.metric_rows("price_conflicts", ["vendor_id", "asset_class", "price_date"])
        story = {d: v for vid, ac, d, v in cells if (vid, ac) == ("V_A", "Corp bond")}
        assert sum(story.get(d, 0) for d in this_week) == 60
        week = Counter()
        for vid, ac, d, v in cells:
            if d in this_week:
                week[(vid, ac)] += v
        assert week.most_common(1)[0] == (("V_A", "Corp bond"), 60)
        combined = await gw.ok("combine", {
            "sql": "SELECT sum(value) AS total, sum(CASE WHEN vendor_id = 'V_A' AND asset_class = 'Corp bond' "
                   f"THEN value ELSE 0 END) AS story FROM pc WHERE price_date >= '{this_week[0]}'",
            "handles": {"pc": handle}})
        assert combined["summary"]["sample_rows"] == [[90, 60]]


async def test_story_src001_late_feeds(live):
    """One custodian (SRC001) turned late: over the last 6 business days every one of its deliveries is late, and it
    clearly outweighs every other source."""
    async with _session("head_data") as c:
        _, _, rows = await Gw(c).metric_rows("late_feeds", ["source_id", "business_date"])
    days = _business_days(d for _, d, _ in rows)
    late = Counter()
    for sid, d, v in rows:
        if d in days[-6:]:
            late[sid] += v
    (top, n), (_, runner_up) = late.most_common(2)
    assert top == "SRC001" and n >= 3 * runner_up
    assert {d for sid, d, _ in rows if sid == "SRC001"} >= set(days[-6:])   # late on every one of those days


async def test_story_pf001_pf002_pf005_position_exceptions_per_day(live):
    """The late custodian's portfolios (PF001, PF002, PF005) lead position exceptions per day since the feeds turned
    late, at least 3x their own earlier daily rate."""
    async with _session("head_data") as c:
        _, _, rows = await Gw(c).metric_rows("position_exceptions", ["portfolio_id", "business_date"])
    days = _business_days(d for _, d, _ in rows)
    late_days, before_days = set(days[-6:]), set(days[:-6])
    per_day = Counter()
    before = Counter()
    for pid, d, v in rows:
        if d in late_days:
            per_day[pid] += v / len(late_days)
        elif d in before_days:
            before[pid] += v / len(before_days)
    top3 = {pid for pid, _ in per_day.most_common(3)}
    assert top3 == {"PF001", "PF002", "PF005"}
    fourth = per_day.most_common(4)[3][1]
    for pid in top3:
        assert per_day[pid] >= 3 * max(before[pid], 1 / len(before_days)), pid
        assert per_day[pid] > 2 * fourth, pid


async def test_story_le00016_aged_usd_breaks(live):
    """Aged (status <> 'closed' AND age_days > 5) USD breaks concentrate on LE00016: 64 of 79."""
    async with _session("head_data") as c:
        gw = Gw(c)
        handle, _, rows = await gw.metric_rows("aged_open_breaks", ["legal_entity_id", "ccy"])
        usd = sorted(((le, v) for le, ccy, v in rows if ccy == "USD"), key=lambda x: -x[1])
        assert usd[0] == ("LE00016", 64) and sum(v for _, v in usd) == 79
        top = await gw.ok("combine", {
            "sql": "SELECT legal_entity_id, sum(value) AS breaks FROM b WHERE ccy = 'USD' "
                   "GROUP BY legal_entity_id ORDER BY breaks DESC LIMIT 1", "handles": {"b": handle}})
        assert top["summary"]["sample_rows"] == [["LE00016", 64]]


async def test_search_context_points_at_the_story_metrics(live):
    async with _session("head_data") as c:
        pack = await Gw(c).ok("search_context", {"question": "Which legal entity has the most aged USD breaks?"})
    assert "aged_open_breaks" in [m["id"] for m in pack["metrics"]]


# ------------------------------------------------------------------------------------------- fan-out and policy
async def test_six_concurrent_run_metric_calls_as_one_user_succeed(live):
    sub = f"e2e-live-{uuid.uuid4().hex[:10]}"
    calls = [("open_breaks", ["region"]), ("aged_open_breaks", ["ccy"]), ("auto_match_rate", ["region"]),
             ("manual_matches", ["region"]), ("late_feeds", ["source_id"]), ("position_exceptions", ["fund_group"])]
    async with _session("head_data", sub) as c:
        results = await asyncio.wait_for(asyncio.gather(*(
            c.call_tool("run_metric", {"metric_id": m, "dimensions": d}) for m, d in calls)), 60)
        same_source = await asyncio.wait_for(asyncio.gather(*(
            c.call_tool("run_metric", {"metric_id": "open_breaks", "dimensions": ["region"]}) for _ in range(6))), 60)
    for r in [*results, *same_source]:
        assert not r.is_error, r.content[0].text
        assert r.structured_content["summary"]["row_count"] >= 1


async def test_bi_analyst_gets_metrics_but_nothing_finer(live):
    async with _session("bi_analyst") as c:
        gw = Gw(c)
        ok = await gw.ok("run_metric", {"metric_id": "open_breaks", "dimensions": ["region"]})
        assert ok["summary"]["row_count"] >= 1
        assert "metrics_only" in gw.error(await gw.call("query_source", {
            "source": "cashrecon", "request": {"sql": "SELECT count(*) FROM breaks"}}))
        assert "metrics_only" in gw.error(await gw.call("query_source", {
            "source": "refmaster", "request": {"endpoint_id": "securities", "params": {}}}))
        assert "sensitive_dimension" in gw.error(await gw.call("run_metric", {
            "metric_id": "manual_matches", "dimensions": ["matched_by"]}))
        assert "grain_too_fine" in gw.error(await gw.call("run_metric", {
            "metric_id": "late_feeds", "dimensions": ["source_id", "business_date"]}))
        assert "grain_too_fine" in gw.error(await gw.call("run_metric", {
            "metric_id": "position_exceptions", "dimensions": ["portfolio_id"],
            "filters": {"business_date": AS_OF.isoformat()}}))


async def test_combine_joins_two_handles_and_refuses_mixed_currency_sums(live):
    async with _session("head_data") as c:
        gw = Gw(c)
        rate = await gw.ok("run_metric", {"metric_id": "auto_match_rate", "dimensions": ["region"]})
        manual = await gw.ok("run_metric", {"metric_id": "manual_matches", "dimensions": ["region"]})
        joined = await gw.ok("combine", {
            "sql": "SELECT r.region, r.value AS auto_rate, m.value AS manual FROM r JOIN m ON r.region = m.region "
                   "ORDER BY r.region",
            "handles": {"r": rate["handle"], "m": manual["handle"]}})
        assert joined["summary"]["row_count"] >= 2 and joined["summary"]["columns"] == ["region", "auto_rate", "manual"]
        amounts = await gw.ok("run_metric", {"metric_id": "open_break_amount", "dimensions": ["ccy"]})
        assert amounts["summary"]["row_count"] >= 2                   # several currencies: a plain sum would mix them
        assert "currency_mixing" in gw.error(await gw.call("combine", {
            "sql": "SELECT sum(value) AS total FROM a", "handles": {"a": amounts["handle"]}}))
        per_ccy = await gw.ok("combine", {"sql": "SELECT ccy, sum(value) AS total FROM a GROUP BY ccy",
                                          "handles": {"a": amounts["handle"]}})
        assert per_ccy["summary"]["row_count"] == amounts["summary"]["row_count"]
