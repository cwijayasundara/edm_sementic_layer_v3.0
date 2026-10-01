"""Shared helpers for the live agent tests (full stack up, scripted model, REAL gateway and source servers).

Every call goes through GatewayClient with a persona token minted for a unique `agent-live-<random>` sub; the live
runs write smoke rows into the real app database (app.audit_log / app.agent_runs, and app.query_log through the
agent's record_answer write-back). Questions carry a random suffix so no two runs ever look like the same question
asked by two callers (the history-distill threshold)."""
import contextlib
import re
import uuid

import pytest

from prism.agent.auth import UserContext, verify_user
from prism.agent.gateway_client import GatewayClient
from prism.agent.service import AgentService
from prism.config import Settings
from prism.gateway.server import GATEWAY_AUDIENCE
from prism.security.personas import claims_for
from prism.security.tokens import mint
from tests.gateway.test_e2e_live import SETTINGS, _why_not_live, gateway_target

__all__ = ["SETTINGS", "live_gateway_factory", "live_service", "unique_question", "user_for", "last_handle",
           "require_live"]
HANDLE_RE = re.compile(r"r_[0-9a-f]{12}")


def require_live() -> None:
    try:
        reason = _why_not_live()
    except ValueError as exc:   # a non-loopback gateway URL: refuse loudly, never send a token there
        pytest.fail(str(exc), pytrace=False)
    if reason:
        pytest.skip(reason)


def user_for(persona: str) -> UserContext:
    claims = {**claims_for(persona, ttl_s=600), "sub": f"agent-live-{uuid.uuid4().hex[:10]}"}
    token = mint(claims, GATEWAY_AUDIENCE, SETTINGS.jwt_secret.get_secret_value(), ttl_s=600)
    return verify_user(token, SETTINGS)


def gateway_base() -> str:
    """The gateway URL without /mcp (GatewayClient appends it)."""
    return gateway_target(SETTINGS.gateway_url)[0].removesuffix("/mcp")


@contextlib.asynccontextmanager
async def live_gateway_factory(user: UserContext):
    async with GatewayClient(gateway_base(), user.token) as gw:
        yield gw


def live_service(model_client, *, run_writer=None) -> AgentService:
    return AgentService(settings=Settings(), model_client=model_client, gateway_factory=live_gateway_factory,
                        run_writer=run_writer)


def unique_question(text: str) -> str:
    return f"{text} [{uuid.uuid4().hex[:8]}]"


def last_handle(req) -> str:
    """The newest result handle in the transcript (scripted items are callables over the model request)."""
    found = HANDLE_RE.findall(repr(req.messages))
    assert found, "the transcript should contain a result handle"
    return found[-1]
