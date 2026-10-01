"""`python -m prism.gateway.cli list|call <tool> --as PERSONA --args JSON`: loopback only, never prints a token."""
import asyncio
import json
import subprocess
import sys

import pytest

from prism.config import Settings
from prism.gateway import cli
from prism.gateway.server import create_app
from tests.gateway.test_server import make_gateway, serving


def run(*args):
    return subprocess.run([sys.executable, "-m", "prism.gateway.cli", *args], capture_output=True, text=True,
                          timeout=60)


def test_usage_errors_exit_2():
    assert run().returncode == 2
    r = run("call", "no_such_tool")
    assert r.returncode == 2 and "unknown tool" in r.stderr


@pytest.mark.parametrize("argv", [["--help"], ["list", "--help"], ["call", "--help"]])
def test_help_exits_0(argv, capsys):
    assert cli.main(argv) == 0
    assert "usage" in capsys.readouterr().out


def test_unreachable_gateway_exits_2_with_a_clear_message():
    r = run("call", "search_context", "--args", '{"question": "q"}', "--url", "http://127.0.0.1:9/mcp")
    assert r.returncode == 2 and "could not reach" in r.stderr


def test_args_must_be_a_json_object():
    r = run("call", "search_context", "--args", "[1]")
    assert r.returncode == 2 and "JSON object" in r.stderr


@pytest.mark.parametrize("url", [
    "http://example.com:8200/mcp",
    "https://127.0.0.1:8200/mcp",
    "http://127.0.0.1/mcp",
    "http://127.0.0.1:8200@evil.example/mcp",
    "http://127.0.0.1.evil.example:8200/mcp",
])
def test_url_override_only_accepts_loopback_http_with_port(url):
    r = run("list", "--url", url)
    assert r.returncode == 2 and "--url must be" in r.stderr and r.stdout == ""


def test_unknown_persona_exits_2():
    assert run("list", "--as", "nobody").returncode == 2


async def test_list_and_call_against_a_running_gateway_print_no_token(fake_catalog, capsys, monkeypatch):
    settings = Settings()
    minted = []
    real_mint = cli.mint

    def spy(*a, **kw):
        minted.append(real_mint(*a, **kw))
        return minted[-1]

    monkeypatch.setattr(cli, "mint", spy)
    _, app = create_app(settings, gateway=make_gateway(settings, fake_catalog))
    async with serving(app) as base:
        url = f"{base}/mcp"
        rc_list = await asyncio.to_thread(cli.main, ["list", "--url", url])
        listed = capsys.readouterr()
        rc_call = await asyncio.to_thread(cli.main, ["call", "run_metric", "--as", "cash_ops_emea", "--url", url,
                                                     "--args", '{"metric_id": "open_breaks"}'])
        called = capsys.readouterr()
        rc_err = await asyncio.to_thread(cli.main, ["call", "run_metric", "--as", "cash_ops_emea", "--url", url,
                                                    "--args", '{"metric_id": "price_conflicts"}'])
        refused = capsys.readouterr()
    assert rc_list == 0 and len(json.loads(listed.out)) == 6
    assert rc_call == 0 and json.loads(called.out)["handle"].startswith("r_")
    assert rc_err == 1 and "not_permitted" in refused.err
    assert minted
    everything = "".join(x.out + x.err for x in (listed, called, refused))
    for tok in minted:
        assert tok not in everything
        assert all(part not in everything for part in tok.split("."))
