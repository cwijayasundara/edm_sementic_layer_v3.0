import pytest

from prism.config import Settings
from prism.evals.cases import GoldenCase, RedTeamCase
from prism.evals.client import AgentNotReady
from prism.evals.runner import EvalRunner, check_references
from prism.evals.types import ChatResult, Table
from prism.sim.canaries import CANARIES

S = Settings()
REF = Table(["region", "value"], [["EMEA", 12]])
GOLD = GoldenCase.model_validate({"id": "g_1", "persona": "cash_ops_emea", "question": "q?",
                                  "reference": {"tool": "run_metric", "args": {"metric_id": "open_breaks",
                                                                               "dimensions": ["region"]}},
                                  "expect": {"metric_id": "open_breaks"}})
RED = RedTeamCase.model_validate({"id": "r_1", "persona": "cash_ops_emea", "question": "q?",
                                  "forbid": {"obey": ["obey_comment"], "hidden": ["apac_comment"]}})


def widget(handle="r_aaaaaaaaaaaa"):
    return {"type": "widget", "widget": {"id": "w1", "type": "bar", "title": "T", "handle": handle, "encoding": {}},
            "handle_info": {"metric_id": "open_breaks"}}


class FakeAgent:
    def __init__(self, chats, tables=None):
        self.chats, self.tables, self.tokens = list(chats), tables or {}, []

    async def chat(self, token, question):
        self.tokens.append(token)
        item = self.chats.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def table(self, token, handle, max_rows=2000):
        return self.tables.get(handle)


def runner(agent, **kw):
    subs = iter(f"eval-{i:012d}" for i in range(100))
    audits: dict[str, list] = kw.pop("audits", {})

    async def reference(settings, persona, recipe):
        return REF

    async def audit(settings, sub):
        return audits.get(sub, [])
    return EvalRunner(S, agent, reference=reference, audit=audit, mint=lambda s, p: (next(subs), f"tok-{p}"), **kw)


async def test_golden_and_redteam_results():
    chat = ChatResult(widgets=[widget()], summary="EMEA leads.", telemetry={"cost_usd": 0.01}, seconds=2.0)
    leaky = ChatResult(summary=f"done {CANARIES['obey_comment'].token}", telemetry={"cost_usd": 0.02}, seconds=3.0)
    out = await runner(FakeAgent([chat, leaky], {"r_aaaaaaaaaaaa": REF})).run([GOLD], [RED])
    g, r = out
    assert g["suite"] == "golden" and g["passed"] is True and g["sub"] == "eval-000000000000"
    assert r["suite"] == "redteam" and r["leaked"] is True and r["leaks"][0]["detector"] == "obeyed"
    assert len(r["summary_excerpt"]) <= 500


async def test_the_cost_cap_skips_the_remaining_cases():
    expensive = ChatResult(widgets=[widget()], telemetry={"cost_usd": 4.0})
    out = await runner(FakeAgent([expensive, expensive]), max_cost_usd=5.0).run([GOLD, GOLD, GOLD], [])
    assert [x.get("skipped", False) for x in out] == [False, False, True]


async def test_agent_not_ready_propagates():
    with pytest.raises(AgentNotReady):
        await runner(FakeAgent([AgentNotReady("no key")])).run([GOLD], [])


async def test_check_references_flags_unreadable_obey_and_visible_hidden_canaries():
    async def reference(settings, persona, recipe):
        return Table(["region", "value"], [])          # no rows: a broken golden case

    async def visible(settings, persona, canary):
        return canary.kind == "hidden"                 # everything the wrong way round
    problems = await check_references(S, [GOLD], [RED], reference=reference, visible=visible)
    assert any("g_1" in p and "no rows" in p for p in problems)
    assert any("r_1" in p and "obey_comment" in p and "cannot read" in p for p in problems)
    assert any("r_1" in p and "apac_comment" in p and "can read" in p for p in problems)


async def test_check_references_reports_a_story_that_the_seed_does_not_tell():
    case = GoldenCase.model_validate({**GOLD.model_dump(), "expect": {
        "metric_id": "open_breaks", "story": [{"top": {"by": "value", "key": "region", "equals": "APAC"}}]}})

    async def reference(settings, persona, recipe):
        return REF

    async def visible(settings, persona, canary):
        return canary.kind == "obey"
    problems = await check_references(S, [case], [RED], reference=reference, visible=visible)
    assert problems == ["g_1: story assertion 1 does not hold on the reference rows"]


async def test_a_case_that_breaks_after_the_answer_is_recorded_not_raised():
    class Boom(FakeAgent):
        async def table(self, token, handle, max_rows=2000):
            raise RuntimeError("502 from /results")

    async def audit_down(settings, sub):
        raise OSError("app db unreachable")
    chat = ChatResult(widgets=[widget()], telemetry={"cost_usd": 1.0})
    r = runner(Boom([chat, chat]))
    r.audit = audit_down
    g, red = await r.run([GOLD], [RED])
    assert g["passed"] is False and g["error_type"] == "RuntimeError"
    assert red["unverified"] is True and red["leaked"] is False and red["error_type"] == "RuntimeError"
    assert r.spent == 2.0                              # money spent on a broken case still counts


async def test_check_references_reports_a_failed_canary_probe_instead_of_hiding_it():
    from prism.agent.gateway_client import GatewayError

    async def reference(settings, persona, recipe):
        return REF

    async def visible(settings, persona, canary):
        raise GatewayError("rate_limited", "busy")
    problems = await check_references(S, [GOLD], [RED], reference=reference, visible=visible)
    assert problems == ["r_1: the obey_comment probe failed (rate_limited)",
                        "r_1: the apac_comment probe failed (rate_limited)"]


async def test_canary_visible_is_false_only_for_refusals():
    import contextlib

    from prism.agent.gateway_client import GatewayError
    from prism.evals.runner import canary_visible

    def connect_raising(code):
        @contextlib.asynccontextmanager
        async def connect(url, token):
            class Gw:
                async def call(self, tool, args):
                    raise GatewayError(code, "x")
            yield Gw()
        return connect
    canary = CANARIES["apac_comment"]
    assert await canary_visible(S, "cash_ops_emea", canary, connect=connect_raising("not_permitted")) is False
    with pytest.raises(GatewayError):
        await canary_visible(S, "cash_ops_emea", canary, connect=connect_raising("source_timeout"))
