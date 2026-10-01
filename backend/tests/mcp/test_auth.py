import pytest

from prism.config import Settings
from prism.mcp.auth import PrismTokenVerifier, current_claims
from prism.security.personas import claims_for
from prism.security.tokens import mint

SECRET = Settings().jwt_secret.get_secret_value()


async def test_valid_token_yields_claims():
    v = PrismTokenVerifier("cashrecon-mcp", SECRET)
    tok = mint(claims_for("cash_ops_emea"), "cashrecon-mcp", SECRET)
    at = await v.verify_token(tok)
    assert at is not None and at.claims["sub"] == "cash_ops_emea" and at.claims["rows"]["region"] == ["EMEA"]
    assert at.scopes == []  # our 'scopes' claim are dataset entitlements, not OAuth scopes


@pytest.mark.parametrize("case", ["wrong_aud", "expired", "wrong_secret", "garbage"])
async def test_bad_tokens_return_none(case):
    v = PrismTokenVerifier("cashrecon-mcp", SECRET)
    tok = {
        "wrong_aud": mint(claims_for("steward"), "assetrecon-mcp", SECRET),
        "expired": mint(claims_for("steward"), "cashrecon-mcp", SECRET, ttl_s=-5),
        "wrong_secret": mint(claims_for("steward"), "cashrecon-mcp", "z" * 48),
        "garbage": "not.a.jwt",
    }[case]
    assert await v.verify_token(tok) is None


def test_current_claims_outside_a_request_raises():
    with pytest.raises(PermissionError):
        current_claims()
