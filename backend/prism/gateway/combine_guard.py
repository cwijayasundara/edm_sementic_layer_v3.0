"""Static guard for combine(): one read-only SELECT over the caller's named handles, and no silent cross-currency sums.

Parse with sqlglot (DuckDB dialect) and refuse: anything but exactly one SELECT / set operation; any command, DDL,
DML, PRAGMA, SET, ATTACH, COPY, INSTALL, placeholder, lambda, PIVOT, VALUES, LATERAL or recursive CTE; any FROM item
that is not a plain unqualified name of a registered handle or a CTE (this is what stops read_csv / read_text /
glob / query() / duckdb_* / information_schema / '/etc/passwd'); any function outside a small allow-list (unknown
functions parse as Anonymous and are refused, so no aggregate can escape the currency check by being unknown).
Columns are then resolved with sqlglot's qualify (a column that cannot be resolved, e.g. a whole-row struct, is
refused) and the QUALIFIED tree is what runs, so the engine executes exactly what was checked.

Currency: an input handle with a `ccy` column has amount columns (unit mentions amount/currency, or, when the unit is
unknown, every numeric column). Lineage is followed through CTEs, subqueries, aliases and set operations; every
non-count aggregate over an amount must group (or partition) by AND project the genuine `ccy` column of the same
table instance. ROLLUP / CUBE / GROUPING SETS (grand totals) and anything unresolvable are refused (fail closed).

A table instance is one reference in one scope: `FROM t x CROSS JOIN t y` over a single CTE `t` is two instances, so
y's amounts can never be labelled with x's ccy. Only a bare column reference carries a genuine ccy: COALESCE, CASE,
IF, NULLIF, casts, string functions etc. over ccy columns are never genuine (they can splice one instance's currency
onto another's amounts). Set operations line up by position (by name for UNION BY NAME) and an output column is a
genuine ccy only if it is one in every branch.

Out of scope: row-level arithmetic across instances (`a.value + b.value`) is not an aggregate and is not refused,
but its result's origins span both instances, so its output lineage has no genuine ccy (ccy=None) and any later
aggregate over it in a chained combine is refused (fails closed). Functions that can build huge strings (repeat,
lpad, rpad, ...) are kept out of the allow-list because the engine memory limit does not reliably stop them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from prism.gateway.errors import GatewayError, echo

CCY = "ccy"
UNKNOWN = "?"
COUNT_LIKE = (exp.Count, exp.CountIf, exp.ApproxDistinct)
_AGGS = (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Median, exp.Stddev, exp.StddevPop, exp.StddevSamp, exp.Variance,
         exp.VariancePop, exp.AnyValue)
_WINDOW = (exp.RowNumber, exp.Rank, exp.DenseRank, exp.PercentRank, exp.CumeDist, exp.Ntile, exp.Lag, exp.Lead,
           exp.FirstValue, exp.LastValue, exp.NthValue)
_SCALAR = (exp.Abs, exp.Round, exp.Floor, exp.Ceil, exp.Sign, exp.Sqrt, exp.Pow, exp.Ln, exp.Log, exp.Exp,
           exp.Coalesce, exp.Nullif, exp.If, exp.Case, exp.Cast, exp.TryCast, exp.Upper, exp.Lower, exp.Length,
           exp.Substring, exp.Trim, exp.Concat, exp.Replace, exp.StartsWith, exp.Left, exp.Right, exp.Contains,
           exp.Greatest, exp.Least, exp.DateTrunc, exp.TimestampTrunc, exp.Extract, exp.DateDiff, exp.DateAdd,
           exp.DateSub, exp.TimeToStr, exp.StrToTime, exp.StrToDate, exp.Year, exp.Month, exp.Day, exp.Quarter,
           exp.CurrentDate)
# sqlglot models the boolean connectors as Func subclasses; they are operators, not functions
_OPERATORS = (exp.And, exp.Or, exp.Xor)
ALLOWED_FUNCS = frozenset((*COUNT_LIKE, *_AGGS, *_WINDOW, *_SCALAR, *_OPERATORS))
_FORBIDDEN_NAMES = ("Command", "Create", "Drop", "Insert", "Update", "Delete", "Merge", "Copy", "Pragma", "Set",
                    "Use", "Attach", "Detach", "Install", "Into", "Describe", "Transaction", "Commit", "Rollback",
                    "Placeholder", "Parameter", "SessionParameter", "Lambda", "Pivot", "Values", "Lateral", "Kill",
                    "Summarize", "Show", "Export", "Analyze", "Cache", "Uncache", "Refresh", "Grant", "Revoke",
                    "Comment", "Declare", "TableSample", "UserDefinedFunction", "Alter")
FORBIDDEN = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))


@dataclass(frozen=True)
class TableInfo:
    columns: dict[str, str]                       # lower-case column -> DuckDB type
    amount: frozenset[str] = frozenset()          # amount columns
    ccy: str | None = None                        # the column holding the genuine currency of those amounts


@dataclass(frozen=True)
class Info:
    amount: frozenset = field(default_factory=frozenset)   # origins (table instances) of amounts flowing in
    ccy: frozenset = field(default_factory=frozenset)      # origins whose genuine ccy this column is


_UNKNOWN_INFO = Info(amount=frozenset({UNKNOWN}))


def _invalid(message: str) -> GatewayError:
    return GatewayError("invalid_sql", message)


def _not_allowed(message: str) -> GatewayError:
    return GatewayError("sql_not_allowed", message)


def _structure(tree: exp.Expression, tables: dict[str, TableInfo]) -> None:
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise _not_allowed("only a single SELECT (optionally WITH ...) statement is allowed")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN):
            raise _not_allowed(f"{type(node).__name__.upper()} is not allowed in combine")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise _not_allowed("recursive CTEs are not allowed in combine")
        if isinstance(node, exp.Func) and type(node) not in ALLOWED_FUNCS:
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            raise _not_allowed(f"function {echo(str(name).lower())} is not allowed in combine")
    ctes = {c.alias_or_name.casefold() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier) or t.args.get("db") or t.args.get("catalog"):
            raise _not_allowed("FROM may only name the handles passed to combine (no functions, files or schemas)")
        if t.name.casefold() not in tables and t.name.casefold() not in ctes:
            raise _invalid(f"unknown table {echo(t.name)}; available: {sorted(tables)}")


def _instance(info: Info, ref: tuple) -> Info:
    """Re-key the origins seen through one reference (scope, alias) to a CTE / derived table, so that two references
    to the same memoised scope (`FROM t x CROSS JOIN t y`) are two table instances, never one."""
    def key(origins: frozenset) -> frozenset:
        return frozenset(o if o == UNKNOWN else (ref, o) for o in origins)
    return Info(amount=key(info.amount), ccy=key(info.ccy))


class _Lineage:
    def __init__(self, tables: dict[str, TableInfo]):
        self.tables = tables
        self.memo: dict[int, list[tuple[str, Info]]] = {}

    def column(self, scope: Scope, col: exp.Column) -> Info:
        s, src = scope, None
        while s is not None and src is None:
            src = s.sources.get(col.table)
            if src is None:
                s = s.parent
        name = col.name.casefold()
        if isinstance(src, exp.Table):
            info = self.tables.get(src.name.casefold())
            if info is None:
                return _UNKNOWN_INFO
            key = (id(s), col.table)
            return Info(amount=frozenset({key}) if name in info.amount else frozenset(),
                        ccy=frozenset({key}) if info.ccy is not None and name == info.ccy else frozenset())
        if isinstance(src, Scope):  # a CTE / derived table: each reference to it is its own instance
            return _instance(self.outputs(src).get(name, _UNKNOWN_INFO), (id(s), col.table))
        return _UNKNOWN_INFO

    def expr(self, scope: Scope, e: exp.Expression) -> Info:
        if isinstance(e, exp.Column):
            return self.column(scope, e)
        amount = frozenset().union(*(self.column(scope, c).amount for c in e.find_all(exp.Column)))
        if e.find(exp.Subquery, exp.Select):
            amount |= {UNKNOWN}
        return Info(amount=amount)

    def outputs(self, scope: Scope) -> dict[str, Info]:
        out: dict[str, Info] = {}
        for name, info in self.columns(scope):   # a duplicated name may resolve to either column: fail closed
            prev = out.get(name)
            out[name] = info if prev is None else Info(amount=prev.amount | info.amount, ccy=prev.ccy & info.ccy)
        return out

    def columns(self, scope: Scope) -> list[tuple[str, Info]]:
        """The scope's output columns, in order (set operations line up by position, or by name for BY NAME)."""
        if id(scope) in self.memo:
            return self.memo[id(scope)]
        node = scope.expression
        if isinstance(node, exp.SetOperation):
            out = _set_operation([self.columns(b) for b in scope.set_operation_scopes], bool(node.args.get("by_name")))
        elif isinstance(node, exp.Select):
            out = [(p.alias_or_name.casefold(), self.expr(scope, p.unalias())) for p in node.expressions]
        else:
            out = []
        self.memo[id(scope)] = out
        return out


