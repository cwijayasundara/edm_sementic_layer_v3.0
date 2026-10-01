import pytest

from prism.security.personas import claims_for
from prism.security.tokens import mint


@pytest.fixture
def headers_for(seeded):
    def make(persona: str, audience: str, ttl_s: int = 300) -> dict:
        return {"Authorization": f"Bearer {mint(claims_for(persona), audience, seeded.jwt_secret.get_secret_value(), ttl_s=ttl_s)}"}
    return make
