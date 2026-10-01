"""AST-based guard for LLM-generated, read-only analytic SQL (PostgreSQL).

validate_select() parses the input with sqlglot, enforces a read-only SELECT
policy on the AST, wraps the query in a row-limiting outer SELECT and returns
SQL *regenerated from the AST* (comments dropped). The original string is never
returned, so comment / dollar-quote / stacked-statement smuggling cannot survive.

This is defence in depth. It MUST be paired with DB-side controls: a dedicated
NOLOGIN/least-privilege role with SELECT only on the allowed relations,
`SET TRANSACTION READ ONLY`, `statement_timeout`, and a pinned
`search_path = public` (the guard resolves unqualified names to public.<name>).

Policy is enforced twice: on the ORIGINAL parsed tree and again on the tree
obtained by RE-PARSING the regenerated SQL (what the database will actually see).
sqlglot can re-emit some nodes (e.g. the opaque `Var` origin of date_bin) with
their contents unquoted, so a text-stability round-trip alone is not a policy
check. Opaque `Var` nodes with non-identifier names are rejected outright.

The guard bounds input size, nesting, node count and ROW count only. The byte
size of results is bounded elsewhere (Task 7 named-cursor budget).
"""

from __future__ import annotations

import re
import unicodedata

import sqlglot
from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.tokens import TokenType
from sqlglot.optimizer.scope import traverse_scope

from prism.mcp.results import UserFacingError

DIALECT = "postgres"

MAX_SQL_CHARS = 4000
MAX_NODES = 5000


class SqlGuardError(UserFacingError, ValueError):
    """Raised when a query violates the read-only SELECT policy."""


# Root node types accepted as "a SELECT". exp.Union/Intersect/Except are set
# operations; WITH ... SELECT parses as exp.Select with a `with` arg.
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

# Node types forbidden ANYWHERE in the tree (covers data-modifying CTEs such as
# `WITH x AS (DELETE ... RETURNING *) SELECT ...`, SELECT INTO, FOR UPDATE/SHARE).
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.DML,  # Insert / Update / Delete / Merge / Copy
    exp.DDL,  # Create / Drop / Alter / ...
    exp.Command,  # sqlglot fallback for SHOW/EXPLAIN/DO/CALL/VACUUM/PREPARE/EXECUTE/RESET ...
    exp.Set,
    exp.Copy,
    exp.Into,  # SELECT ... INTO new_table
    exp.Lock,  # FOR UPDATE / FOR SHARE / FOR NO KEY UPDATE / FOR KEY SHARE
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Grant,
    exp.Pragma,
    exp.Use,
    exp.Describe,
    # Session/identity probes that sqlglot models as dedicated nodes (not exp.Func
    # by name). Resolved by name so the module still imports on other sqlglot versions.
    *[
        t
        for t in (
            getattr(exp, n, None)
            for n in (
                "CurrentUser",
                "SessionUser",
                "CurrentDatabase",
                "CurrentCatalog",
                "CurrentSchema",
                "CurrentSchemas",
                "CurrentRole",
                "CurrentQuery",
            )
        )
        if t is not None
    ],
)

# OPERATOR(schema.op) invokes a schema-qualified operator; sqlglot models it as
# exp.Operator. Also reject hex/bit/byte string literals: sqlglot re-emits 0x1F as
# x'1F' (a bit string), which is not the integer Postgres 16 reads.
_FORBIDDEN_NODES += tuple(
    t for t in (getattr(exp, n, None) for n in ("Operator", "HexString", "BitString", "ByteString")) if t is not None
)

# Bare keywords sqlglot parses as plain columns (no dedicated node).
_DENY_BARE_COLUMNS = frozenset(
    {
        "user",
        "system_user",
        "current_user",
        "session_user",
        "current_role",
        "current_catalog",
        "current_schema",
        "current_schemas",
        "current_database",
        "current_query",
        "inet_client_port",
    }
)

