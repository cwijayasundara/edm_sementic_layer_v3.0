# Plan 2 spike sources (proven prototypes)

Working prototypes produced and tested before writing Plan 2. They are **reference input** for the plan's tasks, not part of the product:

| Dir | What it proves | Result |
|---|---|---|
| `mcp/` | MCP (`mcp` 2.2.x, `MCPServer`) over streamable HTTP with audience-bound HS256 bearer auth via the SDK's `token_verifier`; claims visible in tool handlers and isolated between concurrent clients; client helper (`httpx2`) | 40 tests pass |
| `sqlguard/` | AST-based read-only SQL guard (`sqlglot` 30.x), 119 attack strings rejected, 37 analytic queries accepted and executed | 204 tests pass |
| `metrics/` | Governed-metric YAML + compiler binding all user values as parameters, run as `bi_reader` through the signed-context RLS path | 38 tests pass |

Plan 2 tasks say exactly which spike file to copy and what to change. Versions used: mcp 2.2.0, httpx2 2.13.1, starlette 1.7.0, uvicorn 0.54.0, sqlglot 30.20.0, psycopg 3.3.6, pydantic 2.13.5, pyyaml 6.0.3, pyjwt 2.15.1, Python 3.13.
