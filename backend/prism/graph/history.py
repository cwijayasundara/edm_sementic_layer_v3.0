"""Query-history distillation: verified, metric-backed rows of `app.query_log` -> the context graph's history layer
(:Question)-[:ANSWERED_BY]->(:Execution)-[:USED]->(:Metric | :Dimension), so search_context can offer what worked
before as few-shot examples (spec §4.4, "Query history").

What `verified` means: a person confirmed the answer. The gateway's record_answer always stores verified=false (with
metric_backed=true when every handle resolves to catalog metrics), and only confirm_answer, which the agent calls when
the user presses "Confirm" in the UI, sets verified=true, and only on the caller's own metric-backed, ok row. So
distilled executions carry status `verified`, like the curated seed history.

Trust rules (the question text is user/agent supplied and is treated as untrusted):
- Source rows: verified = true AND status = 'ok' (in SQL, re-checked here) AND >= 1 metric id, every one of which is
  in the current graph's catalog (an unknown or removed metric skips the whole row). Only `question`, `plan`,
  `metric_ids`, `verified` and `status` are read, plus aggregate counts: never `sub`, `persona`, handles or
  timestamps, so no caller identity can reach a node. The read runs in a READ ONLY transaction (the app role needs SELECT on app.query_log only).
- Plans: never the caller's text. The structured plan's dimension names are kept only when they belong to a metric
  used, and the stored plan line is built here from catalog names (`plan_text`) and must pass `plan_result_leaks`.
- Question text (`clean_question`): any zero-width / bidi character or non-ASCII letter or digit makes it unsafe
  (homoglyphs, fullwidth, CJK, roman numerals); typographic quotes / dashes become ASCII, NFKC folds odd spaces,
  control characters become blanks, whitespace collapses, at most MAX_QUESTION_CHARS. A row is SKIPPED (never
  "cleaned" into the graph) when the text leaves the ASCII allowlist (letters, digits, blank, ? , . - '), has a word
  over MAX_WORD_CHARS or mixed case inside a word (encoded payloads), names a tool, an SQL verb or an instruction-like
  phrase, carries a bare domain (a dot glued between letters) or spells out a link (`unsafe_text`); or when it carries values (`result_values`: plan_result_leaks, ids,
  more than one digit, number words, a lone number that is not a method parameter). Digit-free entity names
  ("Vendor A") cannot be told from vocabulary: a residual risk, bounded by the distinct-caller rule and the gate.
  The text is stored only as data (`name`/`text`), never in an instruction-bearing field, and consumers (the
  agent) must quote examples as data, never follow them as instructions.
- Row scope (D5b): the gate is scope-only, so a question naming a row-scope value a role is restricted to (EMEA,
  bank, Growth, custodian, ...: `row_scope_terms`, read from the catalog roles at run time) is skipped
  (`row_scope_term`). Residual risk: values no role is restricted to (APAC, Income, ...) are not in the catalog and
  pass, and so do entity names; the distinct-caller rule and the value rules bound that.
- Scopes: like the seed history (model._add_knowledge), allowed_scopes is the UNION of the used objects' scopes as an
  any-of prefilter; retrieval's gate (and model.visible) additionally requires EVERY object an Execution USED to be
  visible. Together: a past question is shown only to callers who could run all of it. Executions also USE the
  Dimension nodes they grouped by, so a plan that names a sensitive dimension stays hidden from metrics-only callers.
- Callers (D1): the SQL groups rows by (question, plan, metrics) and returns only aggregate counts: count(DISTINCT
  sub) per group and per lower-cased question. A question, and each of its plans, needs `min_callers`
  (settings.history_min_callers, default 2) distinct callers (`few_callers`), so one caller cannot plant examples or
  plans for everyone; raw variants folded into one question take the MAX caller count, never the sum. A single-user
  demo needs PRISM_HISTORY_MIN_CALLERS=1. Distilled questions add examples only, never metrics, and rank after seed
  examples (retrieval).
- Caps (D2): each caller contributes only its MAX_QUESTIONS_PER_CALLER most recent questions (SQL row_number over
  sub; `caller_cap`), the read window is ordered by distinct callers, then rows, then recency (never by recency
  alone), and at most MAX_QUESTIONS Questions are kept (`question_cap`), best attested first. A flood of well-attested
  paraphrases therefore cannot crowd metrics out of retrieval (tested: metric recall@3 holds with 2000 paraphrases).
- Identity: one Question per normalised question text, keyed by an HMAC (audit_hmac_key) of it, whoever asked; its
  stored text is that canonical lower-cased form; one Execution per distinct (metrics, dimensions) plan; `count` (on
  both) is the number of distinct callers, recomputed from the whole log on every run.
- Lifecycle: nodes carry origin = "history" and the namespace's current loaded_version, so the gateway's catalog
  version does not move. Each run rebuilds the history layer in ONE write transaction (stale history nodes removed,
  edges re-created), so distilling twice gives the same graph. A graph load (`make graph`) supersedes history nodes
  like every other older node: run `distill` after every load. Do not run it concurrently with a load.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from neo4j import unit_of_work
from psycopg.rows import dict_row

from prism.config import Settings
from prism.gateway.audit import question_hash
from prism.graph.catalog import Catalog, load_catalog
from prism.graph.embedder import Embedder
from prism.graph.knowledge import plan_result_leaks
from prism.graph.loader import TX_TIMEOUT_S, check_ns, production_ns, uid_for
from prism.graph.model import search_text

ORIGIN = "history"
STATUS = "verified"
MAX_QUESTION_CHARS = 200
MAX_RAW_CHARS = 4 * MAX_QUESTION_CHARS
MAX_WORD_CHARS = 30
MAX_ROWS = 100_000            # grouped rows read per run (best attested first)
MAX_QUESTIONS = 300           # distilled Questions kept per run (most distinct callers, then rows, then recency)
MAX_QUESTIONS_PER_CALLER = 20  # distinct questions one caller contributes per run (its most recent), in SQL
MAX_METRICS_PER_ROW = 16

# Question text alphabet (D3): ASCII letters, digits, blank and ? , . - ' only. No ':' '/' '\\' '@' '_' brackets or
# quotes, so no URL, scheme, path, e-mail, tool name, markup or "role: text" line can be written with it.
_ALLOWED = re.compile(r"[A-Za-z0-9 ?,.'-]+")
_TYPOGRAPHIC = str.maketrans({"\u2018": "'", "\u2019": "'", "\u2032": "'", "\u2010": "-", "\u2011": "-",
                              "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-"})
_INSTRUCTION = re.compile(
    r"\b(?:ignor\w*|disregard\w*|forget\w*|pretend\w*|jailbreak\w*|instruc\w*|prompt\w*|overrid\w*|bypass\w*|"
    r"obey\w*|reveal\w*|exfiltrat\w*|dump\w*|export\w*|print\w*|execut\w*|exec|invok\w*|call|calls|calling|"
    r"sql|select|drop|delete|insert|truncate|alter|grant|password\w*|passwd|secret\w*|token\w*|credential\w*|"
    r"api\s*keys?|configuration|config|assistant|developer|system\s+(?:prompt|message|policy|note)|new\s+polic\w*|"
    r"act\s+as|you\s+are|you're|youre|you\s+must|from\s+now\s+on|respond\w*|"
    r"combine|(?:query|run|get|search|record)[\s-]*(?:source|metric|rows|context|answer)|"
    r"https?|www|javascript|script|mailto|ftp|"
    r"dot\s+(?:com|net|org|io|co|uk|ru|cn|xyz|info|biz|dev|app)|(?:back)?slash|colon)\b"
    r"|\b(?:[a-z]\s+){2,}[a-z]\b",           # spelled-out words: "i g n o r e"
    re.IGNORECASE)
_MIXED_CASE = re.compile(r"[a-z][A-Z]")      # a capital after a small letter inside a word: base64 / encoded text
# a dot glued between a letter/digit and a letter: a bare domain or file name (evil.example.com, docs.example.io);
# also rejects "e.g." / "U.S." (acceptable). Digit.digit is a value (result_values), not matched here.
_DOMAIN = re.compile(r"[A-Za-z0-9]\.[A-Za-z]")
_QUESTION_LEAKS = (
    re.compile(r"\d{2,}"),                                   # multi-digit numbers: counts, amounts, ids
    re.compile(r"\d[.,]\d"),                                 # decimals and thousands
    re.compile(r"\b(?=\w*\d)(?=\w*[A-Za-z])\w+\b"),          # letter+digit tokens: ids, handles, hex
    re.compile(r"\b[A-Z]+_[A-Za-z0-9]+\b"),                  # coded ids such as V_A
    re.compile(r"@|https?://|www\."),                        # contact details and links
)
# D4: numbers written out. One number (a digit or one of these words) is allowed only as a method parameter: right
# after a comparator ("above 5 bps", "older than five days", "top 3") or right before a unit ("5 days"). Any second
# number in the text, or a number elsewhere ("7 open breaks", "CPTY one"), reads as a result value.
NUMBER_WORDS = frozenset((
    "zero nil none one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred hundreds thousand "
    "thousands million millions billion billions trillion trillions dozen dozens half quarter thirds percent "
    "first second third fourth fifth sixth seventh eighth ninth tenth"
).split()) - {"none", "quarter", "second", "first", "percent", "half"}
_PARAM_BEFORE = frozenset("above below over under than top last past next within exceeding least most beyond "
                          "after before".split())
_UNITS = frozenset("bps bp basis day days business week weeks month months year years hour hours minute minutes "
                   "min mins percent pct".split())
_WORD = re.compile(r"[A-Za-z]+|\d+")
DEFAULT_MIN_CALLERS = 2       # settings.history_min_callers
# One row per (question text, plan, metrics) group of eligible rows with aggregate counts only: `sub` is never selected,
# only count(DISTINCT sub) per group (`callers`) and per lower-cased, blank-collapsed question (`question_callers`).
# D2: each caller contributes at most %(per_caller)s distinct questions (its most recent; row_number over sub), and
# the window is ordered by distinct callers, then rows, then recency: never by recency alone.
_SELECT = r"""
WITH elig AS (
  SELECT id, sub, question, plan, metric_ids, verified, status,
         coalesce(lower(regexp_replace(btrim(question), '\s+', ' ', 'g')), '') AS qkey
  FROM app.query_log WHERE verified AND status = 'ok'
), per_caller AS (
  SELECT sub, qkey, row_number() OVER (PARTITION BY sub ORDER BY max(id) DESC, qkey) AS rn
  FROM elig GROUP BY sub, qkey
), kept AS (
  SELECT e.* FROM elig e JOIN per_caller p ON p.sub = e.sub AND p.qkey = e.qkey WHERE p.rn <= %(per_caller)s
), questions AS (
  SELECT qkey, count(DISTINCT sub) AS question_callers, count(*) AS question_rows, max(id) AS question_last
  FROM kept GROUP BY qkey
)
SELECT e.question, e.plan, e.metric_ids, e.verified, e.status, count(DISTINCT e.sub) AS callers,
       q.question_callers, count(*) AS n, max(e.id) AS last_id
