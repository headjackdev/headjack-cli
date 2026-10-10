"""Local embedding: a sentence-transformers wrapper, CPU only, with a disk cache.

CPU only, deliberately — not because GPU wouldn't be faster, but because
reproducible Phase 1 numbers matter more than speed, and the p95 latency target in
`P2-06` is a CPU target. Don't probe for CUDA or MPS.

The cache is what makes the Phase 1 ablation runner (`P1-16`) tolerable: it re-embeds
the same corpus repeatedly across retrieval configurations, and without a cache that
is an exercise in waiting for the model rather than measuring retrieval.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from headjack.db.types import IndexConnection

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "headjack" / "embeddings"
_BATCH_SIZE = 64


@contextlib.contextmanager
def _suppress_native_stderr() -> Iterator[None]:
    """Redirect OS-level stderr (fd 2) for the duration of the block.

    The "unauthenticated requests" warning and "Loading weights" progress bar on
    every model load come from `hf_xet`, a Rust extension that writes straight to
    the OS file descriptor — it never touches Python's `logging` or `warnings`
    modules, so nothing short of an fd-level redirect can silence it. A genuine
    failure here still raises and prints its traceback normally, after this
    context manager has already restored the real fd.
    """
    saved_fd = os.dup(2)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull_fd, 2)
        yield
    finally:
        os.dup2(saved_fd, 2)
        os.close(devnull_fd)
        os.close(saved_fd)


class EmbeddingIdentityError(RuntimeError):
    """The index's recorded embedding model/dimension disagrees with this Embedder.

    Searching an index built with a different embedding model returns
    plausible-looking nonsense — the worst failure mode available, because nothing
    about the result shape signals that anything is wrong. This is the guard against it.
    """


class Embedder:
    MODEL = "sentence-transformers/all-MiniLM-L6-v2"
    DIM = 384

    def __init__(self, *, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._model: Any = None

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Return `(len(texts), DIM)` float32, unit-norm rows.

        Texts already in the cache never touch the model — `sentence_transformers`
        is imported only if at least one text misses the cache, so an all-cache-hit
        call works even where the package itself isn't installed.
        """
        if not texts:
            return np.zeros((0, self.DIM), dtype=np.float32)

        results: list[np.ndarray | None] = [None] * len(texts)
        misses: list[int] = []
        for i, text in enumerate(texts):
            cached = self._cache_get(text)
            if cached is None:
                misses.append(i)
            else:
                results[i] = cached

        if misses:
            model = self._load_model()
            miss_texts = [texts[i] for i in misses]
            vectors = model.encode(
                miss_texts,
                batch_size=_BATCH_SIZE,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            ).astype(np.float32)
            for idx, vector in zip(misses, vectors, strict=True):
                results[idx] = vector
                self._cache_put(texts[idx], vector)

        return np.stack(results).astype(np.float32)  # type: ignore[arg-type]

    def encode_one(self, text: str) -> np.ndarray:
        result: np.ndarray = self.encode([text])[0]
        return result

    def ensure_identity(self, conn: IndexConnection) -> None:
        """Record this embedder's model/dim in `meta` if the index has none yet;
        raise `EmbeddingIdentityError` if a *different* model or dim is already
        recorded. Call this once at index time (`P1-07`) and once per connection
        at search time (`P1-09`) — cheap, and it is the only thing standing between
        a model swap and silently wrong search results.
        """
        row = conn.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('embedding_model', ?)", (self.MODEL,)
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('embedding_dim', ?)", (str(self.DIM),)
            )
            conn.commit()
            return

        dim_row = conn.execute("SELECT value FROM meta WHERE key = 'embedding_dim'").fetchone()
        recorded_dim = int(dim_row["value"]) if dim_row else None
        if row["value"] != self.MODEL or recorded_dim != self.DIM:
            raise EmbeddingIdentityError(
                f"index was built with embedding_model={row['value']!r} dim={recorded_dim!r}, "
                f"but this Embedder uses {self.MODEL!r} dim={self.DIM} — "
                "rebuild the index or use the matching embedder"
            )

    def _load_model(self) -> Any:
        if self._model is None:
            # The import itself (not just the constructor) is what triggers the
            # native hf_xet noise, via transformers/huggingface_hub's own
            # import-time setup — both must run inside the suppressed block.
            with _suppress_native_stderr():
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.MODEL, device="cpu")
        return self._model

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256(f"{self.MODEL}\x00{text}".encode()).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self._cache_dir / key[:2] / f"{key}.npy"

    def _cache_get(self, text: str) -> np.ndarray | None:
        path = self._cache_path(self._cache_key(text))
        if not path.exists():
            return None
        try:
            loaded: np.ndarray = np.load(path).astype(np.float32)
            return loaded
        except (OSError, ValueError):
            return None

    def _cache_put(self, text: str, vector: np.ndarray) -> None:
        path = self._cache_path(self._cache_key(text))
        path.parent.mkdir(parents=True, exist_ok=True)
        # np.save silently appends ".npy" to any filename that doesn't already end
        # in it — passing it a path ending in ".tmp" would write "<path>.tmp.npy"
        # instead, and the rename below would then fail to find "<path>.tmp".
        # Passing an open file object sidesteps that renaming entirely.
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("wb") as f:
            np.save(f, vector)
        tmp.replace(path)
