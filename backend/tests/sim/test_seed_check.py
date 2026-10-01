"""--check must distinguish "not seeded" (1) from "cannot tell" (2): only the former may trigger a re-seed."""
import psycopg
import pytest

from prism.config import Settings
from prism.sim import cli
from prism.sim.seed import is_seeded


def test_is_seeded_raises_when_server_unreachable():
    with pytest.raises(psycopg.OperationalError):
        is_seeded(Settings(pg_host="127.0.0.1", pg_port=1))


def test_check_exits_2_on_unreachable_server(monkeypatch, capsys):
    monkeypatch.setenv("PRISM_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("PRISM_PG_PORT", "1")
    assert cli.main(["--check"]) == 2
    assert "failed" in capsys.readouterr().err


def test_check_exits_2_on_invalid_settings(monkeypatch, capsys):
    monkeypatch.setenv("PRISM_PG_PORT", "not-a-port")
    assert cli.main(["--check"]) == 2
    assert capsys.readouterr().err


def test_check_exit_codes_follow_is_seeded(monkeypatch):
    monkeypatch.setattr(cli, "is_seeded", lambda settings: True)
    assert cli.main(["--check"]) == 0
    monkeypatch.setattr(cli, "is_seeded", lambda settings: False)
    assert cli.main(["--check"]) == 1
