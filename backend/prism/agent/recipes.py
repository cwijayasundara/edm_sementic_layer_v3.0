"""Recipes: how a result handle was produced, so a saved widget can be re-run later with the *current* caller's
token. A recipe holds only tool arguments the caller could send by asking; the gateway (policy, SQL guard, RLS)
re-checks every call, so a client-supplied recipe grants nothing."""
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from prism.agent.gateway_client import GatewayError, GatewayPort
from prism.gateway.combine import MAX_SQL_CHARS
from prism.gateway.policy import MAX_DIMENSIONS, MAX_FILTERS, MAX_LIMIT
from prism.gateway.service import MAX_NAME, MAX_REQUEST_KEYS

MAX_DEPTH = 3
MAX_NODES = 8
_NAME = Annotated[str, Field(min_length=1, max_length=MAX_NAME)]
Status = Literal["not_permitted", "unavailable", "invalid"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricArgs(_Model):
    metric_id: _NAME
    dimensions: list[_NAME] = Field(default_factory=list, max_length=MAX_DIMENSIONS)
    filters: dict[str, Any] = Field(default_factory=dict, max_length=MAX_FILTERS)
    limit: StrictInt | None = Field(default=None, ge=1, le=MAX_LIMIT)


class QueryArgs(_Model):
    source: Annotated[str, Field(min_length=1, max_length=64)]  # mirrors gateway QuerySourceArgs.source
    request: dict[str, Any] = Field(max_length=MAX_REQUEST_KEYS)


class CombineArgs(_Model):
    sql: str = Field(min_length=1, max_length=MAX_SQL_CHARS)
    inputs: dict[_NAME, dict] = Field(min_length=1, max_length=8)  # mirrors gateway CombineArgs.handles


class _Node(_Model):
    tool: Literal["run_metric", "query_source", "combine"]
    args: dict


_ARGS = {"run_metric": MetricArgs, "query_source": QueryArgs, "combine": CombineArgs}


def parse_recipe(raw: object) -> dict:
    """Validate and normalise a recipe tree; ValueError (no detail beyond field paths) on any problem."""
    count = [0]

    def walk(node: object, depth: int) -> dict:
        if depth > MAX_DEPTH:
            raise ValueError(f"recipe deeper than depth {MAX_DEPTH}")
        count[0] += 1
        if count[0] > MAX_NODES:
            raise ValueError(f"recipe has more than {MAX_NODES} nodes")
        try:
            n = _Node.model_validate(node)
            args = _ARGS[n.tool].model_validate(n.args).model_dump()
        except ValidationError as exc:
            paths = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise ValueError(f"invalid recipe: {', '.join(paths)}") from None
        if n.tool == "combine":
            args["inputs"] = {t: walk(child, depth + 1) for t, child in args["inputs"].items()}
        return {"tool": n.tool, "args": args}

    return walk(raw, 1)


class ReplayError(Exception):
    def __init__(self, status: Status):
        super().__init__(status)
        self.status: Status = status


def status_for(exc: GatewayError) -> Status:
    if exc.final:
        return "not_permitted"
    if exc.caller_fixable:
        return "invalid"
    return "unavailable"


def _call_args(recipe: dict, handles: dict[str, str] | None = None) -> dict:
    tool, args = recipe["tool"], recipe["args"]
    if tool == "run_metric":
        out = {"metric_id": args["metric_id"], "dimensions": args["dimensions"], "filters": args["filters"]}
        if args["limit"] is not None:
            out["limit"] = args["limit"]
        return out
    if tool == "query_source":
        return {"source": args["source"], "request": args["request"]}
    return {"sql": args["sql"], "handles": handles or {}}


class Replayer:
    """Runs recipes through the gateway as the caller. Inputs run before the combine that reads them; identical
    sub-recipes (canonical JSON) run once per Replayer, i.e. once per dashboard run."""

    def __init__(self, gateway: GatewayPort):
        self._gateway = gateway
        self._done: dict[str, dict] = {}

    async def run(self, recipe: dict) -> dict:
        key = json.dumps(recipe, sort_keys=True, separators=(",", ":"), default=str)
        if key in self._done:
            return self._done[key]
        handles = None
        if recipe["tool"] == "combine":
            handles = {t: (await self.run(child))["handle"] for t, child in recipe["args"]["inputs"].items()}
        try:
            out = await self._gateway.call(recipe["tool"], _call_args(recipe, handles))
        except GatewayError as exc:
            raise ReplayError(status_for(exc)) from None
        summary = dict(out["summary"])
        summary["handle"] = out["handle"]
        self._done[key] = summary
        return summary
