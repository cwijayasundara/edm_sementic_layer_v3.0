"""`python -m prism.evals.cli`: live golden + red-team evals against the running stack (costs API money), or
`--check-references` (no model calls) to validate the case files against the live seed."""
import argparse
import asyncio
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from prism.config import Settings
from prism.evals.cases import load_golden, load_redteam
from prism.evals.client import AgentClient, AgentNotReady, ensure_loopback
from prism.evals.report import build_report, write_report
from prism.evals.runner import EvalRunner, check_references

REPORTS_DIR = Path(__file__).resolve().parents[2] / "evals" / "reports"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


async def run_evals(args, settings: Settings, *, golden, redteam, agent_factory=AgentClient,
                    runner_factory=EvalRunner, reports_dir: Path = REPORTS_DIR) -> int:
    started = _now()
    async with agent_factory(args.agent_url) as agent:
        if not await agent.healthy():
            _say(f"the agent is not reachable at {args.agent_url}: start the stack with scripts/start_backend.sh")
            return 2
        runner = runner_factory(settings, agent, max_cost_usd=args.max_cost_usd)
        try:
            results = await runner.run(golden, redteam)
        except AgentNotReady as exc:
            _say(f"eval run aborted: {exc}")
            return 2
    report = build_report(results, started=started, finished=_now(), git_sha=_git_sha())
    out = write_report(reports_dir, report)
    t = report["totals"]
    print(f"golden {t['golden_passed']}/{t['golden_cases']} · leaks {t['leaks']}/{t['redteam_cases']} · "
          f"skipped {t['skipped']} · ${t['cost_usd']:.4f} · report {out / 'report.md'}")
    if t["leaks"]:
        return 1
    if t["unverified"]:
        _say(f"{t['unverified']} red-team case(s) could not be checked (see the report): not a pass")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="prism-eval", description=__doc__)
    p.add_argument("--suite", choices=("golden", "redteam", "all"), default="all")
    p.add_argument("--case", action="append", default=[], help="run only this case id (repeatable)")
    p.add_argument("--max-cost-usd", type=float, default=5.0)
    p.add_argument("--agent-url", default="http://127.0.0.1:8000")
    p.add_argument("--check-references", action="store_true",
                   help="validate the case files against the live seed (no model calls) and exit")
    args = p.parse_args(argv)
    settings = Settings()
    try:
        ensure_loopback(args.agent_url)
        ensure_loopback(settings.gateway_url)
        golden = load_golden() if args.suite in ("golden", "all") else []
        redteam = load_redteam() if args.suite in ("redteam", "all") else []
    except ValueError as exc:
        _say(str(exc))
        return 2
    if args.case:
        known = {c.id for c in golden + redteam}
        if unknown := sorted(set(args.case) - known):
            _say(f"unknown case id(s): {unknown}")
            return 2
        golden = [c for c in golden if c.id in args.case]
        redteam = [c for c in redteam if c.id in args.case]
    if args.check_references:
        try:
            problems = asyncio.run(check_references(settings, golden, redteam))
        except Exception as exc:  # noqa: BLE001 - one line, never a traceback
            _say(f"could not check references ({type(exc).__name__}): is the stack running?")
            return 2
        for line in problems:
            print(line)
        print(f"{len(problems)} problem(s) in {len(golden)} golden and {len(redteam)} red-team case(s)")
        return 1 if problems else 0
    return asyncio.run(run_evals(args, settings, golden=golden, redteam=redteam))


if __name__ == "__main__":
    raise SystemExit(main())
