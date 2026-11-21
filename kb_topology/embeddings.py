"""Local embedding models (design doc, section 5.5).

The model files live on the local disk, so no KB text leaves the machine. The default is
all-MiniLM-L6-v2 (safetensors + tokenizer.json), run with numpy so no torch install is needed.

Vectors are cached on disk keyed by a hash of the exact embedded text, so a rebuild embeds
only the nodes whose text changed.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from .paths import DEFAULT_MODEL_DIR, MODEL_DIR_ENV, STATE_DIR_ENV, resolve_model_dir, resolve_state_dir  # noqa: F401  (re-exported)

Progress = Callable[[int, int], None]


class EmbeddingProvider(Protocol):
    model_id: str
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return float32 vectors of shape (len(texts), dim), L2-normalized."""
        ...


def _erf(x: np.ndarray) -> np.ndarray:
    # Abramowitz and Stegun 7.1.26, maximum error 1.5e-7.
    sign = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    poly = ((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592
    return sign * (1.0 - poly * t * np.exp(-x * x))


def _gelu(x: np.ndarray) -> np.ndarray:
    return 0.5 * x * (1.0 + _erf(x / np.sqrt(2.0)))


def _layer_norm(x: np.ndarray, weight: np.ndarray, bias: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    mean = x.mean(-1, keepdims=True)
    var = ((x - mean) ** 2).mean(-1, keepdims=True)
    return (x - mean) / np.sqrt(var + eps) * weight + bias


class MiniLMProvider:
    """BERT-style sentence encoder (mean pooling) in numpy. Built for all-MiniLM-L6-v2."""

    HEADS = 12

    def __init__(
        self,
        model_dir: str | Path,
        max_tokens: int = 128,
        batch_size: int = 32,
        query_prefix: str = "",
        passage_prefix: str = "",
    ) -> None:
        from safetensors import safe_open
        from tokenizers import Tokenizer

        model_dir = Path(model_dir)
        weights = model_dir / "model.safetensors"
        tokenizer = model_dir / "tokenizer.json"
        if not weights.is_file() or not tokenizer.is_file():
            raise FileNotFoundError(f"embedding model not found: need model.safetensors and tokenizer.json in {model_dir}")
        with safe_open(str(weights), "numpy") as f:
            self.w = {k: f.get_tensor(k) for k in f.keys()}
        self.tok = Tokenizer.from_file(str(tokenizer))
        self.tok.no_padding()
        self.tok.enable_truncation(max_length=max_tokens)
        self.batch_size = batch_size
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.dim = int(self.w["embeddings.word_embeddings.weight"].shape[1])
        self.layers = 1 + max(int(k.split(".")[2]) for k in self.w if k.startswith("encoder.layer."))
        base_id = f"{model_dir.name}-{hashlib.sha256(weights.stat().st_size.to_bytes(8, 'big')).hexdigest()[:8]}-t{max_tokens}"
        if query_prefix or passage_prefix:
            prefix_hash = hashlib.sha256(f"{query_prefix}\n{passage_prefix}".encode()).hexdigest()[:6]
            base_id += f"-{prefix_hash}"
        self.model_id = base_id

    def _encode_batch(self, ids: list[list[int]]) -> np.ndarray:
        w = self.w
        n = len(ids)
        length = max(len(x) for x in ids)
        tokens = np.zeros((n, length), dtype=np.int64)
        mask = np.zeros((n, length), dtype=np.float32)
        for i, row in enumerate(ids):
            tokens[i, : len(row)] = row
            mask[i, : len(row)] = 1.0
        x = w["embeddings.word_embeddings.weight"][tokens]
        x = x + w["embeddings.position_embeddings.weight"][:length][None, :, :]
        x = x + w["embeddings.token_type_embeddings.weight"][0]
        x = _layer_norm(x, w["embeddings.LayerNorm.weight"], w["embeddings.LayerNorm.bias"])
        bias = ((1.0 - mask) * -1e9)[:, None, None, :]
        head_dim = self.dim // self.HEADS
        for layer in range(self.layers):
            p = f"encoder.layer.{layer}."

            def dense(inp: np.ndarray, name: str) -> np.ndarray:
                return inp @ w[p + name + ".weight"].T + w[p + name + ".bias"]

            def split(t: np.ndarray) -> np.ndarray:
                return t.reshape(n, length, self.HEADS, head_dim).transpose(0, 2, 1, 3)

            q, k, v = (split(dense(x, f"attention.self.{nm}")) for nm in ("query", "key", "value"))
            scores = q @ k.transpose(0, 1, 3, 2) / np.sqrt(head_dim) + bias
            scores -= scores.max(-1, keepdims=True)
            probs = np.exp(scores)
            probs /= probs.sum(-1, keepdims=True)
            ctx = (probs @ v).transpose(0, 2, 1, 3).reshape(n, length, self.dim)
            x = _layer_norm(
                dense(ctx, "attention.output.dense") + x,
                w[p + "attention.output.LayerNorm.weight"],
                w[p + "attention.output.LayerNorm.bias"],
            )
            hidden = _gelu(dense(x, "intermediate.dense"))
            x = _layer_norm(
                dense(hidden, "output.dense") + x, w[p + "output.LayerNorm.weight"], w[p + "output.LayerNorm.bias"]
            )
        pooled = (x * mask[:, :, None]).sum(1) / np.maximum(mask.sum(1, keepdims=True), 1.0)
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.maximum(norm, 1e-12)).astype(np.float32)

    def _embed_prefixed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        ids = [e.ids for e in self.tok.encode_batch(texts)]
        order = sorted(range(len(texts)), key=lambda i: len(ids[i]))  # similar lengths per batch, less padding
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for start in range(0, len(order), self.batch_size):
            chunk = order[start : start + self.batch_size]
            out[chunk] = self._encode_batch([ids[i] for i in chunk])
        return out

    def embed(self, texts: list[str]) -> np.ndarray:
        return self.embed_passages(texts)

    def embed_query(self, texts: list[str]) -> np.ndarray:
        prefixed = [f"{self.query_prefix}{text}" for text in texts]
        return self._embed_prefixed(prefixed)

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        prefixed = [f"{self.passage_prefix}{text}" for text in texts]
        return self._embed_prefixed(prefixed)


class EmbeddingStore:
    """On-disk cache of vectors keyed by sha256(model id + text). Tool-owned, never inside the KB."""

    SAVE_EVERY = 2048

    def __init__(self, state_dir: str | Path, model_id: str) -> None:
        self.model_id = model_id
        self.path = Path(state_dir) / "embeddings" / f"{model_id}.npz"
        self.vectors: dict[str, np.ndarray] = {}
        if self.path.is_file():
            with np.load(self.path) as data:
                self.vectors = dict(zip(data["keys"].tolist(), data["vectors"]))

    def key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model_id}\n{text}".encode()).hexdigest()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keys = list(self.vectors)
        tmp = self.path.with_suffix(".tmp.npz")
        np.savez(tmp, keys=np.array(keys), vectors=np.stack([self.vectors[k] for k in keys]) if keys else np.zeros((0, 0)))
        tmp.replace(self.path)

    def matrix(
        self,
        provider: EmbeddingProvider,
        texts: list[str],
        progress: Progress | None = None,
        chunk: int = 256,
    ) -> np.ndarray:
        """Vectors for ``texts`` (same order), embedding only the ones not cached."""
        keys = [self.key(t) for t in texts]
        first_index: dict[str, int] = {}
        for i, k in enumerate(keys):
            if k not in self.vectors:
                first_index.setdefault(k, i)  # identical texts are embedded once
        missing = list(first_index.values())
        copies = Counter(keys)
        done = sum(1 for k in keys if k in self.vectors)  # positions already cached
        unsaved = 0
        for start in range(0, len(missing), chunk):
            part = missing[start : start + chunk]
            vecs = provider.embed([texts[i] for i in part])
            for i, v in zip(part, vecs):
                self.vectors[keys[i]] = v
            done += sum(copies[keys[i]] for i in part)
            unsaved += len(part)
            if unsaved >= self.SAVE_EVERY:
                self.save()
                unsaved = 0
            if progress:
                progress(done, len(texts))
        if missing and unsaved:
            self.save()
        if progress and not missing:
            progress(len(texts), len(texts))
        if not texts:
            return np.zeros((0, provider.dim), dtype=np.float32)
        return np.stack([self.vectors[k] for k in keys])
