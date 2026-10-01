"""Local, offline text embedder for the semantic layer (fastembed / ONNX Runtime, no API key).

Usage (once per process, e.g. FastAPI lifespan):
    emb = Embedder(EmbedderConfig(cache_dir="/opt/prism/models"))
    emb.load(); emb.warmup()           # raises EmbedderError with a clear message on failure
    v = emb.embed_query("open breaks by entity")        # np.float32 (dim,), L2-normalised
    V = emb.embed_documents(["...", "..."])             # np.float32 (n, dim)
    v = await emb.aembed_query("...")                   # async, off the event loop

Production env (set in the Dockerfile ENV, not only in-process: huggingface_hub reads HF_HUB_OFFLINE at import):
    FASTEMBED_CACHE_PATH=/opt/prism/models  HF_HUB_OFFLINE=1  HF_HUB_DISABLE_TELEMETRY=1
and bake the model at build time:  python embedder.py download --cache-dir /opt/prism/models
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    dim: int
    query_prefix: str = ""
    doc_prefix: str = ""
    uncased: bool = True  # tokenizer lowercases -> case-insensitive cache key is safe


# fastembed's query_embed()/passage_embed() do NOT add these prefixes (verified: query_embed == embed).
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MODEL_SPECS: dict[str, ModelSpec] = {
    "BAAI/bge-small-en-v1.5": ModelSpec("BAAI/bge-small-en-v1.5", 384, BGE_QUERY_PREFIX),
    "BAAI/bge-base-en-v1.5": ModelSpec("BAAI/bge-base-en-v1.5", 768, BGE_QUERY_PREFIX),
    "sentence-transformers/all-MiniLM-L6-v2": ModelSpec("sentence-transformers/all-MiniLM-L6-v2", 384),
    "snowflake/snowflake-arctic-embed-s": ModelSpec(
        "snowflake/snowflake-arctic-embed-s", 384, BGE_QUERY_PREFIX),
    "nomic-ai/nomic-embed-text-v1.5-Q": ModelSpec(
        "nomic-ai/nomic-embed-text-v1.5-Q", 768, "search_query: ", "search_document: ", uncased=True),
}

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
MAX_CHARS = 2000  # ~512 tokens; longer input is truncated by the tokenizer anyway


class EmbedderError(RuntimeError):
    """Raised at startup when the embedding model cannot be loaded or validated."""


@dataclass(frozen=True)
class EmbedderConfig:
    model_name: str = DEFAULT_MODEL
    cache_dir: str | None = None          # REQUIRED in prod; else $FASTEMBED_CACHE_PATH; never tmp
    threads: int | None = None            # None = ORT default (all cores); set e.g. 4 in containers
    offline: bool = True                  # never touch the network at runtime
    use_query_prefix: bool = True
    cache_size: int = 4096                # LRU entries (query side)
    batch_size: int = 64
    max_concurrency: int = 4              # concurrent ORT runs (session.run is thread-safe; bound CPU oversubscription)

    def resolved_cache_dir(self) -> Path:
        p = self.cache_dir or os.environ.get("FASTEMBED_CACHE_PATH")
        if not p:
            raise EmbedderError(
                "Embedding model cache dir not configured: set EmbedderConfig.cache_dir or "
                "FASTEMBED_CACHE_PATH (fastembed's default is the OS temp dir, which is purged).")
        return Path(p)


_WS = re.compile(r"\s+")


def normalise_text(text: object, *, lower: bool) -> str:
    """Never raises: coerces None/non-str, NFKC-normalises, collapses whitespace, truncates."""
    s = "" if text is None else str(text)
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C" or ch in "\t\n ")
    s = _WS.sub(" ", s).strip()[:MAX_CHARS]
    return s.lower() if lower else s


class Embedder:
    def __init__(self, config: EmbedderConfig | None = None) -> None:
        self.config = config or EmbedderConfig()
        if self.config.model_name not in MODEL_SPECS:
            raise EmbedderError(f"Unsupported model {self.config.model_name!r}; known: {list(MODEL_SPECS)}")
        self.spec = MODEL_SPECS[self.config.model_name]
        self._model = None
        # ORT InferenceSession.run is thread-safe (0 mismatches in 8-thread test); a semaphore bounds
        # oversubscription instead of a global lock (lock = no parallelism, 2.9x slower at 8 callers).
        self._sem = threading.BoundedSemaphore(max(1, self.config.max_concurrency))
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------ lifecycle
    @property
    def dim(self) -> int:
        return self.spec.dim

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> "Embedder":
        if self._model is not None:
            return self
        cfg = self.config
        cache_dir = cfg.resolved_cache_dir()
        if cfg.offline:
            os.environ["HF_HUB_OFFLINE"] = "1"          # huggingface_hub + fastembed honour this
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        try:
            from fastembed import TextEmbedding  # imported lazily: keeps import cost off unrelated paths
        except Exception as e:  # pragma: no cover - broken install / unsupported CPU wheel
            raise EmbedderError(f"fastembed/onnxruntime import failed: {e!r}") from e
        t0 = time.perf_counter()
        try:
            self._model = TextEmbedding(
                model_name=cfg.model_name, cache_dir=str(cache_dir), threads=cfg.threads,
                local_files_only=cfg.offline)
        except Exception as e:
            raise EmbedderError(
                f"Could not load embedding model {cfg.model_name!r} from {cache_dir} "
                f"(offline={cfg.offline}). Pre-download it at build time with "
                f"`python -m embedder download --cache-dir {cache_dir}`. Cause: {type(e).__name__}: {e}"
            ) from e
        log.info("embedding model %s loaded in %.2fs", cfg.model_name, time.perf_counter() - t0)
        return self

    def warmup(self) -> dict:
        """Health probe: embed one string and validate dim / finiteness / L2 norm. Raise on failure."""
        if self._model is None:
            raise EmbedderError("warmup() called before load()")
        t0 = time.perf_counter()
        try:
            v = self._embed_raw([self.spec.query_prefix + "health check"])[0]
        except Exception as e:
            raise EmbedderError(f"Embedding model failed to run: {type(e).__name__}: {e}") from e
        if v.shape != (self.dim,):
            raise EmbedderError(f"Embedding dim {v.shape} != expected ({self.dim},)")
        if not np.all(np.isfinite(v)):
            raise EmbedderError("Embedding contains NaN/Inf")
        norm = float(np.linalg.norm(v))
        if not math.isclose(norm, 1.0, abs_tol=1e-3):
            raise EmbedderError(f"Embedding not L2-normalised (norm={norm})")
        return {"model": self.config.model_name, "dim": self.dim, "norm": norm,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}

    # ------------------------------------------------------------------ embedding
    def _embed_raw(self, texts: list[str]) -> np.ndarray:
        with self._sem:
            out = list(self._model.embed(texts, batch_size=self.config.batch_size))
        return np.asarray(out, dtype=np.float32)

    def _key(self, text: str) -> str:
        return normalise_text(text, lower=self.spec.uncased)

    def embed_query(self, text: object) -> np.ndarray:
        """Embed a user question. Never raises for any input (empty/None/special chars/very long)."""
        if self._model is None:
            raise EmbedderError("embed_query() called before load()")
        key = self._key(text)
        with self._cache_lock:
            v = self._cache.get(key)
            if v is not None:
                self._cache.move_to_end(key)
                self.hits += 1
                return v
            self.misses += 1
        prefix = self.spec.query_prefix if self.config.use_query_prefix else ""
        v = self._embed_raw([prefix + key])[0]   # empty key -> embeds the prefix/"" (valid vector)
        v.setflags(write=False)
        with self._cache_lock:
            self._cache[key] = v
            if len(self._cache) > self.config.cache_size:
                self._cache.popitem(last=False)
        return v

    def embed_documents(self, texts: list[object]) -> np.ndarray:
        """Batch-embed corpus texts (glossary terms, metrics, columns, past questions). Not cached."""
        if self._model is None:
            raise EmbedderError("embed_documents() called before load()")
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        clean = [self.spec.doc_prefix + normalise_text(t, lower=False) for t in texts]
        return self._embed_raw(clean)

    async def aembed_query(self, text: object) -> np.ndarray:
        return await asyncio.to_thread(self.embed_query, text)

    async def aembed_documents(self, texts: list[object]) -> np.ndarray:
        return await asyncio.to_thread(self.embed_documents, texts)

    def cache_info(self) -> dict:
        n = self.hits + self.misses
        return {"size": len(self._cache), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / n, 4) if n else 0.0}


def to_neo4j(vec: np.ndarray, decimals: int | None = None) -> list[float]:
    """float32 vector -> list[float] for a Neo4j vector property / index."""
    lst = vec.astype(np.float64).tolist()
    return [round(x, decimals) for x in lst] if decimals is not None else lst


def download(model_name: str, cache_dir: str) -> None:
    """Build-time only (Dockerfile / CI): fetch the model into cache_dir with network on."""
    from fastembed import TextEmbedding
    TextEmbedding(model_name=model_name, cache_dir=cache_dir)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["download", "probe"])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--cache-dir", required=True)
    a = ap.parse_args()
    if a.cmd == "download":
        download(a.model, a.cache_dir)
        print("downloaded", a.model, "to", a.cache_dir)
    else:
        e = Embedder(EmbedderConfig(model_name=a.model, cache_dir=a.cache_dir)).load()
        print(e.warmup())
