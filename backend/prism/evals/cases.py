"""Golden and red-team eval cases (YAML next to this module), validated on load."""
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from prism.agent.recipes import parse_recipe
from prism.agent.spec import WIDGET_TYPES
from prism.security.personas import PERSONAS
from prism.sim.canaries import CANARIES

GOLDEN = Path(__file__).with_name("golden.yaml")
REDTEAM = Path(__file__).with_name("redteam.yaml")
Scalar = str | int | float
CaseId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _persona(v: str) -> str:
    if v not in PERSONAS:
        raise ValueError(f"unknown persona {v!r}")
    return v


class Top(_M):
    by: str
    key: str
    equals: Scalar
    where: dict[str, Scalar] = {}


class Count(_M):
    min: int = 0
    max: int | None = None


class Story(_M):
    top: Top | None = None
    contains: dict[str, Scalar] | None = None
    count: Count | None = None

    @model_validator(mode="after")
    def _one(self) -> "Story":
        if sum(x is not None for x in (self.top, self.contains, self.count)) != 1:
            raise ValueError("a story assertion has exactly one of top, contains, count")
        return self


class AnswerCheck(_M):
    """Phrases the written answer must contain (case-insensitive): every `all` phrase and at least one `any` phrase."""
    all: list[str] = []
    any: list[str] = []

    @model_validator(mode="after")
    def _some(self) -> "AnswerCheck":
        if not (self.all or self.any):
            raise ValueError("answer needs at least one `all` or `any` phrase")
        return self


class Expect(_M):
    metric_id: str | None = None
    source: str | None = None
    chart_types: list[Literal[WIDGET_TYPES]] | None = None   # type: ignore[valid-type]
    story: list[Story] = []
    answer: AnswerCheck | None = None

    @model_validator(mode="after")
    def _target(self) -> "Expect":
        if (self.metric_id is None) == (self.source is None):
            raise ValueError("expect needs exactly one of metric_id, source")
        return self


class Tolerance(_M):
    rel: float = 1e-6
    abs: float = 0.0


class GoldenCase(_M):
    id: CaseId
    persona: str
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    reference: dict[str, Any]
    expect: Expect
    tolerance: Tolerance = Tolerance()

    check_persona = field_validator("persona")(_persona)

    @field_validator("reference", mode="before")
    @classmethod
    def _recipe(cls, v: object) -> dict:
        return parse_recipe(v)


class ToolRule(_M):
    tool: str
    source: str | None = None
    metric_id: str | None = None


class Scope(_M):
    column: str
    allowed: list[str]


class Forbid(_M):
    obey: list[str] = []
    hidden: list[str] = []
    values: list[str] = []
    tools: list[ToolRule] = []
    out_of_scope: Scope | None = None

    @model_validator(mode="after")
    def _canaries(self) -> "Forbid":
        for kind, keys in (("obey", self.obey), ("hidden", self.hidden)):
            for k in keys:
                if k not in CANARIES or CANARIES[k].kind != kind:
                    raise ValueError(f"{k!r} is not a {kind} canary")
        return self


class RedTeamCase(_M):
    id: CaseId
    persona: str
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    forbid: Forbid

    check_persona = field_validator("persona")(_persona)


def _load(path: Path, model: type[_M]) -> list:
    raw = yaml.safe_load(Path(path).read_text()) or []
    cases = [model.model_validate(item) for item in raw]
    ids = [c.id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate case ids in {Path(path).name}: {dupes}")
    return cases


def load_golden(path: Path = GOLDEN) -> list[GoldenCase]:
    return _load(path, GoldenCase)


def load_redteam(path: Path = REDTEAM) -> list[RedTeamCase]:
    return _load(path, RedTeamCase)
