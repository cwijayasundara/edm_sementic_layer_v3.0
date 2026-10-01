import pytest

from prism.config import Settings
from prism.security.personas import claims_for
from prism.security.tokens import mint


@pytest.fixture
def mcp_token():
    def make(settings: Settings, persona: str, source: str, ttl_s: int = 300) -> str:
        return mint(claims_for(persona), f"{source}-mcp", settings.jwt_secret.get_secret_value(), ttl_s=ttl_s)

    return make
