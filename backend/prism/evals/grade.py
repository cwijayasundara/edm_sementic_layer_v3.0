"""Golden-case grading against reference rows: deterministic, no LLM judge.

`rows` compares a widget's rows with the reference as a multiset keyed on the non-numeric columns, numeric columns
within tolerance. When a case carries `story` assertions, `rows` is reported but not required: a different grouping
that still tells the planted story is a correct answer."""
import math
from dataclasses import dataclass

from prism.evals.cases import GoldenCase, Story, Tolerance
from prism.evals.types import ChatResult, Table


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True


def _num(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str) and v.strip():
        try:
            f = float(v)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _close(a: float, b: float, tol: Tolerance) -> bool:
    return abs(a - b) <= max(tol.abs, tol.rel * max(abs(a), abs(b)))


def rows_match(ref: Table, got: Table, tol: Tolerance) -> Check:
    if set(ref.columns) != set(got.columns):
        return Check("rows", False, f"columns {sorted(got.columns)} != reference {sorted(ref.columns)}")
    if len(ref.rows) != len(got.rows):
        return Check("rows", False, f"{len(got.rows)} rows != reference {len(ref.rows)}")
    order = [got.columns.index(c) for c in ref.columns]
    got_rows = [[r[i] for i in order] for r in got.rows]
    numeric = [i for i in range(len(ref.columns))
               if ref.rows and all(r[i] is None or _num(r[i]) is not None for r in ref.rows)
               and any(r[i] is not None for r in ref.rows)]
    keys = [i for i in range(len(ref.columns)) if i not in numeric]

    def grouped(rows):
        out: dict[tuple, list[tuple]] = {}
        for r in rows:
            out.setdefault(tuple(str(r[i]) for i in keys), []).append(tuple(_num(r[i]) for i in numeric))
        return {k: sorted(v, key=lambda t: tuple((x is None, x or 0.0) for x in t)) for k, v in out.items()}

    want, have = grouped(ref.rows), grouped(got_rows)
    if want.keys() != have.keys():
        return Check("rows", False, "row keys differ from the reference")
    for k, values in want.items():
        for a, b in zip(values, have[k], strict=True):
            for x, y in zip(a, b, strict=True):
                if (x is None) != (y is None) or (x is not None and not _close(x, y, tol)):
                    return Check("rows", False, f"values differ for {k}")
    return Check("rows", True)


def story_holds(story: Story, table: Table) -> bool:
    col = {c: i for i, c in enumerate(table.columns)}
    if story.count is not None:
        n = len(table.rows)
        return n >= story.count.min and (story.count.max is None or n <= story.count.max)
    if story.contains is not None:
        if any(k not in col for k in story.contains):
            return False
        return any(all(str(r[col[k]]) == str(v) for k, v in story.contains.items()) for r in table.rows)
    top = story.top
    if any(k not in col for k in (top.by, top.key, *top.where)):
        return False
    rows = [r for r in table.rows if all(str(r[col[k]]) == str(v) for k, v in top.where.items())
            and _num(r[col[top.by]]) is not None]
    if not rows:
        return False
    best = max(_num(r[col[top.by]]) for r in rows)
    return any(_num(r[col[top.by]]) == best and str(r[col[top.key]]) == str(top.equals) for r in rows)


def grade_golden(case: GoldenCase, chat: ChatResult, tables: dict[str, Table | None], ref: Table | None) -> list[Check]:
    e = case.expect
    checks = [Check("answered", bool(chat.widgets) and chat.error is None and not chat.timed_out,
                    "" if chat.widgets else (chat.error or {}).get("code", "no widget"))]
    infos = [w.get("handle_info") or {} for w in chat.widgets]
    target, field = (e.metric_id, "metric_id") if e.metric_id else (e.source, "source")
    checks.append(Check("routing", any(i.get(field) == target for i in infos),
                        f"widgets used {sorted({str(i.get(field)) for i in infos})}"))
    live = [t for t in (tables.get(h) for h in chat.handles) if t is not None]
    if ref is None:
        rows = Check("rows", False, "the reference could not be computed")
    else:
        results = [rows_match(ref, t, case.tolerance) for t in live]
        rows = next((r for r in results if r.ok), results[0] if results else Check("rows", False, "no widget rows"))
    rows.required = not e.story
    checks.append(rows)
    if e.story:
        ok = any(all(story_holds(s, t) for s in e.story) for t in live)
        checks.append(Check("story", ok, "" if ok else "no widget's rows tell the planted story"))
    if e.chart_types:
        types = [w["widget"]["type"] for w in chat.widgets]
        checks.append(Check("chart", any(t in e.chart_types for t in types), f"widget types {types}"))
    return checks


def passed(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.required)
