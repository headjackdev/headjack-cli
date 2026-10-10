"""Cross-encoder reranking — the single biggest precision win in the pipeline, and
the single biggest latency cost. Rerank set stays at 20: a cross-encoder costs
50-200 ms per batch on CPU, and the 400 ms p95 search budget disappears fast.

Reranks (chunk_id, text) candidates — raw chunk text, before the artifact collapse —
matching `search.py`'s pipeline order (`P1-12`): fuse -> top 20 -> rerank -> collapse.
An earlier version of this module reranked already-collapsed snippets instead; that
was wrong, caught while wiring `P1-12`, because it gave the cross-encoder a
300-character window instead of the full chunk, and let one RRF-selected chunk per
artifact monopolise reranking before the cross-encoder ever saw the alternatives.
Returning `(chunk_id, score)` — the same shape `rrf()` produces — means `collapse()`
takes either one unchanged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)

# A rough character-based stand-in for the model's max token length. A chunk can be
# up to 2400 chars (P1-03's max_chars); without this, the tokenizer would silently
# truncate it with no visibility into that happening.
_MAX_DOC_CHARS = 2000


class Reranker:
    MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def __init__(self) -> None:
        self._model: Any = None

    def predict(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        """Score (query, document) pairs. Empty input never loads the model."""
        if not pairs:
            return []

        model = self._load_model()
        truncated = [(query, doc[:_MAX_DOC_CHARS]) for query, doc in pairs]

        start = time.monotonic()
        scores = model.predict(truncated)
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.debug("reranker.predict: %d pairs in %.1f ms", len(pairs), elapsed_ms)

        return [float(s) for s in scores]

    def rerank(
        self, query: str, candidates: Sequence[tuple[int, str]], *, top_n: int = 20
    ) -> list[tuple[int, float]]:
        """Rescore the top `top_n` (chunk_id, text) candidates against `query`.
        Returns (chunk_id, score) sorted best first. Candidates beyond `top_n` are
        dropped, not merely left unscored and appended."""
        if not candidates:
            return []

        limited = list(candidates[:top_n])
        scores = self.predict([(query, text) for _, text in limited])
        rescored = [
            (chunk_id, score) for (chunk_id, _text), score in zip(limited, scores, strict=True)
        ]
        return sorted(rescored, key=lambda pair: -pair[1])

    def _load_model(self) -> Any:
        if self._model is None:
            from headjack.embed import _suppress_native_stderr

            with _suppress_native_stderr():
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(self.MODEL, device="cpu")
        return self._model


class NullReranker:
    """Drop-in replacement that skips scoring and preserves the input (fused-rank)
    order, truncated to `top_n` — for the Phase 1 ablation's `rrf` (no-rerank)
    configuration, so turning reranking off doesn't need a second code path in
    `search.py`. Assigns a descending placeholder score so a caller sorting by
    score afterwards still gets the fused order back."""

    def rerank(
        self, query: str, candidates: Sequence[tuple[int, str]], *, top_n: int = 20
    ) -> list[tuple[int, float]]:
        limited = list(candidates[:top_n])
        n = len(limited)
        return [(chunk_id, float(n - i)) for i, (chunk_id, _text) in enumerate(limited)]