FROM kept e JOIN questions q ON q.qkey = e.qkey
GROUP BY e.qkey, e.question, e.plan, e.metric_ids, e.verified, e.status, q.question_callers, q.question_rows,
         q.question_last
ORDER BY q.question_callers DESC, q.question_rows DESC, q.question_last DESC, e.qkey, count(DISTINCT e.sub) DESC,
         e.question, e.plan
LIMIT %(limit)s
"""
_CAPPED = r"""
WITH elig AS (
  SELECT id, sub, coalesce(lower(regexp_replace(btrim(question), '\s+', ' ', 'g')), '') AS qkey
  FROM app.query_log WHERE verified AND status = 'ok'
), per_caller AS (
  SELECT sub, qkey, count(*) AS rows, row_number() OVER (PARTITION BY sub ORDER BY max(id) DESC, qkey) AS rn
  FROM elig GROUP BY sub, qkey
)
SELECT coalesce(sum(rows), 0) FROM per_caller WHERE rn > %(per_caller)s
"""
_EXCLUDED = "SELECT count(*) FROM app.query_log WHERE NOT (verified AND coalesce(status, '') = 'ok')"


class DistillError(RuntimeError):
    """The graph cannot take history (empty or no catalog): load it first (`make graph`). Message is caller-safe."""


@dataclass(frozen=True)
class DistilledExecution:
    uid: str                      # local uid "hx:<question hmac>:<plan digest>"
    metrics: list[str]
    dimensions: list[str]
    plan: str                     # built from catalog names; method only
    used: list[str]               # local uids of the Metric / Dimension nodes it used
    allowed_scopes: list[str]
    count: int


@dataclass(frozen=True)
class DistilledQuestion:
    uid: str                      # local uid "hq:<question hmac>"
    text: str
    count: int
    allowed_scopes: list[str]
    executions: list[DistilledExecution] = field(default_factory=list)


@dataclass
class DistillReport:
    version: int | None
    rows: int                     # query_log rows considered (read + excluded as not verified / not ok)
    questions: int
    executions: int
    skipped: dict[str, int]
    deleted: int                  # history nodes removed because their rows no longer qualify
    truncated: bool = False       # more than MAX_ROWS eligible rows: only the most recent were read


# ------------------------------------------------------------------------------------------------ text rules
def question_leaks(text: str) -> list[str]:
    """Fragments of `text` that look like result values or identifiers (stricter than plan_result_leaks, which it
    includes): a question kept as a few-shot example must hold no literal values (spec §4.4 RBAC)."""
    return plan_result_leaks(text) + [m.group(0) for p in _QUESTION_LEAKS for m in p.finditer(text)] + \
        _number_values(text)


def _number_values(text: str) -> list[str]:
    """D4: numbers that read as values: more than one digit in the whole text, more than one number (digit group or
    number word), or a single number that is not a method parameter (comparator before it or unit after it)."""
    if sum(ch.isdigit() for ch in text) > 1:
        return [ch for ch in text if ch.isdigit()]
    words = [w.lower() for w in _WORD.findall(text)]
    at = [i for i, w in enumerate(words) if w.isdigit() or w in NUMBER_WORDS]
    if len(at) > 1:
        return [words[i] for i in at]
    if at:
        i = at[0]
        before = words[i - 1] if i > 0 else ""
        after = words[i + 1] if i + 1 < len(words) else ""
        if before not in _PARAM_BEFORE and after not in _UNITS:
            return [words[i]]
    return []


def clean_question(raw: object) -> tuple[str | None, str | None]:
    """(normalised text, None) or (None, skip reason): no_question | too_long | unsafe_text | result_values.

    The text is untrusted (D3): zero-width / bidi format characters and any non-ASCII letter or digit (homoglyphs,
    fullwidth, CJK, roman numerals, circled digits) make it unsafe; typographic quotes and dashes become ASCII, NFKC
    folds odd spaces, control characters become blanks, whitespace collapses; then the ASCII allowlist, a word-length
    cap and the instruction / link / spelled-out-link patterns apply, and D4's value rules (`question_leaks`)."""
    if not isinstance(raw, str):
        return None, "no_question"
    if len(raw) > MAX_RAW_CHARS:
        return None, "too_long"
    if any(unicodedata.category(ch) == "Cf" or (ch.isalnum() and not ch.isascii()) for ch in raw):
        return None, "unsafe_text"
    text = unicodedata.normalize("NFKC", raw.translate(_TYPOGRAPHIC))
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    text = " ".join(text.split())
    if not text:
        return None, "no_question"
    if len(text) > MAX_QUESTION_CHARS:
        return None, "too_long"
    if (not _ALLOWED.fullmatch(text) or _INSTRUCTION.search(text) or _MIXED_CASE.search(text) or _DOMAIN.search(text)
            or any(len(w) > MAX_WORD_CHARS for w in text.split())):
        return None, "unsafe_text"
    if question_leaks(text):
        return None, "result_values"
    return text, None


