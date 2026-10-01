"""Audience-bound HS256 bearer-JWT auth for an MCP server (mcp>=2.2,<3).

Recommended path: SDK built-in auth (`token_verifier=` + `AuthSettings`).
  - SDK's BearerAuthBackend -> AuthenticatedUser in scope["user"]
  - SDK's RequireAuthMiddleware -> 401 JSON + `WWW-Authenticate: Bearer ...` on /mcp only
  - SDK's AuthContextMiddleware -> ContextVar read via `get_access_token()` inside tools
  - Stateful session manager binds each Mcp-Session-Id to (client_id, iss, sub) of the creator.

Alternative (kept for comparison/tests): `JWTAuthMiddleware`, a pure-ASGI middleware.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JWTConfig:
    secret: str  # >= 32 bytes for HS256 (pyjwt warns on shorter keys)
    audience: str  # e.g. "mcp:postgres-sales"
    issuer: str | None = None  # optional `iss` check
    leeway_s: int = 0


def decode_jwt(token: str, cfg: JWTConfig) -> dict[str, Any]:
    """Raises jwt.PyJWTError on any failure (bad sig, wrong aud, expired, missing claims)."""
    options: dict[str, Any] = {"require": ["exp", "aud", "sub"]}
    return jwt.decode(
        token,
        cfg.secret,
        algorithms=["HS256"],  # pinned: never trust the header's alg
        audience=cfg.audience,
        issuer=cfg.issuer,
        leeway=cfg.leeway_s,
        options=options,
    )


def claims_to_access_token(token: str, claims: Mapping[str, Any]) -> AccessToken:
    scope_claim = claims.get("scope") or ""
    scopes = scope_claim.split() if isinstance(scope_claim, str) else list(scope_claim)
    return AccessToken(
        token=token,
        client_id=str(claims.get("azp") or claims["sub"]),
        subject=str(claims["sub"]),
        scopes=scopes,
        expires_at=int(claims["exp"]),
        claims=dict(claims),  # includes iss -> part of session-owner identity
    )


# ----------------------------------------------------------------------------- option (b): SDK built-in
class HS256TokenVerifier:
    """Implements mcp.server.auth.provider.TokenVerifier (Protocol)."""

    def __init__(self, cfg: JWTConfig) -> None:
        self.cfg = cfg

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = decode_jwt(token, self.cfg)
        except jwt.PyJWTError as exc:  # MUST catch: an escaping exception becomes a 500
            logger.info("rejected bearer token: %s", type(exc).__name__)
            return None
        return claims_to_access_token(token, claims)


# ----------------------------------------------------------------------------- option (a): pure ASGI
_claims_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("mcp_jwt_claims", default=None)


class JWTAuthMiddleware:
    """Pure-ASGI bearer-JWT gate. Wrap the whole Starlette app returned by streamable_http_app().

    - lifespan/websocket scopes pass through untouched (session_manager.run() lives in lifespan)
    - `public_paths` (e.g. /healthz) are not authenticated
    - also sets scope["user"] = AuthenticatedUser(...) so the stateful session manager binds
      sessions to the token principal (without it, requestor=None and session binding is OFF)
    """

    def __init__(self, app: ASGIApp, cfg: JWTConfig, public_paths: frozenset[str] = frozenset({"/healthz"})) -> None:
        self.app = app
        self.cfg = cfg
        self.public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.public_paths:
            await self.app(scope, receive, send)
            return
        auth = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            await _unauthorized(send, "missing bearer token")
            return
        try:
            claims = decode_jwt(token.strip(), self.cfg)
        except jwt.ExpiredSignatureError:
            await _unauthorized(send, "token expired")
            return
        except jwt.PyJWTError:
            await _unauthorized(send, "invalid token")
            return
        scope["user"] = AuthenticatedUser(claims_to_access_token(token, claims))
        reset = _claims_var.set(claims)
        try:
            await self.app(scope, receive, send)
        finally:
            _claims_var.reset(reset)


async def _unauthorized(send: Send, description: str) -> None:
    body = json.dumps({"error": "invalid_token", "error_description": description}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", f'Bearer error="invalid_token", error_description="{description}"'.encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ----------------------------------------------------------------------------- accessor used by tools
def current_claims() -> dict[str, Any]:
    """Verified JWT claims of the request currently being handled. Works with either option.

    Raises PermissionError if called outside an authenticated request (defence in depth).
    """
    tok = get_access_token()  # option (b)
    if tok is not None and tok.claims is not None:
        return tok.claims
    claims = _claims_var.get()  # option (a)
    if claims is not None:
        return claims
    raise PermissionError("no authenticated principal in context")


def mint_token(cfg: JWTConfig, sub: str, *, ttl_s: int = 300, aud: str | None = None, **extra: Any) -> str:
    """Test/gateway helper."""
    now = int(time.time())
    payload = {"sub": sub, "aud": aud or cfg.audience, "iat": now, "exp": now + ttl_s, **extra}
    if cfg.issuer:
        payload.setdefault("iss", cfg.issuer)
    return jwt.encode(payload, cfg.secret, algorithm="HS256")
