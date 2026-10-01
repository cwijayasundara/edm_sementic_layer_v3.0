"""Small shared helpers for the source MCP servers."""
import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from prism.security.tokens import mint

if TYPE_CHECKING:  # results.py is imported by every MCP module; keep it free of the config import at runtime
    from prism.config import Settings

_FORWARDED = ("sub", "roles", "scopes", "rows", "metrics_only")


class UserFacingError(Exception):
    """A failure whose message is safe to show to the caller (surfaces as a tool error)."""


class SourceError(UserFacingError):
    """A source-level failure (entitlement, upstream refusal) with a caller-safe message."""


# Transient refusals. The gateway retries a tool error only when its whole message is exactly one of these, so
# never put caller-supplied text in them.
TOO_MANY_CONCURRENT = "too many concurrent requests for this principal"
SOURCE_BUSY = "the source is busy; retry shortly"
SOURCE_BUSY_MESSAGES = frozenset({TOO_MANY_CONCURRENT, SOURCE_BUSY})


def jsonable(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, Decimal):
        return jsonable(float(value))
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def has_dataset(claims: dict, source: str) -> bool:
    scopes = set(claims.get("scopes", ()))
    return source in scopes or any(s.startswith(f"{source}.") for s in scopes)


SENSITIVE_NOTE = "A metric's sensitive_dimensions can be neither grouped by nor filtered on by this principal."
METRICS_ONLY_NOTE = "Use run_metric for governed measures; free-form query is not available to this principal."


def describe_note(claims: dict, query_hint: str) -> str:
    """The describe() note; `query` is only advertised to principals that may use it (fail closed)."""
    if claims.get("metrics_only", True):
        return METRICS_ONLY_NOTE
    return f"Use run_metric for governed measures; query accepts {query_hint}."


def describe_notes(claims: dict, query_hint: str) -> list[str]:
    """describe() notes: metrics-only principals also learn that sensitive dimensions are closed to them."""
    notes = [describe_note(claims, query_hint)]
    if claims.get("metrics_only", True):
        notes.append(SENSITIVE_NOTE)
    return notes


def sensitive_dimension_error(claims: dict, requested: list[str], sensitive: list[str],
                              filters: Iterable[str] = ()) -> SourceError | None:
    """Metrics-only principals (fail closed: a missing claim counts as metrics-only) may neither group by nor
    filter on a dimension the metric marks sensitive (a filter on one operator id is as revealing as a group)."""
    if not claims.get("metrics_only", True):
        return None
    if blocked := [d for d in requested if d in sensitive]:
        return SourceError(f"dimension(s) {blocked} are not available to metrics-only principals")
    if blocked := [f for f in filters if f in sensitive]:
        return SourceError(f"filter(s) {blocked} are not available to metrics-only principals")
    return None


def forward_claims(claims: dict) -> dict:
    """The only claims that may be re-minted for a downstream API (never the raw incoming token)."""
    return {k: claims[k] for k in _FORWARDED if k in claims}


def mint_source_token(claims: dict, source: str, settings: "Settings", ttl_s: int = 60) -> str:
    """A short-lived token for the `<source>-mcp` server, carrying only the forwarded claims."""
    return mint(forward_claims(claims), f"{source}-mcp", settings.jwt_secret.get_secret_value(), ttl_s=ttl_s)
