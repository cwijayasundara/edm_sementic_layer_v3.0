import pytest

from prism.sim.universe import SimConfig, build_universe


@pytest.fixture(scope="session")
def universe():
    return build_universe(SimConfig.small())
