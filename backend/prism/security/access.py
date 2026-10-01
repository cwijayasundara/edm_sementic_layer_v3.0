"""Python mirror of prism_sec.can() for early, explicit 403s (Postgres RLS remains the enforcement point)."""


def can(claims: dict, db: str, table: str) -> bool:
    scopes = set(claims.get("scopes", ()))
    return db in scopes or f"{db}.{table}" in scopes
