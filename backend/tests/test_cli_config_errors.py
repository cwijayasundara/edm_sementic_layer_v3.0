"""Every CLI that reads Settings reports a refused configuration by message only: pydantic's own text quotes the
input (for a model-level refusal, the whole environment, secrets included)."""
import pytest
from pydantic import SecretStr

from prism.config import Settings
from prism.gateway import cli as gateway_cli
from prism.graph import cli as graph_cli
from prism.sim import cli as sim_cli

FRAGMENT_KEY = "SHORTKEY-FRAG-7f3a"
FRAGMENT_ADMIN = "ADMINPW-FRAG-91c2"


def _plain(value):
    return value.get_secret_value() if isinstance(value, SecretStr) else value


@pytest.fixture(params=["short_audit_key", "production_dev_default"])
def bad_env(request, monkeypatch):
    if request.param == "short_audit_key":
        monkeypatch.setenv("PRISM_AUDIT_HMAC_KEY", FRAGMENT_KEY)   # under 32 characters: a field error
    else:
        monkeypatch.setenv("PRISM_ENV", "production")              # a model error: input_value is the whole env
        monkeypatch.setenv("PRISM_JWT_SECRET", _plain(Settings.model_fields["jwt_secret"].default))
    monkeypatch.setenv("PRISM_PG_ADMIN_PASSWORD", FRAGMENT_ADMIN)
    return request.param


CLIS = {
    "gateway list": lambda: gateway_cli.main(["list"]),
    "gateway call": lambda: gateway_cli.main(["call", "search_context", "--args", '{"question": "q"}']),
    "graph check": lambda: graph_cli.main(["check"]),
    "graph ping": lambda: graph_cli.main(["ping"]),
    "sim check": lambda: sim_cli.main(["--check"]),
    "sim seed": lambda: sim_cli.main([]),
}


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_cli_config_refusals_never_print_secret_values(cli, bad_env, capsys):
    rc = CLIS[cli]()
    out, err = capsys.readouterr()
    assert rc == 2
    both = out + err
    assert "invalid configuration" in both
    for fragment in (FRAGMENT_KEY, FRAGMENT_ADMIN, "SHORTKEY", "ADMINPW", "input_value", "Traceback"):
        assert fragment not in both
    if bad_env == "production_dev_default":
        assert "dev default" in both
    else:
        assert "audit_hmac_key" in both
