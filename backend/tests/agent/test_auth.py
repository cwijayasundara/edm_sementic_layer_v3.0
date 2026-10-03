import time

import pytest

from prism.agent.auth import AuthError, verify_user
from prism.config import Settings
from prism.gateway.server import GATEWAY_AUDIENCE
from prism.security.personas import claims_for
from prism.security.tokens import mint

S = Settings()
SECRET = S.jwt_secret.get_secret_value()


def tok(persona="head_data", aud=GATEWAY_AUDIENCE, ttl=600, **over):
    return mint({**claims_for(persona), **over}, aud, SECRET, ttl_s=ttl)


def test_valid_token_gives_a_user_context_that_hides_the_token():
    u = verify_user(tok("bi_analyst"), S)
    assert (u.sub, u.roles, u.metrics_only) == ("bi_analyst", ("bi_analyst",), True)
    assert "eyJ" not in repr(u) and u.token.startswith("eyJ")


@pytest.mark.parametrize("bad", [
    lambda: tok(aud="refmaster-api"),
    lambda: tok(ttl=7200),
    lambda: tok(sub="has space"),
    lambda: "garbage",
    lambda: mint(claims_for("head_data"), GATEWAY_AUDIENCE, "wrong-secret-wrong-secret-wrong-secret!", 600),
    lambda: tok(ttl=-10),
])
def test_bad_tokens_are_refused(bad):
    with pytest.raises(AuthError):
        verify_user(bad(), S)


@pytest.mark.parametrize("bad", [
    lambda: mint({**claims_for("head_data")}, [GATEWAY_AUDIENCE], SECRET, 600),
    lambda: _raw({**claims_for("head_data"), "aud": GATEWAY_AUDIENCE, "exp": True}),
    lambda: _raw({**claims_for("head_data"), "aud": GATEWAY_AUDIENCE, "exp": None}),
    lambda: _none_alg(),
    lambda: tok(sub=""),
    lambda: tok(sub="a" * 300),
])
def test_edge_case_tokens_are_refused(bad):
    with pytest.raises(AuthError):
        verify_user(bad(), S)


def _raw(claims):
    import jwt
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _none_alg():
    import jwt
    return jwt.encode({**claims_for("head_data"), "aud": GATEWAY_AUDIENCE, "exp": int(time.time()) + 600}, None,
                      algorithm="none")


def test_scope_digest_tracks_scopes_rows_and_metrics_only_and_never_the_token():
    base = verify_user(tok("head_data"), S)
    assert base.scope_digest == verify_user(tok("head_data"), S).scope_digest and len(base.scope_digest) == 16
    claims = claims_for("head_data")
    other_rows = verify_user(tok("head_data", rows={**claims["rows"], "region": ["EMEA"]}), S)
    other_scopes = verify_user(tok("head_data", scopes=["cashrecon"]), S)
    other_mode = verify_user(tok("head_data", metrics_only=not claims["metrics_only"]), S)
    assert len({base.scope_digest, other_rows.scope_digest, other_scopes.scope_digest, other_mode.scope_digest}) == 4
    assert base.token not in repr(base)


def test_verify_user_lists_readable_sources():
    # invest_ops_growth: assetrecon, feedhub, refmaster.securities, refmaster.legal_entities
    assert verify_user(tok("invest_ops_growth"), S).sources == ("refmaster", "assetrecon", "feedhub")
    assert verify_user(tok("steward"), S).sources == ("refmaster", "marketmaster")
    assert verify_user(tok("head_data"), S).sources == ("refmaster", "marketmaster", "cashrecon", "assetrecon",
                                                        "feedhub")                     # pii:read is not a source
