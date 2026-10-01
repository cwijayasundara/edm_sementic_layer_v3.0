import re
from pathlib import Path

import pytest
import yaml

from prism.graph.knowledge import KNOWLEDGE_DIR, Knowledge, load_knowledge, plan_result_leaks
from prism.graph.model import load_ddl
from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics
from prism.mcp.rest_backend import load_rest_config

REPO = Path(__file__).resolve().parents[3]
EVAL_QUESTIONS = REPO / "docs/superpowers/spikes/plan-3/embed/eval_questions.yaml"
# Held as split strings so this file does not itself contain the banned names.
BANNED = ["gre" + "sham", "vir" + "tusa"]


@pytest.fixture(scope="module")
def k() -> Knowledge:
    return load_knowledge()


@pytest.fixture(scope="module")
def ddl() -> dict[str, dict[str, set[str]]]:
    """source -> table -> columns, from the graph loader's DDL parser (public tables and masking views)."""
    return {s: {t: {c for c, _ in spec.columns} for t, spec in tables.items()} for s, tables in load_ddl().items()}


def _metric_ids() -> set[str]:
    ids = set(load_metrics(DEFAULT_METRICS_DIR))
    for source in ("refmaster", "marketmaster"):
        ids |= set(load_rest_config(source)[1])
    return ids


def test_loads_into_models(k):
    assert len(k.concepts) == 10
    assert len(k.concept_relations) == 9
    assert 40 <= len(k.terms) <= 70
    assert len(k.history) == 12
    assert {t.glossary for t in k.terms} >= {"cash", "assets", "feeds", "reference", "market"}


def test_all_governed_metrics_are_defined_by_a_term(k):
    defined = {r.removeprefix("metric:") for t in k.terms for r in t.defines if r.startswith("metric:")}
    assert len(_metric_ids()) == 22
    assert defined == _metric_ids()


def test_metric_refs_resolve(k):
    ids = _metric_ids()
    refs = {r.removeprefix("metric:") for t in k.terms for r in t.defines if r.startswith("metric:")}
    refs |= {m for q in k.history for m in q.metrics}
    assert refs <= ids


def test_unknown_metric_ref_is_rejected(tmp_path):
    for name in ("ontology.yaml", "glossary.yaml", "history.yaml"):
        (tmp_path / name).write_text((KNOWLEDGE_DIR / name).read_text())
    (tmp_path / "history.yaml").write_text(
        "questions:\n  - {question: q, metrics: [no_such_metric], plan: p, status: verified}\n")
    with pytest.raises(ValueError, match="no_such_metric"):
        load_knowledge(tmp_path)


def _write_variant(tmp_path, *, ontology=None, glossary=None):
    """Copy the knowledge dir to tmp_path, applying text edits (old, new) to the given files."""
    for name, edit in (("ontology.yaml", ontology), ("glossary.yaml", glossary), ("history.yaml", None)):
        text = (KNOWLEDGE_DIR / name).read_text()
        if edit:
            assert edit[0] in text
            text = text.replace(*edit, 1)
        (tmp_path / name).write_text(text)
    return tmp_path


@pytest.mark.parametrize("variant, message", [
    (dict(glossary=("{name: late feed,", "{name: Break,")), "duplicate term name 'break'"),
    (dict(glossary=("synonyms: [delayed file,", "synonyms: [cash break, delayed file,")), "duplicate synonym 'cash break'"),
    (dict(glossary=("synonyms: [ticket,", "synonyms: [Break, ticket,")), "also a term name"),
    (dict(ontology=("[Security, ISSUED_BY, LegalEntity]", "[Security, LegalEntity]")), "source, relation, target"),
    (dict(ontology=("[Price, PRICE_OF, Security]", "[Price, PRICE_OF, Security, extra]")), "source, relation, target"),
    (dict(ontology=("column:refmaster.securities.isin, column:marketmaster.instruments.isin",
                    "column:refmaster.securities.isin, marketmaster.instruments.isin")), "malformed"),
    (dict(glossary=("column:feedhub.sources.source_id", "feedhub.sources.source_id")), "malformed tags"),
    (dict(glossary=("broader: break,", "broader: no such term,")), "unknown broader"),
    (dict(glossary=("{name: currency, glossary: cash,", "{name: currency, glossary: cash, public: true,")),
     "term currency: public terms must not have defines or tags"),
    (dict(glossary=("{name: SLA, glossary: feeds, public: true,", "{name: SLA, glossary: feeds,")),
     "term SLA: no defines or tags; link it or mark it public: true"),
])
def test_invalid_knowledge_is_rejected(tmp_path, variant, message):
    with pytest.raises(ValueError, match=message):
        load_knowledge(_write_variant(tmp_path, **variant))


