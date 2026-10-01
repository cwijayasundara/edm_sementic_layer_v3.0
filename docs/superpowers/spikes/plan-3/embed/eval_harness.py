"""Retrieval-quality eval: BM25 vs dense vs hybrid over governed metrics + glossary.

hit@k  = at least one expected target in top k (reported as recall@k)
all@k  = every expected target in top k (strict; matters for multi-concept questions)
MRR    = 1 / rank of the first expected target (0 if not in the list)
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

from corpus import load_corpus
from embedder import Embedder, EmbedderConfig
from retrieval import HybridRetriever

HERE = Path(__file__).parent
CACHE = str(HERE / "models_cache")
MODELS = ["BAAI/bge-small-en-v1.5", "BAAI/bge-base-en-v1.5",
          "sentence-transformers/all-MiniLM-L6-v2", "snowflake/snowflake-arctic-embed-s",
          "nomic-ai/nomic-embed-text-v1.5-Q"]


def load_questions(metric_only: bool = False) -> list[dict]:
    qs = yaml.safe_load((HERE / "eval_questions.yaml").read_text())["questions"]
    if metric_only:
        qs = [dict(q, targets=[t for t in q["targets"] if t.startswith("metric:")]) for q in qs]
        qs = [q for q in qs if q["targets"]]
    return qs


def score(ranker, questions: list[dict]) -> dict:
    agg = defaultdict(float)
    per_cat: dict[str, list[float]] = defaultdict(list)
    fails = []
    for q in questions:
        ranking = ranker(q["q"])
        tg = set(q["targets"])
        ranks = [ranking.index(t) + 1 for t in tg if t in ranking]
        first = min(ranks) if ranks else 10**9
        for k in (1, 3, 5):
            agg[f"r@{k}"] += first <= k
        agg["all@5"] += all(t in ranking[:5] for t in tg)
        agg["mrr"] += 1.0 / first if ranks else 0.0
        per_cat[q["category"]].append(float(first <= 3))
        if first > 5:
            fails.append({"q": q["q"], "targets": q["targets"], "category": q["category"],
                          "top5": ranking[:5], "first_rank": first if ranks else None})
    n = len(questions)
    res = {k: round(v / n, 3) for k, v in agg.items()}
    res["by_cat_r@3"] = {c: round(sum(v) / len(v), 2) for c, v in sorted(per_cat.items())}
    res["fails"] = fails
    return res


def run(models: list[str] = MODELS, metric_only_check: bool = True) -> dict:
    docs = load_corpus()
    qs = load_questions()
    results: dict[str, dict] = {}
    base = HybridRetriever(docs)
    results["bm25"] = score(base.bm25, qs)
    for m in models:
        e_on = Embedder(EmbedderConfig(model_name=m, cache_dir=CACHE, offline=False)).load()
        t0 = time.perf_counter()
        r_on = HybridRetriever(docs, e_on)
        t_docs = time.perf_counter() - t0
        e_off = Embedder(EmbedderConfig(model_name=m, cache_dir=CACHE, offline=False,
                                        use_query_prefix=False)).load()
        r_off = HybridRetriever(docs, e_off, doc_vectors=r_on.doc_vectors)
        short = m.split("/")[-1]
        results[f"{short} | dense no-prefix"] = score(r_off.dense, qs)
        results[f"{short} | dense +prefix"] = score(r_on.dense, qs)
        results[f"{short} | hybrid RRF k=60 no-prefix"] = score(r_off.rrf, qs)
        results[f"{short} | hybrid RRF k=60 +prefix"] = score(r_on.rrf, qs)
        for a in (0.3, 0.5, 0.7):
            results[f"{short} | weighted a={a} +prefix"] = score(lambda q, a=a: r_on.weighted(q, a), qs)
        results[f"{short} | weighted a=0.5 no-prefix"] = score(lambda q: r_off.weighted(q, 0.5), qs)
        print(f"# {m}: embedded {len(docs)} docs in {t_docs:.2f}s", file=sys.stderr)
    if metric_only_check:
        m = "BAAI/bge-small-en-v1.5"
        e = Embedder(EmbedderConfig(model_name=m, cache_dir=CACHE, offline=False)).load()
        qm = load_questions(metric_only=True)
        r_full = HybridRetriever(docs, e)
        r_met = HybridRetriever([d for d in docs if d.kind == "metric"], e)
        results["X-check bge-small RRF | metric-only corpus (metric targets)"] = score(r_met.rrf, qm)
        results["X-check bge-small RRF | metric+glossary corpus (metric targets)"] = score(r_full.rrf, qm)
        results["X-check bge-small w0.5 | metric-only corpus (metric targets)"] = score(
            lambda q: r_met.weighted(q, 0.5), qm)
        results["X-check bge-small w0.5 | metric+glossary corpus (metric targets)"] = score(
            lambda q: r_full.weighted(q, 0.5), qm)
    return results


def table(results: dict) -> str:
    lines = ["| setup | R@1 | R@3 | R@5 | all@5 | MRR | R@3 by category |", "|---|---|---|---|---|---|---|"]
    for name, r in results.items():
        cats = " ".join(f"{c[:4]}={v}" for c, v in r["by_cat_r@3"].items())
        lines.append(f"| {name} | {r['r@1']} | {r['r@3']} | {r['r@5']} | {r['all@5']} | {r['mrr']} | {cats} |")
    return "\n".join(lines)


if __name__ == "__main__":
    res = run()
    (HERE / "out_eval_results.json").write_text(json.dumps(res, indent=1))
    t = table(res)
    (HERE / "out_eval_table.md").write_text(t + "\n")
    print(t)
