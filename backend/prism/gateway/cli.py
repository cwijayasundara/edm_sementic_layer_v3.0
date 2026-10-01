"""Manual smoke tool for the Semantic Gateway (:8200).

  uv run python -m prism.gateway.cli list --as head_data
  uv run python -m prism.gateway.cli call search_context --as cash_ops_emea --args '{"question": "open breaks"}'
  uv run python -m prism.gateway.cli call run_metric --as cash_ops_emea \
      --args '{"metric_id": "open_breaks", "dimensions": ["region"]}'

Demo-only: it mints a short-lived `gateway-mcp` token for a demo persona from the local JWT secret and sends it to the
gateway. It never prints the token or the secret, and it only talks to loopback URLs so the token cannot leave the
machine. Exit codes: 0 ok, 1 tool error (message on stderr), 2 usage or transport failure.
"""
import argparse
import asyncio
import json
import sys

from prism.config import ConfigError, Settings, load_settings
from prism.gateway.server import GATEWAY_AUDIENCE
from prism.gateway.service import TOOLS
from prism.mcp.cli import _is_loopback_url
from prism.mcp.client import mcp_client
from prism.security.personas import PERSONAS, claims_for
from prism.security.tokens import mint


async def _run(args: argparse.Namespace, url: str, settings: Settings) -> int:
    token = mint(claims_for(args.persona), GATEWAY_AUDIENCE, settings.jwt_secret.get_secret_value(), ttl_s=300)
    try:
        async with mcp_client(url, token, timeout_s=10, read_timeout_s=120) as client:
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
    parser = argparse.ArgumentParser(prog="prism.gateway.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "call"):
        p = sub.add_parser(name)
        if name == "call":
            p.add_argument("tool")
            p.add_argument("--args", default="{}")
        p.add_argument("--as", dest="persona", default="head_data", choices=sorted(PERSONAS))
        p.add_argument("--url", default=None,
                       help="override the gateway URL; loopback only (default: <gateway_url>/mcp from settings)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help exits 0; a usage error exits 2
        return 0 if exc.code in (0, None) else 2
    if args.command == "call":
        if args.tool not in TOOLS:
            print(f"unknown tool {args.tool!r}; valid: {list(TOOLS)}", file=sys.stderr)
            return 2
        try:
            parsed = json.loads(args.args)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            print("--args must be a JSON object", file=sys.stderr)
            return 2
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"prism gateway cli: {exc}", file=sys.stderr)
        return 2
    url = args.url or f"{settings.gateway_url.rstrip('/')}/mcp"
    if not _is_loopback_url(url):
        print("--url must be http://127.0.0.1:<port>/... or http://localhost:<port>/... "
              "(demo tokens are never sent to other hosts)", file=sys.stderr)
        return 2
    return asyncio.run(_run(args, url, settings))


if __name__ == "__main__":
    sys.exit(main())
