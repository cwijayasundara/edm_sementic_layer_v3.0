"""Local, offline text embedder (fastembed / ONNX Runtime, no API key).

Usage (once per process, e.g. the gateway lifespan):
    emb = Embedder(cache_dir="backend/.models").load()
    emb.warmup()                                 # raises EmbedderError on any failure
    v = emb.embed_query("open breaks by entity")  # list[float], L2-normalised
    v = await emb.aembed_query("...")             # async, off the event loop

The model is baked at build time: `python -m prism.graph.embedder download --cache-dir <dir>`.
Set HF_HUB_OFFLINE=1 in the environment too: huggingface_hub reads it at import.
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
    dim: int
    query_prefix: str = ""


# fastembed's query_embed() does NOT add this prefix (verified: query_embed == embed), so we do.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MODEL_SPECS: dict[str, ModelSpec] = {
    "BAAI/bge-small-en-v1.5": ModelSpec(384, BGE_QUERY_PREFIX),
    "BAAI/bge-base-en-v1.5": ModelSpec(768, BGE_QUERY_PREFIX),
}
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
MAX_CHARS = 2000  # ~512 tokens; longer input is truncated by the tokenizer anyway
DECIMALS = 6
MAX_CONCURRENCY = 4

_WS = re.compile(r"\s+")


class EmbedderError(Exception):
    """The embedding model cannot be configured, loaded or validated."""


def normalise_text(text: object) -> str:
    """Never raises: coerces None/non-str, NFKC-normalises, drops control chars, collapses whitespace,
    truncates, lower-cases (the bge tokenizer is uncased, so this is a safe cache key)."""
    s = "" if text is None else str(text)
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C" or ch in "\t\n ")
    return _WS.sub(" ", s).strip()[:MAX_CHARS].lower()


def _to_floats(vec: np.ndarray) -> list[float]:
    return [round(x, DECIMALS) for x in vec.astype(np.float32).astype(np.float64).tolist()]


class Embedder:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        cache_dir: str | Path | None = None,
        threads: int | None = None,
        cache_size: int = 2048,
    ) -> None:
        if model not in MODEL_SPECS:
            raise EmbedderError(f"Unsupported model {model!r}; known: {list(MODEL_SPECS)}")
        self.model = model
        self.spec = MODEL_SPECS[model]
        self.cache_dir = cache_dir
        self.threads = threads
        self.cache_size = cache_size
        self._model = None
        # ORT InferenceSession.run is thread-safe; a semaphore bounds CPU oversubscription.
        self._sem = threading.BoundedSemaphore(MAX_CONCURRENCY)
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def dim(self) -> int:
        return self.spec.dim

    def _resolved_cache_dir(self) -> Path:
        p = self.cache_dir or os.environ.get("FASTEMBED_CACHE_PATH")
        if not p:
            raise EmbedderError(
                "Embedding model cache dir not configured: pass cache_dir or set FASTEMBED_CACHE_PATH "
                "(fastembed's default is the OS temp dir, which is purged)."
            )
        return Path(p)

    def load(self) -> "Embedder":
        """Load the model from the local cache only. Side effect: sets HF_HUB_OFFLINE=1 for the whole process
        (forced, never restored) so the runtime can never touch the network."""
        if self._model is not None:
            return self
        cache_dir = self._resolved_cache_dir()
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        try:
            from fastembed import TextEmbedding  # lazy: keeps import cost off unrelated paths
        except Exception as e:  # broken install / unsupported CPU wheel
            raise EmbedderError(f"fastembed/onnxruntime import failed: {e!r}") from e
        t0 = time.perf_counter()
        try:
            self._model = TextEmbedding(
                model_name=self.model, cache_dir=str(cache_dir), threads=self.threads, local_files_only=True
            )
        except Exception as e:
            raise EmbedderError(
                f"Could not load embedding model {self.model!r} from {cache_dir}. Run `make models` "
                f"(or `python -m prism.graph.embedder download --cache-dir {cache_dir}`). "
                f"Cause: {type(e).__name__}: {e}"
            ) from e
        log.info("embedding model %s loaded in %.2fs", self.model, time.perf_counter() - t0)
        return self

    def warmup(self) -> dict:
        """Health probe: embed one string and validate dim / finiteness / L2 norm."""
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
        return {"model": self.model, "dim": self.dim, "norm": norm,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}

    def _embed_raw(self, texts: list[str]) -> np.ndarray:
        with self._sem:
            out = list(self._model.embed(texts))
        return np.asarray(out, dtype=np.float32)

    def embed_query(self, text: object) -> list[float]:
        """Embed a user question. Never raises for odd input (empty/None/special chars/very long)."""
        if self._model is None:
            raise EmbedderError("embed_query() called before load()")
        key = normalise_text(text)
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                self.hits += 1
                return list(hit)
            self.misses += 1
        vec = _to_floats(self._embed_raw([self.spec.query_prefix + key])[0])
        with self._cache_lock:
            self._cache[key] = tuple(vec)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return vec

    def embed_documents(self, texts: list[object]) -> list[list[float]]:
        """Batch-embed corpus texts (no prefix, not cached)."""
        if self._model is None:
            raise EmbedderError("embed_documents() called before load()")
        if not texts:
            return []
        return [_to_floats(v) for v in self._embed_raw([normalise_text(t) for t in texts])]

    async def aembed_query(self, text: object) -> list[float]:
        # Concurrency is bounded by a threading semaphore inside the worker (_embed_raw): no event-loop affinity.
        return await asyncio.to_thread(self.embed_query, text)

    def cache_info(self) -> dict:
        n = self.hits + self.misses
        return {"size": len(self._cache), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / n, 4) if n else 0.0}


def download(model: str, cache_dir: str) -> None:
    """Build-time only (Dockerfile / `make models`): fetch the model into cache_dir with network on."""
    from fastembed import TextEmbedding

    TextEmbedding(model_name=model, cache_dir=cache_dir)


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m prism.graph.embedder")
    ap.add_argument("cmd", choices=["download", "probe"])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--cache-dir", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "download":
        download(a.model, a.cache_dir)
        print("downloaded", a.model, "to", a.cache_dir)
    else:
        print(Embedder(a.model, a.cache_dir).load().warmup())


if __name__ == "__main__":
    main()
