"""Local embedder: bge-small, offline, cached. Model-dependent tests need `make models`."""
import asyncio
import math
import shutil
import subprocess
import sys
import time

import pytest

from prism.config import MODELS_DIR, Settings
from prism.graph.embedder import BGE_QUERY_PREFIX, Embedder, EmbedderError

CACHE = MODELS_DIR
needs_model = pytest.mark.skipif(
    not any(CACHE.glob("models--*/snapshots/*/*.onnx")),
    reason=f"embedding model not cached in {CACHE}: run `make models`",
)

JUNK = ["", None, "   ", "x" * 50_000, "ünïcødé 寄存器 ✓ 🚀 😀", 'AND OR ( ) : "', "\x00\x01\x1f", "\n\t\r", 12345]


@pytest.fixture(autouse=True)
def _restore_offline_env(monkeypatch):
    """load() forces HF_HUB_OFFLINE=1 process-wide; register it so every test restores the original."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture(scope="module")
def emb() -> Embedder:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HF_HUB_OFFLINE", "1")
        e = Embedder(cache_dir=CACHE).load()
        e.warmup()
    return e


def norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def test_settings_default_cache_dir_is_absolute_and_cwd_independent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    p = Settings().embed_cache_dir
    assert p == str(MODELS_DIR) and MODELS_DIR.is_absolute() and MODELS_DIR.parts[-2:] == ("backend", ".models")


# ---------------------------------------------------------------- error paths (no model needed)
def test_missing_cache_dir_config_raises(monkeypatch):
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    with pytest.raises(EmbedderError, match="cache dir not configured"):
        Embedder().load()


def test_empty_cache_fails_fast(tmp_path):
    t0 = time.perf_counter()
    with pytest.raises(EmbedderError, match="make models"):
        Embedder(cache_dir=tmp_path).load()
    assert time.perf_counter() - t0 < 5


def test_unsupported_model_raises(tmp_path):
    with pytest.raises(EmbedderError, match="Unsupported model"):
        Embedder(model="nope/none", cache_dir=tmp_path)


def test_use_before_load_raises(tmp_path):
    e = Embedder(cache_dir=tmp_path)
    for call in (e.warmup, lambda: e.embed_query("a"), lambda: e.embed_documents(["a"])):
        with pytest.raises(EmbedderError, match="before load"):
            call()


@needs_model
def test_truncated_onnx_raises_embedder_error(tmp_path):
    shutil.copytree(CACHE, tmp_path / "c", symlinks=False)
    f = next((tmp_path / "c").glob("models--*/snapshots/*/*.onnx"))
    data = f.read_bytes()
    f.unlink()
    f.write_bytes(data[: len(data) // 2])
    with pytest.raises(EmbedderError, match="Could not load embedding model"):
        Embedder(cache_dir=tmp_path / "c").load()


@needs_model
def test_missing_tokenizer_raises_embedder_error(tmp_path):
    shutil.copytree(CACHE, tmp_path / "c", symlinks=False)
    for f in (tmp_path / "c").glob("models--*/snapshots/*/tokenizer.json"):
        f.unlink()
    with pytest.raises(EmbedderError, match="Could not load embedding model"):
        Embedder(cache_dir=tmp_path / "c").load()


@needs_model
def test_known_model_not_in_cache_raises_at_load():
    with pytest.raises(EmbedderError, match="Could not load embedding model"):
        Embedder(model="BAAI/bge-base-en-v1.5", cache_dir=CACHE).load()


# ---------------------------------------------------------------- behaviour
@needs_model
def test_warmup_reports_dim_and_unit_norm(emb):
    info = emb.warmup()
    assert info["dim"] == 384 and abs(info["norm"] - 1) < 1e-3


@needs_model
def test_query_dim_unit_norm_python_floats(emb):
    v = emb.embed_query("open breaks by entity")
    assert len(v) == 384 and all(type(x) is float for x in v) and abs(norm(v) - 1) < 1e-3
    assert all(x == round(x, 6) for x in v)


@needs_model
def test_query_prefix_is_applied(emb):
    q = "open breaks by entity"
    prefixed = emb.embed_documents([BGE_QUERY_PREFIX + q])[0]
    plain = emb.embed_documents([q])[0]
    v = emb.embed_query(q)
    assert v == prefixed and v != plain


@needs_model
def test_documents_batch(emb):
    docs = emb.embed_documents(["a", "b c", ""])
    assert len(docs) == 3 and all(len(d) == 384 for d in docs)
    assert emb.embed_documents([]) == []


@needs_model
def test_deterministic_across_calls_and_subprocess(emb):
    code = ("from prism.graph.embedder import Embedder;import json,sys;"
            f"print(json.dumps(Embedder(cache_dir={str(CACHE)!r}).load().embed_query('aged breaks')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    import json
    fresh = Embedder(cache_dir=CACHE, cache_size=0).load()
    assert json.loads(out) == emb.embed_query("aged breaks") == fresh.embed_query("aged breaks")


@needs_model
def test_cache_hit_returns_identical_list_and_normalises_key(emb):
    a = emb.embed_query("Open Breaks  by entity")
    before = emb.hits
    b = emb.embed_query("  open breaks by ENTITY ")
    assert emb.hits == before + 1 and a == b


@needs_model
def test_mutating_returned_vector_does_not_corrupt_cache(emb):
    first = emb.embed_query("mutation probe")
    ref = list(first)
    first[0] = 99.0
    first.clear()
    assert emb.embed_query("mutation probe") == ref
    hit = emb.embed_query("mutation probe")
    hit[1] = -5.0
    assert emb.embed_query("mutation probe") == ref


@needs_model
def test_lru_evicts():
    e = Embedder(cache_dir=CACHE, cache_size=2).load()
    for q in ("a", "b", "c"):
        e.embed_query(q)
    assert e.cache_info()["size"] == 2


@needs_model
@pytest.mark.parametrize("text", JUNK, ids=range(len(JUNK)))
def test_junk_input_never_raises(emb, text):
    v = emb.embed_query(text)
    assert len(v) == 384 and all(math.isfinite(x) for x in v)
    assert len(emb.embed_documents([text])[0]) == 384


@needs_model
async def test_gather_equals_sequential():
    e = Embedder(cache_dir=CACHE, cache_size=0).load()
    qs = [f"question number {i} about breaks" for i in range(8)]
    seq = [e.embed_query(q) for q in qs]
    assert await asyncio.gather(*(e.aembed_query(q) for q in qs)) == seq


@needs_model
def test_one_embedder_across_two_event_loops_with_contention():
    e = Embedder(cache_dir=CACHE, cache_size=0).load()
    qs = [f"loop probe {i}" for i in range(8)]
    seq = [e.embed_query(q) for q in qs]

    async def burst():
        return await asyncio.gather(*(e.aembed_query(q) for q in qs))

    assert asyncio.run(burst()) == seq
    assert asyncio.run(burst()) == seq
