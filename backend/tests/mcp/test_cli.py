import subprocess
import sys

import pytest


def run(*args):
    return subprocess.run([sys.executable, "-m", "prism.mcp.cli", *args], capture_output=True, text=True, timeout=60)


def test_usage_errors_exit_2():
    assert run().returncode == 2
    r = run("call", "nosuchsource", "describe")
    assert r.returncode == 2 and "unknown source" in r.stderr


def test_unreachable_server_exits_2_with_a_clear_message():
    r = run("call", "cashrecon", "describe", "--as", "head_data", "--url", "http://127.0.0.1:9/mcp")
    assert r.returncode == 2 and "could not reach" in r.stderr


def test_args_must_be_json_object():
    r = run("call", "cashrecon", "describe", "--args", "[1]")
    assert r.returncode == 2 and "JSON object" in r.stderr


@pytest.mark.parametrize("url", [
    "http://example.com:8203/mcp",
    "https://127.0.0.1:8203/mcp",
    "http://127.0.0.1/mcp",
    "http://127.0.0.1:8203@evil.example/mcp",
    "http://evil.example:80/@127.0.0.1:8203/mcp",
    "http://127.0.0.1.evil.example:8203/mcp",
    "file:///etc/passwd",
])
def test_url_override_only_accepts_loopback_http_with_port(url):
    r = run("list", "cashrecon", "--url", url)
    assert r.returncode == 2 and "--url must be" in r.stderr
    assert r.stdout == ""


def test_localhost_url_is_accepted():
    r = run("list", "cashrecon", "--url", "http://localhost:9/mcp")
    assert r.returncode == 2 and "could not reach" in r.stderr


def test_unknown_persona_exits_2():
    assert run("list", "cashrecon", "--as", "nobody").returncode == 2
