"""Runs eval cases against the live agent: one fresh sub per case, golden answers graded against the reference
recipe replayed through the gateway (under its own sub), red-team answers checked by the leak detectors (with this
case's own app.audit rows). `check_references` validates the case files against the live seed without the model."""
from collections.abc import Awaitable, Callable
from dataclasses import asdict

import psycopg
from psycopg.rows import dict_row

from prism.agent.gateway_client import GatewayClient, GatewayError
from prism.agent.recipes import Replayer
from prism.config import APP_DB, Settings
from prism.evals.cases import GoldenCase, RedTeamCase
from prism.evals.client import AgentClient, mint_case_token
from prism.evals.grade import grade_golden, passed, story_holds
from prism.evals.leaks import find_leaks
from prism.evals.types import ChatResult, Table
from prism.sim.canaries import CANARIES, Canary

PAGE = 200
MAX_ROWS = 2000
EXCERPT = 500


async def reference_table(settings: Settings, persona: str, recipe: dict, *, connect=GatewayClient) -> Table:
    _, token = mint_case_token(settings, persona)
    async with connect(settings.gateway_url, token) as gw:
        summary = await Replayer(gw).run(recipe)
        handle, rows, row_count, columns = summary["handle"], [], None, summary.get("columns", [])
        while len(rows) < MAX_ROWS:
            page = await gw.call("get_rows", {"handle": handle, "offset": len(rows), "limit": PAGE})
            columns, row_count = page["columns"], page["row_count"]
            rows.extend(page["rows"])
            if not page["rows"] or len(rows) >= row_count:
                break
        return Table(columns, rows, truncated=row_count is not None and len(rows) < row_count)


async def canary_visible(settings: Settings, persona: str, canary: Canary, *, connect=GatewayClient) -> bool:
    _, token = mint_case_token(settings, persona)
    async with connect(settings.gateway_url, token) as gw:
        try:
            out = await gw.call("query_source", {"source": "cashrecon", "request": {"sql": canary.probe_sql}})
            page = await gw.call("get_rows", {"handle": out["handle"], "offset": 0, "limit": 1})
        except GatewayError as exc:
            if exc.final:   # refused (scope, metrics-only): the persona cannot read it
                return False
            raise           # busy / timeout / broken probe: unknown, never "cannot read"
    value = page["rows"][0][0] if page["rows"] else 0
    return float(value or 0) > 0


async def audit_calls(settings: Settings, sub: str) -> list[dict]:
    async with await psycopg.AsyncConnection.connect(settings.app_dsn(APP_DB, connect_timeout=3)) as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute("SELECT tool, source, metric_id, status FROM app.audit WHERE sub = %s ORDER BY id", (sub,))
        return await cur.fetchall()


def _excerpt(text: str | None) -> str:
    return (text or "")[:EXCERPT]


class EvalRunner:
    def __init__(self, settings: Settings, agent: AgentClient, *,
                 reference: Callable[..., Awaitable[Table]] = reference_table,
                 audit: Callable[..., Awaitable[list[dict]]] = audit_calls,
                 mint: Callable[[Settings, str], tuple[str, str]] = mint_case_token, max_cost_usd: float = 5.0):
        self.settings, self.agent = settings, agent
        self.reference, self.audit, self.mint = reference, audit, mint
        self.max_cost_usd, self.spent = max_cost_usd, 0.0

    async def _tables(self, token: str, chat: ChatResult) -> dict[str, Table | None]:
        return {h: await self.agent.table(token, h) for h in chat.handles}

    def _base(self, suite: str, case, sub: str, chat: ChatResult) -> dict:
        self.spent += chat.cost_usd
        return {"suite": suite, "id": case.id, "persona": case.persona, "question": case.question, "sub": sub,
                "seconds": round(chat.seconds, 2), "telemetry": chat.telemetry, "timed_out": chat.timed_out,
                "error": chat.error, "widgets": [w["widget"]["type"] for w in chat.widgets],
                "summary_excerpt": _excerpt(chat.summary)}

    async def golden(self, case: GoldenCase) -> dict:
        sub, token = self.mint(self.settings, case.persona)
        chat = await self.agent.chat(token, case.question)
        out = self._base("golden", case, sub, chat)   # the spend counts even if grading breaks below
        try:
            tables = await self._tables(token, chat)
            try:
                ref = await self.reference(self.settings, case.persona, case.reference)
            except Exception:  # noqa: BLE001 - graded as a failed `rows` check
                ref = None
            checks = grade_golden(case, chat, tables, ref)
        except Exception as exc:  # noqa: BLE001 - one broken case never kills a paid run
            return {**out, "passed": False, "checks": [], "error_type": type(exc).__name__}
        return {**out, "passed": passed(checks), "checks": [asdict(c) for c in checks]}

    async def redteam(self, case: RedTeamCase) -> dict:
        sub, token = self.mint(self.settings, case.persona)
        chat = await self.agent.chat(token, case.question)
        out = self._base("redteam", case, sub, chat)
        try:
            tables = await self._tables(token, chat)
            leaks = find_leaks(case, chat, tables, await self.audit(self.settings, sub))
        except Exception as exc:  # noqa: BLE001 - unchecked is never a pass: reported as unverified
            return {**out, "leaked": False, "unverified": True, "leaks": [], "error_type": type(exc).__name__}
        return {**out, "leaked": bool(leaks), "leaks": [asdict(x) for x in leaks]}

    async def run(self, golden: list[GoldenCase], redteam: list[RedTeamCase]) -> list[dict]:
        results = []
        for suite, cases, fn in (("golden", golden, self.golden), ("redteam", redteam, self.redteam)):
            for case in cases:
                if self.spent >= self.max_cost_usd:
                    results.append({"suite": suite, "id": case.id, "persona": case.persona, "skipped": True})
                    continue
                results.append(await fn(case))
        return results


async def check_references(settings: Settings, golden: list[GoldenCase], redteam: list[RedTeamCase], *,
                           reference=reference_table, visible=canary_visible) -> list[str]:
    problems = []
    for case in golden:
        try:
            ref = await reference(settings, case.persona, case.reference)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{case.id}: the reference failed ({getattr(exc, 'status', type(exc).__name__)})")
            continue
        if not ref.rows:
            problems.append(f"{case.id}: the reference returned no rows")
        for i, story in enumerate(case.expect.story, 1):
            if ref.rows and not story_holds(story, ref):
                problems.append(f"{case.id}: story assertion {i} does not hold on the reference rows")
    for case in redteam:
        for want, keys in ((True, case.forbid.obey), (False, case.forbid.hidden)):
            for key in keys:
                try:
                    seen = await visible(settings, case.persona, CANARIES[key])
                except Exception as exc:  # noqa: BLE001 - a failed probe is a problem, never a silent pass
                    problems.append(f"{case.id}: the {key} probe failed ({getattr(exc, 'code', type(exc).__name__)})")
                    continue
                if want and not seen:
                    problems.append(f"{case.id}: {case.persona} cannot read {key}, so the case tests nothing")
                elif not want and seen:
                    problems.append(f"{case.id}: {case.persona} can read {key}, which must be hidden from it")
    return problems
