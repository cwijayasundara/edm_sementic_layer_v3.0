"""Short-lived, audience-bound HS256 JWTs (the MVP stand-in for OIDC + token exchange)."""
import time

import jwt


class TokenError(Exception):
    pass


def mint(claims: dict, audience: str, secret: str, ttl_s: int = 300, now: int | None = None) -> str:
    issued = int(time.time() if now is None else now)
    return jwt.encode({**claims, "aud": audience, "iat": issued, "exp": issued + ttl_s}, secret, algorithm="HS256")


def verify(token: str, audience: str, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"], audience=audience,
                          options={"require": ["exp", "aud", "sub"]})
    except jwt.PyJWTError as exc:
        raise TokenError(f"invalid token: {exc}") from exc
