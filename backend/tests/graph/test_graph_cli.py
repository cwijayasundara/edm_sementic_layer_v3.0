"""`python -m prism.graph.cli check|ping`: what scripts/start_backend.sh relies on to decide whether to (re)load the graph.

check: 0 = the namespace holds a current graph (the gateway catalog loads), 1 = empty or written by an older loader
(load it), 2 = could not tell (Neo4j unreachable): only 1 may trigger a load.
"""
import pytest

from prism.graph.cli import main
from prism.graph.schema import GRAPH_SCHEMA_VERSION
from tests.graph_ns import TEST_GRAPH_NS


@pytest.mark.neo4j
def test_check_reports_a_current_graph(context_graph, capsys):
    assert main(["check", "--ns", TEST_GRAPH_NS]) == 0
    assert "current" in capsys.readouterr().out


@pytest.mark.neo4j
def test_check_reports_an_empty_namespace(neo4j_driver, scratch_ns, capsys):
    assert main(["check", "--ns", scratch_ns]) == 1
    assert "empty" in capsys.readouterr().out


@pytest.mark.neo4j
def test_check_reports_a_graph_from_an_older_loader(neo4j_driver, scratch_ns, capsys):
    neo4j_driver.execute_query(
        "CREATE (:Ctx:Metric {uid: $uid, ns: $ns, id: 'old_metric', schema_version: $v, loaded_version: 1})",
        uid=f"{scratch_ns}:metric:old_metric", ns=scratch_ns, v=GRAPH_SCHEMA_VERSION - 1)
    assert main(["check", "--ns", scratch_ns]) == 1
    assert "older" in capsys.readouterr().out


@pytest.mark.neo4j
def test_ping_answers_when_neo4j_is_up(neo4j_driver):
    assert main(["ping"]) == 0


@pytest.mark.parametrize("command", ["check", "ping"])
def test_unreachable_neo4j_is_undetermined_not_empty(monkeypatch, capsys, command):
    monkeypatch.setenv("PRISM_NEO4J_URI", "bolt://127.0.0.1:17999")
    assert main([command]) == 2
    assert "Neo4j not reachable" in capsys.readouterr().err


class _Driver:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on

    def verify_connectivity(self):
        if self.fail_on == "connect":
            raise OSError("unexpected socket failure password=hunter2")

    def execute_query(self, *a, **kw):
        if self.fail_on == "query":
            raise RuntimeError("driver bug password=hunter2")

    def close(self):
        pass


@pytest.mark.parametrize("command,fail_on", [("check", "catalog"), ("check", "connect"), ("ping", "query"),
                                             ("check", "driver")])
def test_any_unexpected_error_is_undetermined_never_empty(monkeypatch, capsys, command, fail_on):
    """1 would make the start script load the graph; an unexpected failure must be 2 (stop), never a traceback's 1."""
    from prism.graph import cli

    def driver(*a, **kw):
        if fail_on == "driver":
            raise ValueError("bad uri bolt://neo4j:hunter2@host")
        return _Driver(fail_on)

    def catalog(*a, **kw):
        raise KeyError("loaded_version")

    monkeypatch.setattr(cli.GraphDatabase, "driver", driver)
    monkeypatch.setattr(cli, "load_catalog", catalog)
    assert main([command]) == 2
    err = capsys.readouterr().err
    assert "could not" in err and "hunter2" not in err


def test_unreachable_message_strips_uri_credentials(monkeypatch, capsys):
    from neo4j.exceptions import ServiceUnavailable

    from prism.graph import cli

    class Down(_Driver):
        def verify_connectivity(self):
            raise ServiceUnavailable("down")

    monkeypatch.setenv("PRISM_NEO4J_URI", "bolt://neo4j:hunter2@127.0.0.1:17999")
    monkeypatch.setattr(cli.GraphDatabase, "driver", lambda *a, **kw: Down())
    assert main(["ping"]) == 2
    err = capsys.readouterr().err
    assert "bolt://127.0.0.1:17999" in err and "hunter2" not in err


# ------------------------------------------------------------------------------------------- distill
@pytest.mark.db
@pytest.mark.neo4j
def test_distill_reports_counts_as_json(monkeypatch, capsys, neo4j_driver, graph_embedder, scratch_ns):
    import json

    import psycopg

    from prism.config import APP_DB, Settings
    from prism.db.app_migrate import migrate_app
    from prism.graph.loader import load

    settings = Settings(db_prefix="testhistcli_")
    migrate_app(settings)
    with psycopg.connect(settings.dsn(APP_DB, admin=True)) as conn:
        conn.execute("TRUNCATE app.query_log")
        for sub in ("someone", "someone-else"):    # history_min_callers (default 2) distinct callers
            conn.execute("INSERT INTO app.query_log (sub, question_hash, question, plan, metric_ids, verified, "
                         "status) VALUES (%s, %s, 'How are the open breaks spread over the regions?', "
                         "'{\"metric_ids\":[\"open_breaks\"],\"dimensions\":[\"region\"]}', %s, true, 'ok')",
                         (sub, "0" * 64, ["open_breaks"]))
    load(neo4j_driver, graph_embedder, scratch_ns)
    monkeypatch.setenv("PRISM_DB_PREFIX", "testhistcli_")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    assert main(["distill", "--ns", scratch_ns]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["questions"], out["executions"], out["rows"], out["skipped"]) == (1, 1, 2, {})


@pytest.mark.neo4j
def test_distill_refuses_an_empty_namespace(monkeypatch, capsys, scratch_ns):
    from prism.graph import cli

    monkeypatch.setattr(cli, "_open_app_db", lambda settings: _NoPg())
    monkeypatch.setattr(cli, "_embedder", lambda settings: None)
    assert main(["distill", "--ns", scratch_ns]) == 2
    err = capsys.readouterr().err
    assert "make graph" in err


class _NoPg:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        raise AssertionError("the app database must not be read before the graph is known to be loaded")


def test_distill_unreachable_app_db_is_a_message_not_a_traceback(monkeypatch, capsys):
    import psycopg

    from prism.graph import cli

    def refuse(*a, **kw):
        raise psycopg.OperationalError("connection failed: password=hunter2")

    monkeypatch.setattr(cli, "_check_graph_for_history", lambda driver, ns: None)
    monkeypatch.setattr(cli.GraphDatabase, "driver", lambda *a, **kw: _Driver())
    monkeypatch.setattr(cli.psycopg, "connect", refuse)
    assert main(["distill"]) == 2
    err = capsys.readouterr().err
    assert "app database not reachable" in err and "hunter2" not in err
