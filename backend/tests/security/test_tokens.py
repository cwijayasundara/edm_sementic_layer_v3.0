import pytest

from prism.security.access import can
from prism.security.personas import PERSONAS, claims_for
from prism.security.tokens import TokenError, mint, verify

SECRET = "test-secret-0123456789abcdef0123456789abcdef"


def test_round_trip_and_audience_binding():
    token = mint(claims_for("steward"), "refmaster-api", SECRET)
    claims = verify(token, "refmaster-api", SECRET)
    assert claims["sub"] == "steward" and "refmaster" in claims["scopes"]
    with pytest.raises(TokenError):
        verify(token, "marketmaster-api", SECRET)


def test_expired_and_tampered_tokens_are_rejected():
    with pytest.raises(TokenError):
        verify(mint(claims_for("steward"), "refmaster-api", SECRET, ttl_s=-10), "refmaster-api", SECRET)
    with pytest.raises(TokenError):
        verify(mint(claims_for("steward"), "refmaster-api", SECRET), "refmaster-api", SECRET + "x")


def test_dataset_access_mirror():
    assert can(claims_for("invest_ops_growth"), "refmaster", "securities")
    assert not can(claims_for("invest_ops_growth"), "refmaster", "exceptions")
    assert can(claims_for("head_data"), "cashrecon", "breaks")
    assert not can(claims_for("steward"), "cashrecon", "breaks")


def test_personas_are_well_formed():
    assert set(PERSONAS) == {"steward", "cash_ops_emea", "invest_ops_growth", "bi_analyst", "head_data"}
    assert claims_for("bi_analyst")["metrics_only"] is True
    with pytest.raises(KeyError):
        claims_for("nobody")
