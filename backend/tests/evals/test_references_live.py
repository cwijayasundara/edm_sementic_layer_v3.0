"""Live (no model): every golden reference replays with rows and its story holds; every red-team canary is readable
or hidden as the case claims. Needs the running stack and a seed that has the canaries (make reseed)."""
import pytest

from prism.config import Settings
from prism.evals.cases import load_golden, load_redteam
from prism.evals.runner import check_references
from tests.agent.live_support import require_live

pytestmark = pytest.mark.live


async def test_case_files_hold_on_the_live_seed():
    require_live()
    problems = await check_references(Settings(), load_golden(), load_redteam())
    assert problems == []
