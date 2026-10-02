import json

import pytest

from prism.config import Settings
from prism.evals.cli import run_evals
from prism.evals.client import AgentNotReady
from prism.evals.report import build_report, to_markdown, write_report


def results():
    return [
        {"suite": "golden", "id": "g_1", "persona": "head_data", "passed": True, "seconds": 2.0,
         "telemetry": {"cost_usd": 0.01, "input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 50},
         "checks": [], "summary_excerpt": "ok"},
        {"suite": "golden", "id": "g_2", "persona": "head_data", "passed": False, "seconds": 4.0,
         "telemetry": {"cost_usd": 0.02, "input_tokens": 200, "output_tokens": 20, "cache_read_input_tokens": 0},
         "checks": [{"name": "rows", "ok": False, "detail": "values differ", "required": True}],
         "summary_excerpt": ""},
        {"suite": "redteam", "id": "r_1", "persona": "cash_ops_emea", "leaked": True, "seconds": 3.0,
         "telemetry": None, "leaks": [{"detector": "obeyed", "detail": "x"}], "summary_excerpt": ""},
        {"suite": "golden", "id": "g_3", "persona": "steward", "skipped": True},
    ]


def test_build_report_totals():
    r = build_report(results(), started="2026-10-02T10:00:00Z", finished="2026-10-02T10:05:00Z", git_sha="abc1234")
    t = r["totals"]
    assert t["golden_cases"] == 2 and t["golden_passed"] == 1 and t["golden_pass_rate"] == 0.5
    assert t["redteam_cases"] == 1 and t["leaks"] == 1 and t["skipped"] == 1
    assert t["cost_usd"] == 0.03 and t["input_tokens"] == 300 and t["p50_s"] == 3.0 and t["p95_s"] == 4.0
    md = to_markdown(r)
    assert "| g_2 |" in md and "values differ" in md and "obeyed" in md and "50.0%" in md


def test_write_report_creates_a_stamped_folder(tmp_path):
    r = build_report(results(), started="2026-10-02T10:00:00Z", finished="2026-10-02T10:05:00Z", git_sha="abc")
    out = write_report(tmp_path, r)
    assert out.name == "20261002T100000Z"
    assert json.loads((out / "report.json").read_text())["totals"]["leaks"] == 1
    assert (out / "report.md").read_text().startswith("# Prism eval report")


class Args:
    suite, case, max_cost_usd, agent_url, check_references = "all", [], 5.0, "http://127.0.0.1:8000", False


class FakeClient:
    def __init__(self, healthy=True):
        self._healthy = healthy

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def healthy(self):
        return self._healthy


@pytest.mark.parametrize("healthy,error", [(False, None), (True, AgentNotReady("no key"))])
async def test_run_exits_2_when_agent_is_down_or_has_no_key(tmp_path, capsys, healthy, error):
    class Runner:
        def __init__(self, *a, **kw):
            pass

        async def run(self, golden, redteam):
            raise error
    code = await run_evals(Args(), Settings(), golden=[], redteam=[], agent_factory=lambda url: FakeClient(healthy),
                           runner_factory=Runner, reports_dir=tmp_path)
    assert code == 2 and list(tmp_path.iterdir()) == []
    assert "Traceback" not in capsys.readouterr().err


async def test_run_exit_code_follows_leaks(tmp_path):
    class Runner:
        def __init__(self, *a, **kw):
            pass

        async def run(self, golden, redteam):
            return results()
    code = await run_evals(Args(), Settings(), golden=[], redteam=[], agent_factory=lambda url: FakeClient(),
                           runner_factory=Runner, reports_dir=tmp_path)
    assert code == 1 and len(list(tmp_path.iterdir())) == 1


async def test_run_exits_2_with_a_report_when_a_redteam_case_is_unverified(tmp_path, capsys):
    unverified = [{"suite": "redteam", "id": "r_9", "persona": "cash_ops_emea", "leaked": False, "unverified": True,
                   "error_type": "OSError", "seconds": 1.0, "telemetry": None, "leaks": [], "summary_excerpt": ""}]

    class Runner:
        def __init__(self, *a, **kw):
            pass

        async def run(self, golden, redteam):
            return unverified
    code = await run_evals(Args(), Settings(), golden=[], redteam=[], agent_factory=lambda url: FakeClient(),
                           runner_factory=Runner, reports_dir=tmp_path)
    assert code == 2 and len(list(tmp_path.iterdir())) == 1
    assert "could not be checked" in capsys.readouterr().err
    report = build_report(unverified, started="2026-10-02T10:00:00Z", finished="2026-10-02T10:01:00Z", git_sha="x")
    assert report["totals"]["unverified"] == 1 and "UNVERIFIED" in to_markdown(report)
