"""AgentService.chat: one question in, an ordered event stream out (plan* -> widget* -> summary -> answer? -> telemetry, or
... -> error -> telemetry). Owns the supervisor run, the one-step model escalation, the dashboard fallback, the
record_answer write-back and the per-run telemetry."""
import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, aclosing
from dataclasses import dataclass
from datetime import date

from prism.agent.auth import UserContext
from prism.agent.gateway_client import GatewayError, GatewayPort
from prism.agent.model import ModelClient, ModelError, ModelTruncated
from prism.agent.prompts import SUPERVISOR_SYSTEM, SUPERVISOR_TOOLS, dynamic_context
from prism.agent.runner import MessagesRunner, RunLimitExceeded, RunLimits
from prism.agent.spec import fallback_spec
from prism.agent.state import RunState, UsageMeter
from prism.agent.telemetry import AgentRunWriter, estimate_cost
from prism.agent.tools import ToolBox
from prism.config import Settings

log = logging.getLogger("prism.agent")
MAX_QUESTION_CHARS = 2000
RECORDED_HANDLES = 20
NO_ANSWER = "I could not produce an answer for that question."
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_TRACE_STEPS = 40
TRACE_WRITE_TIMEOUT_S = 3.0
RECORD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass
class _Run:
    status: str = "error"   # ok | refused | error | limit
    finished: bool = False