def _set_operation(branches: list[list[tuple[str, Info]]], by_name: bool) -> list[tuple[str, Info]]:
    """Each output row comes from one branch, and branch origins are distinct table instances, so an output column is a
    genuine ccy only if it is one in EVERY branch (then for the union of their origins)."""
    if by_name:
        names = list(dict.fromkeys(n for b in branches for n, _ in b))
        maps = [dict(b) if len(dict(b)) == len(b) else {} for b in branches]   # duplicate names: unresolvable
        rows = [(n, [m.get(n, Info()) if m else _UNKNOWN_INFO for m in maps]) for n in names]
    elif len({len(b) for b in branches}) != 1:
        rows = [(n, [_UNKNOWN_INFO]) for n, _ in branches[0]]
    else:
        rows = [(n, [b[i][1] for b in branches]) for i, (n, _) in enumerate(branches[0])]
    return [(n, Info(amount=frozenset().union(*(x.amount for x in infos)),
                     ccy=frozenset().union(*(x.ccy for x in infos)) if all(x.ccy for x in infos) else frozenset()))
            for n, infos in rows]


def _keys(sel: exp.Select, agg: exp.AggFunc) -> list[exp.Expression]:
    p = agg.parent
    if isinstance(p, exp.Filter):
        p = p.parent
    if isinstance(p, exp.Window):
        return list(p.args.get("partition_by") or [])
    group = sel.args.get("group")
    if group is None:
        return []
    if group.args.get("rollup") or group.args.get("cube") or group.args.get("grouping_sets"):
        raise GatewayError("currency_mixing", "ROLLUP / CUBE / GROUPING SETS would total amounts across currencies")
    if group.args.get("all"):
        return [x.unalias() for x in sel.expressions if not x.find(exp.AggFunc)]
    return list(group.expressions)


