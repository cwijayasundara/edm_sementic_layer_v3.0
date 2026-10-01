"""Context-graph operations: `python -m prism.graph.cli load|counts|check|ping|distill [--ns NS]`.

load    create the schema, build the graph from the registries + knowledge YAML and load it (idempotent)
counts  per-label and per-relationship counts of a namespace
check   exit 0 = the namespace holds a current graph (the gateway catalog loads from it), 1 = empty or written by an
        older loader (GRAPH_SCHEMA_VERSION): load it, 2 = could not tell (Neo4j unreachable, a refused configuration
        or ANY unexpected error: never a traceback's exit 1). Read-only.
ping    exit 0 when Neo4j answers `RETURN 1`, 2 otherwise (the start script polls it)
distill rebuild the query-history layer from app.query_log (verified, metric-backed rows; prism.graph.history) as the
        app role, read-only on Postgres. Run it after every `load` (a load supersedes history nodes). Exit 0 with a
        JSON report, 2 when Neo4j / the app database / the model is unavailable or the namespace holds no graph.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

import psycopg
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable

from prism.config import APP_DB, ConfigError, load_settings, redact_uri
from prism.graph.catalog import CatalogError, load_catalog
from prism.graph.embedder import Embedder, EmbedderError
from prism.graph.history import DistillError, distill
from prism.graph.loader import counts, load
from prism.graph.retrieval import GraphError, GraphUnavailable
from prism.graph.schema import GRAPH_SCHEMA_VERSION


def _check(driver, ns: str) -> int:
    try:
        catalog = load_catalog(driver, ns, timeout_s=30)
    except CatalogError as exc:
        print(f"stale: {exc}")
        return 1
    if not catalog.metrics:
        print(f"empty: no metrics in namespace {ns!r}")
        return 1
    print(f"current: {len(catalog.metrics)} metrics, loaded_version {catalog.version}, "
          f"schema {GRAPH_SCHEMA_VERSION}")
    return 0


def _check_graph_for_history(driver, ns: str) -> None:
    """Refuse (DistillError) before anything else is opened when the namespace holds no current graph."""
    if not load_catalog(driver, ns, timeout_s=30).metrics:
        raise DistillError(f"graph namespace {ns!r} has no metrics: load the context graph first (make graph)")


def _embedder(settings) -> Embedder:
    return Embedder(settings.embed_model, cache_dir=settings.embed_cache_dir).load()


def _open_app_db(settings):
    """The app role (SELECT on app.query_log); the distiller reads in a READ ONLY transaction."""
    return psycopg.connect(settings.app_dsn(APP_DB, connect_timeout=3))


def _distill(driver, settings, ns: str) -> dict:
    _check_graph_for_history(driver, ns)
    embedder = _embedder(settings)
    try:
        conn = _open_app_db(settings)
    except psycopg.Error as exc:
        raise _AppDbUnavailable(type(exc).__name__) from None
    with conn:
        return asdict(distill(driver, embedder, conn, ns, min_callers=settings.history_min_callers))


class _AppDbUnavailable(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"prism graph cli: {exc}", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="python -m prism.graph.cli", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["load", "counts", "check", "ping", "distill"])
    parser.add_argument("--ns", default=settings.graph_ns, help=f"graph namespace (default {settings.graph_ns})")
    args = parser.parse_args(argv)
    try:
        return _run(args, settings)
    except Exception as exc:  # noqa: BLE001 - anything unexpected is "could not tell" (2), never "empty" (1)
        # type only: driver messages can quote the URI or credentials
        print(f"could not {args.command} the context graph: unexpected {type(exc).__name__}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace, settings) -> int:
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth(),
                                  connection_timeout=settings.neo4j_timeout_s, notifications_min_severity="OFF")
    try:
        driver.verify_connectivity()
        if args.command == "ping":
            driver.execute_query("RETURN 1")
            return 0
        if args.command == "check":
            return _check(driver, args.ns)
        if args.command == "distill":
            out = _distill(driver, settings, args.ns)
        elif args.command == "load":
            embedder = Embedder(settings.embed_model, cache_dir=settings.embed_cache_dir).load()
            out = asdict(load(driver, embedder, args.ns))
        else:
            out = counts(driver, args.ns)
    except (ServiceUnavailable, AuthError, GraphUnavailable) as exc:
        print(f"Neo4j not reachable at {redact_uri(settings.neo4j_uri)}: run `make db` ({type(exc).__name__})",
              file=sys.stderr)
        return 2
    except GraphError as exc:
        print(f"could not read the context graph: {exc}", file=sys.stderr)
        return 2
    except EmbedderError as exc:
        print(f"embedding model unavailable: {exc}", file=sys.stderr)
        return 2
    except (DistillError, CatalogError) as exc:
        print(f"could not distill the query history: {exc}", file=sys.stderr)
        return 2
    except _AppDbUnavailable as exc:
        # type only: psycopg messages can quote the connection string
        print(f"app database not reachable at {settings.pg_host}:{settings.pg_port}: run `make db` ({exc})",
              file=sys.stderr)
        return 2
    finally:
        driver.close()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
