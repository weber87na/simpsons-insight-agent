from simpsons_insight_agent.cloud import build_cloud_batches


def test_cloud_batches_enforce_count_and_character_limits() -> None:
    reviews = [{"review_key": str(index), "text": "x" * 80} for index in range(85)]
    batches = build_cloud_batches(reviews, max_reviews=40, max_chars=5_000)

    assert sum(len(batch) for batch in batches) == 85
    assert all(len(batch) <= 40 for batch in batches)
    assert len(batches) >= 3
