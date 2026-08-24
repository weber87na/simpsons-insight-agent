from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import Settings, get_settings


@dataclass(slots=True)
class RankedReview:
    review_id: str
    score: float


class EmbeddingService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._model = None
        self._load_error: str | None = None

    def encode_passages(self, texts: list[str]) -> np.ndarray:
        model = self._ensure_model()
        values = [f"passage: {text}" for text in texts]
        return np.asarray(
            model.encode(values, normalize_embeddings=True, show_progress_bar=False),
            dtype=np.float32,
        )

    def encode_query(self, query: str) -> np.ndarray:
        model = self._ensure_model()
        vector: np.ndarray = model.encode(
            [f"query: {query}"], normalize_embeddings=True, show_progress_bar=False
        )[0]
        return np.asarray(vector, dtype=np.float32)

    def rank(
        self,
        query: str,
        review_vectors: list[tuple[str, bytes, int]],
        limit: int = 20,
    ) -> list[RankedReview]:
        if not review_vectors:
            return []
        query_vector = self.encode_query(query)
        scored: list[RankedReview] = []
        for review_id, raw, dimension in review_vectors:
            vector: np.ndarray = np.frombuffer(raw, dtype=np.float32, count=dimension)
            if vector.shape != query_vector.shape:
                continue
            scored.append(RankedReview(review_id, float(np.dot(query_vector, vector))))
        return sorted(scored, key=lambda item: item.score, reverse=True)[:limit]

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._load_error:
            raise RuntimeError(self._load_error)
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.settings.embedding_model,
                cache_folder=str(self.settings.model_cache_dir),
                local_files_only=self.settings.model_local_files_only,
            )
            return self._model
        except Exception as exc:
            self._load_error = str(exc)
            raise


def vector_to_bytes(vector: np.ndarray) -> tuple[bytes, int]:
    value = np.asarray(vector, dtype=np.float32)
    return value.tobytes(), int(value.shape[0])
