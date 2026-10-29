"""Embedding providers. Vectors are derived data keyed by `model_id`; switching models means re-indexing."""

from __future__ import annotations

import hashlib
import json
import math
import urllib.parse
import urllib.request
from typing import Protocol

from .text import NgramTokenizer

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatibleEmbedder:
    """Any server exposing POST {base_url}/embeddings (Ollama, vLLM, LM Studio, OpenAI, ...)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        batch_size: int = 32,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_id = model
        self._api_key = api_key
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        host = urllib.parse.urlparse(self.base_url).hostname or ""
        handlers = [urllib.request.ProxyHandler({})] if host in _LOCAL_HOSTS else []
        self._opener = urllib.request.build_opener(*handlers)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            out.extend(self._embed_batch(texts[start : start + self._batch_size]))
        return out

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/embeddings", data=body, headers=headers, method="POST")
        with self._opener.open(req, timeout=self._timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        data = sorted(payload["data"], key=lambda item: item.get("index", 0))
        if len(data) != len(texts):
            raise RuntimeError(f"embedding server returned {len(data)} vectors for {len(texts)} inputs")
        return [list(map(float, item["embedding"])) for item in data]


class HashEmbedder:
    """Deterministic, offline feature-hashing embedder. For tests and for running without a model.

    It only captures word overlap, not meaning, so do not use it for a real companion.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.model_id = f"hash-v1-{dim}"
        self._tok = NgramTokenizer()

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in self._tok.tokenize(text):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[index] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


def embedder_from_config(cfg: dict) -> Embedder | None:
    """Build an embedder from the [embedding] table of config.toml."""
    if not cfg:
        return None
    provider = cfg.get("provider", "openai-compatible")
    if provider == "openai-compatible":
        return OpenAICompatibleEmbedder(
            base_url=cfg["base_url"],
            model=cfg["model"],
            api_key=cfg.get("api_key"),
            timeout=float(cfg.get("timeout", 60)),
            batch_size=int(cfg.get("batch_size", 32)),
        )
    if provider == "hash":
        return HashEmbedder(int(cfg.get("dim", 256)))
    if provider in ("none", ""):
        return None
    raise ValueError(f"unknown embedding provider {provider!r}")
