from datetime import UTC, datetime, timedelta

import pytest

from review_agent.dateparse import parse_relative_date


@pytest.mark.parametrize(
    ("value", "days", "precision"),
    [
        ("2 週前", 14, "week"),
        ("a month ago", 30, "month"),
        ("3 years ago", 1095, "year"),
        ("5 天前（已編輯）", 5, "day"),
    ],
)
def test_relative_date_parsing(value: str, days: int, precision: str) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    parsed = parse_relative_date(value, now)
    assert parsed.precision == precision
    assert parsed.estimated_at == now - timedelta(days=days)


def test_unknown_relative_date_is_preserved_as_unknown() -> None:
    parsed = parse_relative_date("很久以前")
    assert parsed.estimated_at is None
    assert parsed.precision == "unknown"