# Function deny-list (compared after lower-casing + NFKC normalisation).
_DENY_FUNCS = frozenset(
    {
        "set_config",
        "current_setting",
        "copy",
        "dblink",
        "nextval",
        "setval",
        "currval",
        "lastval",
        "version",
        "inet_server_addr",
        "inet_server_port",
        "inet_client_addr",
        "inet_client_port",
        "user",
        "getpgusername",
        "current_user",
        "session_user",
        "system_user",
        "current_database",
        "current_catalog",
        "current_query",
        "current_schema",
        "current_schemas",
        "current_role",
        # sqlglot mis-models date_bin's origin argument: the string literal becomes an
        # opaque Var re-emitted UNQUOTED, turning its contents into live SQL.
        "date_bin",
        # same defect class: sqlglot emits date_part's first (string) argument
        # unquoted as EXTRACT(<Var> FROM ...). Use EXTRACT / date_trunc instead.
        "date_part",
        "xmlparse",
        # functions that EXECUTE a query string (bypass the table allow-list)
        "ts_stat",
        "ts_rewrite",
        # large-object I/O without the lo_ prefix
        "loread",
        "lowrite",
        # function-style casts to reg* OID-alias types, e.g. regclass('private.x').
        # Exact names on purpose: a "reg" prefix would also block regexp_*.
        "regclass",
        "regproc",
        "regprocedure",
        "regtype",
        "regnamespace",
        "regrole",
        "regoper",
        "regoperator",
        "regconfig",
        "regdictionary",
        "regcollation",
    }
)
_DENY_FUNC_PREFIXES = (
    "pg_",  # pg_sleep*, pg_read_*, pg_ls_*, pg_terminate_backend, pg_cancel_backend, ...
    "dblink",
    "lo_",
    "txid_",
    "to_reg",  # to_regclass / to_regproc ... catalog probing
    "query_to_xml",
    "xpath",
    "has_",  # has_table_privilege & friends: privilege probing
)
_DENY_FUNC_SUBSTRINGS = ("_to_xml",)  # table_to_xml, cursor_to_xml, schema_to_xml, database_to_xml...

# Catalog-ish schemas that must never be referenced.
_DENY_SCHEMA_PREFIXES = ("pg_",)
_DENY_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})

_SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
# U&"..." / U&'...' unicode-escape identifiers/strings. sqlglot mis-tokenises
# them as `U & "..."` (bitwise AND), so the AST would not reflect what Postgres
# sees. Reject outright.
_UNICODE_ESCAPE = re.compile(r"(?i)\bu&\s*['\"]")
# E'...' strings use backslash escapes that sqlglot 30.x round-trips as e'\' (an
# UNTERMINATED string for Postgres), shifting string boundaries so a later literal's
# contents can become live SQL. Reject E-strings, and any backslash directly before
# a single quote (which can terminate/escape differently in sqlglot and Postgres).
# Other backslashes (regex idioms like '\s+', '\d+', LIKE '%\\_%') are harmless in
# standard_conforming_strings and are allowed.
_E_STRING = re.compile(r"(?i)(?<![A-Za-z0-9_])e\s*'")
_WORDLIKE = frozenset({TokenType.VAR, TokenType.IDENTIFIER, TokenType.NUMBER})
_BARE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


MAX_ECHO_CHARS = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _fail(msg: str) -> None:
    raise SqlGuardError(msg)


def _echo(text: object) -> str:
    """A caller-supplied fragment safe to put in a message: no control characters, at most 80 chars."""
    clean = _CONTROL.sub("", str(text))
    return clean if len(clean) <= MAX_ECHO_CHARS else clean[: MAX_ECHO_CHARS - 3] + "..."


def _parse_failure(err: Exception) -> str:
    """A fixed message plus the position of the first error. Never sqlglot's own text: it carries token
    reprs, class paths and source excerpts with ANSI escapes."""
    for detail in getattr(err, "errors", None) or ():
        if isinstance(detail, dict):
            line, col = detail.get("line"), detail.get("col")
            if isinstance(line, int) and isinstance(col, int):
                return f"could not parse SQL (line {line}, column {col})"
    return "could not parse SQL"


def _norm(identifier: exp.Identifier | str | None) -> str:
    """Postgres identifier folding: unquoted -> lower-case, quoted -> as-is."""
    if identifier is None:
        return ""
    if isinstance(identifier, exp.Identifier):
        name = identifier.this
        return name if identifier.quoted else name.lower()
    return str(identifier).lower()


def _function_names(func: exp.Func) -> set[str]:
    """Every name a function node could be known by (normalised, lower-case)."""
    names: set[str] = set()
    if isinstance(func, exp.Anonymous):
        names.add(str(func.this))
    else:
        names.add(func.sql_name())
        # What sqlglot will actually emit for Postgres, e.g. GENERATE_SERIES(...).
        # Skipped for operators (AND/OR/...): they have no callable name, and
        # rendering their whole subtree at every level of a long chain is quadratic.
        if not isinstance(func, exp.Binary | exp.Unary):
            rendered = func.sql(dialect=DIALECT, comments=False)
            head = rendered.split("(", 1)[0].strip().strip('"')
            if head:
                names.add(head)
    return {unicodedata.normalize("NFKC", n).lower() for n in names}


