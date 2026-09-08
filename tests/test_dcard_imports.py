from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpsons_insight_agent.author_privacy import AuthorHasher
from simpsons_insight_agent.config import Settings
from simpsons_insight_agent.forum_sources import dcard_import_item
from simpsons_insight_agent.imports import DCARD_IMPORT_FIELDS, dcard_template, parse_dcard_import


def make_hasher(tmp_path: Path) -> AuthorHasher:
    return AuthorHasher(Settings(author_hash_key_path=tmp_path / "import-author.key"))


def valid_row() -> dict:
    return {
        "item_type": "comment",
        "source_item_id": "",
        "thread_id": "256789012",
        "parent_id": "256789012",
        "title": "範例討論",
        "text": "客服等待時間偏久",
        "published_at": "2026-01-15T12:00:00+08:00",
        "forum": "food",
        "source_url": "https://www.dcard.tw/f/food/p/256789012",
        "author": "不可保存的卡稱",
        "reaction_count": 3,
    }


@pytest.mark.parametrize("suffix", ["csv", "json"])
def test_dcard_import_is_strict_anonymous_and_stable(tmp_path: Path, suffix: str) -> None:
    row = valid_row()
    if suffix == "json":
        content = json.dumps([row], ensure_ascii=False).encode()
    else:
        content = (
            ",".join(DCARD_IMPORT_FIELDS)
            + "\n"
            + ",".join(str(row[field]) for field in DCARD_IMPORT_FIELDS)
        ).encode()
    rows = parse_dcard_import(f"items.{suffix}", content, make_hasher(tmp_path))
    assert len(rows) == 1
    assert "author" not in rows[0]
    assert rows[0]["author_hash"]
    assert "不可保存的卡稱" not in str(rows)
    first = dcard_import_item(rows[0], make_hasher(tmp_path))
    second = dcard_import_item(rows[0], make_hasher(tmp_path))
    assert first.source_item_id == second.source_item_id


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_url", "https://evil.example/f/food/p/1", "公開文章網址"),
        ("source_url", "https://www.dcard.tw/f/food/p/not-a-number", "公開文章網址"),
        ("published_at", "昨天", "ISO 8601"),
        ("reaction_count", -1, "不可為負數"),
        ("thread_id", "", "thread_id"),
    ],
)
def test_dcard_import_rejects_entire_file_on_any_invalid_row(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    invalid = valid_row()
    invalid[field] = value
    content = json.dumps([valid_row(), invalid], ensure_ascii=False).encode()
    with pytest.raises(ValueError, match=message):
        parse_dcard_import("items.json", content, make_hasher(tmp_path))


def test_dcard_templates_have_the_documented_fields() -> None:
    csv_body, csv_type = dcard_template("csv")
    json_body, json_type = dcard_template("json")
    assert csv_body.lstrip("\ufeff").splitlines()[0] == ",".join(DCARD_IMPORT_FIELDS)
    assert set(json.loads(json_body)[0]) == set(DCARD_IMPORT_FIELDS)
    assert csv_type.startswith("text/csv")
    assert json_type == "application/json"


def test_dcard_import_rejects_missing_or_unknown_fields(tmp_path: Path) -> None:
    missing = valid_row()
    missing.pop("author")
    with pytest.raises(ValueError, match="欄位必須完整"):
        parse_dcard_import(
            "missing.json",
            json.dumps([missing], ensure_ascii=False).encode(),
            make_hasher(tmp_path),
        )
    extra = valid_row()
    extra["unexpected"] = "value"
    with pytest.raises(ValueError, match="欄位必須完整"):
        parse_dcard_import(
            "extra.json",
            json.dumps([extra], ensure_ascii=False).encode(),
            make_hasher(tmp_path),
        )


@pytest.mark.parametrize("value", [True, False, 1.5, 1.0, "1.5", [], {}])
def test_dcard_import_rejects_non_integer_reactions(tmp_path: Path, value: object) -> None:
    row = {**valid_row(), "reaction_count": value}
    with pytest.raises(ValueError, match="reaction_count 必須是整數"):
        parse_dcard_import("items.json", json.dumps([row]).encode(), make_hasher(tmp_path))


@pytest.mark.parametrize("field", ["text", "author", "thread_id", "source_item_id", "forum"])
def test_dcard_import_rejects_structured_or_coerced_strings(tmp_path: Path, field: str) -> None:
    row = {**valid_row(), field: {"unexpected": "data"}}
    with pytest.raises(ValueError, match=f"{field} 必須是字串"):
        parse_dcard_import("items.json", json.dumps([row]).encode(), make_hasher(tmp_path))


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"thread_id": "999"}, "thread_id"),
        ({"forum": "travel"}, "forum"),
        ({"item_type": "post", "source_item_id": "999"}, "source_item_id"),
        ({"source_item_id": "256789012"}, "source_item_id"),
    ],
)
def test_dcard_import_rejects_mismatched_article_identity(
    tmp_path: Path, changes: dict, message: str
) -> None:
    row = {**valid_row(), **changes}
    with pytest.raises(ValueError, match=message):
        parse_dcard_import("items.json", json.dumps([row]).encode(), make_hasher(tmp_path))


def test_dcard_import_fills_post_identity_and_deduplicates_share_urls(tmp_path: Path) -> None:
    row = {
        **valid_row(), "item_type": "post", "thread_id": "", "parent_id": "", "forum": "",
        "source_url": "https://www.dcard.tw/f/Food/p/256789012/?utm_source=share#comments",
    }
    duplicate = {**row, "source_url": "https://www.dcard.tw/f/food/p/256789012"}
    rows = parse_dcard_import(
        "items.json", json.dumps([row, duplicate]).encode(), make_hasher(tmp_path)
    )
    assert len(rows) == 1
    assert rows[0]["source_item_id"] == rows[0]["thread_id"] == "256789012"
    assert rows[0]["forum"] == "food"
    assert rows[0]["source_url"] == "https://www.dcard.tw/f/food/p/256789012"


@pytest.mark.parametrize("source_id", ["", "comment-123"])
def test_dcard_import_rejects_conflicting_stable_duplicates(tmp_path: Path, source_id: str) -> None:
    row = {**valid_row(), "source_item_id": source_id}
    changed = {**row, "reaction_count": row["reaction_count"] + 1}
    with pytest.raises(ValueError, match="識別資料衝突"):
        parse_dcard_import("items.json", json.dumps([row, changed]).encode(), make_hasher(tmp_path))


def test_dcard_import_rejects_same_thread_in_different_forums(tmp_path: Path) -> None:
    row = valid_row()
    changed = {
        **row, "forum": "travel", "source_url": "https://www.dcard.tw/f/travel/p/256789012",
    }
    with pytest.raises(ValueError, match="forum 不一致"):
        parse_dcard_import("items.json", json.dumps([row, changed]).encode(), make_hasher(tmp_path))
