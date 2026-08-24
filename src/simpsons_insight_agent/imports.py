from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .author_privacy import AuthorHasher
from .privacy import normalize_text

DCARD_IMPORT_FIELDS = (
    "item_type",
    "source_item_id",
    "thread_id",
    "parent_id",
    "title",
    "text",
    "published_at",
    "forum",
    "source_url",
    "author",
    "reaction_count",
)
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_ROWS = 2100


def parse_dcard_import(
    filename: str,
    content: bytes,
    hasher: AuthorHasher,
) -> list[dict]:
    if len(content) > MAX_IMPORT_BYTES:
        raise ValueError("匯入檔不可超過 10 MB")
    suffix = Path(filename).suffix.lower()
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("匯入檔必須使用 UTF-8 編碼") from exc
    if suffix == ".csv":
        reader = csv.DictReader(io.StringIO(decoded))
        fieldnames = reader.fieldnames or []
        if len(fieldnames) != len(DCARD_IMPORT_FIELDS) or set(fieldnames) != set(
            DCARD_IMPORT_FIELDS
        ):
            raise ValueError(f"CSV 欄位必須完整符合：{', '.join(DCARD_IMPORT_FIELDS)}")
        rows = list(reader)
    elif suffix == ".json":
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 格式錯誤：第 {exc.lineno} 行") from exc
        if isinstance(payload, dict):
            payload = payload.get("items")
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ValueError("JSON 必須是物件陣列，或包含 items 陣列")
        rows = payload
    else:
        raise ValueError("只接受 .csv 或 .json")
    if not rows:
        raise ValueError("匯入檔沒有資料")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ValueError(f"匯入檔最多 {MAX_IMPORT_ROWS} 筆")
    required_fields = set(DCARD_IMPORT_FIELDS)
    if any(set(row) != required_fields for row in rows):
        raise ValueError(f"每筆欄位必須完整符合：{', '.join(DCARD_IMPORT_FIELDS)}")

    normalized: list[dict] = []
    errors: list[str] = []
    for number, row in enumerate(rows, start=2 if suffix == ".csv" else 1):
        try:
            normalized.append(_normalize_dcard_row(row, hasher))
        except ValueError as exc:
            errors.append(f"第 {number} 筆：{exc}")
    if errors:
        preview = "；".join(errors[:20])
        if len(errors) > 20:
            preview += f"；另有 {len(errors) - 20} 筆錯誤"
        raise ValueError(preview)
    return normalized


def _normalize_dcard_row(row: dict, hasher: AuthorHasher) -> dict:
    item_type = normalize_text(str(row.get("item_type") or "")).lower()
    if item_type not in {"post", "comment"}:
        raise ValueError("item_type 必須是 post 或 comment")
    text = normalize_text(str(row.get("text") or ""))
    if not text or len(text) > 50_000:
        raise ValueError("text 必填且不可超過 50,000 字")
    source_url = str(row.get("source_url") or "").strip().rstrip("/")
    parsed = urlparse(source_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source_url 必須是 Dcard 公開文章網址") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "www.dcard.tw"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or not re.fullmatch(r"/f/[A-Za-z0-9_-]+/p/\d+/?", parsed.path)
    ):
        raise ValueError("source_url 必須是 Dcard 公開文章網址")
    published_at = str(row.get("published_at") or "").strip()
    try:
        datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("published_at 必須是 ISO 8601 日期時間") from exc
    thread_id = normalize_text(str(row.get("thread_id") or ""))
    if item_type == "comment" and not thread_id:
        raise ValueError("comment 必須提供 thread_id")
    try:
        reaction_count = int(row.get("reaction_count") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("reaction_count 必須是整數") from exc
    if reaction_count < 0:
        raise ValueError("reaction_count 不可為負數")
    return {
        "item_type": item_type,
        "source_item_id": normalize_text(str(row.get("source_item_id") or "")) or None,
        "thread_id": thread_id or None,
        "parent_id": normalize_text(str(row.get("parent_id") or "")) or None,
        "title": normalize_text(str(row.get("title") or "")) or None,
        "text": text,
        "published_at": published_at,
        "forum": normalize_text(str(row.get("forum") or "")) or None,
        "source_url": source_url,
        "author_hash": hasher.hash("dcard", str(row.get("author") or "")),
        "reaction_count": reaction_count,
    }


def dcard_template(format: str) -> tuple[str, str]:
    example = {
        "item_type": "post",
        "source_item_id": "123456789",
        "thread_id": "123456789",
        "parent_id": "",
        "title": "範例文章",
        "text": "範例內容",
        "published_at": "2026-01-01T12:00:00+08:00",
        "forum": "food",
        "source_url": "https://www.dcard.tw/f/food/p/123456789",
        "author": "",
        "reaction_count": 0,
    }
    if format == "json":
        return json.dumps([example], ensure_ascii=False, indent=2), "application/json"
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=DCARD_IMPORT_FIELDS)
    writer.writeheader()
    writer.writerow(example)
    return "\ufeff" + output.getvalue(), "text/csv; charset=utf-8"
