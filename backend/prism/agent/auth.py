"""Verify the caller's persona JWT before the agent does anything; the raw token is kept only to forward it to the
gateway and never appears in a repr."""
import hashlib
import json
import time
from dataclasses import dataclass, field

from prism.config import Settings
from prism.gateway.audit import valid_caller_id
from prism.gateway.server import GATEWAY_AUDIENCE, MAX_TOKEN_LIFETIME_S
from prism.security.tokens import TokenError, verify


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class UserContext:
    sub: str
    roles: tuple[str, ...]
    metrics_only: bool
    token: str = field(repr=False)
    scope_digest: str = ""   # what the gateway enforces besides roles (scopes, row grants, metrics_only); cache keys use it


def verify_user(token: str, settings: Settings, now: float | None = None) -> UserContext:
    try:
        claims = verify(token, GATEWAY_AUDIENCE, settings.jwt_secret.get_secret_value())
    except TokenError:
        raise AuthError("invalid token") from None
    exp = claims.get("exp")
    if (not valid_caller_id(claims.get("sub")) or claims.get("aud") != GATEWAY_AUDIENCE
            or not isinstance(exp, int) or isinstance(exp, bool)
            or exp > (time.time() if now is None else now) + MAX_TOKEN_LIFETIME_S):
        raise AuthError("invalid token")
    roles = claims.get("roles")
    enforced = {k: claims.get(k) for k in ("scopes", "rows", "metrics_only")}
    digest = hashlib.sha256(json.dumps(enforced, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return UserContext(sub=claims["sub"], roles=tuple(roles) if isinstance(roles, list) else (),
                       metrics_only=claims.get("metrics_only") is not False, token=token,
                       scope_digest=digest[:16])
