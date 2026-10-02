"""Plain result types shared by the eval client, graders and runner."""
from dataclasses import dataclass, field


@dataclass
class Table:
    columns: list[str]
    rows: list[list]
    truncated: bool = False


@dataclass
class ChatResult:
    plans: list[dict] = field(default_factory=list)
    widgets: list[dict] = field(default_factory=list)
    summary: str | None = None
    answer: dict | None = None
    error: dict | None = None
    telemetry: dict | None = None
    timed_out: bool = False
    seconds: float = 0.0

    @classmethod
    def from_events(cls, events: list[dict], *, seconds: float, timed_out: bool = False) -> "ChatResult":
        out = cls(seconds=seconds, timed_out=timed_out)
        for e in events:
            kind = e.get("type")
            if kind == "plan":
                out.plans.append(e)
            elif kind == "widget":
                out.widgets.append(e)
            elif kind == "summary":
                out.summary = e.get("text")
            elif kind == "answer":
                out.answer = e
            elif kind == "error" and out.error is None:
                out.error = e
            elif kind == "telemetry":
                out.telemetry = e
        return out

    @property
    def cost_usd(self) -> float:
        cost = (self.telemetry or {}).get("cost_usd")
        return float(cost) if isinstance(cost, (int, float)) else 0.0

    @property
    def handles(self) -> list[str]:
        return list(dict.fromkeys(w["widget"]["handle"] for w in self.widgets))
