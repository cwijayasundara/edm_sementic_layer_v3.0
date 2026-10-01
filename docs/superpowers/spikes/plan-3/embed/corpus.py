"""Build the retrieval corpus (governed metrics + glossary terms) from the repo (read-only)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_BACKEND = Path(os.environ.get(
    "PRISM_BACKEND",
    "/Users/chamindawijayasundara/Documents/learning_101/edm_sementic_layer_v2.0/backend",
))
HERE = Path(__file__).parent


@dataclass(frozen=True)
class Doc:
    id: str                      # "metric:<id>" or "term:<name>"
    kind: str                    # "metric" | "term"
    name: str
    description: str
    synonyms: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    unit: str = ""
    related: tuple[str, ...] = field(default=())

    def text(self) -> str:
        """Text used for BOTH dense passage embedding and BM25."""
        human = self.name.replace("_", " ")
        parts = [human + ".", self.description]
        if self.synonyms:
            parts.append("Also called: " + ", ".join(self.synonyms) + ".")
        if self.unit:
            parts.append(f"Unit: {self.unit}.")
        if self.dimensions:
            parts.append("By: " + ", ".join(d.replace("_", " ") for d in self.dimensions) + ".")
        return " ".join(parts)


def load_metrics(backend: Path = REPO_BACKEND) -> list[Doc]:
    docs: list[Doc] = []
    for f in sorted((backend / "prism/mcp/metrics").glob("*.yaml")):
        m = yaml.safe_load(f.read_text())
        docs.append(Doc(id=f"metric:{m['id']}", kind="metric", name=m["id"],
                        description=m.get("description", ""), unit=str(m.get("unit", "")),
                        dimensions=tuple((m.get("dimensions") or {}).keys())))
    for f in ("refmaster.yaml", "marketmaster.yaml"):
        spec = yaml.safe_load((backend / "prism/mcp/rest" / f).read_text())
        for m in spec.get("metrics") or []:
            docs.append(Doc(id=f"metric:{m['id']}", kind="metric", name=m["id"],
                            description=m.get("description", ""), unit=str(m.get("unit", "")),
                            dimensions=tuple((m.get("dimensions") or {}).keys())))
    return docs


def load_glossary(path: Path = HERE / "glossary.yaml") -> list[Doc]:
    raw = yaml.safe_load(path.read_text())
    return [Doc(id=f"term:{t['term']}", kind="term", name=t["term"], description=t["definition"],
                synonyms=tuple(t.get("synonyms") or ()), related=tuple(t.get("related") or ()))
            for t in raw["terms"]]


def load_corpus(include_glossary: bool = True) -> list[Doc]:
    docs = load_metrics()
    if include_glossary:
        docs += load_glossary()
    ids = [d.id for d in docs]
    assert len(ids) == len(set(ids)), "duplicate doc ids"
    return docs


if __name__ == "__main__":
    ms = load_metrics()
    gs = load_glossary()
    print(f"metrics={len(ms)} glossary={len(gs)}")
    for d in ms[:2] + gs[:2]:
        print(d.id, "=>", d.text())