class AgentService:
    def __init__(self, *, settings: Settings, model_client: ModelClient,
                 gateway_factory: Callable[[UserContext], AbstractAsyncContextManager[GatewayPort]],
                 run_writer: AgentRunWriter | None, clock: Callable[[], date] = date.today):
        self._settings, self._client, self._gateway_factory = settings, model_client, gateway_factory
        self._writer, self._clock = run_writer, clock

    async def chat(self, user: UserContext, question: str) -> AsyncIterator[dict]:
        question = (question or "").strip()
        if not question or len(question) > MAX_QUESTION_CHARS:
            yield {"type": "error", "code": "invalid_question",
                   "message": f"Ask a question of 1 to {MAX_QUESTION_CHARS} characters."}
            return
        state = RunState(run_id=uuid.uuid4().hex, sub=user.sub, question=question, meter=UsageMeter())
        run = _Run()
        try:
            try:
                async with self._gateway_factory(user) as gateway:
                    # aclosing: a disconnect must stop the supervisor task before the gateway session exits
                    async with aclosing(self._converse(user, question, state, gateway, run)) as events:
                        async for event in events:
                            yield event
            except GatewayError:
                state.error_code, run.status = "gateway_unavailable", "error"
                yield self._error("gateway_unavailable", FAILURE_MESSAGES["gateway_unavailable"])
            except Exception as exc:  # noqa: BLE001 - telemetry must stay last; never leak the message
                log.error("agent run failed: %s", type(exc).__name__)
                state.error_code, run.status = "internal_error", "error"
                yield self._error("internal_error", FAILURE_MESSAGES["internal_error"])
            run.finished = True
            yield self._telemetry(state)
        finally:
            if not run.finished:   # client disconnected or cancelled mid-run
                state.error_code = state.error_code or "client_disconnected"
            if self._writer is not None:
                await asyncio.shield(self._writer.write(state, path=self._path(state), status=run.status))

    async def _converse(self, user: UserContext, question: str, state: RunState, gateway: GatewayPort,
                        run: _Run) -> AsyncIterator[dict]:
        toolbox = ToolBox(gateway=gateway, state=state, client=self._client, settings=self._settings)
        task = asyncio.create_task(self._supervise(user, toolbox, state, question))
        try:
            while (event := await state.events.get()) is not None:
                yield event
            text, failure = await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if failure:
            state.error_code = failure
            run.status = "limit" if failure == "run_limit" else "error"
            yield self._error(failure, FAILURE_MESSAGES[failure])
            await self._record_trace(gateway, state, run.status, FAILURE_MESSAGES[failure])
            return
        if state.error_code == "gateway_unavailable" and not state.handles:
            run.status = "error"
            yield self._error("gateway_unavailable", FAILURE_MESSAGES["gateway_unavailable"])
            await self._record_trace(gateway, state, run.status, FAILURE_MESSAGES["gateway_unavailable"])
            return
        run.status = "refused" if state.refusal else "ok"
        events = self._answer_events(state, text)
        for event in events:
            yield event
        recorded = await self._record_answer(gateway, question, state)
        if recorded is not None:
            yield {"type": "answer", **recorded}
        summary_text = next((e["text"] for e in reversed(events) if e["type"] == "summary"), "")
        await self._record_trace(gateway, state, run.status, summary_text)   # after the answer event, before telemetry

    async def _supervise(self, user: UserContext, toolbox: ToolBox, state: RunState,
                         question: str) -> tuple[str, str | None]:
        """Returns (final text, failure code). Always ends the event stream."""
        model = self._settings.agent_supervisor_model
        try:
            limits = RunLimits(self._settings.agent_max_turns, self._settings.agent_max_tool_calls,
                               self._settings.agent_wall_clock_s)
            for attempt, model in enumerate((self._settings.agent_supervisor_model,
                                             self._settings.agent_escalation_model)):
                if attempt:
                    self._reset_for_retry(state)
                    # (escalation is on run limits only; the spec's multi-source/low-confidence triggers are not built)
                runner = MessagesRunner(self._client, model=model, stable_system=SUPERVISOR_SYSTEM,
                                        dynamic_system=dynamic_context(user, self._clock(), self._settings.as_of),
                                        tools=SUPERVISOR_TOOLS, handler=toolbox.supervisor_handler, limits=limits,
                                        meter=state.meter,
                                        observe=lambda text: setattr(state, "pending_note", text))
                try:
                    return (await runner.run(question)).text, None
                except RunLimitExceeded as exc:
                    log.warning("supervisor on %s hit the %s cap", model, exc.reason)
            return "", "run_limit"
        except ModelTruncated:
            log.error("supervisor output truncated at max_tokens (model %s)", model)
            return "", "model_truncated"
        except ModelError as exc:
            log.error("model call failed: model=%s status=%s type=%s detail=%s", model, exc.status, exc.error_type,
                      exc.detail)
            return "", "model_unavailable"
        except Exception as exc:  # noqa: BLE001 - never let a bug strand the stream or leak its message
            log.error("supervisor failed: %s", type(exc).__name__)
            return "", "internal_error"
        finally:
            state.events.put_nowait(None)

    @staticmethod
    def _reset_for_retry(state: RunState) -> None:
        """The escalated attempt starts clean; handles stay (they are real results the new run may reuse)."""
        state.spec, state.spec_is_fallback = None, False
        state.refusal = state.error_code = None
        state.delegated = False
        state.metric_handles, state.last_handle = set(), None
        state.pending_note = None   # steps stay: the trace shows both attempts

    async def _record_trace(self, gateway: GatewayPort, state: RunState, status: str, text: str) -> None:
        """Best effort, before telemetry; never raises and never changes the answer."""
        kind = "error" if status in ("error", "limit") else "refusal" if status == "refused" else "answer"
        label = {"answer": "Answered", "refusal": "Declined", "error": "Stopped with an error"}[kind]
        state.add_step(kind=kind, label=label, status="ok" if kind == "answer" else kind,
                       error_code=state.error_code)
        # over the cap: keep the first 39 steps and the outcome step, never drop the outcome
        steps = state.steps if len(state.steps) <= MAX_TRACE_STEPS else \
            [*state.steps[:MAX_TRACE_STEPS - 1], state.steps[-1]]
        try:
            await asyncio.wait_for(gateway.call("record_trace", {
                "run_id": state.run_id, "question": state.question, "answer": (text or "")[:2000],
                "path": self._path(state), "status": status, "steps": steps}), TRACE_WRITE_TIMEOUT_S)
        except TimeoutError:
            log.warning("trace_write_failed: timeout")
        except Exception as exc:  # noqa: BLE001 - the trace is a side record
            log.warning("trace_write_failed: %s", getattr(exc, "code", type(exc).__name__))

    @staticmethod
    def _answer_events(state: RunState, text: str) -> list[dict]:
        if state.spec is None and state.handles:
            handle = state.last_handle if state.last_handle in state.handles else next(iter(state.handles))
            state.spec, state.spec_is_fallback = fallback_spec(state.handles, handle, text), True
        if state.spec is None:
            fallback = f"I cannot help with that: {state.refusal}" if state.refusal else NO_ANSWER
            return [{"type": "summary", "text": text or fallback}]
        events = []
        for widget in state.spec.widgets:
            info = state.handles[widget.handle]
            events.append({"type": "widget", "widget": widget.model_dump(),
                           "handle_info": {"columns": info.columns, "row_count": info.row_count,
                                           "source": info.source, "metric_id": info.metric_id,
                                           "recipe": info.recipe}})
        # the supervisor's text saw the sample rows; the spec narrative (written from columns only) is the backup
        summary = text or (state.spec.narrative if not state.spec_is_fallback else "") or NO_ANSWER
        events.append({"type": "summary", "text": summary})
        return events

    @staticmethod
    async def _record_answer(gateway: GatewayPort, question: str, state: RunState) -> dict | None:
        """Write back only the governed-metric handles the delivered dashboard rests on. Returns the `answer` event
        payload ({record_id, confirmable}) when the gateway recorded it, else None: the user can confirm only an
        answer the gateway holds, and the gateway decides whether it is metric-backed."""
        if state.spec is None or state.spec_is_fallback or state.refusal or state.error_code:
            return None
        handles = [h for h in dict.fromkeys(w.handle for w in state.spec.widgets) if h in state.metric_handles]
        if not handles:
            return None
        try:
            out = await gateway.call("record_answer", {"question": question, "plan": "agent run",
                                                       "handles": handles[:RECORDED_HANDLES]})
        except Exception as exc:  # noqa: BLE001 - a failed write-back never fails a delivered answer
            log.warning("record_answer failed: %s", getattr(exc, "code", type(exc).__name__))
            return None
        record_id = out.get("record_id")
        if not isinstance(record_id, str) or not RECORD_ID.match(record_id):
            return None
        return {"record_id": record_id, "confirmable": out.get("metric_backed") is True}

    @staticmethod
    def _path(state: RunState) -> str:
        if state.delegated:
            return "delegated"
        if state.metric_handles and set(state.handles) == state.metric_handles:
            return "metric"
        return "direct"

    def _telemetry(self, state: RunState) -> dict:
        m = state.meter
        return {"type": "telemetry", "run_id": state.run_id, "path": self._path(state), "models": list(m.models),
                "input_tokens": m.input_tokens, "output_tokens": m.output_tokens,
                "cache_read_input_tokens": m.cache_read_input_tokens, "llm_turns": m.llm_turns,
                "tool_calls": m.tool_calls, "tool_latency_ms": m.tool_latency_ms,
                "cost_usd": estimate_cost(m.by_model)}

    @staticmethod
    def _error(code: str, message: str) -> dict:
        return {"type": "error", "code": code, "message": message}


FAILURE_MESSAGES = {
    "model_unavailable": "The assistant is temporarily unavailable.",
    "model_truncated": "The assistant ran out of room before finishing. Try a narrower question.",
    "run_limit": "That question needed more steps than allowed. Try a narrower question.",
    "gateway_unavailable": "The data service is unavailable.",
    "internal_error": "Something went wrong.",
}
