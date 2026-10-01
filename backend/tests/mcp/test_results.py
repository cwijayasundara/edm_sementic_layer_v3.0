import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import jwt

from prism.config import Settings
from prism.mcp.auth import PrismTokenVerifier
from prism.mcp.results import (
    METRICS_ONLY_NOTE,
    SourceError,
    UserFacingError,
    describe_note,
    forward_claims,
    has_dataset,
    jsonable,
    mint_source_token,
)
from prism.security.personas import claims_for


def test_jsonable_converts_recursively():
    out = jsonable({"a": Decimal("1.5"), "b": [date(2026, 9, 30), datetime(2026, 9, 30, 1, 2, tzinfo=timezone.utc)],
                    "c": (1, {2}), "d": uuid.UUID(int=1)})
    assert out == {"a": 1.5, "b": ["2026-09-30", "2026-09-30T01:02:00+00:00"], "c": [1, [2]],
                   "d": "00000000-0000-0000-0000-000000000001"}


def test_has_dataset_accepts_db_or_table_scopes():
    assert has_dataset({"scopes": ["cashrecon"]}, "cashrecon")
    assert has_dataset({"scopes": ["refmaster.securities"]}, "refmaster")
    assert not has_dataset({"scopes": ["refmaster.securities"]}, "cashrecon")
    assert not has_dataset({}, "cashrecon")
    assert not has_dataset({"scopes": ["cashrecon_x"]}, "cashrecon")


def test_forward_claims_is_a_strict_subset():
    src = {"sub": "u", "roles": ["r"], "scopes": ["s"], "rows": {"region": ["*"]}, "metrics_only": True,
           "aud": "x-mcp", "exp": 1, "iat": 0, "name": "N", "evil": 1}
    assert forward_claims(src) == {"sub": "u", "roles": ["r"], "scopes": ["s"], "rows": {"region": ["*"]},
                                   "metrics_only": True}
    assert forward_claims({"sub": "u"}) == {"sub": "u"}


def test_source_error_is_user_facing():
    assert issubclass(SourceError, UserFacingError)


def test_jsonable_passthrough_and_odd_types():
    import ipaddress
    from datetime import timedelta

    from psycopg.types.range import Range

    assert jsonable([True, None, 3, "s", 1.5]) == [True, None, 3, "s", 1.5]
    assert jsonable([float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity")]) == [
        "NaN", "Infinity", "-Infinity", "NaN", "Infinity"]
    assert jsonable(timedelta(days=1, hours=2)) == "1 day, 2:00:00"
    assert jsonable(ipaddress.ip_address("10.0.0.1")) == "10.0.0.1"
    assert isinstance(jsonable(Range(1, 5)), str)


async def test_mint_source_token_carries_only_the_forwarded_claims():
    settings = Settings()
    claims = {**claims_for("cash_ops_emea"), "name": "x", "email": "x@example.invalid", "aud": "other"}
    tok = mint_source_token(claims, "cashrecon", settings, ttl_s=45)
    decoded = jwt.decode(tok, settings.jwt_secret.get_secret_value(), algorithms=["HS256"], audience="cashrecon-mcp")
    assert decoded["aud"] == "cashrecon-mcp"
    assert set(decoded) == {"sub", "roles", "scopes", "rows", "metrics_only", "aud", "iat", "exp"}
    assert decoded["exp"] - decoded["iat"] == 45
    assert {k: decoded[k] for k in ("sub", "roles", "scopes", "rows", "metrics_only")} == forward_claims(claims)
    access = await PrismTokenVerifier("cashrecon-mcp", settings.jwt_secret.get_secret_value()).verify_token(tok)
    assert access is not None and access.subject == "cash_ops_emea"
    assert await PrismTokenVerifier("feedhub-mcp", settings.jwt_secret.get_secret_value()).verify_token(tok) is None
    default = jwt.decode(mint_source_token(claims, "feedhub", settings), settings.jwt_secret.get_secret_value(), algorithms=["HS256"],
                         audience="feedhub-mcp")
    assert default["exp"] - default["iat"] == 60


def test_describe_note_hides_query_from_metrics_only_principals():
    assert describe_note({"metrics_only": True}, "a single SELECT") == METRICS_ONLY_NOTE
    assert describe_note({}, "a single SELECT") == METRICS_ONLY_NOTE  # fail closed
    open_note = describe_note({"metrics_only": False}, "a single SELECT")
    assert open_note == "Use run_metric for governed measures; query accepts a single SELECT."
    assert METRICS_ONLY_NOTE == ("Use run_metric for governed measures; free-form query is not available to this "
                                 "principal.")