@pytest.mark.parametrize("plan, leak", [
    ("run_metric(late_feeds, dims=[source_id]); SRC001 leads", "SRC001"),
    ("run_metric(aged_open_breaks, dims=[legal_entity_id]); LE00016 is top", "LE00016"),
    ("run_metric(nav_break_bps_max, dims=[portfolio_id]); worst is PF_003", "PF_003"),
    ("run_metric(aged_open_breaks); the top entity has 64 of 79", "64 of 79"),
    ("run_metric(price_conflicts); share is 66.7% of the total", "66.7%"),
    ("run_metric(price_conflicts); total went from 75 to 90", "75 to 90"),
    ("run_metric(position_exceptions); about 20 a day", "about 20"),
    ("run_metric(position_exceptions); ~20 exceptions per day", "20 exceptions per day"),
])
def test_plan_result_leaks_are_found(plan, leak):
    assert leak in " ".join(plan_result_leaks(plan))


@pytest.mark.parametrize("plan", [
    "run_metric(late_feeds, dims=[source_id], time_range={last_business_days: 6}) ordered by count",
    "run_metric(price_conflicts) over the trailing 5 business days and the 5 before for week over week",
    "run_metric(aged_open_breaks, filters={ccy: USD}); aged = status <> 'closed' AND age_days > 5",
    "run_metric(nav_breaches_above_5bps, dims=[portfolio_id]); a breach is a NAV gap above 5 bps",
    "run_metric(open_breaks, dims=[region]) for the last 7 days",
])
def test_method_only_plans_pass(plan):
    assert plan_result_leaks(plan) == []


def test_history_plan_with_results_is_rejected(tmp_path):
    _write_variant(tmp_path)
    (tmp_path / "history.yaml").write_text(
        "questions:\n  - {question: q, metrics: [late_feeds], plan: 'run_metric(late_feeds); SRC001 leads',"
        " status: verified}\n")
    with pytest.raises(ValueError, match="method only.*SRC001"):
        load_knowledge(tmp_path)


def test_bad_history_status_is_rejected(tmp_path):
    _write_variant(tmp_path)
    (tmp_path / "history.yaml").write_text(
        "questions:\n  - {question: q, metrics: [open_breaks], plan: p, status: maybe}\n")
    with pytest.raises(ValueError, match="status"):
        load_knowledge(tmp_path)


PUBLIC_TERMS = {"SLA", "fund administrator", "basis point", "security master", "corporate action", "exception",
                "data quality rule", "data domain", "data steward", "four-eyes approval", "data lineage",
                "stale price", "tolerance", "week over week", "business day"}


def test_public_terms_are_explicit_and_unlinked(k):
    public = {t.name for t in k.terms if t.public}
    assert public == PUBLIC_TERMS
    assert all(not t.defines and not t.tags for t in k.terms if t.public)
    assert all(t.defines or t.tags for t in k.terms if not t.public)


def test_term_lookup_is_case_insensitive(k):
    by_key = {t.key: t for t in k.terms}
    assert by_key["nav break"].name == "NAV break"


def test_concept_refs_resolve(k):
    names = {c.name for c in k.concepts}
    assert {r.removeprefix("concept:") for t in k.terms for r in t.defines if r.startswith("concept:")} <= names
    assert names <= {r.removeprefix("concept:") for t in k.terms for r in t.defines}  # every concept has a term
    assert all(r.source in names and r.target in names for r in k.concept_relations)
    assert {t.broader for t in k.terms if t.broader} <= {t.name for t in k.terms}