def _check_function(func: exp.Func) -> None:
    # Schema-qualified calls (pg_catalog.pg_sleep(), private.fn(), prism_sec.x())
    # parse as Dot(this=<schema>, expression=<func>). Reject all of them.
    parent = func.parent
    if isinstance(parent, exp.Dot) and parent.expression is func:
        _fail(f"schema-qualified function calls are not allowed: {_echo(parent.sql(dialect=DIALECT))}")
    for name in _function_names(func):
        if isinstance(func, exp.Anonymous) and not _SAFE_IDENT.match(name):
            _fail(f"invalid function name: {_echo(repr(name))}")
        if (
            name in _DENY_FUNCS
            or name.startswith(_DENY_FUNC_PREFIXES)
            or any(s in name for s in _DENY_FUNC_SUBSTRINGS)
        ):
            _fail(f"function not allowed: {_echo(name)}")


def _check_datatype(dt: exp.DataType) -> None:
    # Reject casts to reg* OID-alias types (regclass, regproc, regprocedure,
    # regtype, regnamespace, regrole, ...) and any schema-qualified/user type.
    rendered = dt.sql(dialect=DIALECT).lower().replace('"', "")
    base = rendered.split("(", 1)[0].split("[", 1)[0].strip()
    if "." in base:
        _fail(f"schema-qualified types are not allowed: {_echo(rendered)}")
    if base.startswith("reg") or base.startswith("pg_"):
        _fail(f"cast to type not allowed: {_echo(rendered)}")


def _qualified_table(table: exp.Table) -> str:
    if table.args.get("catalog"):
        _fail(f"cross-database reference not allowed: {_echo(table.sql(dialect=DIALECT))}")
    schema = _norm(table.args.get("db")) or "public"
    name = _norm(table.this)
    return f"{schema}.{name}"


def _check_tables(tree: exp.Expression, allowed: frozenset[str]) -> None:
    """Every real relation (not CTE reference, not table function) must be allowed.

    Uses sqlglot scope analysis so a CTE name is only treated as a CTE where it is
    actually in scope (prevents `WITH secret AS (...)` in one subquery from
    whitelisting a real table named `secret` elsewhere).
    """
    try:
        scopes = list(traverse_scope(tree))
    except SqlglotError:  # fail closed
        _fail("could not analyse query scopes")

    seen: set[int] = set()
    for scope in scopes:
        for table in scope.tables:
            if id(table) in seen:
                continue
            seen.add(id(table))
            if isinstance(table.this, exp.Func):
                continue  # table function (unnest, generate_series...) -> checked as function
            if not table.args.get("db") and table.name in scope.cte_sources:
                continue  # in-scope CTE reference
            fq = _qualified_table(table)
            schema, name = fq.split(".", 1)
            if schema in _DENY_SCHEMAS or schema.startswith(_DENY_SCHEMA_PREFIXES):
                _fail(f"system catalog access not allowed: {_echo(fq)}")
            if name.startswith("pg_"):
                # unqualified pg_* resolves to pg_catalog (implicitly first in search_path)
                _fail(f"system catalog access not allowed: {_echo(fq)}")
            if fq not in allowed:
                _fail(f"table not allowed: {_echo(fq)}")

    # Cross-check: scope traversal must have visited every Table node in the tree.
    for table in tree.find_all(exp.Table):
        if id(table) not in seen:
            _fail(f"unresolvable relation reference: {_echo(table.sql(dialect=DIALECT))}")


def _check_numeric_tokens(sql: str) -> None:
    """Reject numeric forms sqlglot and Postgres 16 read differently.

    1_000_000 tokenises as NUMBER `1` + alias `_000_000`; 0o17 as `0` + alias `o17`
    (Postgres reads one integer). Any NUMBER token directly followed (no gap) by a
    word token is such a form. Uses the tokenizer, so string literals, comments and
    quoted identifiers are never inspected.
    """
    try:
        tokens = Postgres().tokenize(sql)
    except SqlglotError as e:
        _fail(_parse_failure(e))
    for prev, nxt in zip(tokens, tokens[1:]):
        if prev.token_type == TokenType.NUMBER and nxt.start == prev.end + 1 and nxt.token_type in _WORDLIKE:
            _fail("numeric literals with underscores or 0o/0x/0b prefixes are not allowed")


def _iter_nodes(root: exp.Expression):
    """Linear, non-recursive traversal (tree.walk() is quadratic on long chains)."""
    stack = [root]
    count = 0
    while stack:
        node = stack.pop()
        count += 1
        if count > MAX_NODES:
            _fail(f"query too complex (more than {MAX_NODES} syntax nodes)")
        yield node
        for value in node.args.values():
            if isinstance(value, exp.Expression):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(v for v in value if isinstance(v, exp.Expression))


