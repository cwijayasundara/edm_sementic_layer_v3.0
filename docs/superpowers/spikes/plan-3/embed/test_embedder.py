"""Acceptance tests for the chosen embedding setup (bge-small-en-v1.5 + query prefix, offline)."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from corpus import load_corpus
from embedder import BGE_QUERY_PREFIX, Embedder, EmbedderConfig, EmbedderError, to_neo4j
from eval_harness import load_questions, score
from retrieval import HybridRetriever, tokenize

HERE = Path(__file__).parent
CACHE = str(HERE / "models_cache")
MODEL = "BAAI/bge-small-en-v1.5"

# Thresholds (measured: dense R@3=0.95 R@5=1.0; typed-metric R@3=1.0; RRF R@5=0.95; BM25 R@3=0.85)
MIN_DENSE_R3, MIN_DENSE_R5, MIN_DENSE_MRR = 0.93, 0.98, 0.85
MIN_TYPED_METRIC_R3 = 0.98
MIN_RRF_R5 = 0.93


@pytest.fixture(scope="session")
def emb() -> Embedder:
    e = Embedder(EmbedderConfig(model_name=MODEL, cache_dir=CACHE, offline=True)).load()
    e.warmup()
    return e


@pytest.fixture(scope="session")
def retriever(emb) -> HybridRetriever:
    return HybridRetriever(load_corpus(), emb)


# ---------------------------------------------------------------- basics
def test_warmup_validates_dim_and_norm(emb):
    info = emb.warmup()
    assert info["dim"] == 384 and abs(info["norm"] - 1) < 1e-3


def test_fastembed_query_embed_does_not_add_bge_prefix(emb):
    q = "open breaks by entity"
    m = emb._model
    assert np.array_equal(next(iter(m.query_embed(q))), next(iter(m.embed([q]))))
    # ...so Embedder adds it itself:
    assert np.array_equal(emb.embed_query(q), next(iter(m.embed([BGE_QUERY_PREFIX + q]))))


def test_documents_batch_shape_dtype_norm(emb):
    V = emb.embed_documents(["a", "b c", ""])
    assert V.shape == (3, 384) and V.dtype == np.float32
    assert np.allclose(np.linalg.norm(V, axis=1), 1, atol=1e-3)
    assert emb.embed_documents([]).shape == (0, 384)


# ---------------------------------------------------------------- robustness: must never raise
NASTY = ["", "   ", None, 12345, "breeaks", "OPEN BREAKS", "open breaks?!...", "x" * 50_000,
         "word " * 5000, "ünïcødé 寄存器 ценa ✓ 🚀", "\x00\x01\x1f​", 'AND OR ( ) : " ^ ~ * ? \\ / [ ] { } ! - + && ||',
         "status:open AND (age_days:>5 OR ccy:\"USD\")^2~ *", "'; DROP TABLE breaks; --", "\n\t\r"]


@pytest.mark.parametrize("text", NASTY, ids=range(len(NASTY)))
def test_embed_query_never_raises(emb, retriever, text):
    v = emb.embed_query(text)
    assert v.shape == (384,) and np.all(np.isfinite(v))
    assert isinstance(tokenize(text if isinstance(text, str) else ""), list)
    assert len(retriever.rrf(text if isinstance(text, str) else "")) > 0


def test_case_and_whitespace_hit_cache(emb):
    a = emb.embed_query("Open Breaks  by entity")
    before = emb.hits
    b = emb.embed_query("  open breaks by ENTITY ")
    assert emb.hits == before + 1 and np.array_equal(a, b)


def test_cache_hit_rate_on_repeated_eval_questions():
    e = Embedder(EmbedderConfig(model_name=MODEL, cache_dir=CACHE, cache_size=1000)).load()
    qs = [q["q"] for q in load_questions()]
    for q in qs + [q.upper() for q in qs] + [f" {q} " for q in qs]:
        e.embed_query(q)
    assert e.cache_info()["hit_rate"] >= 0.66


def test_lru_evicts():
    e = Embedder(EmbedderConfig(model_name=MODEL, cache_dir=CACHE, cache_size=2)).load()
    for q in ("a", "b", "c"):
        e.embed_query(q)
    assert e.cache_info()["size"] == 2


# ---------------------------------------------------------------- determinism / storage
def test_deterministic_across_processes(emb):
    code = ("import numpy as np,sys;sys.path.insert(0,%r);from embedder import *;"
            "e=Embedder(EmbedderConfig(cache_dir=%r)).load();sys.stdout.buffer.write(e.embed_query('aged breaks').tobytes())"
            % (str(HERE), CACHE))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True).stdout
    assert out == emb.embed_query("aged breaks").tobytes()


def test_neo4j_rounding_6dp_keeps_top5(emb, retriever):
    D6 = np.array([to_neo4j(v, 6) for v in retriever.doc_vectors])
    for q in (x["q"] for x in load_questions()):
        qv = emb.embed_query(q)
        a = np.argsort(-(retriever.doc_vectors @ qv))[:5]
        b = np.argsort(-(D6 @ np.array(to_neo4j(qv, 6))))[:5]
        assert list(a) == list(b), q
    assert all(type(x) is float for x in to_neo4j(retriever.doc_vectors[0]))


# ---------------------------------------------------------------- retrieval quality thresholds
def test_recall_dense_full_corpus(retriever):
    r = score(retriever.dense, load_questions())
    assert r["r@3"] >= MIN_DENSE_R3 and r["r@5"] >= MIN_DENSE_R5 and r["mrr"] >= MIN_DENSE_MRR, r


def test_recall_typed_metric_retrieval(retriever):
    only_metrics = lambda q: [d for d in retriever.dense(q) if d.startswith("metric:")]
    r = score(only_metrics, load_questions(metric_only=True))
    assert r["r@3"] >= MIN_TYPED_METRIC_R3, r


def test_recall_hybrid_rrf(retriever):
    r = score(retriever.rrf, load_questions())
    assert r["r@5"] >= MIN_RRF_R5, r


def test_dense_beats_bm25_on_paraphrases(retriever):
    qs = [q for q in load_questions() if q["category"] == "paraphrase"]
    assert score(retriever.dense, qs)["r@3"] > score(retriever.bm25, qs)["r@3"]


# ---------------------------------------------------------------- concurrency
def test_async_concurrent_calls_bit_identical(emb):
    qs = [q["q"] for q in load_questions()]
    ref = {q: next(iter(emb._model.embed([BGE_QUERY_PREFIX + q.lower()]))) for q in qs}
    e = Embedder(EmbedderConfig(model_name=MODEL, cache_dir=CACHE, cache_size=0, max_concurrency=8)).load()

    async def go():
        return await asyncio.gather(*(e.aembed_query(q) for q in qs * 4))
    for q, v in zip(qs * 4, asyncio.run(go())):
        assert np.array_equal(v, ref[q])


# ---------------------------------------------------------------- offline / startup failures
PROBE = ("import sys;sys.path.insert(0,%r);from embedder import *;"
         "e=Embedder(EmbedderConfig(cache_dir=%r)).load();print(e.warmup()['dim'])")


def _offline_env():
    env = dict(os.environ, HF_HUB_OFFLINE="1", HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    env.pop("FASTEMBED_CACHE_PATH", None)
    return env


def test_works_offline_with_dead_proxy():
    out = subprocess.run([sys.executable, "-c", PROBE % (str(HERE), CACHE)], env=_offline_env(),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "384", out.stderr[-500:]


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="macOS sandbox-exec needed to hard-block network")
def test_works_with_network_hard_blocked():
    prof = HERE / "nonet.sb"
    out = subprocess.run(["sandbox-exec", "-f", str(prof), sys.executable, "-c", PROBE % (str(HERE), CACHE)],
                         env=_offline_env(), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "384", out.stderr[-500:]


def test_missing_model_fails_fast_with_clear_error():
    t0 = time.perf_counter()
    with pytest.raises(EmbedderError, match="Could not load embedding model"):
        Embedder(EmbedderConfig(model_name=MODEL, cache_dir=tempfile.mkdtemp())).load()
    assert time.perf_counter() - t0 < 5


def test_corrupt_model_fails_fast_with_clear_error(tmp_path):
    shutil.copytree(CACHE, tmp_path / "c", symlinks=False)
    f = next((tmp_path / "c").glob("models--Qdrant--bge-small-en-v1.5-onnx-Q/snapshots/*/model_optimized.onnx"))
    data = f.read_bytes(); f.unlink(); f.write_bytes(data[: len(data) // 2])
    with pytest.raises(EmbedderError, match="INVALID_PROTOBUF|Could not load"):
        Embedder(EmbedderConfig(model_name=MODEL, cache_dir=str(tmp_path / "c"))).load()


def test_unconfigured_cache_dir_refused(monkeypatch):
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    with pytest.raises(EmbedderError, match="cache dir not configured"):
        Embedder(EmbedderConfig(model_name=MODEL)).load()
