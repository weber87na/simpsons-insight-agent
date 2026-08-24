import numpy as np

from simpsons_insight_agent.embeddings import EmbeddingService, vector_to_bytes


def test_numpy_cosine_ranking_uses_saved_float32_vectors() -> None:
    service = EmbeddingService.__new__(EmbeddingService)
    service.encode_query = lambda _query: np.asarray([1.0, 0.0], dtype=np.float32)
    first, dimension = vector_to_bytes(np.asarray([0.9, 0.1], dtype=np.float32))
    second, _ = vector_to_bytes(np.asarray([0.1, 0.9], dtype=np.float32))

    ranked = service.rank("服務", [("r1", first, dimension), ("r2", second, dimension)])
    assert [item.review_id for item in ranked] == ["r1", "r2"]
