"""Hand-written knowledge for the context graph: ontology, business glossary and seed query history (YAML next to
this module). load_knowledge() parses them into pydantic models and checks every cross-reference, so a typo in a
metric id, concept name or term fails at load time instead of producing a silently broken graph."""
import re
from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics
from prism.mcp.rest_backend import load_rest_config

KNOWLEDGE_DIR = Path(__file__).parent
REST_SOURCES = ("refmaster", "marketmaster")


_COLUMN_REF = re.compile(r"^column:\w+\.\w+\.\w+$")
_TABLE_REF = re.compile(r"^table:\w+\.\w+$")
_ENDPOINT_REF = re.compile(r"^endpoint:\w+\.\w+$")
# A plan says HOW to answer (metric, dimensions, filters, time range, how to read the result), never WHAT the answer
# was: the graph gate is scope-only, so a row-restricted caller would read any figure or entity id kept in a plan.
# These patterns are the mechanical backstop; numbers used as parameters ("last_business_days: 6", "age_days > 5",
# "5 bps", "trailing 5 business days") pass. Entity names without digits ("Vendor A") cannot be told apart from
# method text, so authors (and the history distiller) must also keep those out by rule.
_RESULT_PATTERNS = (
    re.compile(r"\b[A-Z]{2,}[-_]?\d{2,}\b"),                                   # entity ids: SRC001, LE00016, PF_003
    re.compile(r"\b\d+(?:\.\d+)? of \d+(?:\.\d+)?\b"),                             # result counts: 64 of 79
    re.compile(r"\d+(?:\.\d+)?\s*%"),                                             # shares: 66.7%
    re.compile(r"\b\d+(?:\.\d+)? to \d+(?:\.\d+)?\b"),                             # observed moves: 75 to 90
    re.compile(r"(?:\b(?:about|around|approximately|roughly)\s+|~\s*)\d+(?:\.\d+)?"),  # approximated results
    re.compile(r"\b\d+(?:\.\d+)? \w+ (?:per|a|each) (?:day|week|month)\b"),           # observed rates
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _bad_refs(refs: list[str], pattern: re.Pattern) -> list[str]:
    return [r for r in refs if not pattern.match(r)]


class Concept(_Model):
    name: str
    description: str
    implemented_by: list[str]  # table:<src>.<table> | endpoint:<src>.<endpoint_id>
    identified_by: list[str]   # column:<src>.<table>.<col>

    @model_validator(mode="after")
    def _ref_shapes(self) -> "Concept":
        bad = _bad_refs(self.identified_by, _COLUMN_REF)
        bad += [r for r in self.implemented_by if not (_TABLE_REF.match(r) or _ENDPOINT_REF.match(r))]
        if bad:
            raise ValueError(f"concept {self.name}: malformed refs {bad}")
        return self


class ConceptRelation(_Model):
    source: str
    rel: str
    target: str

    @model_validator(mode="before")
    @classmethod
    def _from_triple(cls, v):
        if isinstance(v, (list, tuple)):
            if len(v) != 3:
                raise ValueError(f"concept relation must be [source, relation, target], got {list(v)}")
            return dict(zip(("source", "rel", "target"), v))
        return v


class Term(_Model):
    name: str
    glossary: str
    definition: str
    synonyms: list[str] = []
    rule: str | None = None
    broader: str | None = None
    defines: list[str] = []  # metric:<id> | concept:<Name>
    tags: list[str] = []     # column:<src>.<table>.<col>
    public: bool = False     # readable by everyone; only for terms bound to no source object

    @property
    def key(self) -> str:
        """Case-insensitive identity; `name` keeps the authored display form."""
        return self.name.casefold()

    @model_validator(mode="after")
    def _tag_shapes(self) -> "Term":
        if bad := _bad_refs(self.tags, _COLUMN_REF):
            raise ValueError(f"term {self.name}: malformed tags {bad}")
        linked = bool(self.defines or self.tags)
        if self.public and linked:
            raise ValueError(f"term {self.name}: public terms must not have defines or tags")
        if not self.public and not linked:  # would be readable by nobody: an authoring error, not a silent default
            raise ValueError(f"term {self.name}: no defines or tags; link it or mark it public: true")
        return self


def plan_result_leaks(plan: str) -> list[str]:
    """Fragments of `plan` that look like results (entity ids, counts, shares, observed moves or rates) rather than
    method. Empty for a method-only plan. Shared with the history distiller, which must apply the same rule."""
    return [m.group(0) for p in _RESULT_PATTERNS for m in p.finditer(plan)]


class SeedQuestion(_Model):
    question: str
    metrics: list[str]
    plan: str
    status: Literal["verified", "candidate"]

    @model_validator(mode="after")
    def _method_only(self) -> "SeedQuestion":
        if leaks := plan_result_leaks(self.plan):
            raise ValueError(f"history {self.question!r}: plan must be method only, found results {leaks}")
        return self


class Knowledge(_Model):
    concepts: list[Concept]
    concept_relations: list[ConceptRelation]
    same_key: list[list[str]]  # groups of columns holding the same identifier under different names
    terms: list[Term]
    history: list[SeedQuestion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references_resolve(self) -> "Knowledge":
        errors: list[str] = []
        concepts = {c.name for c in self.concepts}
        metric_ids = set(load_metrics(DEFAULT_METRICS_DIR))
        endpoints: set[str] = set()
        for source in REST_SOURCES:
            rest_endpoints, rest_metrics = load_rest_config(source)
            metric_ids |= set(rest_metrics)
            endpoints |= {f"{source}.{e}" for e in rest_endpoints}
        if len(concepts) != len(self.concepts):
            errors.append("duplicate concept names")
        for group in self.same_key:
            if len(group) < 2 or (bad := _bad_refs(group, _COLUMN_REF)):
                errors.append(f"same_key group {group}: needs 2+ column refs" + (f", malformed {bad}" if len(group) >= 2 else ""))
        keys = Counter(t.key for t in self.terms)
        errors += [f"duplicate term name {k!r}" for k, n in keys.items() if n > 1]
        syns = Counter(x.casefold() for t in self.terms for x in t.synonyms)
        errors += [f"duplicate synonym {x!r}" for x, n in syns.items() if n > 1]
        errors += [f"synonym {x!r} is also a term name" for x in syns if x in keys]
        for r in self.concept_relations:
            errors += [f"relation {r.source}-{r.rel}->{r.target}: unknown concept {n}" for n in (r.source, r.target)
                       if n not in concepts]
        for c in self.concepts:
            errors += [f"concept {c.name}: endpoint {r} is not in the REST registry" for r in c.implemented_by
                       if r.startswith("endpoint:") and r.removeprefix("endpoint:") not in endpoints]
        names = set(keys)
        for t in self.terms:
            if t.broader is not None and t.broader.casefold() not in names:
                errors.append(f"term {t.name}: unknown broader term {t.broader}")
            for ref in t.defines:
                kind, _, value = ref.partition(":")
                if kind == "metric" and value not in metric_ids:
                    errors.append(f"term {t.name}: unknown metric {value}")
                elif kind == "concept" and value not in concepts:
                    errors.append(f"term {t.name}: unknown concept {value}")
                elif kind not in ("metric", "concept"):
                    errors.append(f"term {t.name}: bad defines ref {ref}")
        for q in self.history:
            errors += [f"history {q.question!r}: unknown metric {m}" for m in q.metrics if m not in metric_ids]
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _read(directory: Path, name: str) -> dict:
    return yaml.safe_load((directory / name).read_text())


def load_knowledge(directory: Path | None = None) -> Knowledge:
    d = directory or KNOWLEDGE_DIR
    ontology, glossary, history = _read(d, "ontology.yaml"), _read(d, "glossary.yaml"), _read(d, "history.yaml")
    return Knowledge(
        concepts=ontology["concepts"],
        concept_relations=ontology["concept_relations"],
        same_key=ontology["same_key"],
        terms=glossary["terms"],
        history=history["questions"],
    )