def test_column_and_table_refs_exist_in_ddl(k, ddl):
    def check(ref: str):
        kind, _, rest = ref.partition(":")
        parts = rest.split(".")
        if kind == "endpoint":
            assert parts[1] in load_rest_config(parts[0])[0], ref
            return
        assert parts[1] in ddl[parts[0]], ref
        if kind == "column":
            assert parts[2] in ddl[parts[0]][parts[1]], ref

    refs = [r for c in k.concepts for r in c.implemented_by + c.identified_by]
    refs += [r for t in k.terms for r in t.tags]
    refs += [r for group in k.same_key for r in group]
    assert refs
    for r in refs:
        check(r)
    assert all(len(g) >= 2 and all(r.startswith("column:") for r in g) for g in k.same_key)


def test_same_key_declarations(k):
    groups = [set(g) for g in k.same_key]
    assert any({"column:refmaster.securities.security_id", "column:marketmaster.instruments.security_id",
                "column:assetrecon.internal_positions.security_id"} <= g for g in groups)
    assert any({"column:refmaster.legal_entities.entity_id", "column:cashrecon.breaks.legal_entity_id"} <= g
               for g in groups)
    assert any({"column:feedhub.sources.source_id", "column:assetrecon.custodians.feed_source_id",
                "column:cashrecon.cash_accounts.bank_source_id"} <= g for g in groups)


def test_seed_history(k):
    assert len(k.history) == 12
    assert all(q.status == "verified" and q.metrics and q.plan for q in k.history)
    # Plans are method only: the graph gate is scope-only, so a row-restricted caller would read any result in them.
    plans = " ".join(q.plan for q in k.history)
    for needle in ("Vendor A", "V_A", "Corp bond", "SRC001", "PF001", "PF002", "PF005", "LE00016", "60 of 90",
                   "64 of 79", "75 to 90", "%", "leads"):
        assert needle not in plans
    assert all(not plan_result_leaks(q.plan) for q in k.history)
    assert [q.metrics[0] for q in k.history[:4]] == [
        "price_conflicts", "late_feeds", "position_exceptions", "aged_open_breaks"]


def test_seed_plans_use_only_what_the_gateway_accepts(k):
    """run_metric takes metric_id, dimensions, filters and limit (no time_range): a date window is a group-by on the
    metric's date dimension (as the live e2e does), and every dims=[...] name is a dimension of that metric."""
    from prism.gateway.service import RunMetricArgs
    from tests.gateway.fakes import registry_catalog

    assert "time_range" not in RunMetricArgs.model_fields
    catalog = registry_catalog()
    for q in k.history:
        assert "time_range" not in q.plan and "last_business_days" not in q.plan, q.question
        for metric, dims in re.findall(r"run_metric\((\w+), dims=\[([^\]]*)\]", q.plan):
            assert {d.strip() for d in dims.split(",")} <= set(catalog.metrics[metric].dimensions), (metric, dims)


def test_vocabulary_definitions(k):
    by_name = {t.name: t for t in k.terms}
    assert by_name["aged break"].rule == "status <> 'closed' AND age_days > 5"
    assert "trailing 5 business days" in by_name["week over week"].definition
    assert "WoW" in by_name["week over week"].synonyms
    assert "'transaction'" in by_name["recon type"].definition
    for name in ("golden copy", "price conflict", "stale price", "late feed", "NAV break", "four-eyes approval",
                 "auto-match rate", "LEI", "ISIN"):
        assert name in by_name


def test_eval_set_targets_are_reachable(k):
    targets = {t for q in yaml.safe_load(EVAL_QUESTIONS.read_text())["questions"] for t in q["targets"]}
    metric_ids = _metric_ids()
    term_names = {t.name for t in k.terms}
    for target in targets:
        kind, _, name = target.partition(":")
        if kind == "metric":
            assert name in metric_ids, target
        else:
            assert kind == "term" and name in term_names, target
    term_defines = {r.removeprefix("metric:") for t in k.terms for r in t.defines}
    assert {t.split(":", 1)[1] for t in targets if t.startswith("metric:")} <= term_defines


def test_no_banned_names_in_knowledge_dir():
    for path in KNOWLEDGE_DIR.iterdir():
        if path.is_file():
            text = path.read_text().lower()
            assert not [b for b in BANNED if b in text], path.name
