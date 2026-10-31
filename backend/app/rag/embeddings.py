import asyncio
import hashlib
import math
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Protocol

from app.config import Settings, get_settings
from app.rag.stopwords import STOPWORDS

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class Embedder(Protocol):
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class FakeEmbedder:
    """Deterministic hashed bag-of-words embedder: lexical overlap -> similarity. For tests and offline demos."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _TOKEN_RE.findall(text.lower()):
            if len(tok) < 3 or tok in STOPWORDS:
                continue
            h = int.from_bytes(hashlib.blake2b(tok[:4].encode(), digest_size=8).digest(), "big")  # crude stemming
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class FastEmbedEmbedder:
    """Local multilingual ONNX embeddings (no data leaves the server)."""

    def __init__(self, settings: Settings) -> None:
        from fastembed import TextEmbedding

        self.dim = settings.embeddings_dim
        # Without a cap ONNX spawns one thread per core *per call*; 30 parallel chats then thrash the CPU.
        # 0 = library default (bulk indexing in the worker); 1 = predictable latency for chat traffic.
        self._model = TextEmbedding(settings.embeddings_model, threads=settings.embeddings_threads or None)
        self._qp = settings.embeddings_query_prefix
        self._pp = settings.embeddings_passage_prefix
        self._pool = ThreadPoolExecutor(max_workers=max(1, settings.embeddings_concurrency), thread_name_prefix="embed")
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_size = 1024  # repeated FAQ-style questions skip the model entirely

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts, batch_size=32)]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.get_running_loop().run_in_executor(self._pool, self._embed, [self._pp + t for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        key = text.strip().lower()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        vec = (await asyncio.get_running_loop().run_in_executor(self._pool, self._embed, [self._qp + text]))[0]
        self._cache[key] = vec
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return vec


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.embeddings_provider == "fake":
        return FakeEmbedder(s.embeddings_dim)
    return FastEmbedEmbedder(s)
