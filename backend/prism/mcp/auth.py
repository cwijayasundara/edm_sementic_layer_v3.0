"""Bearer-token verification for source MCP servers: audience-bound HS256 JWTs from prism.security.tokens."""
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from prism.security.tokens import TokenError, verify


class PrismTokenVerifier:
    """Implements the SDK's TokenVerifier protocol. Must never raise: an escaping exception becomes a 500."""

    def __init__(self, audience: str, secret: str) -> None:
        self.audience = audience
        self.secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = verify(token, self.audience, self.secret)
            return AccessToken(
                token=token,
                client_id=str(claims["sub"]),
                subject=str(claims["sub"]),
                scopes=[],  # our `scopes` claim holds dataset entitlements; it is not an OAuth scope list
                expires_at=int(claims["exp"]),
                claims=claims,
            )
        except (TokenError, KeyError, TypeError, ValueError):
            return None


def current_claims() -> dict[str, Any]:
    """Verified claims of the request being handled (set by the SDK's auth middleware)."""
    token = get_access_token()
    if token is None or token.claims is None:
        raise PermissionError("no authenticated principal in context")
    return token.claims