def _enforce(tree: exp.Expression, allowed_tables: frozenset[str]) -> None:
    """The whole read-only policy. Run on the original AND the re-parsed tree."""
    if not isinstance(tree, (*_ALLOWED_ROOTS, exp.Subquery)):
        _fail(f"only SELECT queries are allowed, got {type(tree).__name__}")
    for node in _iter_nodes(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            _fail(f"forbidden construct: {type(node).__name__}")
        if isinstance(node, exp.Func):
            _check_function(node)
        elif isinstance(node, exp.DataType):
            _check_datatype(node)
        elif isinstance(node, exp.Var):
            # Opaque verbatim node: sqlglot may re-emit its text unquoted.
            if not _BARE_IDENT.match(str(node.this)):
                _fail("opaque literal construct not allowed")
        elif (
            isinstance(node, exp.Column)
            and not node.args.get("table")
            and _norm(node.this) in _DENY_BARE_COLUMNS
        ):
            _fail(f"function not allowed: {_echo(_norm(node.this))}")
    _check_tables(tree, allowed_tables)


def validate_select(sql: str, *, allowed_tables: frozenset[str], max_rows: int = 500) -> str:
    """Validate a read-only SELECT and return a row-limited, AST-regenerated SQL string.

    allowed_tables: fully-qualified lower-case names, e.g. {"public.breaks"}.
    Raises SqlGuardError on any policy violation.
    """
    if not isinstance(sql, str) or not sql.strip():
        _fail("empty query")
    if max_rows < 1:
        _fail("max_rows must be >= 1")
    if "\x00" in sql:
        _fail("NUL byte in query")
    if _UNICODE_ESCAPE.search(sql):
        _fail("U&'...' unicode-escape literals/identifiers are not allowed")
    if len(sql) > MAX_SQL_CHARS:
        _fail(f"query too long (max {MAX_SQL_CHARS} characters)")
    if _E_STRING.search(sql):
        _fail("E'...' escape strings are not allowed")
    if "\\'" in sql:
        _fail("backslash before a quote is not allowed (string-escape ambiguity)")

    try:
        _check_numeric_tokens(sql)
        return _validate(sql, allowed_tables, max_rows)
    except SqlGuardError:
        raise
    except Exception as e:  # noqa: BLE001 - RecursionError / any sqlglot failure: fail closed
        raise SqlGuardError("query too deeply nested / could not be analysed") from e


def _validate(sql: str, allowed_tables: frozenset[str], max_rows: int) -> str:
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except (ParseError, SqlglotError) as e:
        _fail(_parse_failure(e))

    if len(statements) != 1:
        _fail(f"exactly one statement is required, got {len(statements)}")
    tree = statements[0]

    if not isinstance(tree, _ALLOWED_ROOTS):
        _fail(f"only SELECT queries are allowed, got {type(tree).__name__}")

    _enforce(tree, allowed_tables)

    # Result-size control: ALWAYS wrap as
    #     SELECT * FROM (<q>) AS _q LIMIT <max_rows>
    # rather than clamping the top-level LIMIT, because clamping has to special
    # case UNION/INTERSECT/EXCEPT, FETCH FIRST, LIMIT ALL, OFFSET and non-literal
    # LIMIT expressions (`LIMIT (SELECT 10^9)`). The wrapper is uniform, keeps the
    # inner query's own LIMIT/ORDER BY semantics, and `SELECT *` preserves the
    # column names/order (duplicate names in the inner list are fine in PG as long
    # as the outer query does not reference them by name). PG preserves the inner
    # ORDER BY through a plain subquery scan + LIMIT.
    wrapped = (
        exp.select(exp.Star())
        .from_(exp.Subquery(this=tree, alias=exp.TableAlias(this=exp.to_identifier("_q"))))
        .limit(max_rows)
    )
    # comments=False: never re-emit comments from the input.
    out = wrapped.sql(dialect=DIALECT, comments=False)

    # Round-trip self-check: the output must re-parse to exactly one statement,
    # regenerate to the identical text, AND pass the full policy again (text
    # stability alone does not prove policy stability).
    try:
        reparsed = [s for s in sqlglot.parse(out, read=DIALECT) if s is not None]
    except (ParseError, SqlglotError):
        _fail("rewritten SQL does not re-parse")
    if len(reparsed) != 1 or reparsed[0].sql(dialect=DIALECT, comments=False) != out:
        _fail("rewritten SQL is not stable under re-parse")
    _enforce(reparsed[0], allowed_tables)
    return out
