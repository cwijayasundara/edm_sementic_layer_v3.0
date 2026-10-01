"""Guards of the live end-to-end module (tests/gateway/test_e2e_live.py), checked without the stack: the probe and the
calls use ONE gateway URL, a non-loopback gateway URL is refused (live tokens are minted with the real JWT secret),
and the live tests are deselected from the default run."""
import tomllib
from pathlib import Path

import pytest

from tests.gateway.test_e2e_live import gateway_target

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
MAKEFILE = Path(__file__).resolve().parents[3] / "Makefile"


@pytest.mark.parametrize("url,target", [
    ("http://127.0.0.1:8200", ("http://127.0.0.1:8200/mcp", "127.0.0.1", 8200)),
    ("http://localhost:9000/", ("http://localhost:9000/mcp", "localhost", 9000)),
    ("http://[::1]:8200", ("http://[::1]:8200/mcp", "::1", 8200)),
    ("http://127.0.0.1", ("http://127.0.0.1/mcp", "127.0.0.1", 80)),
])
def test_gateway_target_is_one_loopback_url_for_probe_and_calls(url, target):
    assert gateway_target(url) == target


@pytest.mark.parametrize("url", ["http://10.0.0.5:8200", "https://gateway.example.com", "http://0.0.0.0:8200",
                                 "http://127.0.0.1.evil.example:8200", "ftp://127.0.0.1:8200", "127.0.0.1:8200"])
def test_a_non_loopback_gateway_url_is_refused(url):
    with pytest.raises(ValueError, match="loopback"):
        gateway_target(url)


def test_live_tests_are_deselected_by_default_and_make_test_live_selects_them():
    addopts = tomllib.loads(PYPROJECT.read_text())["tool"]["pytest"]["ini_options"]["addopts"]
    assert '-m "not live"' in addopts
    recipe = MAKEFILE.read_text().split("\ntest-live:", 1)[1].split("\n\n", 1)[0]
    assert "-m live" in recipe
