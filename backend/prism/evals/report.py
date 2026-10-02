"""Eval reports: report.json (everything) and report.md (totals plus one table per suite)."""
import json
import math
from datetime import datetime
from pathlib import Path


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def _tel(r: dict, key: str) -> float:
    return float((r.get("telemetry") or {}).get(key) or 0)


def build_report(results: list[dict], *, started: str, finished: str, git_sha: str) -> dict:
    ran = [r for r in results if not r.get("skipped")]
    golden = [r for r in ran if r["suite"] == "golden"]
    red = [r for r in ran if r["suite"] == "redteam"]
    ok = sum(1 for r in golden if r["passed"])
    secs = [r["seconds"] for r in ran]
    totals = {
        "golden_cases": len(golden), "golden_passed": ok,
        "golden_pass_rate": round(ok / len(golden), 3) if golden else None,
        "redteam_cases": len(red), "leaks": sum(1 for r in red if r["leaked"]),
        "unverified": sum(1 for r in red if r.get("unverified")),
        "skipped": len(results) - len(ran),
        "cost_usd": round(sum(_tel(r, "cost_usd") for r in ran), 4),
        "input_tokens": int(sum(_tel(r, "input_tokens") for r in ran)),
        "output_tokens": int(sum(_tel(r, "output_tokens") for r in ran)),
        "cache_read_input_tokens": int(sum(_tel(r, "cache_read_input_tokens") for r in ran)),
        "p50_s": _pct(secs, 0.5), "p95_s": _pct(secs, 0.95),
    }
    return {"started": started, "finished": finished, "git_sha": git_sha, "totals": totals, "results": results}


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")[:160]


def to_markdown(report: dict) -> str:
    t = report["totals"]
    rate = f"{t['golden_pass_rate'] * 100:.1f}%" if t["golden_pass_rate"] is not None else "n/a"
    lines = ["# Prism eval report", "",
             f"Started {report['started']} · finished {report['finished']} · commit {report['git_sha']}", "",
             f"- Golden: {t['golden_passed']}/{t['golden_cases']} passed ({rate})",
             f"- Red-team: {t['leaks']} leak(s) in {t['redteam_cases']} case(s), {t['unverified']} unverified",
             f"- Skipped (cost cap): {t['skipped']}",
             f"- Cost: ${t['cost_usd']:.4f} · tokens in/out/cached {t['input_tokens']}/{t['output_tokens']}/"
             f"{t['cache_read_input_tokens']} · latency p50 {t['p50_s']} s, p95 {t['p95_s']} s", "",
             "## Golden", "", "| case | persona | result | failed checks | seconds |", "|---|---|---|---|---|"]
    for r in report["results"]:
        if r["suite"] != "golden":
            continue
        if r.get("skipped"):
            lines.append(f"| {r['id']} | {r['persona']} | skipped | | |")
            continue
        failed = "; ".join(f"{c['name']}: {c['detail']}" for c in r["checks"] if not c["ok"] and c["required"]) \
            or r.get("error_type", "")
        lines.append(f"| {r['id']} | {r['persona']} | {'pass' if r['passed'] else 'FAIL'} | {_cell(failed)} | "
                     f"{r['seconds']} |")
    lines += ["", "## Red-team", "", "| case | persona | result | detectors | seconds |", "|---|---|---|---|---|"]
    for r in report["results"]:
        if r["suite"] != "redteam":
            continue
        if r.get("skipped"):
            lines.append(f"| {r['id']} | {r['persona']} | skipped | | |")
            continue
        found = "; ".join(f"{x['detector']}: {x['detail']}" for x in r["leaks"]) or r.get("error_type", "")
        result = "LEAK" if r["leaked"] else "UNVERIFIED" if r.get("unverified") else "ok"
        lines.append(f"| {r['id']} | {r['persona']} | {result} | {_cell(found)} | "
                     f"{r['seconds']} |")
    return "\n".join(lines) + "\n"


def write_report(root: Path, report: dict) -> Path:
    stamp = datetime.strptime(report["started"], "%Y-%m-%dT%H:%M:%SZ").strftime("%Y%m%dT%H%M%SZ")
    out = Path(root) / stamp
    out.mkdir(parents=True, exist_ok=False)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (out / "report.md").write_text(to_markdown(report))
    return out
