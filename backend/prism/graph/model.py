"""build_graph(): the repo's own registries (DDL, SQL metric YAML, REST endpoint YAML) + hand-written knowledge YAML
-> plain node / edge lists with stable local uids. Pure apart from one embedding batch; nothing here talks to Neo4j.

Ported from the plan-3 Neo4j spike. Every node carries `allowed_scopes`: the caller scopes any one of which makes
the object readable, mirroring prism.security.access.can() (`<source>` or `<source>.<table>`); ['*'] marks objects
that are not bound to a source (glossary terms marked `public: true`); an empty list is readable by nobody (Role
nodes, which only the gateway's catalog reads, and e.g. a term whose only links were excluded). Terms, concepts and
questions carry the UNION of their links' scopes, so retrieval re-checks every linked object on every hop (and a
Question every object its Execution USED). Row-level security is not modelled here; it stays at the sources.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from prism.db.migrate import DDL_DIR
from prism.graph.embedder import Embedder
from prism.graph.knowledge import REST_SOURCES, Knowledge, load_knowledge
from prism.graph.schema import GRAPH_SCHEMA_VERSION
from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics
from prism.mcp.rest_backend import load_rest_config
from prism.security.personas import ALL_SOURCES, PERSONAS

PUBLIC = ["*"]
SEARCHABLE = "Searchable"
_REL_TYPE = re.compile(r"[A-Z][A-Z_]*")
_LABEL = re.compile(r"[A-Z][A-Za-z0-9]*")


def check_label(label: str) -> str:
    """Labels are interpolated into Cypher by the loader: identifiers only."""
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise ValueError(f"bad node label {label!r}")
    return label


def check_rel_type(typ: str) -> str:
    """Relationship types are interpolated into Cypher by the loader: identifiers only."""
    if not isinstance(typ, str) or not _REL_TYPE.fullmatch(typ):
        raise ValueError(f"bad relationship type {typ!r}")
    return typ

# ------------------------------------------------------------------------------------------------- DDL parsing
_CREATE = re.compile(r"CREATE TABLE\s+(?:(\w+)\.)?(\w+)\s*\(", re.IGNORECASE)
_VIEW = re.compile(r"CREATE VIEW\s+(?:public\.)?(\w+)\b[^;]*?\bAS\s+SELECT\s+(.*?)\s+FROM\s+(\w+)\.(\w+)\s*;",
                   re.IGNORECASE | re.DOTALL)
_COL = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s+([a-z]+(?:\s*\([\d,\s]+\))?)", re.IGNORECASE)
_REF = re.compile(r"REFERENCES\s+(?:\w+\.)?(\w+)(?:\s*\(\s*(\w+)\s*\))?", re.IGNORECASE)
_CONSTRAINT = re.compile(r"^\s*(PRIMARY|UNIQUE|CHECK|FOREIGN|CONSTRAINT|EXCLUDE)\b", re.IGNORECASE)
_PK = re.compile(r"^\s*PRIMARY KEY\s*\(([^)]*)\)", re.IGNORECASE)
_ALIAS = re.compile(r"\bAS\s+(\w+)\s*$", re.IGNORECASE)
_IDENT = re.compile(r"^\s*(\w+)\s*$")


@dataclass
class TableSpec:
    columns: list[tuple[str, str]]          # (name, type) in DDL order
    primary_key: list[str]
    references: list[tuple[str, str, str | None]]  # (column, target table, target column or None = its PK)
    masked: set[str] = field(default_factory=set)   # columns a masking view rewrites (e.g. account numbers)


def _split_top(body: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in body:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def parse_ddl(path: Path) -> dict[str, TableSpec]:
    """Tables the source's MCP server exposes: public tables, plus each private table that a public masking view
    re-exposes under its own name (columns the view does not select verbatim are flagged masked)."""
    text = re.sub(r"--[^\n]*", "", path.read_text())
    raw: dict[tuple[str, str], TableSpec] = {}
    for m in _CREATE.finditer(text):
        i, depth = m.end(), 1
        while depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        cols, pk, refs = [], [], []
        for part in _split_top(text[m.end():i - 1]):
            if pm := _PK.match(part):
                pk = [c.strip() for c in pm.group(1).split(",")]
                continue
            if _CONSTRAINT.match(part) or not (cm := _COL.match(part)):
                continue
            name, typ = cm.group(1), re.sub(r"\s+", "", cm.group(2)).lower()
            cols.append((name, typ))
            if re.search(r"PRIMARY KEY", part, re.IGNORECASE):
                pk = [name]
            if rm := _REF.search(part):
                refs.append((name, rm.group(1), rm.group(2)))
        raw[((m.group(1) or "public").lower(), m.group(2))] = TableSpec(cols, pk, refs)
    tables = {name: spec for (schema, name), spec in raw.items() if schema == "public"}
    for vm in _VIEW.finditer(text):
        view, select, base = vm.group(1), vm.group(2), raw.get((vm.group(3).lower(), vm.group(4)))
        if base is None:
            continue
        types = dict(base.columns)
        cols, masked = [], set()
        for item in _split_top(select):
            if im := _IDENT.match(item):
                cols.append((im.group(1), types[im.group(1)]))
            elif am := _ALIAS.search(item.strip()):
                cols.append((am.group(1), types.get(am.group(1), "text")))
                masked.add(am.group(1))
            else:
                raise ValueError(f"{path.name}: view {view}: cannot name select item {item.strip()!r}")
        tables[view] = TableSpec(cols, base.primary_key, base.references, masked)
    return tables


def load_ddl(sources=ALL_SOURCES, ddl_dir: Path = DDL_DIR) -> dict[str, dict[str, TableSpec]]:
    return {s: parse_ddl(ddl_dir / f"{s}.sql") for s in sources}


# ------------------------------------------------------------------------------------------------- scopes
def readers(source: str, tables: list[str]) -> list[str]:
    """Scopes any one of which satisfies can(claims, source, t) for every t in tables. allowed_scopes is an any-of
    list, so an object spanning several tables is readable through the source scope only (fails closed)."""
    return [source] + ([f"{source}.{tables[0]}"] if len(tables) == 1 else [])


def _union(scope_lists) -> list[str]:
    return sorted({s for sc in scope_lists for s in sc})


# ------------------------------------------------------------------------------------------------- graph
@dataclass
class Graph:
    nodes: dict[str, dict] = field(default_factory=dict)   # local uid -> {"labels": [...], "props": {...}}
    edges: list[tuple[str, str, str, dict]] = field(default_factory=list)

    def node(self, uid: str, labels: list[str], **props) -> None:
        if uid in self.nodes:
            raise ValueError(f"duplicate node uid {uid}")
        for label in labels:
            check_label(label)
        self.nodes[uid] = {"labels": labels, "props": {k: v for k, v in props.items() if v is not None}}

    def edge(self, a: str, typ: str, b: str, **props) -> None:
        check_rel_type(typ)
        self.edges.append((a, typ, b, props))

    def scopes(self, uid: str) -> list[str]:
        return self.nodes[uid]["props"]["allowed_scopes"]

    def labelled(self, label: str) -> list[dict]:
        return [n["props"] for n in self.nodes.values() if label in n["labels"]]


def _words(*parts) -> str:
    return " ".join(str(p).replace("_", " ") for p in parts if p)


def _term_uid(name: str) -> str:
    return f"term:{name.casefold()}"


def _question_uid(text: str) -> str:
    return hashlib.sha256(text.casefold().encode()).hexdigest()[:16]


def _add_schema(g: Graph, ddl: dict[str, dict[str, TableSpec]]) -> None:
    for s, tables in ddl.items():
        g.node(f"source:{s}", ["Source"], name=s, kind="rest" if s in REST_SOURCES else "sql",
               mcp_server=f"{s}-mcp", allowed_scopes=[s] + [f"{s}.{t}" for t in sorted(tables)])
        for t, spec in tables.items():
            tu, sc = f"table:{s}.{t}", readers(s, [t])
            g.node(tu, ["Table"], name=t, source=s, qualified_name=f"{s}.{t}", primary_key=spec.primary_key,
                   allowed_scopes=sc)
            g.edge(f"source:{s}", "HAS_TABLE", tu)
            for c, typ in spec.columns:
                cu = f"column:{s}.{t}.{c}"
                g.node(cu, ["Column", SEARCHABLE], name=c, table=t, source=s, type=typ, masked=c in spec.masked,
                       description=_words(s, t, c, typ, "masked" if c in spec.masked else None), synonyms=[],
                       allowed_scopes=sc)
                g.edge(tu, "HAS_COLUMN", cu)
        for t, spec in tables.items():
            for c, target, target_col in spec.references:
                pk = tables.get(target, TableSpec([], [], [])).primary_key
                target_col = target_col or (pk[0] if len(pk) == 1 else None)
                if target_col:
                    g.edge(f"column:{s}.{t}.{c}", "REFERENCES", f"column:{s}.{target}.{target_col}")


def _metric_rows(ddl: dict[str, dict[str, TableSpec]], g: Graph) -> list[dict]:
    rows = []
    for m in load_metrics(DEFAULT_METRICS_DIR).values():
        tables = sorted(m.tables)
        rows.append(dict(id=m.id, source=m.source, kind="sql", type=m.type, unit=m.unit, definition=m.description,
                         time_column=m.time_column, dims=m.dimensions,
                         filters=[f"{k}:{f.type}" for k, f in sorted(m.filters.items())],
                         sensitive=m.sensitive_dimensions, required=m.required_dimensions,
                         fine=m.fine_grain_dimensions, tables=tables, endpoint=None, scopes=readers(m.source, tables)))
    for s in REST_SOURCES:
        endpoints, metrics = load_rest_config(s)
        for e in endpoints.values():
            eu, sc = f"endpoint:{s}.{e.id}", readers(s, [e.table])
            g.node(eu, ["Endpoint"], name=e.id, endpoint_id=e.id, method="GET", path=e.path, source=s, table=e.table,
                   result_key=e.result_key, mcp_tool="query", description=e.description,
                   params=[f"{n}:{p.type}:{p.location}" + (":required" if p.required else "")
                           for n, p in e.params.items()],
                   allowed_scopes=sc)
            g.edge(f"source:{s}", "HAS_ENDPOINT", eu)
            g.edge(eu, "BACKED_BY", f"table:{s}.{e.table}")
            for c, typ in ddl[s][e.table].columns:
                fu = f"field:{s}.{e.id}.{c}"
                g.node(fu, ["Field"], name=c, type=typ, source=s, endpoint_id=e.id, allowed_scopes=sc,
                       json_path=f"$.{e.result_key}[*].{c}" if e.result_key else f"$.{c}")
                g.edge(eu, "RETURNS", fu)
                g.edge(fu, "MAPS_TO", f"column:{s}.{e.table}.{c}")
        for m in metrics.values():
            table = endpoints[m.endpoint].table
            rows.append(dict(id=m.id, source=s, kind="rest", type="endpoint", unit=m.unit, definition=m.description,
                             time_column="from/to", dims=m.dimensions,
                             filters=[f"{k}:{f.type}" for k, f in sorted(m.filters.items())],
                             sensitive=m.sensitive_dimensions, required=[], fine=m.fine_grain_dimensions,
                             tables=[table],
                             endpoint=m.endpoint,
                             scopes=readers(s, [table])))
    return rows


def _add_metrics(g: Graph, ddl: dict[str, dict[str, TableSpec]], exclude: frozenset[str]) -> None:
    for r in _metric_rows(ddl, g):
        mu = f"metric:{r['id']}"
        if mu in exclude:
            continue
        s = r["source"]
        g.node(mu, ["Metric", SEARCHABLE], id=r["id"], name=r["id"], source=s, kind=r["kind"], type=r["type"],
               mcp_server=f"{s}-mcp", mcp_tool="run_metric", endpoint_id=r["endpoint"], unit=r["unit"],
               definition=r["definition"], description=_words(r["id"], r["definition"]), synonyms=[],
               time_column=r["time_column"], dimensions=sorted(r["dims"]), required_dimensions=sorted(r["required"]),
               sensitive_dimensions=sorted(r["sensitive"]), fine_grain_dimensions=sorted(r["fine"]),
               filters=r["filters"],
               tables=[f"{s}.{t}" for t in r["tables"]], allowed_scopes=r["scopes"],
               schema_version=GRAPH_SCHEMA_VERSION)
        if r["endpoint"]:
            g.edge(mu, "COMPUTED_FROM", f"endpoint:{s}.{r['endpoint']}")
        for t in r["tables"]:
            g.edge(mu, "COMPUTED_FROM", f"table:{s}.{t}")
        single = r["tables"][0] if len(r["tables"]) == 1 else None
        columns = {c for c, _ in ddl[s][single].columns} if single else set()
        for d, expr in r["dims"].items():
            du = f"dim:{r['id']}.{d}"
            g.node(du, ["Dimension"], name=d, metric=r["id"], expr=expr, sensitive=d in r["sensitive"],
                   required=d in r["required"], grain="fine" if d in r["fine"] else None,
                   allowed_scopes=r["scopes"])
            g.edge(mu, "HAS_DIMENSION", du)
            if expr in columns:
                g.edge(du, "ON_COLUMN", f"column:{s}.{single}.{expr}")


def _add_knowledge(g: Graph, k: Knowledge, exclude: frozenset[str]) -> None:
    for c in k.concepts:
        cu = f"concept:{c.name}"
        g.node(cu, ["Concept", SEARCHABLE], name=c.name, description=c.description, synonyms=[],
               allowed_scopes=_union(g.scopes(ref) for ref in c.implemented_by))
        for ref in c.implemented_by:
            g.edge(cu, "IMPLEMENTED_BY", ref)
        for ref in c.identified_by:
            g.edge(cu, "IDENTIFIED_BY", ref)
    for r in k.concept_relations:
        g.edge(f"concept:{r.source}", r.rel, f"concept:{r.target}")

    for t in k.terms:
        links = [ref for ref in t.defines + t.tags if ref not in exclude]
        g.node(_term_uid(t.name), ["BusinessTerm", SEARCHABLE], name=t.name, key=t.key, glossary=t.glossary,
               definition=t.definition, description=t.definition, synonyms=t.synonyms, rule=t.rule,
               status="approved", owner=f"{t.glossary}-data-owner",
               allowed_scopes=list(PUBLIC) if t.public else _union(g.scopes(ref) for ref in links))
        for ref in t.defines:
            if ref not in exclude:
                g.edge(_term_uid(t.name), "DEFINES", ref)
                if ref.startswith("metric:"):  # glossary vocabulary makes the metric findable by its business words
                    syn = g.nodes[ref]["props"]["synonyms"]
                    syn += [s for s in (t.name, *t.synonyms) if s not in syn]
        for ref in t.tags:
            g.edge(ref, "TAGGED_WITH", _term_uid(t.name))
    for t in k.terms:
        if t.broader:
            g.edge(_term_uid(t.name), "BROADER", _term_uid(t.broader))

    for group in k.same_key:
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                g.edge(a, "SAME_KEY_AS", b)

    for q in k.history:
        h = _question_uid(q.question)
        used = [f"metric:{m}" for m in q.metrics if f"metric:{m}" not in exclude]
        # allowed_scopes is only a prefilter (the union: an any-of list cannot say "all of these"); retrieval's gate
        # additionally requires every USED object to be readable (see `visible`). No USED object = nobody.
        sc = _union(g.scopes(u) for u in used)
        g.node(f"question:{h}", ["Question", SEARCHABLE], name=q.question, text=q.question, description=q.plan,
               synonyms=[], status=q.status, allowed_scopes=sc)
        g.node(f"exec:{h}", ["Execution"], tool="run_metric", plan=q.plan, metrics=list(q.metrics),
               status=q.status, allowed_scopes=sc)
        g.edge(f"question:{h}", "ANSWERED_BY", f"exec:{h}")
        for u in used:
            g.edge(f"exec:{h}", "USED", u)


def _add_roles(g: Graph) -> None:
    for p in PERSONAS.values():
        ru = f"role:{p.persona_id}"
        # Gateway-internal (read by load_catalog only): no caller scope ever makes a Role visible to retrieval.
        g.node(ru, ["Role"], persona_id=p.persona_id, name=p.display_name, metrics_only=p.metrics_only,
               scopes=list(p.scopes), allowed_scopes=[])
        row_scope = json.dumps({k: list(v) for k, v in p.rows.items()}, sort_keys=True)
        for scope in p.scopes:
            source, _, table = scope.partition(".")
            target = f"table:{scope}" if table else f"source:{source}"
            if target in g.nodes:  # non-dataset scopes such as pii:read grant no graph object
                g.edge(ru, "CAN_READ", target, scope=scope, row_scope=row_scope)


def _check(g: Graph) -> None:
    dangling = [(a, t, b) for a, t, b, _ in g.edges if a not in g.nodes or b not in g.nodes]
    if dangling:
        raise ValueError(f"dangling edge endpoints: {dangling[:10]}")
    for uid, n in g.nodes.items():
        sc = n["props"].get("allowed_scopes")
        if not isinstance(sc, list):
            raise ValueError(f"{uid}: allowed_scopes missing")


def search_text(props: dict) -> str:
    """Embedding text: name + synonyms + description/definition."""
    return ". ".join(x for x in (props.get("name"), ", ".join(props.get("synonyms", [])),
                                 props.get("description")) if x)


def build_graph(embedder: Embedder, exclude: frozenset[str] = frozenset(), knowledge: Knowledge | None = None) -> Graph:
    """Registries + knowledge -> Graph with local uids. `exclude` drops metrics by uid (`metric:<id>`) together with
    their dimensions and the edges that pointed at them (used to test stale cleanup)."""
    k = knowledge or load_knowledge()
    ddl = load_ddl()
    g = Graph()
    _add_schema(g, ddl)
    _add_metrics(g, ddl, exclude)
    _add_knowledge(g, k, exclude)
    _add_roles(g)
    g.edges = [e for e in g.edges if e[0] not in exclude and e[2] not in exclude]
    _check(g)
    searchable = [n["props"] for n in g.nodes.values() if SEARCHABLE in n["labels"]]
    for props, vec in zip(searchable, embedder.embed_documents([search_text(p) for p in searchable]), strict=True):
        props["embedding"] = vec
    return g


def can_read(claims: dict, props: dict) -> bool:
    """The graph's any-of scope rule alone (the first clause of retrieval's gate), for tests and tools."""
    scopes = set(claims.get("scopes", ())) | set(PUBLIC)
    return any(s in scopes for s in props["allowed_scopes"])


PHYSICAL_KINDS = ("Table", "Column", "Field", "Endpoint")
HIDDEN_KINDS = ("Role",)


def _base_visible(claims: dict, node: dict) -> bool:
    labels = node["labels"]
    if any(k in labels for k in HIDDEN_KINDS) or not can_read(claims, node["props"]):
        return False
    metrics_only = bool(claims.get("metrics_only"))
    return not (metrics_only and (any(k in labels for k in PHYSICAL_KINDS) or node["props"].get("sensitive")))


def visible(g: Graph, claims: dict, uid: str) -> bool:
    """Python twin of retrieval.gate() over a built Graph (tests compare the two on the loaded graph): any-of scope,
    Roles never, metrics-only callers see no physical schema and no sensitive dimension, and a Question / Execution
    needs at least one USED object and every USED object visible."""
    node = g.nodes[uid]
    if not _base_visible(claims, node):
        return False
    if "Question" in node["labels"] or "Execution" in node["labels"]:
        execs = [uid] if "Execution" in node["labels"] else \
            [b for a, typ, b, _ in g.edges if a == uid and typ == "ANSWERED_BY"]
        used = [b for a, typ, b, _ in g.edges if a in execs and typ == "USED"]
        return bool(used) and all(_base_visible(claims, g.nodes[u]) for u in used)
    return True


__all__ = ["GRAPH_SCHEMA_VERSION", "Graph", "TableSpec", "build_graph", "can_read", "check_label", "check_rel_type", "load_ddl", "parse_ddl",
           "readers", "search_text", "visible"]
