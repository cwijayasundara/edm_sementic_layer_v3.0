"""No drift (M9 spec §5.1, part a): on the columns they had on `main`, the projectors' output is unchanged.
projection_digest.json was generated from `main` before any M9 change (`uv run python -m prism.sim.digest`).
Part b (the incident pass only appends or edits rows it owns) lives in test_incident.py."""
import json
from pathlib import Path

import pytest

from prism.sim.digest import table_digest
from prism.sim.seed import PROJECTORS
from prism.sim.universe import SimConfig, build_universe

BASELINE = json.loads(Path(__file__).with_name("projection_digest.json").read_text())


@pytest.mark.parametrize("profile", ["small", pytest.param("full", marks=pytest.mark.slow)])
def test_projectors_match_the_main_baseline(profile):
    cfg = SimConfig.small() if profile == "small" else SimConfig()
    u = build_universe(cfg)
    seen = set()
    for db, project in PROJECTORS.items():
        for name, t in project(u).items():
            key = f"{db}.{name}"
            want = BASELINE[profile][key]
            seen.add(key)
            assert len(t.rows) == want["rows"], key
            assert table_digest(t, want["columns"]) == want["sha256"], key
    assert seen == set(BASELINE[profile])
