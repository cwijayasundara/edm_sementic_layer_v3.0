"""Manual smoke tool for the source MCP servers.

  uv run python -m prism.mcp.cli list cashrecon --as head_data
  uv run python -m prism.mcp.cli call cashrecon run_metric --as cash_ops_emea \
      --args '{"metric_id": "open_breaks", "dimensions": ["region"]}'

Demo-only: it mints a short-lived token for a demo persona from the local JWT secret and sends it to the server.
It never prints the token or the secret, and it only talks to loopback URLs so the token cannot leave the machine.
"""
import argparse
import asyncio
import json
import sys
from urllib.parse import urlsplit

from prism.config import Settings
from prism.mcp.client import mcp_client
from prism.mcp.servers import MCP_PORTS, SOURCES
from prism.security.personas import PERSONAS, claims_for
from prism.security.tokens import mint

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def _is_loopback_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS
                and parts.port is not None and parts.username is None and parts.password is None)
    except ValueError:
        return False


async def _run(args: argparse.Namespace, url: str) -> int:
    settings = Settings()
    token = mint(claims_for(args.persona), f"{args.source}-mcp", settings.jwt_secret.get_secret_value(), ttl_s=300)
    try:
        async with mcp_client(url, token, timeout_s=10) as client:
            if args.command == "list":
                tools = (await client.list_tools()).tools
                print(json.dumps([{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                                  for t in tools], indent=2))
                return 0
            result = await client.call_tool(args.tool, json.loads(args.args))
    except BaseException as exc:  # the SDK raises nested ExceptionGroups for connection/auth failures
        if isinstance(exc, KeyboardInterrupt):
            raise
        print(f"could not reach {url} or the call failed at transport level: {type(exc).__name__}", file=sys.stderr)
        return 2
    if result.is_error:
        print(result.content[0].text if result.content else "tool error", file=sys.stderr)
        return 1
    print(json.dumps(result.structured_content, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prism.mcp.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "call"):
        p = sub.add_parser(name)
        p.add_argument("source")
        if name == "call":
            p.add_argument("tool")
            p.add_argument("--args", default="{}")
        p.add_argument("--as", dest="persona", default="head_data", choices=sorted(PERSONAS))
        p.add_argument("--url", default=None,
                       help="override the server URL; loopback only (default: http://127.0.0.1:<port>/mcp)")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    if args.source not in SOURCES:
        print(f"unknown source {args.source!r}; valid: {list(SOURCES)}", file=sys.stderr)
        return 2
    if args.command == "call":
        try:
            parsed = json.loads(args.args)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            print("--args must be a JSON object", file=sys.stderr)
            return 2
    url = args.url or f"http://127.0.0.1:{MCP_PORTS[args.source]}/mcp"
    if not _is_loopback_url(url):
        print("--url must be http://127.0.0.1:<port>/... or http://localhost:<port>/... "
              "(demo tokens are never sent to other hosts)", file=sys.stderr)
        return 2
    return asyncio.run(_run(args, url))


if __name__ == "__main__":
    sys.exit(main())
