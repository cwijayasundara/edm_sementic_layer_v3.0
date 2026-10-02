"""The eval runner's view of the agent: one HTTP client, per-case tokens, SSE parsing and result paging.

Each case gets its own `eval-<12 hex>` sub (minted here with the real JWT secret, like the live tests), so the
audit rows of one case are exactly that case's calls. Tokens go only to loopback URLs."""
import asyncio
import json
import time
import uuid
from urllib.parse import urlparse

import httpx

from prism.config import Settings
from prism.evals.types import ChatResult, Table
from prism.gateway.server import GATEWAY_AUDIENCE
from prism.security.personas import claims_for
from prism.security.tokens import mint

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_TTL_S = 900
PAGE = 200


class AgentNotReady(Exception):
    """The agent refused the run as a whole (no model key, bad token, server error): abort, do not grade."""


def ensure_loopback(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or u.hostname not in LOOPBACK:
        raise ValueError(f"refusing a non-loopback URL ({u.scheme or '?'}://{u.hostname or '?'}): eval tokens are "
                         "minted with the real JWT secret")


def mint_case_token(settings: Settings, persona: str) -> tuple[str, str]:
    sub = f"eval-{uuid.uuid4().hex[:12]}"
    claims = {**claims_for(persona, ttl_s=TOKEN_TTL_S), "sub": sub}
    return sub, mint(claims, GATEWAY_AUDIENCE, settings.jwt_secret.get_secret_value(), ttl_s=TOKEN_TTL_S)


def parse_sse(text: str) -> list[dict]:
    events = []
    for frame in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        data = [line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:")]
        if not data:
            continue
        try:
            event = json.loads("\n".join(data))
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            events.append(event)
    return events


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class AgentClient:
    def __init__(self, base_url: str, *, timeout_s: float = 180.0, http: httpx.AsyncClient | None = None):
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=timeout_s))

    async def __aenter__(self) -> "AgentClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def healthy(self) -> bool:
        try:
            r = await self._http.get(f"{self._base}/healthz")
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    async def chat(self, token: str, question: str) -> ChatResult:
        started, buf, timed_out, dropped = time.monotonic(), [], False, False
        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._http.stream("POST", f"{self._base}/chat", json={"question": question},
                                             headers=_auth(token)) as r:
                    if r.status_code == 503:
                        raise AgentNotReady("the agent has no model access: set ANTHROPIC_API_KEY and restart it")
                    if r.status_code != 200:
                        raise AgentNotReady(f"the agent answered /chat with HTTP {r.status_code}")
                    async for chunk in r.aiter_text():
                        buf.append(chunk)
        except TimeoutError:
            timed_out = True
        except httpx.HTTPError:
            dropped = True
        result = ChatResult.from_events(parse_sse("".join(buf)), seconds=time.monotonic() - started,
                                        timed_out=timed_out)
        if dropped and result.error is None:
            result.error = {"type": "error", "code": "transport_error", "message": "the stream ended early"}
        return result

    async def table(self, token: str, handle: str, max_rows: int = 2000) -> Table | None:
        columns: list[str] = []
        rows: list[list] = []
        row_count = 0
        while len(rows) < max_rows:
            r = await self._http.get(f"{self._base}/results/{handle}", params={"offset": len(rows), "limit": PAGE},
                                     headers=_auth(token))
            if r.status_code == 404:
                return None
            r.raise_for_status()
            page = r.json()
            columns, row_count = page["columns"], page["row_count"]
            rows.extend(page["rows"])
            if not page["rows"] or len(rows) >= row_count:
                break
        return Table(columns, rows, truncated=len(rows) < row_count)
