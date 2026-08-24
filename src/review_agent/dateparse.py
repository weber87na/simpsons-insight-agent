from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(slots=True)
class ParsedRelativeDate:
    estimated_at: datetime | None
    precision: str


_EN_PATTERN = re.compile(
    r"(?P<n>\d+|a|an)\s+(?P<unit>minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)
_ZH_PATTERN = re.compile(
    r"(?P<n>\d+|一|幾)\s*(?:個)?(?P<unit>分鐘|小時|天|週|周|星期|月|年)前"
)


def parse_relative_date(value: str | None, now: datetime | None = None) -> ParsedRelativeDate:
    if not value:
        return ParsedRelativeDate(None, "unknown")
    now = now or datetime.now(UTC)
    text = value.strip().lower().replace("edited", "").replace("已編輯", "")
    if any(token in text for token in ("just now", "剛剛", "分鐘前")):
        match = _EN_PATTERN.search(text) or _ZH_PATTERN.search(text)
        minutes = _number(match.group("n")) if match else 0
        return ParsedRelativeDate(now - timedelta(minutes=minutes), "day")

    match = _EN_PATTERN.search(text) or _ZH_PATTERN.search(text)
    if not match:
        return ParsedRelativeDate(None, "unknown")

    amount = _number(match.group("n"))
    unit = match.group("unit")
    if unit in {"minute", "hour", "分鐘", "小時"}:
        delta = timedelta(minutes=amount if unit in {"minute", "分鐘"} else amount * 60)
        precision = "day"
    elif unit in {"day", "天"}:
        delta, precision = timedelta(days=amount), "day"
    elif unit in {"week", "週", "周", "星期"}:
        delta, precision = timedelta(weeks=amount), "week"
    elif unit in {"month", "月"}:
        delta, precision = timedelta(days=amount * 30), "month"
    else:
        delta, precision = timedelta(days=amount * 365), "year"
    return ParsedRelativeDate(now - delta, precision)


def _number(value: str) -> int:
    if value in {"a", "an", "一"}:
        return 1
    if value == "幾":
        return 2
    try:
        return int(value)
    except ValueError:
        return 1

