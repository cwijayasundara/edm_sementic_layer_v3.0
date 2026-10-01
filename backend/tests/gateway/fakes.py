"""Test doubles shared by the gateway tests (not collected: no test_ prefix)."""
import json
from types import MappingProxyType

from prism.graph.catalog import Catalog, CatalogMetric, CatalogRole, Grant
from prism.graph.knowledge import REST_SOURCES
from prism.graph.model import readers
from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics
from prism.mcp.rest_backend import load_rest_config
from prism.security.personas import PERSONAS


def _metric(mid, source, kind, endpoint, m, tables, required, fine) -> CatalogMetric:
    return CatalogMetric(id=mid, source=source, kind=kind, server=f"{source}-mcp", tool="run_metric",
                         endpoint=endpoint, unit=m.unit, dimensions=tuple(sorted(m.dimensions)),
                         required=tuple(sorted(required)), sensitive=tuple(sorted(m.sensitive_dimensions)),
                         tables=tuple(f"{source}.{t}" for t in tables),
                         allowed_scopes=tuple(readers(source, list(tables))), fine_grain=tuple(sorted(fine)),
                         filters=tuple(sorted(m.filters)))


def registry_catalog() -> Catalog:
    metrics = {}
    for m in load_metrics(DEFAULT_METRICS_DIR).values():
        metrics[m.id] = _metric(m.id, m.source, "sql", None, m, sorted(m.tables), m.required_dimensions,
                                m.fine_grain_dimensions)
    for s in REST_SOURCES:
        endpoints, rest = load_rest_config(s)
        for m in rest.values():
            metrics[m.id] = _metric(m.id, s, "rest", m.endpoint, m, [endpoints[m.endpoint].table], (),
                                    m.fine_grain_dimensions)
    roles = {}
    for pid, p in PERSONAS.items():
        rows = json.loads(json.dumps({k: list(v) for k, v in p.rows.items()}))
        roles[pid] = CatalogRole(persona_id=pid, metrics_only=p.metrics_only, scopes=p.scopes,
                                 grants=tuple(Grant(f"source:{s}", s) for s in p.scopes if s != "pii:read"),
                                 row_scope=MappingProxyType({k: tuple(v) for k, v in rows.items()}))
    return Catalog(version=1, metrics=MappingProxyType(metrics), roles=MappingProxyType(roles))
