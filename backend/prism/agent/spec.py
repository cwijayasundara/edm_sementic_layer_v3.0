"""The dashboard spec the agent emits: widgets reference gateway result handles and name columns, never carry rows.
The harness validates a spec against the handles it actually saw before anything is rendered."""
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

WIDGET_TYPES = ("kpi", "bar", "stacked_bar", "line", "heatmap", "table", "pie", "scatter")


class Encoding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: str | None = None
    y: str | None = None
    series: str | None = None
    value: str | None = None
    unit: str | None = None


class Widget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: Literal["kpi", "bar", "stacked_bar", "line", "heatmap", "table", "pie", "scatter"]
    title: str = Field(min_length=1, max_length=120)
    handle: str
    encoding: Encoding


class DashboardSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    widgets: list[Widget] = Field(min_length=1, max_length=8)
    narrative: str = Field(min_length=1, max_length=600)


SPEC_JSON_SCHEMA: dict = DashboardSpec.model_json_schema()

# required encoding fields per widget type
_REQUIRED = {"kpi": ("value",), "bar": ("x", "y"), "stacked_bar": ("x", "y"), "line": ("x", "y"),
             "scatter": ("x", "y"), "pie": ("x", "y"), "heatmap": ("x", "y", "value"), "table": ()}


@dataclass
class HandleInfo:
    handle: str
    columns: list[str]
    source: str | None
    metric_id: str | None
    row_count: int = 0
    sample_rows: tuple = ()
    recipe: dict | None = None   # how the handle was produced (prism.agent.recipes); None when it cannot be replayed


def validate_spec(spec: DashboardSpec, handles: dict[str, HandleInfo]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for w in spec.widgets:
        if w.id in seen:
            problems.append(f"widget {w.id}: duplicate widget id")
        seen.add(w.id)
        info = handles.get(w.handle)
        if info is None:
            problems.append(f"widget {w.id}: unknown handle {w.handle}")
            continue
        for field in ("x", "y", "series", "value"):
            col = getattr(w.encoding, field)
            if col is not None and col not in info.columns:
                problems.append(f"widget {w.id}: {field} column {col!r} is not in handle {w.handle}")
        for field in _REQUIRED[w.type]:
            if getattr(w.encoding, field) is None:
                problems.append(f"widget {w.id}: {w.type} needs encoding.{field}")
    return problems


def parse_spec(raw: dict) -> DashboardSpec:
    try:
        return DashboardSpec.model_validate(raw)
    except ValidationError as exc:
        paths = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise ValueError(f"invalid dashboard spec: {', '.join(paths)}") from None


def fallback_spec(handles: dict[str, HandleInfo], last_handle: str, narrative: str) -> DashboardSpec:
    widget = Widget(id="w1", type="table", title="Results", handle=last_handle, encoding=Encoding())
    return DashboardSpec(widgets=[widget], narrative=narrative[:600] or "Results.")