def row_scope_terms(catalog: Catalog) -> frozenset[str]:
    """D5b: every row-scope VALUE a catalog role is restricted to (region EMEA, source_type bank, fund_group Growth,
    ...), lower-cased; '*' (all values) is not a term. Read from the catalog at run time."""
    return frozenset(str(v).casefold() for r in catalog.roles.values() for vs in r.row_scope.values() for v in vs
                     if str(v) != "*" and str(v).strip())


def _row_scope_pattern(terms: frozenset[str]) -> re.Pattern | None:
    if not terms:
        return None
    phrases = sorted((r"\s+".join(re.escape(w) for w in t.split()) for t in terms if t.split()), key=len,
                     reverse=True)
    return re.compile(r"\b(?:" + "|".join(phrases) + r")(?:e?s)?\b", re.IGNORECASE)


def question_key(text: str) -> str:
    """Identity of a (cleaned) question: case- and whitespace-insensitive."""
    return " ".join(text.split()).casefold()


def plan_text(steps: list[tuple[str, list[str]]]) -> str:
    """The stored plan line, from catalog metric ids and dimension names only."""
    return " and ".join(f"run_metric({m}, dims=[{', '.join(d)}])" if d else f"run_metric({m})" for m, d in steps)


# ------------------------------------------------------------------------------------------------ rows -> nodes
def _plan_dimensions(plan: object) -> list[str]:
    try:
        parsed = json.loads(plan) if isinstance(plan, str) else None
    except (ValueError, RecursionError):
        return []
    dims = parsed.get("dimensions") if isinstance(parsed, dict) else None
    return [d for d in dims if isinstance(d, str)] if isinstance(dims, list) else []


