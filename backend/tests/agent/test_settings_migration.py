import psycopg
import pytest

from prism.config import Settings
from prism.db.app_migrate import APP_TABLES, MIGRATIONS


def test_agent_settings_defaults():
    s = Settings()
    assert s.agent_port == 8000
    assert s.agent_supervisor_model == "claude-sonnet-5-5"
    assert s.agent_escalation_model == "claude-opus-5-5"
    assert s.agent_subagent_model == "claude-haiku-4-5-20251001"
    assert (s.agent_max_turns, s.agent_max_tool_calls) == (8, 24)
    assert s.agent_wall_clock_s == 90.0


def test_api_key_is_optional_and_secret(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    s = Settings()
    assert s.anthropic_api_key.get_secret_value() == "sk-test-123"
    assert "sk-test-123" not in repr(s)


def test_agent_runs_is_a_migration_and_a_granted_table():
    assert "agent_runs" in APP_TABLES
    assert max(v for v, _ in MIGRATIONS) >= 3
    ddl = " ".join(d for v, d in MIGRATIONS if v == 3)
    for col in ("run_id", "sub", "question_hash", "path", "models", "input_tokens", "output_tokens",
                "cache_read_input_tokens", "llm_turns", "tool_calls", "tool_latency_ms", "cost_usd", "status",
                "error_code"):
        assert col in ddl


@pytest.mark.db
def test_app_role_can_insert_and_select_agent_runs_but_not_update():
    from prism.db.app_migrate import migrate_app
    s = Settings()
    migrate_app(s)
    with psycopg.connect(s.app_dsn(), autocommit=True) as c:
        c.execute("INSERT INTO app.agent_runs (run_id, sub, question_hash, status) VALUES "
                  "('t-run-1', 'agent-test', %s, 'ok')", ("a" * 64,))
        assert c.execute("SELECT count(*) FROM app.agent_runs WHERE run_id='t-run-1'").fetchone()[0] >= 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE app.agent_runs SET status='x' WHERE run_id='t-run-1'")