def _check_currency(tree: exp.Expression, tables: dict[str, TableInfo]) -> None:
    lineage = _Lineage(tables)
    for scope in traverse_scope(tree):
        sel = scope.expression
        if not isinstance(sel, exp.Select):
            continue
        for agg in sel.find_all(exp.AggFunc):
            if isinstance(agg, COUNT_LIKE) or agg.find_ancestor(exp.Select) is not sel:
                continue
            info = lineage.expr(scope, agg)
            if not info.amount:
                continue
            grouped = frozenset().union(*(lineage.expr(scope, k).ccy for k in _keys(sel, agg)))
            shown = frozenset().union(*(lineage.expr(scope, x.unalias()).ccy for x in sel.expressions))
            if not info.amount <= (grouped & shown):
                raise GatewayError("currency_mixing",
                                   "amounts in different currencies would be combined: group by ccy (and select it) "
                                   "wherever an amount is aggregated")


@dataclass(frozen=True)
class Checked:
    sql: str                                      # the qualified SQL to run
    amount: frozenset[str]                        # output columns carrying amounts (lineage for the next combine)
    ccy: str | None                               # output column with the genuine currency of ALL those amounts
    unresolved: bool = False                      # output lineage unknown: treat every numeric column as an amount


def _output_lineage(tree: exp.Expression, tables: dict[str, TableInfo]) -> tuple[frozenset[str], str | None, bool]:
    if not any(t.amount for t in tables.values()):
        return frozenset(), None, False
    scopes = traverse_scope(tree)
    outs = _Lineage(tables).outputs(scopes[-1]) if scopes else {}
    if not outs:
        return frozenset(), None, True
    amount = frozenset(n for n, i in outs.items() if i.amount)
    origins = frozenset().union(*(outs[n].amount for n in amount))
    ccy = next((n for n, i in outs.items() if origins and origins <= i.ccy), None)
    return amount, ccy, False


def check_sql(sql: str, tables: dict[str, TableInfo]) -> Checked:
    """Validate `sql` against the registered tables; return the qualified SQL that may run and its output lineage.
    Any failure other than a deliberate refusal (e.g. RecursionError on absurd nesting) is also a refusal."""
    try:
        return _check_sql(sql, tables)
    except GatewayError:
        raise
    except Exception:  # noqa: BLE001 - never let a parser / optimizer crash become a pass or an internal error
        raise _invalid("could not analyse the query") from None


def _check_sql(sql: str, tables: dict[str, TableInfo]) -> Checked:
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.SqlglotError:
        raise _invalid("could not parse the SQL (DuckDB dialect)") from None
    if len(statements) != 1:
        raise _invalid("exactly one SELECT statement is allowed")
    tree = statements[0]
    _structure(tree, tables)
    schema = {name: dict(t.columns) for name, t in tables.items()}
    try:
        qualified = qualify(tree.copy(), schema=schema, dialect="duckdb", validate_qualify_columns=True,
                            quote_identifiers=True)
    except sqlglot.errors.SqlglotError as e:
        raise _invalid(f"could not resolve the query: {str(e).splitlines()[0][:200]}") from None
    except Exception:  # noqa: BLE001 - an optimizer crash on odd input is a refusal, never a pass
        raise _invalid("could not resolve the query") from None
    _structure(qualified, tables)
    if any(t.amount for t in tables.values()):
        _check_currency(qualified, tables)  # amounts without a genuine ccy can never be aggregated (fail closed)
    amount, ccy, unresolved = _output_lineage(qualified, tables)
    return Checked(sql=qualified.sql(dialect="duckdb"), amount=amount, ccy=ccy, unresolved=unresolved)


__all__ = ["Checked", "TableInfo", "check_sql"]