def _check_row(row: dict, catalog: Catalog) -> tuple[list[str] | None, str | None]:
    if row.get("verified") is not True or row.get("status") != "ok":
        return None, "not_verified"
    ids = row.get("metric_ids")
    if not isinstance(ids, (list, tuple)) or not ids:
        return None, "no_metrics"
    if len(ids) > MAX_METRICS_PER_ROW or not all(isinstance(m, str) and m in catalog.metrics for m in ids):
        return None, "unknown_metric"
    return sorted(set(ids)), None


def _n(row: dict, name: str, default: int = 1) -> int:
    v = row.get(name, default)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else default


def prepare(rows, catalog: Catalog, *, key: str, seeded: frozenset[str] = frozenset(),
            min_callers: int = DEFAULT_MIN_CALLERS, max_questions: int = MAX_QUESTIONS,
            row_scope_terms: frozenset[str] = frozenset()) -> tuple[list[DistilledQuestion], Counter]:
    """Pure: grouped query_log rows (read_rows) -> distilled questions (sorted by uid) and skip counts by reason, in
    query_log rows. `seeded` holds the question_key of seed-history questions already in the graph (a distilled
    duplicate is skipped). A question needs `min_callers` distinct callers, and so does each of its plans
    (`few_callers`); raw variants that fold into one question take the MAX of their caller counts, never the sum.
    At most `max_questions` are kept (`question_cap`): most distinct callers first, then rows, then recency. A
    question naming any of `row_scope_terms` (whole words, plural too, case-insensitive) is skipped
    (`row_scope_term`)."""
    scoped = _row_scope_pattern(row_scope_terms)
    skipped: Counter = Counter()
    callers: dict[str, int] = defaultdict(int)
    rows_of: dict[str, int] = defaultdict(int)
    last: dict[str, int] = defaultdict(int)
    plans: dict[str, dict[tuple, int]] = defaultdict(lambda: defaultdict(int))
    plan_rows: dict[str, dict[tuple, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        n = _n(row, "n")
        metrics, reason = _check_row(row, catalog)
        text = None
        if reason is None:
            text, reason = clean_question(row.get("question"))
        if reason is None and scoped is not None and scoped.search(text):
            reason = "row_scope_term"
        if reason is None and question_key(text) in seeded:
            reason = "seeded"
        if reason is not None:
            skipped[reason] += n
            continue
        known = {d for m in metrics for d in catalog.metrics[m].dimensions}
        dims = sorted({d for d in _plan_dimensions(row.get("plan")) if d in known})
        k = question_key(text)
        plan = (tuple(metrics), tuple(dims))
        row_callers = _n(row, "callers")
        callers[k] = max(callers[k], _n(row, "question_callers", row_callers))
        rows_of[k] += n
        last[k] = max(last[k], _n(row, "last_id", 0))
        plans[k][plan] = max(plans[k][plan], row_callers)
        plan_rows[k][plan] += n
    out = []
    for k in sorted(callers, key=lambda k: (-callers[k], -rows_of[k], -last[k], k)):   # best attested first
        if callers[k] < min_callers:
            skipped["few_callers"] += rows_of[k]
            continue
        attested = {p: c for p, c in plans[k].items() if c >= min_callers}
        skipped.update({"few_callers": sum(r for p, r in plan_rows[k].items() if p not in attested)})
        if not attested:
            continue
        if len(out) >= max_questions:
            skipped["question_cap"] += sum(plan_rows[k][p] for p in attested)
            continue
        h = question_hash(k, key)[:32]
        execs = [_execution(h, metrics, dims, c, catalog) for (metrics, dims), c in attested.items()]
        execs.sort(key=lambda e: e.uid)
        scopes = sorted({s for e in execs for s in e.allowed_scopes})
        out.append(DistilledQuestion(uid=f"hq:{h}", text=k, count=callers[k], allowed_scopes=scopes,
                                     executions=execs))
    out.sort(key=lambda q: q.uid)
    return out, +skipped     # unary plus drops zero counts


def _execution(h: str, metrics: tuple[str, ...], dims: tuple[str, ...], count: int, catalog: Catalog
               ) -> DistilledExecution:
    steps = [(m, [d for d in dims if d in catalog.metrics[m].dimensions]) for m in metrics]
    plan = plan_text(steps)
    if plan_result_leaks(plan):    # catalog names never look like results; fail loudly if one ever does
        raise ValueError("a generated history plan looks like a result; check the metric and dimension names")
    used = sorted({f"metric:{m}" for m in metrics} | {f"dim:{m}.{d}" for m, ds in steps for d in ds})
    digest = hashlib.sha256(json.dumps([list(metrics), list(dims)]).encode()).hexdigest()[:12]
    return DistilledExecution(uid=f"hx:{h}:{digest}", metrics=list(metrics), dimensions=list(dims), plan=plan,
                              used=used, count=count,
                              allowed_scopes=sorted({s for m in metrics for s in catalog.metrics[m].allowed_scopes}))


# ------------------------------------------------------------------------------------------------ Neo4j
_SEEDED = "MATCH (q:Question {ns: $ns}) WHERE q.origin IS NULL RETURN q.text AS text"
_VERSION = "MATCH (n:Ctx {ns: $ns}) WHERE n.origin IS NULL RETURN max(n.loaded_version) AS v"
_STALE = ("MATCH (n:Ctx {ns: $ns}) WHERE n.origin = $origin AND NOT n.uid IN $keep "
          "DETACH DELETE n RETURN count(n) AS c")
_OLD_EDGES = "MATCH (n:Ctx {ns: $ns})-[r]->() WHERE n.origin = $origin DELETE r"
_NODES = {"Question": "UNWIND $rows AS r MERGE (n:Ctx {uid: r.uid, ns: r.ns}) SET n = r, n:Question:Searchable",
          "Execution": "UNWIND $rows AS r MERGE (n:Ctx {uid: r.uid, ns: r.ns}) SET n = r, n:Execution"}
_EDGES = {typ: ("UNWIND $rows AS r MATCH (a:Ctx {uid: r.a, ns: $ns}) MATCH (b:Ctx {uid: r.b, ns: $ns}) "
                f"MERGE (a)-[e:{typ}]->(b) SET e = r.p RETURN count(e) AS n")
          for typ in ("ANSWERED_BY", "USED")}


def _graph_rows(questions: list[DistilledQuestion], vectors: list[list[float]], ns: str, version: int
                ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    base = {"ns": ns, "loaded_version": version, "origin": ORIGIN, "status": STATUS}
    nodes: dict[str, list[dict]] = {"Question": [], "Execution": []}
    edges: dict[str, list[dict]] = {"ANSWERED_BY": [], "USED": []}
    edge_props = {"ns": ns, "loaded_version": version, "origin": ORIGIN}
    for q, vec in zip(questions, vectors, strict=True):
        quid = uid_for(ns, q.uid)
        nodes["Question"].append({**base, "uid": quid, "local_uid": q.uid, "name": q.text, "text": q.text,
                                  "description": _top_plan(q), "synonyms": [], "count": q.count,
                                  "allowed_scopes": q.allowed_scopes, "embedding": vec})
        for e in q.executions:
            euid = uid_for(ns, e.uid)
            nodes["Execution"].append({**base, "uid": euid, "local_uid": e.uid, "tool": "run_metric",
                                       "plan": e.plan, "metrics": e.metrics, "dimensions": e.dimensions,
                                       "count": e.count, "allowed_scopes": e.allowed_scopes})
            edges["ANSWERED_BY"].append({"a": quid, "b": euid, "p": edge_props})
            edges["USED"] += [{"a": euid, "b": uid_for(ns, u), "p": edge_props} for u in e.used]
    return nodes, edges


def _top_plan(q: DistilledQuestion) -> str:
    return max(q.executions, key=lambda e: (e.count, e.uid)).plan


@unit_of_work(timeout=TX_TIMEOUT_S)
def _write_tx(tx, ns: str, questions: list[DistilledQuestion], vectors: list[list[float]]) -> tuple[int, int]:
    version = tx.run(_VERSION, ns=ns).single()["v"]
    if version is None:
        raise DistillError(f"graph namespace {ns!r} is empty: load the context graph first (make graph)")
    nodes, edges = _graph_rows(questions, vectors, ns, version)
    keep = [r["uid"] for rows in nodes.values() for r in rows]
    deleted = tx.run(_STALE, ns=ns, origin=ORIGIN, keep=keep).single()["c"]
    tx.run(_OLD_EDGES, ns=ns, origin=ORIGIN).consume()
    for label, rows in nodes.items():
        if rows:
            tx.run(_NODES[label], rows=rows).consume()
    for typ, rows in edges.items():
        if rows:
            n = tx.run(_EDGES[typ], rows=rows, ns=ns).single()["n"]
            if n != len(rows):   # a USED target missing from the graph: never write a half-linked execution
                raise RuntimeError(f"{typ}: wrote {n} of {len(rows)} relationships")
    return version, deleted


def distill_rows(driver, embedder: Embedder, rows, ns: str | None = None, *, hmac_key: str | None = None,
                 excluded: int = 0, min_callers: int | None = None) -> DistillReport:
    """Rebuild the history layer of `ns` (default settings.graph_ns) from grouped query_log rows (read_rows).
    `excluded` counts rows the caller's SQL already dropped as not verified / not ok (reported under `not_verified`);
    `min_callers` defaults to settings.history_min_callers."""
    ns = production_ns() if ns is None else check_ns(ns)
    if hmac_key is None or min_callers is None:
        settings = Settings()
        hmac_key = settings.audit_hmac_key.get_secret_value() if hmac_key is None else hmac_key
        min_callers = settings.history_min_callers if min_callers is None else min_callers
    key = hmac_key
    question_hash("", key)  # refuse a short key before touching anything
    catalog = load_catalog(driver, ns, timeout_s=30)
    if not catalog.metrics:
        raise DistillError(f"graph namespace {ns!r} has no metrics: load the context graph first (make graph)")
    seeded = frozenset(question_key(r["text"]) for r in driver.execute_query(_SEEDED, ns=ns).records
                       if isinstance(r["text"], str))
    rows = list(rows)
    questions, skipped = prepare(rows, catalog, key=key, seeded=seeded, min_callers=min_callers,
                                 row_scope_terms=row_scope_terms(catalog))
    if excluded:
        skipped["not_verified"] += excluded
    vectors = embedder.embed_documents([search_text({"name": q.text, "description": _top_plan(q)})
                                        for q in questions]) if questions else []
    with driver.session(database="neo4j") as s:
        version, deleted = s.execute_write(_write_tx, ns, questions, vectors)
    return DistillReport(version=version, rows=sum(_n(r, "n") for r in rows) + excluded, questions=len(questions),
                         executions=sum(len(q.executions) for q in questions),
                         skipped={k: v for k, v in sorted(skipped.items()) if v}, deleted=deleted)


# ------------------------------------------------------------------------------------------------ Postgres
def read_rows(conn, limit: int = MAX_ROWS, per_caller: int = MAX_QUESTIONS_PER_CALLER
              ) -> tuple[list[dict], int, int]:
    """(grouped eligible rows, best attested first, at most `limit` + 1; rows excluded as not verified / not ok; rows
    over a caller's `per_caller` most recent questions), read in one READ ONLY transaction through a sync psycopg
    connection (the app role: SELECT on app.query_log)."""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        excluded = conn.execute(_EXCLUDED).fetchone()[0]
        capped = conn.execute(_CAPPED, {"per_caller": per_caller}).fetchone()[0]
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(_SELECT, {"limit": limit + 1, "per_caller": per_caller})
        return cur.fetchall(), int(excluded), int(capped)


def distill(driver, embedder: Embedder, pg, ns: str | None = None, *, hmac_key: str | None = None,
            limit: int = MAX_ROWS, min_callers: int | None = None) -> DistillReport:
    """Read app.query_log through `pg` (a sync psycopg Connection, or a pool with .connection()) and rebuild the
    history layer of `ns` (default settings.graph_ns)."""
    ns = production_ns() if ns is None else check_ns(ns)
    if hasattr(pg, "connection") and not hasattr(pg, "transaction"):
        with pg.connection() as conn:
            rows, excluded, capped = read_rows(conn, limit)
    else:
        rows, excluded, capped = read_rows(pg, limit)
    truncated = len(rows) > limit
    report = distill_rows(driver, embedder, rows[:limit], ns, hmac_key=hmac_key, excluded=excluded,
                          min_callers=min_callers)
    if capped:
        report.skipped = dict(sorted({**report.skipped, "caller_cap": capped}.items()))
        report.rows += capped
    report.truncated = truncated
    return report


__all__ = ["DEFAULT_MIN_CALLERS", "DistillError", "DistillReport", "DistilledExecution", "DistilledQuestion",
           "MAX_QUESTIONS", "MAX_QUESTIONS_PER_CALLER", "MAX_QUESTION_CHARS", "ORIGIN", "STATUS", "clean_question",
           "distill", "distill_rows", "plan_text", "prepare", "question_key", "question_leaks", "read_rows",
           "row_scope_terms"]
